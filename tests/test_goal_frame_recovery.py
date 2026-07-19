"""Regression tests for robots becoming wedged against the goal frame."""

from __future__ import annotations

import math
import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    GameState,
    MoveIntent,
    PlayContext,
    Pose2D,
    RobotState,
    SoccerConfig,
    SetPlay,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.kick_hysteresis import KickHysteresis
from src.tactics.motion import MotionController
from src.tactics.navigation import ObstacleCollector


def _controller(*, clock=lambda: 0.0) -> MotionController:
    config = SoccerConfig()
    field = TeamFieldFrame(config)
    return MotionController(
        config,
        field,
        KickHysteresis(enter=0.3, exit=0.5, exit_delay=0.2),
        ObstacleCollector(config, field),
        clock=clock,
    )


def _context(
    pose: Pose2D,
    *,
    opponent_pose: Pose2D | None = None,
    ball: BallState | None = None,
    game: GameControlState | None = None,
) -> PlayContext:
    return PlayContext(
        game_state=game or GameControlState(),
        ball=ball or BallState(),
        teammates={1: RobotState(1, pose, 1.0)},
        opponents=(
            {1: RobotState(1, opponent_pose, 1.0)}
            if opponent_pose is not None
            else {}
        ),
    )


class GoalFrameRecoveryTests(unittest.TestCase):
    def test_attacker_touching_front_post_routes_to_a_side_shooting_angle(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)

        command = controller.move_to_target(
            1, _context(pose), Pose2D(0.0, 0.0, 0.0), "return"
        )

        self.assertIn("escape goal frame", command.reason)
        context = _context(pose)
        self.assertIsNotNone(controller._goal_escape_target(1, pose, context))
        escape = controller._goal_escape_target(1, pose, context)
        assert escape is not None
        self.assertLess(escape.x, pose.x)
        self.assertGreater(abs(escape.y), controller._config.goal_width / 2.0)

    def test_robot_near_but_not_touching_post_uses_normal_planning(self) -> None:
        controller = _controller()
        # The post obstacle radius (0.30 m) already contains the robot body.
        # At 0.40 m this is a safe nearby pose, not a physical overlap.
        pose = Pose2D(6.60, 1.30, 0.0)

        escape = controller._goal_escape_target(1, pose, _context(pose))

        self.assertIsNone(escape)

    def test_robot_touching_outer_side_net_routes_around_post(self) -> None:
        controller = _controller()
        pose = Pose2D(7.30, 1.42, 0.0)

        escape = controller._goal_escape_target(1, pose, _context(pose))

        self.assertIsNotNone(escape)
        assert escape is not None
        self.assertLess(escape.x, pose.x)
        self.assertGreater(escape.y, pose.y)

    def test_robot_behind_back_net_routes_around_rear_corner_first(self) -> None:
        controller = _controller()
        pose = Pose2D(7.95, 0.0, math.pi)
        context = _context(pose)

        escape = controller._goal_escape_target(1, pose, context)

        self.assertIsNotNone(escape)
        assert escape is not None
        self.assertGreater(escape.x, 7.60)
        self.assertGreater(abs(escape.y), 1.30)

    def test_robot_past_side_net_returns_infield_outside_frame(self) -> None:
        controller = _controller()
        pose = Pose2D(8.10, 2.10, math.pi)

        escape = controller._goal_escape_target(1, pose, _context(pose))

        self.assertIsNotNone(escape)
        assert escape is not None
        self.assertLess(escape.x, 7.0)
        self.assertGreater(escape.y, 1.30)

    def test_robot_behind_net_does_not_chase_ball_through_net(self) -> None:
        controller = _controller()
        pose = Pose2D(7.95, 0.0, math.pi)

        command = controller.move_to_target(
            1,
            _context(pose),
            Pose2D(7.30, 0.0, 0.0),
            "chase ball in goal",
        )
        escape = controller._goal_escape_target(1, pose, _context(pose))

        self.assertIn("escape goal frame", command.reason)
        assert escape is not None
        self.assertGreater(escape.x, 7.60)
        self.assertGreater(abs(escape.y), 1.30)

    def test_opponent_pinning_robot_to_post_changes_escape_route(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)
        opponent = Pose2D(6.62, 0.98, 0.0)
        clear_escape = controller._goal_escape_target(1, pose, _context(pose))
        pinned_escape = controller._goal_escape_target(
            1, pose, _context(pose, opponent_pose=opponent)
        )

        assert clear_escape is not None and pinned_escape is not None
        self.assertGreaterEqual(
            math.hypot(pinned_escape.x - opponent.x, pinned_escape.y - opponent.y),
            math.hypot(clear_escape.x - opponent.x, clear_escape.y - opponent.y),
        )
        self.assertGreater(abs(pinned_escape.y), controller._config.goal_width / 2.0)

    def test_opponent_restart_ball_is_part_of_goal_escape_scoring(self) -> None:
        controller = _controller()
        pose = Pose2D(7.30, 1.42, math.pi)
        ball = BallState(x=6.75, y=1.05, last_seen_at=1.0)
        game = GameControlState(
            state=GameState.PLAYING,
            set_play=SetPlay.DIRECT_FREE_KICK,
            kicking_team=2,
        )
        context = _context(pose, ball=ball, game=game)

        escape = controller._goal_escape_target(1, pose, context)

        self.assertIsNotNone(escape)
        assert escape is not None
        self.assertGreater(
            math.hypot(escape.x - ball.x, escape.y - ball.y),
            math.hypot(pose.x - ball.x, pose.y - ball.y),
        )

    def test_pinned_robot_translates_without_waiting_for_turn(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)
        context = _context(pose, opponent_pose=Pose2D(6.62, 0.98, 0.0))

        command = controller.move_to_target(
            1, context, Pose2D(0.0, 0.0, 0.0), "return"
        )

        self.assertIsInstance(command.intent, MoveIntent)
        assert isinstance(command.intent, MoveIntent)
        self.assertNotEqual(command.intent.vx, 0.0)
        self.assertIn("escape goal frame", command.reason)

    def test_goal_escape_owns_steering_instead_of_generic_yaw_avoidance(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)
        context = _context(pose, opponent_pose=Pose2D(6.62, 0.98, 0.0))

        command = controller.move_to_target(
            1, context, Pose2D(0.0, 0.0, 0.0), "return"
        )
        escape = controller._goal_escape_plan_by_player[1][1]
        expected = controller._compute_goal_escape_velocity(
            pose, escape, command.reason
        )

        self.assertEqual(command.intent, expected.intent)
        self.assertEqual(abs(command.intent.vx), 0.55)

    def test_stalled_escape_rotates_to_a_different_route(self) -> None:
        now = [0.0]
        controller = _controller(clock=lambda: now[0])
        pose = Pose2D(6.95, 1.28, 0.0)
        context = _context(pose, opponent_pose=Pose2D(6.62, 0.98, 0.0))

        first = controller._goal_escape_target(1, pose, context)
        now[0] = 2.1
        second = controller._goal_escape_target(1, pose, context)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(second, first)

    def test_small_progress_cannot_keep_the_same_route_forever(self) -> None:
        now = [0.0]
        controller = _controller(clock=lambda: now[0])
        context = _context(
            Pose2D(6.95, 1.28, 0.0),
            opponent_pose=Pose2D(6.62, 0.98, 0.0),
        )

        controller._goal_escape_target(
            1, Pose2D(6.95, 1.28, 0.0), context
        )
        now[0] = 1.0
        controller._goal_escape_target(
            1, Pose2D(6.82, 1.20, 0.0), context
        )
        self.assertEqual(
            controller._goal_escape_progress_by_player[1].route_index,
            0,
        )

        now[0] = 4.1
        controller._goal_escape_target(
            1, Pose2D(6.68, 1.12, 0.0), context
        )
        self.assertEqual(
            controller._goal_escape_progress_by_player[1].route_index,
            1,
        )

    def test_behind_net_progress_does_not_flip_rear_corner_at_four_seconds(self) -> None:
        now = [0.0]
        controller = _controller(clock=lambda: now[0])

        controller._goal_escape_target(
            1,
            Pose2D(7.95, 0.0, math.pi),
            _context(Pose2D(7.95, 0.0, math.pi)),
        )
        now[0] = 1.0
        controller._goal_escape_target(
            1,
            Pose2D(8.00, 0.25, math.pi),
            _context(Pose2D(8.00, 0.25, math.pi)),
        )
        now[0] = 4.1
        controller._goal_escape_target(
            1,
            Pose2D(8.10, 0.80, math.pi),
            _context(Pose2D(8.10, 0.80, math.pi)),
        )

        self.assertEqual(
            controller._goal_escape_progress_by_player[1].phase,
            "behind_net",
        )
        self.assertEqual(
            controller._goal_escape_progress_by_player[1].route_index,
            0,
        )

    def test_escape_route_opens_clearance_from_touched_post(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)
        escape = controller._goal_escape_target(
            1,
            pose,
            _context(pose, opponent_pose=Pose2D(6.62, 0.98, 0.0)),
        )

        assert escape is not None
        start_distance = math.hypot(pose.x - 7.0, pose.y - 1.3)
        next_x = pose.x + (escape.x - pose.x) * 0.35
        next_y = pose.y + (escape.y - pose.y) * 0.35
        self.assertGreater(
            math.hypot(next_x - 7.0, next_y - 1.3),
            start_distance,
        )

    def test_play_motion_avoids_close_opponent_by_default(self) -> None:
        controller = _controller()
        pose = Pose2D(0.0, 0.0, 0.0)
        context = _context(pose, opponent_pose=Pose2D(0.20, 0.15, 0.0))

        command = controller.move_to_target(
            1, context, Pose2D(2.0, 0.0, 0.0), "play chase"
        )

        self.assertIsInstance(command.intent, MoveIntent)
        assert isinstance(command.intent, MoveIntent)
        self.assertNotEqual(command.intent.vyaw, 0.0)
        self.assertIn("yaw avoid", command.reason)

    def test_ball_challenge_does_not_detour_around_opponent_at_ball(self) -> None:
        controller = _controller()
        pose = Pose2D(0.0, 0.0, 0.0)
        ball = BallState(x=2.0, y=0.0, last_seen_at=1.0)
        context = _context(
            pose,
            opponent_pose=Pose2D(1.3, 0.0, 0.0),
            ball=ball,
        )

        command = controller.move_to_target(
            1,
            context,
            Pose2D(2.0, 0.0, 0.0),
            "contest ball",
            contest_ball=ball,
        )

        self.assertIsInstance(command.intent, MoveIntent)
        assert isinstance(command.intent, MoveIntent)
        self.assertGreater(command.intent.vx, 0.0)
        self.assertEqual(command.intent.vyaw, 0.0)
        self.assertNotIn("obstacle", command.reason)
        self.assertNotIn("yaw avoid", command.reason)

    def test_target_inside_goal_is_projected_back_into_field(self) -> None:
        controller = _controller()

        projected = controller._project_out_of_goal(Pose2D(7.35, 0.2, 1.0))
        projected_behind_net = controller._project_out_of_goal(
            Pose2D(8.20, 0.0, 1.0)
        )

        self.assertLess(projected.x, 7.0)
        self.assertLess(projected_behind_net.x, 7.0)
        self.assertEqual(projected.theta, 1.0)

    def test_legal_goalkeeper_target_in_front_of_line_is_preserved(self) -> None:
        controller = _controller()
        target = Pose2D(-6.60, 0.0, 0.0)

        self.assertEqual(controller._project_out_of_goal(target), target)

    def test_live_goal_defense_suppresses_infield_post_escape(self) -> None:
        controller = _controller()
        pose = Pose2D(-6.95, 1.28, 0.0)
        context = _context(
            pose,
            ball=BallState(x=-5.2, y=0.7, last_seen_at=1.0, vx=-1.5),
        )

        command = controller.move_to_target(
            1,
            context,
            Pose2D(-6.1, 0.7, 0.0),
            "goalkeeper block line",
            goal_defense_active=True,
        )

        self.assertNotIn("escape goal frame", command.reason)

    def test_keeper_behind_line_still_escapes_during_live_defense(self) -> None:
        controller = _controller()
        pose = Pose2D(-7.15, 0.0, 0.0)
        context = _context(
            pose,
            ball=BallState(x=-6.0, y=0.0, last_seen_at=1.0, vx=-1.5),
        )

        command = controller.move_to_target(
            1,
            context,
            Pose2D(-6.1, 0.0, 0.0),
            "goalkeeper block line",
            goal_defense_active=True,
        )

        self.assertIn("escape goal frame", command.reason)

    def test_approach_target_respects_kick_direction(self) -> None:
        controller = _controller()
        ball = BallState(x=0.0, y=0.0, last_seen_at=1.0)

        target = controller.approach_target(ball, math.pi / 2.0, 0.4)

        self.assertAlmostEqual(target.x, 0.0, places=6)
        self.assertAlmostEqual(target.y, -0.4, places=6)


if __name__ == "__main__":
    unittest.main()
