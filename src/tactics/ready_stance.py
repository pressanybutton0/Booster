"""READY-stage positioning: base targets for three ReadySlots plus SetPlay variants.

Pure model extracted from :class:`SoccerKit`; it depends only on
:class:`SoccerConfig` and :class:`TeamFieldFrame`, owns no cross-tick state, and is
held by :class:`SoccerKit`.
"""

from __future__ import annotations

import math

from ..soccer_framework import (
    BallState,
    GameControlState,
    Pose2D,
    ReadySlot,
    SetPlay,
    SoccerConfig,
)
from .geometry import TeamFieldFrame, clamp


# Keep this value shared with GoReadyTarget.  Legal READY targets must reserve
# at least this much distance because walking intentionally stops anywhere
# inside the arrival radius.
READY_ARRIVE_DISTANCE_M = 0.28
_READY_RULE_CLEARANCE_M = 0.25
_READY_TARGET_RULE_MARGIN_M = (
    READY_ARRIVE_DISTANCE_M + _READY_RULE_CLEARANCE_M
)


class ReadyStance:
    """READY-stage target-position calculation.

    Three pieces of logic:

    :meth:`base_ready_target`: base positions for CENTER, SIDE, and KEEPER.
    :meth:`ready_target_for`: final target from current SetPlay and ball position,
    including own restart, opponent restart, or base target.
    :meth:`goalkeeper_guard_target`: goalkeeper guard-position formula.
    """

    def __init__(self, config: SoccerConfig, field: TeamFieldFrame):
        self.config = config
        self.field = field

    def base_ready_target(
        self,
        slot: ReadySlot,
        own_restart: bool,
    ) -> Pose2D:
        """Base target for the three ReadySlots: center, side, and keeper.

        ``own_restart`` means the restart belongs to us. Own restarts push the
        attacker toward the center circle; opponent restarts pull back for safety.
        """
        field_length = self.config.field_length
        field_width = self.config.field_width
        circle_radius = self.config.center_circle_radius
        goal_area_length = self.config.goal_area_length

        goal_x = self.field.own_goal_x() + goal_area_length + 0.25
        side_y = min(field_width / 2.0 - 0.45, max(0.9, field_width * 0.30))
        attack_x = -max(
            circle_radius * (0.95 if own_restart else 1.6),
            field_length * (0.12 if own_restart else 0.20),
        )
        attack_line_x = self.field.own_half_x(attack_x, margin=0.15)

        if slot == ReadySlot.CENTER:
            return Pose2D(
                x=attack_line_x,
                y=0.0,
                theta=self.field.attack_theta(),
            )
        if slot == ReadySlot.SIDE:
            return Pose2D(
                x=attack_line_x,
                y=side_y,
                theta=self.field.attack_theta(),
            )
        return Pose2D(x=goal_x, y=0.0, theta=self.field.attack_theta())

    def ready_target_for(
        self,
        slot: ReadySlot,
        game: GameControlState,
        ball: BallState | None,
    ) -> Pose2D:
        """Compute READY positioning from the current SetPlay and ball position.

        No SetPlay or no ball: use base target.
        Own restart: stand close to the ball, ready to restart.
        Opponent restart: avoid the configured area around the ball.
        """
        own_restart = game.is_restart_for_team(self.config.team_id)
        base_target = self.base_ready_target(slot, own_restart)
        if game.set_play == SetPlay.NONE or ball is None:
            target = base_target
        elif own_restart:
            target = self._own_set_play_ready_target(slot, ball, base_target)
        else:
            target = self.field.avoid_ball_target(base_target, ball)
        return self._legalize_ready_target(target, game, ball)

    def _legalize_ready_target(
        self,
        target: Pose2D,
        game: GameControlState,
        ball: BallState | None,
    ) -> Pose2D:
        """Apply kickoff placement constraints after all set-play rewrites.

        A kickoff READY target must remain safely inside our half.  During an
        opponent kickoff it must also stay outside the centre circle.  The
        margins prevent small localisation/arrival oscillations from turning a
        nominally legal target into an illegal measured pose.
        """

        target = self.field.clamp_inside_field(target, margin=0.45)
        if game.set_play != SetPlay.NONE:
            return target

        target = Pose2D(
            x=self.field.own_half_x(
                target.x,
                margin=_READY_TARGET_RULE_MARGIN_M,
            ),
            y=target.y,
            theta=target.theta,
        )
        opponent_kickoff = (
            game.has_kicking_team()
            and game.kicking_team != self.config.team_id
        )
        if not opponent_kickoff:
            return target

        # The restricted circle is fixed at the field origin; do not move the
        # legal boundary with a noisy perceived ball position.
        centre_x = 0.0
        centre_y = 0.0
        min_radius = (
            self.config.center_circle_radius + _READY_TARGET_RULE_MARGIN_M
        )
        dx = target.x - centre_x
        dy = target.y - centre_y
        distance = math.hypot(dx, dy)
        if distance >= min_radius:
            return target
        if distance <= 1e-6:
            dx, dy, distance = -1.0, 0.0, 1.0
        scale = min_radius / distance
        return self.field.clamp_inside_field(
            Pose2D(
                x=self.field.own_half_x(
                    centre_x + dx * scale,
                    margin=_READY_TARGET_RULE_MARGIN_M,
                ),
                y=centre_y + dy * scale,
                theta=target.theta,
            ),
            margin=0.45,
        )

    def goalkeeper_guard_target(
        self,
        ball: BallState | None,
    ) -> Pose2D:
        """Goalkeeper guard formula; the default goalkeeper role calls this."""
        keeper_x = self.field.own_goal_x() + self.config.goal_area_length + 0.50
        keeper_y = clamp((ball.y * 0.38) if ball else 0.0, -1.35, 1.35)
        return Pose2D(
            keeper_x,
            keeper_y,
            self.field.face_ball_theta(keeper_x, keeper_y, ball),
        )

    def _own_set_play_ready_target(
        self,
        slot: ReadySlot,
        ball: BallState,
        base_target: Pose2D,
    ) -> Pose2D:
        """Own-restart close-ball positioning: center behind the ball, side diagonally behind for support."""
        if slot == ReadySlot.CENTER:
            return self.field.clamp_inside_field(
                Pose2D(
                    x=ball.x - 0.45,
                    y=ball.y,
                    theta=self.field.face_ball_theta(ball.x, ball.y, ball),
                )
            )
        if slot == ReadySlot.SIDE:
            y_offset = -1.1 if ball.y > 0.0 else 1.1
            return self.field.clamp_inside_field(
                Pose2D(
                    x=ball.x - 1.3,
                    y=ball.y + y_offset,
                    theta=self.field.face_ball_theta(ball.x, ball.y, ball),
                )
            )
        return base_target
