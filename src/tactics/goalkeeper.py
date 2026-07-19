"""Goalkeeper shot projection and high-level defensive state machine.

The motion SDK exposes chassis velocity and a generic kick manager, not a
per-foot save primitive. This module therefore decides where the keeper must
meet a shot and when a clearance may take over; the existing motion layer still
executes the walk or kick.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from ..soccer_framework import BallState, Pose2D, SoccerConfig
from .geometry import TeamFieldFrame, clamp


__all__ = [
    "GoalkeeperStateMachine",
    "KeeperPhase",
    "KeeperPlan",
    "ShotProjection",
    "project_shot_to_x",
]


class KeeperPhase(str, Enum):
    GUARD = "guard"
    TRACK_SHOT = "track_shot"
    BLOCK_LINE = "block_line"
    CLEAR = "clear"
    RECOVER = "recover"


@dataclass(frozen=True)
class ShotProjection:
    target_x: float
    target_y: float
    time_sec: float


@dataclass(frozen=True)
class KeeperPlan:
    phase: KeeperPhase
    move_target: Pose2D | None = None
    projection: ShotProjection | None = None

    @property
    def wants_kick(self) -> bool:
        return self.phase == KeeperPhase.CLEAR

    @property
    def defends_live_shot(self) -> bool:
        return self.phase in {
            KeeperPhase.TRACK_SHOT,
            KeeperPhase.BLOCK_LINE,
            KeeperPhase.CLEAR,
        }


def project_shot_to_x(
    ball: BallState,
    target_x: float,
    *,
    min_toward_speed: float,
    horizon_sec: float,
) -> ShotProjection | None:
    """Project a team-frame ball trajectory onto ``target_x``."""

    if ball.vx >= -abs(min_toward_speed):
        return None
    time_sec = (target_x - ball.x) / ball.vx
    if time_sec < 0.0 or time_sec > horizon_sec:
        return None
    return ShotProjection(
        target_x=target_x,
        target_y=ball.y + ball.vy * time_sec,
        time_sec=time_sec,
    )


class GoalkeeperStateMachine:
    """Stateful keeper phase selection with clearance/recovery hysteresis."""

    def __init__(self) -> None:
        self.phase = KeeperPhase.GUARD
        self._recover_started_at = 0.0

    def update(
        self,
        config: SoccerConfig,
        field: TeamFieldFrame,
        ball: BallState,
        keeper_pose: Pose2D | None,
        *,
        in_claim_area: bool,
        in_clear_exit_area: bool,
    ) -> KeeperPlan:
        own_goal_x = field.own_goal_x()
        block_x = own_goal_x + config.strategy.goalkeeper_block_offset_m
        projection = project_shot_to_x(
            ball,
            block_x,
            min_toward_speed=config.strategy.goalkeeper_shot_min_speed_mps,
            horizon_sec=config.strategy.goalkeeper_shot_horizon_sec,
        )
        if projection is not None:
            goal_y = ball.y + ball.vy * ((own_goal_x - ball.x) / ball.vx)
            if abs(goal_y) > config.goal_width / 2.0 + 0.25:
                projection = None

        distance_to_ball = math.inf
        if keeper_pose is not None:
            distance_to_ball = math.hypot(
                keeper_pose.x - ball.x,
                keeper_pose.y - ball.y,
            )
        emergency_clear = (
            ball.x <= own_goal_x + config.strategy.goalkeeper_goal_line_emergency_m
            or distance_to_ball
            <= config.strategy.goalkeeper_emergency_kick_distance_m
        )

        if projection is not None and not emergency_clear:
            phase = (
                KeeperPhase.BLOCK_LINE
                if projection.time_sec
                <= config.strategy.goalkeeper_block_imminent_sec
                else KeeperPhase.TRACK_SHOT
            )
            self.phase = phase
            return KeeperPlan(
                phase=phase,
                move_target=self._block_target(config, field, ball, projection),
                projection=projection,
            )

        was_clearing = self.phase == KeeperPhase.CLEAR
        if in_claim_area or (was_clearing and in_clear_exit_area):
            self.phase = KeeperPhase.CLEAR
            return KeeperPlan(phase=self.phase, projection=projection)

        if was_clearing:
            self.phase = KeeperPhase.RECOVER
            self._recover_started_at = ball.last_seen_at
        elif self.phase == KeeperPhase.RECOVER:
            elapsed = ball.last_seen_at - self._recover_started_at
            if elapsed >= config.strategy.goalkeeper_recover_hold_sec:
                self.phase = KeeperPhase.GUARD
        else:
            self.phase = KeeperPhase.GUARD
        return KeeperPlan(phase=self.phase, projection=projection)

    @staticmethod
    def _block_target(
        config: SoccerConfig,
        field: TeamFieldFrame,
        ball: BallState,
        projection: ShotProjection,
    ) -> Pose2D:
        # Leave body-radius clearance from both posts. Centering the torso on
        # this point closes the gap between the feet more reliably than merely
        # turning toward the current ball position.
        max_y = max(0.0, config.goal_width / 2.0 - 0.28)
        target_y = clamp(projection.target_y, -max_y, max_y)
        return Pose2D(
            projection.target_x,
            target_y,
            field.face_ball_theta(projection.target_x, target_y, ball),
        )
