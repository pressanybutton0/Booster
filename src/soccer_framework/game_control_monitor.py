"""Pure GameController transition detection for logs and offline evaluation.

The ROS provider receives a full GameController snapshot on every packet.  This
module reduces those snapshots to the changes that matter for rule compliance:
player availability/discipline and score changes.  It deliberately has no ROS
dependency so the exact transition semantics can be regression-tested locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .types import GameControlState, Penalty


__all__ = [
    "DisciplineChange",
    "PlayerDisciplineSnapshot",
    "ScoreChange",
    "discipline_changes",
    "discipline_snapshot",
    "score_changes",
    "score_snapshot",
]


@dataclass(frozen=True)
class PlayerDisciplineSnapshot:
    """Immutable rule/availability fields for one player."""

    player_id: int
    penalty: Penalty
    secs_till_unpenalised: int
    warnings: int
    cautions: int

    @property
    def is_active(self) -> bool:
        return self.penalty == Penalty.NONE and self.secs_till_unpenalised <= 0

    @property
    def inactive_reason(self) -> str | None:
        if self.penalty != Penalty.NONE:
            return self.penalty.value
        if self.secs_till_unpenalised > 0:
            return "UNPENALISED_COUNTDOWN"
        return None


@dataclass(frozen=True)
class DisciplineChange:
    """One meaningful player-discipline transition.

    ``kind`` is one of ``penalised``, ``cleared``, ``penalty_changed``, or
    ``discipline_changed``. Countdown-only packet updates are intentionally not
    emitted, which keeps 30-second penalties from producing 30 console lines.
    """

    kind: str
    current: PlayerDisciplineSnapshot
    previous: PlayerDisciplineSnapshot | None = None


@dataclass(frozen=True)
class ScoreChange:
    team_id: int
    previous_score: int
    current_score: int


def discipline_snapshot(
    game_state: GameControlState,
    team_id: int,
    player_ids: tuple[int, ...],
) -> dict[int, PlayerDisciplineSnapshot]:
    """Copy the configured players' rule state out of a GC packet."""

    result: dict[int, PlayerDisciplineSnapshot] = {}
    for player_id in player_ids:
        player = game_state.get_player_state(team_id, player_id)
        if player is None:
            continue
        result[player_id] = PlayerDisciplineSnapshot(
            player_id=player_id,
            penalty=player.penalty,
            secs_till_unpenalised=player.secs_till_unpenalised,
            warnings=player.warnings,
            cautions=player.cautions,
        )
    return result


def discipline_changes(
    previous: Mapping[int, PlayerDisciplineSnapshot],
    current: Mapping[int, PlayerDisciplineSnapshot],
) -> tuple[DisciplineChange, ...]:
    """Return only actionable availability/discipline transitions."""

    changes: list[DisciplineChange] = []
    for player_id in sorted(current):
        now = current[player_id]
        before = previous.get(player_id)
        if before is None:
            if not now.is_active:
                changes.append(DisciplineChange("penalised", now))
            continue

        if before.is_active and not now.is_active:
            changes.append(DisciplineChange("penalised", now, before))
            continue
        if not before.is_active and now.is_active:
            changes.append(DisciplineChange("cleared", now, before))
            continue
        if (
            not now.is_active
            and (
                before.penalty != now.penalty
                or before.inactive_reason != now.inactive_reason
            )
        ):
            changes.append(DisciplineChange("penalty_changed", now, before))
            continue
        if before.warnings != now.warnings or before.cautions != now.cautions:
            changes.append(DisciplineChange("discipline_changed", now, before))
    return tuple(changes)


def score_snapshot(game_state: GameControlState) -> dict[int, int]:
    return {team.team_number: team.score for team in game_state.teams}


def score_changes(
    previous: Mapping[int, int],
    current: Mapping[int, int],
) -> tuple[ScoreChange, ...]:
    return tuple(
        ScoreChange(team_id, previous[team_id], score)
        for team_id, score in sorted(current.items())
        if team_id in previous and previous[team_id] != score
    )
