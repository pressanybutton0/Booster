"""Regression tests for READY targets near kickoff rule boundaries."""

from __future__ import annotations

import math
import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    GameState,
    Pose2D,
    SetPlay,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.ready_stance import READY_ARRIVE_DISTANCE_M, ReadyStance


class ReadyStanceRuleTests(unittest.TestCase):
    def test_kickoff_target_stays_in_own_half_after_arrival_tolerance(self) -> None:
        config = SoccerConfig()
        stance = ReadyStance(config, TeamFieldFrame(config))
        game = GameControlState(
            state=GameState.READY,
            set_play=SetPlay.NONE,
            kicking_team=config.team_id,
        )

        target = stance._legalize_ready_target(
            Pose2D(-0.05, 0.0, 0.0), game, BallState()
        )

        self.assertLessEqual(target.x + READY_ARRIVE_DISTANCE_M, -0.25)

    def test_opponent_circle_margin_survives_arrival_tolerance(self) -> None:
        config = SoccerConfig()
        stance = ReadyStance(config, TeamFieldFrame(config))
        game = GameControlState(
            state=GameState.READY,
            set_play=SetPlay.NONE,
            kicking_team=2,
        )

        target = stance._legalize_ready_target(
            Pose2D(-0.05, 0.0, 0.0), game, BallState()
        )

        self.assertGreaterEqual(
            math.hypot(target.x, target.y) - READY_ARRIVE_DISTANCE_M,
            config.center_circle_radius + 0.25 - 1e-9,
        )


if __name__ == "__main__":
    unittest.main()
