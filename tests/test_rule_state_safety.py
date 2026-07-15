"""Regression tests for referee-state safety that do not require the SDK runtime."""

from __future__ import annotations

import math
import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    GameState,
    ReadySlot,
    SetPlay,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.ready_stance import ReadyStance


class RuleStateSafetyTests(unittest.TestCase):
    def test_kickoff_window_closes_when_ball_ownership_opens(self) -> None:
        game = GameControlState(
            state=GameState.PLAYING,
            set_play=SetPlay.NONE,
            kicking_team=1,
            secondary_time=3,
        )
        self.assertTrue(game.is_kickoff_active_for_team(1))
        self.assertFalse(game.is_kickoff_active_for_team(2))

        game.secondary_time = 0
        self.assertFalse(game.is_kickoff_active_for_team(1))

    def test_stopped_game_never_reports_active_kickoff(self) -> None:
        game = GameControlState(
            state=GameState.PLAYING,
            stopped=True,
            set_play=SetPlay.NONE,
            kicking_team=1,
            secondary_time=3,
        )
        self.assertFalse(game.is_kickoff_active_for_team(1))

    def test_only_center_slot_is_designated_kickoff_player(self) -> None:
        config = SoccerConfig()
        self.assertEqual(config.kickoff_player_id(), 1)
        self.assertEqual(config.ready_slot_for_player(1), ReadySlot.CENTER)

    def test_ready_kickoff_targets_stay_safely_in_own_half(self) -> None:
        config = SoccerConfig()
        stance = ReadyStance(config, TeamFieldFrame(config))
        game = GameControlState(
            state=GameState.READY,
            set_play=SetPlay.NONE,
            kicking_team=config.opponent_team_id(),
        )
        ball = BallState(x=0.12, y=-0.08, last_seen_at=1.0)
        minimum_circle_distance = config.center_circle_radius + 0.20

        for slot in ReadySlot:
            with self.subTest(slot=slot):
                target = stance.ready_target_for(slot, game, ball)
                self.assertLessEqual(target.x, -0.35)
                self.assertGreaterEqual(
                    math.hypot(target.x, target.y),
                    minimum_circle_distance - 1e-9,
                )
                self.assertLessEqual(
                    abs(target.y),
                    config.field_width / 2.0 - 0.45 + 1e-9,
                )


if __name__ == "__main__":
    unittest.main()
