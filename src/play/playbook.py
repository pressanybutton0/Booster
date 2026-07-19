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
    # The only robot allowed to start SoccerKickManager this tick.  Keeping the
    # claim beside the role snapshot makes arbitration atomic: roles and ball
    # ownership can never describe different frames.
    ball_owner_id: int | None = None

    def __post_init__(self) -> None:
        # Freeze as a read-only view so external by_player edits cannot change ``role_of`` behavior.
        object.__setattr__(self, "by_player", MappingProxyType(dict(self.by_player)))

    def role_of(self, player_id: int) -> str:
        return self.by_player.get(player_id, ROLE_NONE)

    def players_of(self, name: str) -> tuple[int, ...]:
        return tuple(pid for pid, role in self.by_player.items() if role == name)

    def owns_ball(self, player_id: int) -> bool:
        return self.ball_owner_id == player_id


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
        self._keeper_claiming = False
        self._goal_kick_origin: tuple[float, float] | None = None
        self._goal_kick_complete = False

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
        own_goal_kick = self._is_own_goal_kick(context)
        goal_kick_complete = self._track_goal_kick_touch(
            context,
            active=own_goal_kick,
        )
        if own_goal_kick:
            mapping: dict[int, str] = {}
            for player_id in sorted(eligible):
                if player_id == goalkeeper_id:
                    mapping[player_id] = (
                        ROLE_NONE if goal_kick_complete else ROLE_GOALKEEPER
                    )
                elif self.kit.config.ready_slot_for_player(player_id) == ReadySlot.SIDE:
                    mapping[player_id] = ROLE_DEFENDER
                else:
                    mapping[player_id] = ROLE_SUPPORTER
            self._last_chaser_id = None
            self._keeper_claiming = not goal_kick_complete
            return RoleAssignment(
                mapping,
                ball_owner_id=(
                    goalkeeper_id
                    if goalkeeper_id is not None and not goal_kick_complete
                    else None
                ),
            )

        goalkeeper_claims_ball = False
        if goalkeeper_id is not None:
            goalkeeper_claims_ball = self.kit.targeting.ball_in_own_defensive_area(
                context.known_ball,
                extra_margin_m=(
                    self.kit.config.strategy.goalkeeper_clear_exit_margin_m
                    if self._keeper_claiming
                    else 0.0
                ),
            ) or self.kit.targeting.keeper_should_sweep_loose_ball(
                context,
                goalkeeper_id,
                continuing=self._keeper_claiming,
            )
        self._keeper_claiming = goalkeeper_claims_ball
        if goalkeeper_claims_ball:
            # Nobody else is allowed to collapse into the keeper's working
            # space.  Reset field-player hysteresis so the next open-ball claim
            # is selected from fresh distances after the clearance.
            chaser_id = None
            self._last_chaser_id = None
        else:
            chaser_id = self.select_chaser(
                context,
                eligible_players=(
                    eligible - ({goalkeeper_id} if goalkeeper_id else set())
                ),
            )

        mapping: dict[int, str] = {}
        for player_id in sorted(eligible):
            if player_id == goalkeeper_id:
                mapping[player_id] = ROLE_GOALKEEPER
            elif player_id == chaser_id:
                mapping[player_id] = ROLE_CHASER
            elif self.kit.config.ready_slot_for_player(player_id) == ReadySlot.SIDE:
                # robot2 is the midfield/defensive pivot whenever it does not
                # own the ball.  It protects the second ball rather than
                # becoming a second striker beside robot1.
                mapping[player_id] = ROLE_DEFENDER
            elif profile.value == "defensive":
                mapping[player_id] = ROLE_DEFENDER
            else:
                mapping[player_id] = ROLE_SUPPORTER

        return RoleAssignment(
            mapping,
            ball_owner_id=(goalkeeper_id if goalkeeper_claims_ball else chaser_id),
        )

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
        incumbent_is_kicking = False
        is_active = getattr(self.kit.kicker, "is_active", None)
        if self._last_chaser_id is not None and callable(is_active):
            incumbent_is_kicking = bool(is_active(self._last_chaser_id))
        handoff_margin = (
            tie_margin
            if incumbent_is_kicking
            else config.strategy.teammate_challenge_idle_margin_m
        )
        if (
            self._last_chaser_id in score_by_player
            and score_by_player[self._last_chaser_id] <= best_score + handoff_margin
        ):
            return self._last_chaser_id

        selected = ranked[0][1]
        self._last_chaser_id = selected
        return selected

    def _is_own_goal_kick(self, context: PlayContext) -> bool:
        game = context.known_game
        return (
            game.is_restart_for_team(self.kit.config.team_id)
            and game.set_play.value == "GOAL_KICK"
        )

    def _track_goal_kick_touch(
        self,
        context: PlayContext,
        *,
        active: bool,
    ) -> bool:
        """Latch the first legal goal-kick touch until GameController clears it."""

        if not active:
            self._goal_kick_origin = None
            self._goal_kick_complete = False
            return False
        ball = context.known_ball
        if self._goal_kick_origin is None:
            self._goal_kick_origin = (ball.x, ball.y)
        if not self._goal_kick_complete:
            dx = ball.x - self._goal_kick_origin[0]
            dy = ball.y - self._goal_kick_origin[1]
            if (dx * dx + dy * dy) ** 0.5 >= self.kit.config.strategy.restart_touch_distance:
                self._goal_kick_complete = True
        return self._goal_kick_complete

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
