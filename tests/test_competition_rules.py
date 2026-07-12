"""Regression tests for the competition rules that directly constrain strategy."""

from __future__ import annotations

import math
import unittest

from src.play.playbook import (
    DefaultPlaybook,
    ROLE_CHASER,
    ROLE_DEFENDER,
    ROLE_GOALKEEPER,
)
from src.play.strategy_profiles import StrategyProfile
from src.soccer_framework import (
    ADULT_FIELD_DIMENSIONS,
    BallState,
    GameControlState,
    GameState,
    Penalty,
    PlayContext,
    PlayerState,
    Pose2D,
    ReadySlot,
    RobotState,
    SetPlay,
    SoccerConfig,
    TeamState,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.targeting.restart import opponent_restart_target


class _FakeTargeting:
    def ball_in_own_defensive_area(self, _ball: BallState) -> bool:
        return False

    def side_should_challenge(self, _context: PlayContext) -> bool:
        return True

    def ball_claim_score(
        self,
        slot: ReadySlot,
        pose: Pose2D,
        ball: BallState,
    ) -> float:
        bias = {
            ReadySlot.CENTER: -0.20,
            ReadySlot.SIDE: -0.10,
            ReadySlot.KEEPER: 14.0,
        }[slot]
        return math.hypot(pose.x - ball.x, pose.y - ball.y) + bias


class _FakeKit:
    def __init__(self, config: SoccerConfig):
        self.config = config
        self.targeting = _FakeTargeting()
        self.kicker = _FakeKicker()


class _FakeKicker:
    def configure(self, enter: float, exit: float, exit_delay: float) -> None:
        self.values = (enter, exit, exit_delay)


def _context(
    *,
    penalties: dict[int, Penalty] | None = None,
    own_score: int = 0,
    opponent_score: int = 0,
    secs_remaining: int = 600,
    ball_x: float = 0.0,
) -> PlayContext:
    penalties = penalties or {}
    own_players = [
        PlayerState(penalty=penalties.get(player_id, Penalty.NONE))
        for player_id in range(1, 4)
    ]
    game = GameControlState(
        state=GameState.PLAYING,
        secs_remaining=secs_remaining,
        teams=[
            TeamState(team_number=1, score=own_score, players=own_players),
            TeamState(team_number=2, score=opponent_score),
        ],
    )
    return PlayContext(
        game_state=game,
        ball=BallState(x=ball_x, y=0.0, last_seen_at=1.0),
        teammates={
            1: RobotState(1, Pose2D(-1.0, 0.0, 0.0), 1.0),
            2: RobotState(2, Pose2D(-4.0, 1.0, 0.0), 1.0),
            3: RobotState(3, Pose2D(-6.0, 0.0, 0.0), 1.0),
        },
    )


class CompetitionRuleTests(unittest.TestCase):
    def test_online_simulation_defaults_stay_inside_documented_limits(self) -> None:
        strategy = SoccerConfig().strategy
        self.assertEqual(strategy.max_linear_speed, 1.2)
        self.assertEqual(strategy.max_angular_speed, 1.5)
        self.assertLessEqual(strategy.soccer_kick_power, 2.5)
        self.assertLessEqual(strategy.dribble_advance_m, 2.0)

    def test_late_deficit_switches_to_aggressive_profile(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        playbook.assign_roles(
            _context(own_score=0, opponent_score=1, secs_remaining=120)
        )
        self.assertEqual(
            playbook.strategy_manager.current_profile,
            StrategyProfile.AGGRESSIVE,
        )
        self.assertEqual(playbook.kit.config.strategy.max_angular_speed, 1.5)
        self.assertEqual(playbook.kit.config.strategy.soccer_kick_power, 2.5)

    def test_late_lead_assigns_a_real_defender(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        assignment = playbook.assign_roles(
            _context(own_score=2, opponent_score=1, secs_remaining=120)
        )
        self.assertEqual(
            playbook.strategy_manager.current_profile,
            StrategyProfile.DEFENSIVE,
        )
        self.assertIn(ROLE_DEFENDER, assignment.by_player.values())

    def test_final_third_switches_to_precision_profile(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        playbook.assign_roles(_context(ball_x=4.0))
        self.assertEqual(
            playbook.strategy_manager.current_profile,
            StrategyProfile.PRECISION,
        )
        self.assertEqual(playbook.kit.config.strategy.shot_lane_min_score, 0.65)

    def test_field_dimensions_match_current_rules(self) -> None:
        field = ADULT_FIELD_DIMENSIONS
        self.assertEqual((field.length, field.width), (14.0, 9.0))
        self.assertEqual(field.goal_width, 2.6)
        self.assertEqual(
            (field.penalty_area_length, field.penalty_area_width),
            (3.0, 6.0),
        )
        self.assertEqual(
            (field.goal_area_length, field.goal_area_width),
            (1.0, 4.0),
        )

    def test_penalised_player_cannot_consume_chaser_role(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        assignment = playbook.assign_roles(
            _context(penalties={1: Penalty.ILLEGAL_POSITIONING})
        )
        self.assertEqual(assignment.role_of(1), "none")
        self.assertEqual(assignment.role_of(2), ROLE_CHASER)
        self.assertEqual(assignment.role_of(3), ROLE_GOALKEEPER)

    def test_penalised_keeper_is_replaced_by_rearmost_active_player(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        assignment = playbook.assign_roles(
            _context(penalties={3: Penalty.INCAPABLE_ROBOT})
        )
        self.assertEqual(assignment.role_of(3), "none")
        self.assertEqual(assignment.role_of(2), ROLE_GOALKEEPER)
        self.assertEqual(assignment.role_of(1), ROLE_CHASER)

    def test_opponent_restart_target_keeps_rule_distance(self) -> None:
        config = SoccerConfig()
        field = TeamFieldFrame(config)
        ball = BallState(x=1.0, y=0.0, last_seen_at=1.0)
        game = GameControlState(
            state=GameState.PLAYING,
            set_play=SetPlay.GOAL_KICK,
            kicking_team=2,
        )
        context = PlayContext(
            game_state=game,
            ball=ball,
            teammates={1: RobotState(1, Pose2D(1.1, 0.0, 0.0), 1.0)},
        )
        target = opponent_restart_target(
            config,
            field,
            player_id=1,
            slot=ReadySlot.CENTER,
            context=context,
            base_ready_target=lambda _slot, _kickoff: Pose2D(1.0, 0.0, 0.0),
        )
        self.assertGreaterEqual(
            math.hypot(target.x - ball.x, target.y - ball.y),
            config.strategy.opponent_restart_avoid_distance_m - 1e-9,
        )
        self.assertGreaterEqual(
            config.strategy.opponent_restart_avoid_distance_m,
            1.45,
        )


if __name__ == "__main__":
    unittest.main()
