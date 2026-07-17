"""Regression tests for penalty attribution and score transitions."""

from __future__ import annotations

import unittest

from src.soccer_framework import (
    GameControlState,
    Penalty,
    PlayerState,
    TeamState,
)
from src.soccer_framework.game_control_monitor import (
    discipline_changes,
    discipline_snapshot,
    score_changes,
    score_snapshot,
)
from src.soccer_framework.game_state import (
    game_control_state_from_json,
    game_control_state_to_json,
)


def _game(player: PlayerState, *, own_score: int = 0, opponent_score: int = 0) -> GameControlState:
    return GameControlState(
        teams=[
            TeamState(team_number=1, score=own_score, players=[player]),
            TeamState(team_number=2, score=opponent_score),
        ]
    )


class GameControlMonitorTests(unittest.TestCase):
    def test_every_official_penalty_makes_player_inactive(self) -> None:
        for penalty in Penalty:
            with self.subTest(penalty=penalty):
                snapshot = discipline_snapshot(
                    _game(PlayerState(penalty=penalty)),
                    1,
                    (1,),
                )[1]
                self.assertEqual(snapshot.is_active, penalty == Penalty.NONE)
                self.assertEqual(
                    snapshot.inactive_reason,
                    None if penalty == Penalty.NONE else penalty.value,
                )

    def test_penalty_is_emitted_once_while_countdown_ticks(self) -> None:
        active = discipline_snapshot(_game(PlayerState()), 1, (1,))
        penalised = discipline_snapshot(
            _game(
                PlayerState(
                    penalty=Penalty.PUSHING,
                    secs_till_unpenalised=30,
                )
            ),
            1,
            (1,),
        )
        ticking = discipline_snapshot(
            _game(
                PlayerState(
                    penalty=Penalty.PUSHING,
                    secs_till_unpenalised=29,
                )
            ),
            1,
            (1,),
        )

        changes = discipline_changes(active, penalised)
        self.assertEqual(tuple(change.kind for change in changes), ("penalised",))
        self.assertEqual(changes[0].current.inactive_reason, "PUSHING")
        self.assertEqual(discipline_changes(penalised, ticking), ())

    def test_countdown_only_state_and_clear_are_attributed(self) -> None:
        active = discipline_snapshot(_game(PlayerState()), 1, (1,))
        countdown = discipline_snapshot(
            _game(PlayerState(secs_till_unpenalised=5)),
            1,
            (1,),
        )

        penalised = discipline_changes(active, countdown)
        self.assertEqual(penalised[0].kind, "penalised")
        self.assertEqual(
            penalised[0].current.inactive_reason,
            "UNPENALISED_COUNTDOWN",
        )
        self.assertEqual(discipline_changes(countdown, active)[0].kind, "cleared")

    def test_warning_and_caution_changes_are_preserved_by_json_and_monitor(self) -> None:
        previous = _game(PlayerState(warnings=1, cautions=0))
        current = _game(PlayerState(warnings=2, cautions=1))
        decoded = game_control_state_from_json(game_control_state_to_json(current))
        decoded_player = decoded.get_player_state(1, 1)

        self.assertIsNotNone(decoded_player)
        assert decoded_player is not None
        self.assertEqual((decoded_player.warnings, decoded_player.cautions), (2, 1))
        changes = discipline_changes(
            discipline_snapshot(previous, 1, (1,)),
            discipline_snapshot(decoded, 1, (1,)),
        )
        self.assertEqual(tuple(change.kind for change in changes), ("discipline_changed",))

    def test_score_change_is_emitted_only_after_baseline(self) -> None:
        baseline = score_snapshot(_game(PlayerState(), own_score=0, opponent_score=0))
        updated = score_snapshot(_game(PlayerState(), own_score=1, opponent_score=0))

        self.assertEqual(score_changes({}, baseline), ())
        changes = score_changes(baseline, updated)
        self.assertEqual(len(changes), 1)
        self.assertEqual(
            (changes[0].team_id, changes[0].previous_score, changes[0].current_score),
            (1, 0, 1),
        )


if __name__ == "__main__":
    unittest.main()
