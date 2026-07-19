"""Regression tests for the competition rules that directly constrain strategy."""

from __future__ import annotations

import math
import unittest

from src.play.playbook import (
    DefaultPlaybook,
    ROLE_CHASER,
    ROLE_DEFENDER,
    ROLE_GOALKEEPER,
    ROLE_NONE,
    ROLE_SUPPORTER,
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
    def ball_in_own_defensive_area(
        self,
        _ball: BallState,
        extra_margin_m: float = 0.0,
    ) -> bool:
        return _ball.x <= -4.0 + extra_margin_m

    def side_should_challenge(self, _context: PlayContext) -> bool:
        return True

    def keeper_should_sweep_loose_ball(
        self,
        context: PlayContext,
        keeper_id: int,
        *,
        continuing: bool = False,
    ) -> bool:
        del continuing
        keeper = context.teammates.get(keeper_id)
        if keeper is None or keeper.pose is None or not context.opponents:
            return False
        ball = context.known_ball
        nearest_opponent = min(
            math.hypot(robot.pose.x - ball.x, robot.pose.y - ball.y)
            for robot in context.opponents.values()
            if robot.pose is not None
        )
        return -4.0 < ball.x <= -2.5 and nearest_opponent >= 0.85

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
    def __init__(self) -> None:
        self.active_players: set[int] = set()

    def configure(self, enter: float, exit: float, exit_delay: float) -> None:
        self.values = (enter, exit, exit_delay)

    def is_active(self, player_id: int) -> bool:
        return player_id in self.active_players


def _context(
    *,
    penalties: dict[int, Penalty] | None = None,
    own_score: int = 0,
    opponent_score: int = 0,
    secs_remaining: int = 600,
    ball_x: float = 0.0,
    ball_y: float = 0.0,
    teammate_xy: dict[int, tuple[float, float]] | None = None,
    opponent_xy: dict[int, tuple[float, float]] | None = None,
    set_play: SetPlay = SetPlay.NONE,
    kicking_team: int = 0,
) -> PlayContext:
    penalties = penalties or {}
    teammate_xy = teammate_xy or {
        1: (-1.0, 0.0),
        2: (-4.0, 1.0),
        3: (-6.0, 0.0),
    }
    own_players = [
        PlayerState(penalty=penalties.get(player_id, Penalty.NONE))
        for player_id in range(1, 4)
    ]
    game = GameControlState(
        state=GameState.PLAYING,
        set_play=set_play,
        kicking_team=kicking_team,
        secs_remaining=secs_remaining,
        teams=[
            TeamState(team_number=1, score=own_score, players=own_players),
            TeamState(team_number=2, score=opponent_score),
        ],
    )
    return PlayContext(
        game_state=game,
        ball=BallState(x=ball_x, y=ball_y, last_seen_at=1.0),
        teammates={
            player_id: RobotState(
                player_id,
                Pose2D(x, y, 0.0),
                1.0,
            )
            for player_id, (x, y) in teammate_xy.items()
        },
        opponents={
            player_id: RobotState(player_id, Pose2D(x, y, 0.0), 1.0)
            for player_id, (x, y) in (opponent_xy or {}).items()
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

    def test_keeper_exclusively_owns_ball_in_defensive_area(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))

        assignment = playbook.assign_roles(_context(ball_x=-5.0))

        self.assertEqual(assignment.ball_owner_id, 3)
        self.assertTrue(assignment.owns_ball(3))
        self.assertNotIn(ROLE_CHASER, assignment.by_player.values())
        self.assertEqual(assignment.role_of(2), ROLE_DEFENDER)

    def test_keeper_ball_claim_uses_exit_hysteresis(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))

        entered = playbook.assign_roles(_context(ball_x=-4.2))
        still_owned = playbook.assign_roles(_context(ball_x=-3.8))
        released = playbook.assign_roles(_context(ball_x=-3.5))

        self.assertEqual(entered.ball_owner_id, 3)
        self.assertEqual(still_owned.ball_owner_id, 3)
        self.assertNotEqual(released.ball_owner_id, 3)

    def test_robot2_is_defensive_pivot_when_robot1_owns_ball(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))

        assignment = playbook.assign_roles(
            _context(
                ball_x=1.0,
                teammate_xy={1: (0.8, 0.0), 2: (-0.5, 1.0), 3: (-6.0, 0.0)},
            )
        )

        self.assertEqual(assignment.ball_owner_id, 1)
        self.assertEqual(assignment.role_of(1), ROLE_CHASER)
        self.assertEqual(assignment.role_of(2), ROLE_DEFENDER)

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

    def test_chaser_handoff_is_fast_until_the_incumbent_starts_kicking(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))

        initial = playbook.assign_roles(
            _context(teammate_xy={1: (-0.9, 0.0), 2: (-1.5, 0.0), 3: (-6.0, 0.0)})
        )
        self.assertEqual(initial.role_of(1), ROLE_CHASER)

        # Player 2 is 0.05 score units better. Before a kick starts this exceeds
        # the narrow idle band and ownership transfers immediately.
        near_tie = playbook.assign_roles(
            _context(teammate_xy={1: (-1.0, 0.0), 2: (-0.85, 0.0), 3: (-6.0, 0.0)})
        )
        self.assertEqual(near_tie.role_of(2), ROLE_CHASER)

        # Once player 2 is actually kicking, the wider tie band protects the
        # action from a one-frame role reversal.
        playbook.kit.kicker.active_players.add(2)
        protected = playbook.assign_roles(
            _context(teammate_xy={1: (-0.75, 0.0), 2: (-0.90, 0.0), 3: (-6.0, 0.0)})
        )
        self.assertEqual(protected.role_of(2), ROLE_CHASER)

        clear_winner = playbook.assign_roles(
            _context(teammate_xy={1: (-1.0, 0.0), 2: (-0.3, 0.0), 3: (-6.0, 0.0)})
        )
        self.assertEqual(clear_winner.role_of(2), ROLE_CHASER)

    def test_goal_kick_is_one_touch_with_wide_receivers(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        initial = playbook.assign_roles(
            _context(
                ball_x=-5.7,
                set_play=SetPlay.GOAL_KICK,
                kicking_team=1,
            )
        )
        touched = playbook.assign_roles(
            _context(
                ball_x=-5.20,
                set_play=SetPlay.GOAL_KICK,
                kicking_team=1,
            )
        )

        self.assertEqual(initial.ball_owner_id, 3)
        self.assertEqual(initial.role_of(1), ROLE_SUPPORTER)
        self.assertEqual(initial.role_of(2), ROLE_DEFENDER)
        self.assertIsNone(touched.ball_owner_id)
        self.assertEqual(touched.role_of(3), ROLE_NONE)

    def test_keeper_sweeps_loose_one_on_one_ball(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        assignment = playbook.assign_roles(
            _context(
                ball_x=-3.4,
                teammate_xy={1: (0.0, 0.0), 2: (-0.5, 1.0), 3: (-5.8, 0.0)},
                opponent_xy={1: (-2.4, 0.0)},
            )
        )

        self.assertEqual(assignment.ball_owner_id, 3)
        self.assertNotIn(ROLE_CHASER, assignment.by_player.values())

    def test_chaser_hysteresis_drops_an_ineligible_incumbent(self) -> None:
        playbook = DefaultPlaybook(_FakeKit(SoccerConfig()))
        playbook.assign_roles(_context())

        reassigned = playbook.assign_roles(
            _context(penalties={1: Penalty.ILLEGAL_POSITIONING})
        )
        self.assertEqual(reassigned.role_of(1), "none")
        self.assertEqual(reassigned.role_of(2), ROLE_CHASER)

    def test_every_opponent_restart_target_keeps_rule_distance(self) -> None:
        config = SoccerConfig()
        field = TeamFieldFrame(config)
        ball = BallState(x=1.0, y=0.0, last_seen_at=1.0)
        for set_play in SetPlay:
            if set_play == SetPlay.NONE:
                continue
            with self.subTest(set_play=set_play):
                game = GameControlState(
                    state=GameState.PLAYING,
                    set_play=set_play,
                    kicking_team=2,
                )
                context = PlayContext(
                    game_state=game,
                    ball=ball,
                    teammates={
                        1: RobotState(1, Pose2D(1.1, 0.0, 0.0), 1.0)
                    },
                )
                target = opponent_restart_target(
                    config,
                    field,
                    player_id=1,
                    slot=ReadySlot.CENTER,
                    context=context,
                    base_ready_target=(
                        lambda _slot, _kickoff: Pose2D(1.0, 0.0, 0.0)
                    ),
                )
                self.assertGreaterEqual(
                    math.hypot(target.x - ball.x, target.y - ball.y),
                    config.strategy.opponent_restart_avoid_distance_m - 1e-9,
                )
        self.assertGreaterEqual(
            config.strategy.opponent_restart_avoid_distance_m,
            1.45,
        )

    def test_opponent_goal_kick_target_leaves_penalty_area(self) -> None:
        config = SoccerConfig()
        field = TeamFieldFrame(config)
        ball = BallState(x=6.0, y=0.0, last_seen_at=1.0)
        game = GameControlState(
            state=GameState.PLAYING,
            set_play=SetPlay.GOAL_KICK,
            kicking_team=2,
        )
        context = PlayContext(
            game_state=game,
            ball=ball,
            teammates={1: RobotState(1, Pose2D(0.0, 3.5, 0.0), 1.0)},
        )

        target = opponent_restart_target(
            config,
            field,
            player_id=1,
            slot=ReadySlot.CENTER,
            context=context,
            base_ready_target=lambda _slot, _kickoff: Pose2D(5.5, 0.0, 0.0),
        )
        penalty_front_x = (
            config.field_length / 2.0
            - config.penalty_area_length
            - 0.55
        )
        inside_penalty_area = (
            target.x >= penalty_front_x
            and abs(target.y) <= config.penalty_area_width / 2.0 + 0.35
        )

        self.assertFalse(inside_penalty_area)
        self.assertGreaterEqual(
            math.hypot(target.x - ball.x, target.y - ball.y),
            config.strategy.opponent_restart_avoid_distance_m - 1e-9,
        )


if __name__ == "__main__":
    unittest.main()
