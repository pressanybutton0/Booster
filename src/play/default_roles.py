"""Default dynamic role implementations for SoccerSim.

Each role extends :class:`RoleStrategy` and assembles its subtree through
:meth:`build_subtree`. Methods such as ``target``, ``wants_to_kick``, and
``kick_target`` are implementation helpers for role utility nodes, not
base-class contracts.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import py_trees

from ..soccer_framework import PlayContext, Pose2D, ReadySlot
from ..tactics.geometry import clamp
from ..tactics.goalkeeper import GoalkeeperStateMachine, KeeperPhase, KeeperPlan
from .nodes import AttackSubtreeConfig, build_attack_subtree
from .role import RoleStrategy

if TYPE_CHECKING:
    from ..runtime import SoccerKit


# ----------------------------------------------------------------------
# Chaser: approach and kick the ball
# ----------------------------------------------------------------------

# Approach alignment distance behind the ball while chasing, in meters.
_CHASER_APPROACH_OFFSET = 0.4
def _forward_clear_target(kit: "SoccerKit") -> Pose2D:
    return Pose2D(
        kit.field.opponent_goal_x(),
        0.0,
        kit.field.attack_theta(),
    )


class ChaserRole(RoleStrategy):
    """Default chaser; kick target is split by ReadySlot into center shot or side clearance."""

    name = "chaser"

    def target(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> Pose2D:
        ball = context.known_ball
        kt = self.kick_target(kit, player_id, context)
        kick_theta = math.atan2(kt.y - ball.y, kt.x - ball.x)
        return kit.motion.approach_target(
            ball,
            kick_theta,
            _CHASER_APPROACH_OFFSET,
        )

    def kick_target(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> Pose2D:
        slot = kit.config.ready_slot_for_player(player_id)
        if slot == ReadySlot.SIDE:
            return kit.targeting.select_clear_or_pass_target(
                player_id,
                context,
                kit.is_player_allowed,
            )
        return kit.targeting.select_kick_target(
            player_id,
            context,
            kit.is_player_allowed,
        )

    def _approach_reason(self, kit: "SoccerKit", player_id: int) -> str:
        slot = kit.config.ready_slot_for_player(player_id)
        return f"{slot.value} approach ball"

    def _kick_reason(
        self,
        kit: "SoccerKit",
        player_id: int,
        target: Pose2D,
        context: PlayContext,
    ) -> str:
        slot = kit.config.ready_slot_for_player(player_id)
        game = context.known_game
        if (
            game.is_restart_for_team(kit.config.team_id)
            and game.set_play.value == "CORNER_KICK"
        ):
            return f"{slot.value} corner cutback"
        default = "side clear" if slot == ReadySlot.SIDE else "center kick"
        return kit.targeting.kick_reason(
            target,
            default=default,
            ball=context.known_ball,
        )

    def build_subtree(
        self,
        kit: "SoccerKit",
        player_id: int,
    ) -> py_trees.behaviour.Behaviour:
        return build_attack_subtree(
            kit,
            player_id,
            AttackSubtreeConfig(
                target_fn=lambda context: self.target(kit, player_id, context),
                kick_target_fn=lambda context: self.kick_target(
                    kit,
                    player_id,
                    context,
                ),
                reason_fn=lambda: self._approach_reason(kit, player_id),
                kick_reason_fn=lambda target, context: self._kick_reason(
                    kit,
                    player_id,
                    target,
                    context,
                ),
                contest_ball_fn=lambda _context: True,
            ),
        )


# ----------------------------------------------------------------------
# Supporter: attacking support position
# ----------------------------------------------------------------------


class SupporterRole(RoleStrategy):
    """Attacking support role; uses :meth:`Targeting.support_target` for positioning."""

    name = "supporter"

    def target(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> Pose2D:
        return kit.targeting.support_target(
            player_id,
            context,
            kit.is_player_allowed,
        )

    def wants_to_kick(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> bool:
        # Ball ownership is also movement ownership: only the assigned chaser
        # may collapse onto the ball. The supporter opens a passing lane.
        return False

    def kick_target(
        self,
        kit: "SoccerKit",
        context: PlayContext,
    ) -> Pose2D:
        return _forward_clear_target(kit)

    def build_subtree(
        self,
        kit: "SoccerKit",
        player_id: int,
    ) -> py_trees.behaviour.Behaviour:
        return build_attack_subtree(
            kit,
            player_id,
            AttackSubtreeConfig(
                target_fn=lambda context: self.target(kit, player_id, context),
                kick_target_fn=lambda context: self.kick_target(kit, context),
                wants_kick_fn=lambda context: self.wants_to_kick(
                    kit, player_id, context
                ),
                reason_fn=lambda: "supporter receive",
                kick_reason_fn=lambda _target, _context: "supporter challenge clear",
                contest_ball_fn=None,
            ),
        )


# ----------------------------------------------------------------------
# Defender: extension defensive position, defaulting to supporter target
# ----------------------------------------------------------------------


class DefenderRole(RoleStrategy):
    """Hold a compact line between the ball and the own goal."""

    name = "defender"

    def __init__(self) -> None:
        self._supporting_by_player: dict[int, bool] = {}

    def target(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> Pose2D:
        game = context.game
        if (
            game is not None
            and game.is_restart_for_team(kit.config.team_id)
            and game.set_play.value == "GOAL_KICK"
        ):
            self._supporting_by_player[player_id] = True
            return kit.targeting.support_target(
                player_id,
                context,
                kit.is_player_allowed,
            )
        supporting = context.known_ball.x >= -0.25
        self._supporting_by_player[player_id] = supporting
        if supporting:
            return kit.targeting.support_target(
                player_id,
                context,
                kit.is_player_allowed,
            )
        return self._hold_target(kit, context)

    def _hold_target(
        self,
        kit: "SoccerKit",
        context: PlayContext,
    ) -> Pose2D:
        ball = context.known_ball
        own_goal_x = kit.field.own_goal_x()
        # Stay behind the ball, but never collapse onto the goalkeeper line.
        target_x = clamp(ball.x - 1.5, own_goal_x + 2.25, -0.75)
        target_y = clamp(
            ball.y * 0.60,
            -(kit.config.goal_width / 2.0 + 0.9),
            kit.config.goal_width / 2.0 + 0.9,
        )
        return kit.field.clamp_inside_field(
            Pose2D(
                target_x,
                target_y,
                kit.field.face_ball_theta(target_x, target_y, ball),
            ),
            margin=0.35,
        )

    def wants_to_kick(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> bool:
        # The pivot challenges only after role arbitration promotes it to
        # chaser. While it is the defender it holds or becomes a receiver.
        return False

    def kick_target(
        self,
        kit: "SoccerKit",
        context: PlayContext,
    ) -> Pose2D:
        return _forward_clear_target(kit)

    def build_subtree(
        self,
        kit: "SoccerKit",
        player_id: int,
    ) -> py_trees.behaviour.Behaviour:
        return build_attack_subtree(
            kit,
            player_id,
            AttackSubtreeConfig(
                target_fn=lambda context: self.target(kit, player_id, context),
                kick_target_fn=lambda context: self.kick_target(kit, context),
                wants_kick_fn=lambda context: self.wants_to_kick(
                    kit, player_id, context
                ),
                reason_fn=lambda: (
                    "defender support"
                    if self._supporting_by_player.get(player_id, False)
                    else "defender hold"
                ),
                kick_reason_fn=lambda _target, _context: "defender challenge clear",
                contest_ball_fn=None,
            ),
        )


# ----------------------------------------------------------------------
# Goalkeeper: guard the goal and clear dangerous balls
# ----------------------------------------------------------------------


class GoalkeeperRole(RoleStrategy):
    """Trajectory-aware goalkeeper with exclusive-area clearance."""

    name = "goalkeeper"

    # Approach alignment distance for goalkeeper challenges, tighter than the chaser, in meters.
    _APPROACH_OFFSET = 0.22

    def __init__(self) -> None:
        self._planner = GoalkeeperStateMachine()
        self._last_plan = KeeperPlan(KeeperPhase.GUARD)

    def _plan(
        self,
        kit: "SoccerKit",
        context: PlayContext,
        player_id: int | None = None,
    ) -> KeeperPlan:
        ball = context.known_ball
        keeper_id = (
            kit.config.goalkeeper_player_id()
            if player_id is None
            else player_id
        )
        keeper = context.teammates.get(keeper_id) if keeper_id is not None else None
        keeper_pose = keeper.pose if keeper is not None else None
        sweeping = kit.targeting.keeper_should_sweep_loose_ball(
            context,
            keeper_id,
            continuing=self._planner.phase == KeeperPhase.CLEAR,
        )
        in_claim_area = (
            kit.targeting.ball_in_own_defensive_area(ball) or sweeping
        )
        in_clear_exit_area = kit.targeting.ball_in_own_defensive_area(
            ball,
            extra_margin_m=kit.config.strategy.goalkeeper_clear_exit_margin_m,
        ) or kit.targeting.keeper_should_sweep_loose_ball(
            context,
            keeper_id,
            continuing=True,
        )
        self._last_plan = self._planner.update(
            kit.config,
            kit.field,
            ball,
            keeper_pose,
            in_claim_area=in_claim_area,
            in_clear_exit_area=in_clear_exit_area,
        )
        return self._last_plan

    def target(
        self,
        kit: "SoccerKit",
        player_id: int,
        context: PlayContext,
    ) -> Pose2D:
        ball = context.known_ball
        plan = self._plan(kit, context, player_id)
        if plan.wants_kick:
            kt = self.kick_target(kit, context)
            kick_theta = math.atan2(kt.y - ball.y, kt.x - ball.x)
            approach = kit.motion.approach_target(
                ball,
                kick_theta,
                self._APPROACH_OFFSET,
            )
            # A forward clearance normally asks for a point behind the ball.
            # On the goal line that point is inside the net, which produced the
            # observed keeper walking into its own goal.  Keep the chassis on
            # the playable side and let SoccerKickManager handle the close ball.
            min_x = kit.field.own_goal_x() + 0.32
            if approach.x < min_x:
                approach = Pose2D(min_x, approach.y, approach.theta)
            return kit.field.clamp_inside_field(approach, margin=0.30)
        if plan.move_target is not None:
            return plan.move_target
        return kit.ready_stance.goalkeeper_guard_target(ball)

    def wants_to_kick(
        self,
        kit: "SoccerKit",
        context: PlayContext,
        player_id: int | None = None,
    ) -> bool:
        return self._plan(kit, context, player_id).wants_kick

    def kick_target(
        self,
        kit: "SoccerKit",
        context: PlayContext,
    ) -> Pose2D:
        ball = context.known_ball
        game = context.game
        if (
            game is not None
            and game.is_restart_for_team(kit.config.team_id)
            and game.set_play.value == "GOAL_KICK"
        ):
            return kit.targeting.goal_kick_delivery_target(ball)
        if kit.targeting.ball_near_sideline(ball):
            return kit.targeting.sideline_recovery_target(ball)
        # Clear diagonally into the less crowded flank.  A straight kick through
        # the centre is both easier to block and asks a goal-line keeper to walk
        # behind the ball into the net.
        positive_opponents = sum(
            1
            for opponent in context.opponents.values()
            if opponent.pose is not None and opponent.pose.y >= 0.0
        )
        negative_opponents = sum(
            1
            for opponent in context.opponents.values()
            if opponent.pose is not None and opponent.pose.y < 0.0
        )
        if abs(ball.y) >= 0.25:
            side = 1.0 if ball.y >= 0.0 else -1.0
        else:
            side = 1.0 if positive_opponents <= negative_opponents else -1.0
        target_x = max(
            ball.x + 1.6,
            kit.field.own_goal_x() + kit.config.penalty_area_length + 0.35,
        )
        target_y = side * min(
            kit.config.field_width / 2.0 - 0.45,
            kit.config.goal_area_width / 2.0 + 0.85,
        )
        return kit.field.clamp_inside_field(
            Pose2D(target_x, target_y, kit.field.attack_theta()),
            margin=0.35,
        )

    def _guard_reason(self) -> str:
        return f"goalkeeper {self._last_plan.phase.value.replace('_', ' ')}"

    def _kick_reason(
        self,
        kit: "SoccerKit",
        target: Pose2D,
        context: PlayContext,
    ) -> str:
        return kit.targeting.kick_reason(
            target,
            default="goalkeeper clear",
            ball=context.known_ball,
        )

    def build_subtree(
        self,
        kit: "SoccerKit",
        player_id: int,
    ) -> py_trees.behaviour.Behaviour:
        return build_attack_subtree(
            kit,
            player_id,
            AttackSubtreeConfig(
                target_fn=lambda context: self.target(kit, player_id, context),
                kick_target_fn=lambda context: self.kick_target(kit, context),
                wants_kick_fn=lambda context: self.wants_to_kick(
                    kit, context, player_id
                ),
                reason_fn=self._guard_reason,
                kick_reason_fn=lambda target, context: self._kick_reason(
                    kit,
                    target,
                    context,
                ),
                hold_vyaw=0.12,
                contest_ball_fn=lambda context: self.wants_to_kick(
                    kit, context, player_id
                ),
                goal_defense_fn=lambda context: self._plan(
                    kit, context, player_id
                ).defends_live_shot,
            ),
        )
