"""Regression tests for robots becoming wedged against the goal frame."""

from __future__ import annotations

import math
import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    MoveIntent,
    PlayContext,
    Pose2D,
    RobotState,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.kick_hysteresis import KickHysteresis
from src.tactics.motion import MotionController
from src.tactics.navigation import ObstacleCollector


def _controller() -> MotionController:
    config = SoccerConfig()
    field = TeamFieldFrame(config)
    return MotionController(
        config,
        field,
        KickHysteresis(enter=0.3, exit=0.5, exit_delay=0.2),
        ObstacleCollector(config, field),
    )


def _context(
    pose: Pose2D,
    *,
    opponent_pose: Pose2D | None = None,
) -> PlayContext:
    return PlayContext(
        game_state=GameControlState(),
        ball=BallState(),
        teammates={1: RobotState(1, pose, 1.0)},
        opponents=(
            {1: RobotState(1, opponent_pose, 1.0)}
            if opponent_pose is not None
            else {}
        ),
    )


class GoalFrameRecoveryTests(unittest.TestCase):
    def test_robot_touching_front_post_gets_forced_infield_escape(self) -> None:
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
        self.assertLess(escape.y, pose.y)

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
        self.assertNotEqual(pinned_escape, clear_escape)
        self.assertGreater(
            math.hypot(pinned_escape.x - opponent.x, pinned_escape.y - opponent.y),
            math.hypot(clear_escape.x - opponent.x, clear_escape.y - opponent.y),
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


if __name__ == "__main__":
    unittest.main()
