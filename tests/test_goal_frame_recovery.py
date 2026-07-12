"""Regression tests for robots becoming wedged against the goal frame."""

from __future__ import annotations

import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
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


def _context(pose: Pose2D) -> PlayContext:
    return PlayContext(
        game_state=GameControlState(),
        ball=BallState(),
        teammates={1: RobotState(1, pose, 1.0)},
    )


class GoalFrameRecoveryTests(unittest.TestCase):
    def test_robot_touching_front_post_gets_forced_infield_escape(self) -> None:
        controller = _controller()
        pose = Pose2D(6.95, 1.28, 0.0)

        command = controller.move_to_target(
            1, _context(pose), Pose2D(0.0, 0.0, 0.0), "return"
        )

        self.assertIn("escape goal frame", command.reason)
        self.assertIsNotNone(controller._goal_escape_target(pose))
        escape = controller._goal_escape_target(pose)
        assert escape is not None
        self.assertLess(escape.x, pose.x)
        self.assertLess(escape.y, pose.y)

    def test_robot_touching_outer_side_net_routes_around_post(self) -> None:
        controller = _controller()
        pose = Pose2D(7.30, 1.42, 0.0)

        escape = controller._goal_escape_target(pose)

        self.assertIsNotNone(escape)
        assert escape is not None
        self.assertLess(escape.x, pose.x)
        self.assertGreater(escape.y, pose.y)

    def test_target_inside_goal_is_projected_back_into_field(self) -> None:
        controller = _controller()

        projected = controller._project_out_of_goal(Pose2D(7.35, 0.2, 1.0))

        self.assertLess(projected.x, 7.0)
        self.assertEqual(projected.theta, 1.0)

    def test_legal_goalkeeper_target_in_front_of_line_is_preserved(self) -> None:
        controller = _controller()
        target = Pose2D(-6.60, 0.0, 0.0)

        self.assertEqual(controller._project_out_of_goal(target), target)


if __name__ == "__main__":
    unittest.main()
