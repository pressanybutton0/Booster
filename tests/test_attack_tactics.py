"""Regression tests for blocked-shot recycling and safe backpasses."""

from __future__ import annotations

import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    GameState,
    PlayContext,
    Pose2D,
    RobotState,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.navigation import ObstacleCollector
from src.tactics.targeting.attack import kick_reason, select_kick_target


def _context(*, mark_receiver: bool, block_shot: bool) -> PlayContext:
    opponents = {}
    if block_shot:
        opponents[4] = RobotState(4, Pose2D(5.5, 0.0, 0.0), 1.0)
    if mark_receiver:
        opponents[5] = RobotState(5, Pose2D(2.5, 1.0, 0.0), 1.0)
    return PlayContext(
        game_state=GameControlState(state=GameState.PLAYING),
        ball=BallState(x=4.0, y=0.0, last_seen_at=1.0),
        teammates={
            1: RobotState(1, Pose2D(3.6, 0.0, 0.0), 1.0),
            2: RobotState(2, Pose2D(2.5, 1.0, 0.0), 1.0),
            3: RobotState(3, Pose2D(-5.5, 0.0, 0.0), 1.0),
        },
        opponents=opponents,
    )


class AttackTacticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SoccerConfig()
        self.field = TeamFieldFrame(self.config)
        self.obstacles = ObstacleCollector(self.config, self.field)

    def _target(self, context: PlayContext) -> Pose2D:
        return select_kick_target(
            self.config,
            self.field,
            self.obstacles,
            1,
            context,
            lambda _game, _player_id: True,
        )

    def test_blocked_shot_recycles_to_safe_teammate(self) -> None:
        target = self._target(_context(mark_receiver=False, block_shot=True))
        self.assertAlmostEqual(target.x, 2.5)
        self.assertAlmostEqual(target.y, 1.0)

    def test_clear_shot_is_not_replaced_by_backpass(self) -> None:
        target = self._target(_context(mark_receiver=False, block_shot=False))
        self.assertAlmostEqual(target.x, self.field.opponent_goal_x())
        self.assertAlmostEqual(target.y, 0.0)

    def test_marked_backpass_receiver_falls_back_to_dribble(self) -> None:
        target = self._target(_context(mark_receiver=True, block_shot=True))
        expected = self.field.clamp_inside_field(Pose2D(5.5, 0.0, 0.0))
        self.assertAlmostEqual(target.x, expected.x)
        self.assertAlmostEqual(target.y, expected.y)

    def test_backpass_has_an_explicit_runtime_reason(self) -> None:
        reason = kick_reason(
            self.config,
            Pose2D(2.5, 1.0, 0.0),
            default="center kick",
            ball=BallState(4.0, 0.0, 1.0),
        )
        self.assertEqual(reason, "center backpass")


if __name__ == "__main__":
    unittest.main()
