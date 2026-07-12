"""Adaptive strategy profiles derived from the official tuning guide."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..soccer_framework import PlayContext

if TYPE_CHECKING:
    from ..runtime import SoccerKit


class StrategyProfile(str, Enum):
    AGGRESSIVE = "aggressive"
    DEFENSIVE = "defensive"
    FLUID = "fluid"
    PRECISION = "precision"


@dataclass(frozen=True)
class ProfileValues:
    values: dict[str, float | bool]


PROFILES: dict[StrategyProfile, ProfileValues] = {
    StrategyProfile.AGGRESSIVE: ProfileValues({
        "max_linear_speed": 1.2,
        "max_angular_speed": 1.5,
        "soccer_kick_power": 2.5,
        "soccer_kick_enter_distance": 2.5,
        "soccer_kick_exit_distance": 3.2,
        "soccer_kick_exit_delay_sec": 1.5,
        "opponent_obstacle_radius": 0.40,
        "teammate_obstacle_radius": 0.38,
        "obstacle_safety_margin": 0.12,
        "obstacle_start_ignore_distance": 0.50,
        "yaw_avoid_min_distance_m": 0.50,
        "yaw_avoid_bias_max": 0.35,
        "teammate_challenge_tie_margin_m": 0.20,
        "pass_min_score": 0.42,
        "pass_min_forward_m": 0.80,
        "pass_lane_clearance": 0.65,
        "shot_lane_min_score": 0.55,
        "dribble_advance_m": 2.0,
        "dribble_center_pull": 0.80,
        "support_depth_m": 0.60,
        "sideline_recovery_advance_m": 1.20,
        "goalkeeper_challenge_margin_m": 0.70,
    }),
    StrategyProfile.DEFENSIVE: ProfileValues({
        "max_linear_speed": 1.2,
        "max_angular_speed": 1.3,
        "soccer_kick_power": 2.5,
        "soccer_kick_enter_distance": 2.4,
        "soccer_kick_exit_distance": 3.2,
        "soccer_kick_exit_delay_sec": 2.5,
        "opponent_obstacle_radius": 0.70,
        "teammate_obstacle_radius": 0.48,
        "obstacle_safety_margin": 0.35,
        "obstacle_start_ignore_distance": 0.40,
        "yaw_avoid_min_distance_m": 1.10,
        "yaw_avoid_bias_max": 0.85,
        "teammate_challenge_tie_margin_m": 0.25,
        "pass_min_score": 0.58,
        "pass_min_forward_m": 0.20,
        "pass_lane_clearance": 1.00,
        "shot_lane_min_score": 0.65,
        "dribble_advance_m": 1.00,
        "dribble_center_pull": 0.90,
        "support_depth_m": 1.60,
        "sideline_recovery_advance_m": 0.50,
        "goalkeeper_challenge_margin_m": 0.40,
    }),
    StrategyProfile.FLUID: ProfileValues({
        "max_linear_speed": 1.2,
        "max_angular_speed": 1.5,
        "soccer_kick_power": 2.2,
        "soccer_kick_enter_distance": 2.2,
        "soccer_kick_exit_distance": 3.2,
        "soccer_kick_exit_delay_sec": 1.5,
        "opponent_obstacle_radius": 0.40,
        "teammate_obstacle_radius": 0.38,
        "obstacle_safety_margin": 0.12,
        "obstacle_start_ignore_distance": 0.50,
        "yaw_avoid_min_distance_m": 0.50,
        "yaw_avoid_bias_max": 0.35,
        "teammate_challenge_tie_margin_m": 0.25,
        "pass_min_score": 0.52,
        "pass_min_forward_m": 0.35,
        "pass_lane_clearance": 0.75,
        "shot_lane_min_score": 0.55,
        "dribble_advance_m": 1.50,
        "dribble_center_pull": 0.75,
        "support_depth_m": 1.00,
        "sideline_recovery_advance_m": 0.90,
        "goalkeeper_challenge_margin_m": 0.60,
    }),
    StrategyProfile.PRECISION: ProfileValues({
        "max_linear_speed": 1.2,
        "max_angular_speed": 1.3,
        "soccer_kick_power": 2.2,
        "soccer_kick_enter_distance": 2.2,
        "soccer_kick_exit_distance": 3.2,
        "soccer_kick_exit_delay_sec": 1.5,
        "opponent_obstacle_radius": 0.48,
        "teammate_obstacle_radius": 0.42,
        "obstacle_safety_margin": 0.18,
        "obstacle_start_ignore_distance": 0.45,
        "yaw_avoid_min_distance_m": 0.65,
        "yaw_avoid_bias_max": 0.50,
        "teammate_challenge_tie_margin_m": 0.25,
        "pass_min_score": 0.65,
        "pass_min_forward_m": 0.35,
        "pass_lane_clearance": 1.00,
        "shot_lane_min_score": 0.65,
        "dribble_advance_m": 1.20,
        "dribble_center_pull": 1.00,
        "support_depth_m": 0.80,
        "sideline_recovery_advance_m": 0.75,
        "goalkeeper_challenge_margin_m": 0.55,
    }),
}


class AdaptiveStrategyManager:
    """Select and apply a profile using score, clock, and ball territory."""

    def __init__(self, kit: "SoccerKit") -> None:
        self.kit = kit
        self.current_profile = StrategyProfile.FLUID
        self.apply(self.current_profile)

    def update(self, context: PlayContext) -> StrategyProfile:
        profile = self.select(context)
        if profile != self.current_profile:
            self.apply(profile)
        return self.current_profile

    def select(self, context: PlayContext) -> StrategyProfile:
        game = context.known_game
        own = game.get_team_state(self.kit.config.team_id)
        opponent = game.get_team_state(self.kit.config.opponent_team_id())
        score_delta = 0 if own is None or opponent is None else own.score - opponent.score

        # Score/clock overrides territory: chase the game late, protect a late lead.
        if game.secs_remaining <= 150 and score_delta < 0:
            return StrategyProfile.AGGRESSIVE
        if game.secs_remaining <= 150 and score_delta > 0:
            return StrategyProfile.DEFENSIVE

        ball_x = context.known_ball.x
        if self.current_profile == StrategyProfile.DEFENSIVE and ball_x < -2.5:
            return StrategyProfile.DEFENSIVE
        if ball_x <= -3.5:
            return StrategyProfile.DEFENSIVE
        if self.current_profile == StrategyProfile.PRECISION and ball_x > 2.5:
            return StrategyProfile.PRECISION
        if ball_x >= 3.5:
            return StrategyProfile.PRECISION
        return StrategyProfile.FLUID

    def apply(self, profile: StrategyProfile) -> None:
        strategy = self.kit.config.strategy
        for name, value in PROFILES[profile].values.items():
            setattr(strategy, name, value)
        self.kit.kicker.configure(
            strategy.soccer_kick_enter_distance,
            strategy.soccer_kick_exit_distance,
            strategy.soccer_kick_exit_delay_sec,
        )
        self.current_profile = profile
