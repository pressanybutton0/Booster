"""PLAY-stage strategy entry point; competitors usually edit this file first.

:class:`RoleAssignment` snapshots "who does what" once per tick and stores a
``player_id -> role_name`` mapping that can hold custom roles. :class:`Playbook`
centralizes competitor-overridable PLAY decisions and explicitly registers roles
through :meth:`register_role`.

:class:`DefaultPlaybook` is the template default. It registers the chaser,
supporter, and goalkeeper roles; fixed starting slots use ``ReadySlot`` for
non-PLAY branches. To change tactics, override ``assign_roles``, customize
``select_chaser`` or ``kick_target``, or register a new role after
``super().__init__(kit)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..soccer_framework import PlayContext, ReadySlot, RobotCommand

if TYPE_CHECKING:
    from .role import RoleRegistry, RoleStrategy
    from ..runtime import SoccerKit


# Default role labels kept as constants for ``assign_roles``; they no longer restrict the string set.
ROLE_CHASER = "chaser"
ROLE_SUPPORTER = "supporter"
ROLE_GOALKEEPER = "goalkeeper"
ROLE_DEFENDER = "defender"
ROLE_NONE = "none"


@dataclass(frozen=True)
class RoleAssignment:
    """Snapshot of this tick's dynamic role assignment.

    The storage is ``by_player: Mapping[int, str]`` where keys are player IDs
    and values are role labels. ``role_of`` returns :data:`ROLE_NONE` when absent.

    Construction is direct: ``RoleAssignment({1: "chaser", 2: "supporter"})``.

    Use :meth:`players_of` for reverse lookup by role; specialized attributes
    such as chaser/supporters are intentionally not provided.
    """

    by_player: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze as a read-only view so external by_player edits cannot change ``role_of`` behavior.
        object.__setattr__(self, "by_player", MappingProxyType(dict(self.by_player)))

    def role_of(self, player_id: int) -> str:
        return self.by_player.get(player_id, ROLE_NONE)

    def players_of(self, name: str) -> tuple[int, ...]:
        return tuple(pid for pid, role in self.by_player.items() if role == name)


class Playbook:
    """Entry point for all PLAY-stage decisions competitors may override.

    Leaf nodes hold both :class:`SoccerKit` for tools and :class:`Playbook`
    for decisions. In the PLAY subtree, ``AssignRoles`` writes
    :meth:`assign_roles` output, per-player branches choose by
    :class:`RoleAssignment` plus ``role_registry``, kick leaves call
    :meth:`kick_target`, hold-style roles own their targets, and
    ``WaitForBall`` calls :meth:`waiting_command`.

    Subclasses add or remove roles in ``__init__`` with :meth:`register_role` and
    override :meth:`assign_roles` to choose this tick's assignment.
    """

    def __init__(self, kit: SoccerKit):
        # Delayed import breaks the role -> kit.blackboard -> kit -> play -> playbook cycle.
        from .role import RoleRegistry

        self.kit = kit
        self._registry = RoleRegistry()

    # Role registry

    def register_role(self, role: RoleStrategy) -> "Playbook":
        """Register a role on this Playbook and return self for chaining.

        Subclasses call this in ``__init__`` after ``super().__init__(kit)``.
        Registration order determines PLAY Selector branch priority.
        """

        self._registry.register(role)
        return self

    @property
    def role_registry(self) -> RoleRegistry:
        return self._registry

    # Role assignment, the core strategy node

    def assign_roles(
        self,
        context: PlayContext,
    ) -> RoleAssignment:
        """Return one :class:`RoleAssignment` per tick; subclasses decide the actual assignment."""

        raise NotImplementedError

    # Cross-role cooperative targets

    def waiting_command(
        self,
        player_id: int,
        context: PlayContext,
    ) -> RobotCommand:
        """Fallback command when no role is assigned.

        The default is a stop tagged by ReadySlot. Competitors can override this
        for custom idle positioning; the node has already cleared :class:`KickHysteresis`.
        """

        slot = self.kit.config.ready_slot_for_player(player_id)
        return RobotCommand.stop(f"{slot.value} waiting for ball")


# ----------------------------------------------------------------------
# Default implementation: fixed ReadySlot starts plus PLAY dynamic roles
# ----------------------------------------------------------------------


class DefaultPlaybook(Playbook):
    """Default SoccerSim playbook: chaser/supporter/goalkeeper dynamic roles plus Targeting scores.

    Subclasses can selectively override one method, for example:

    .. code-block:: python

    class AggressivePlaybook(DefaultPlaybook):
    def assign_roles(self, context):
    base = super().assign_roles(context)
    Move more players to supporters when trailing.
    """

    def __init__(self, kit: SoccerKit):
        super().__init__(kit)
        # Explicitly register default PLAY dynamic roles; competitor subclasses can
        # call register_role(...) after super().__init__(kit). DefenderRole is reserved for explicit custom Playbook registration.
        from .default_roles import (
            ChaserRole,
            DefenderRole,
            GoalkeeperRole,
            SupporterRole,
        )
        from .strategy_profiles import AdaptiveStrategyManager

        self.register_role(ChaserRole())
        self.register_role(SupporterRole())
        self.register_role(DefenderRole())
        self.register_role(GoalkeeperRole())
        self.strategy_manager = AdaptiveStrategyManager(kit)
        # Preserve the current ball claimant inside the configured tie band.
        # Without this state, tiny pose updates can swap chaser/support roles on
        # consecutive ticks and abort an otherwise healthy kick sequence.
        self._last_chaser_id: int | None = None

    def assign_roles(self, context: PlayContext) -> RoleAssignment:
        """Assign only players that GameControl currently allows on the field.

        Penalised, sent-off, substituted, or still-timed-out players are deliberately
        omitted. ``RoleAssignment.role_of`` maps omitted players to ``none`` and the
        safety layer sends them a stop command. This prevents a penalised player from
        consuming the only chaser or goalkeeper role.
        """

        profile = self.strategy_manager.update(context)
        eligible = self._eligible_players(context)
        goalkeeper_id = self._select_goalkeeper(context, eligible)
        chaser_id = self.select_chaser(
            context,
            eligible_players=eligible - ({goalkeeper_id} if goalkeeper_id else set()),
        )

        mapping: dict[int, str] = {}
        for player_id in sorted(eligible):
            if player_id == goalkeeper_id:
                mapping[player_id] = ROLE_GOALKEEPER
            elif player_id == chaser_id:
                mapping[player_id] = ROLE_CHASER
            elif profile.value == "defensive":
                mapping[player_id] = ROLE_DEFENDER
            else:
                mapping[player_id] = ROLE_SUPPORTER

        return RoleAssignment(mapping)

    # Internals

    def _eligible_players(self, context: PlayContext) -> set[int]:
        game = context.known_game
        return {
            player_id
            for player_id in self.kit.config.player_ids
            if game.is_active_player(self.kit.config.team_id, player_id)
        }

    def _select_goalkeeper(
        self,
        context: PlayContext,
        eligible_players: set[int],
    ) -> int | None:
        """Keep the configured keeper when available, otherwise use the rearmost player."""

        configured = self.kit.config.goalkeeper_player_id()
        if configured in eligible_players:
            return configured
        if not eligible_players:
            return None

        with_pose = [
            (context.teammates[player_id].pose.x, player_id)
            for player_id in eligible_players
            if player_id in context.teammates
            and context.teammates[player_id].pose is not None
        ]
        if with_pose:
            return min(with_pose)[1]
        return min(eligible_players)

    def select_chaser(
        self,
        context: PlayContext,
        eligible_players: set[int] | None = None,
    ) -> int | None:
        """Select this tick's chaser from our team.

        Decision priority:
        1. ReadySlot eligibility: keeper only joins dangerous balls, side only challenges when suitable.
        2. ``ball_claim_score``: cost based on distance to ball plus ReadySlot preference.
        3. Lowest player ID wins ties for predictable debugging.

        Only GameControl-active players in ``eligible_players`` are considered.
        Return ``None`` when no eligible slot can challenge; the remaining active
        roles continue safely without assigning a forbidden player.
        """
        config = self.kit.config
        targeting = self.kit.targeting
        ball = context.known_ball

        if eligible_players is None:
            eligible_players = self._eligible_players(context)

        candidates: list[int] = []
        scored: list[tuple[float, int]] = []
        for player_id in config.player_ids:
            if player_id not in eligible_players:
                continue
            slot = config.ready_slot_for_player(player_id)
            if not self._slot_can_challenge(slot, context):
                continue
            candidates.append(player_id)
            robot = context.teammates.get(player_id)
            if robot is None or robot.pose is None:
                continue
            scored.append(
                (targeting.ball_claim_score(slot, robot.pose, ball), player_id)
            )

        if not candidates:
            self._last_chaser_id = None
            return None
        if not scored:
            selected = min(candidates)
            self._last_chaser_id = selected
            return selected

        tie_margin = config.strategy.teammate_challenge_tie_margin_m
        # Lower is better. Keep the incumbent while it remains inside the tie
        # margin of the best challenger; this is the actual hysteresis that
        # prevents role handoff oscillation. Only use the player-id tie-breaker
        # when there is no eligible incumbent to preserve.
        ranked = sorted(scored, key=lambda item: item[0])
        best_score = ranked[0][0]
        score_by_player = {player_id: score for score, player_id in ranked}
        if (
            self._last_chaser_id in score_by_player
            and score_by_player[self._last_chaser_id] <= best_score + tie_margin
        ):
            return self._last_chaser_id

        tied_ids = [
            player_id for score, player_id in ranked if score <= best_score + tie_margin
        ]
        selected = min(tied_ids)
        self._last_chaser_id = selected
        return selected

    def _slot_can_challenge(
        self,
        slot: ReadySlot,
        context: PlayContext,
    ) -> bool:
        targeting = self.kit.targeting
        ball = context.known_ball
        if slot == ReadySlot.KEEPER:
            return targeting.ball_in_own_defensive_area(ball)
        if slot == ReadySlot.SIDE:
            return targeting.side_should_challenge(context)
        return True
