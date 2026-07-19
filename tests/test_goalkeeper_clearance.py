"""Regression tests for conservative goalkeeper clearance boundaries."""

from __future__ import annotations

import unittest

from src.play.default_roles import DefenderRole, GoalkeeperRole, SupporterRole
from src.soccer_framework import (
    BallState,
    PlayContext,
    Pose2D,
    RobotState,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.goalkeeper import GoalkeeperStateMachine, KeeperPhase
from src.tactics.ready_stance import ReadyStance
from src.tactics.targeting import predicates


class _Targeting:
    def __init__(self, config: SoccerConfig) -> None:
        self.config = config

    def ball_in_own_defensive_area(
        self,
        ball: BallState,
        extra_margin_m: float = 0.0,
    ) -> bool:
        return predicates.ball_in_own_defensive_area(
            self.config,
            ball,
            extra_margin_m=extra_margin_m,
        )

    def ball_near_sideline(self, _ball: BallState) -> bool:
        return False

    def keeper_should_sweep_loose_ball(
        self,
        context: PlayContext,
        keeper_id: int,
        *,
        continuing: bool = False,
    ) -> bool:
        return predicates.keeper_should_sweep_loose_ball(
            self.config,
            context,
            keeper_id,
            continuing=continuing,
        )

    def goal_kick_delivery_target(self, ball: BallState) -> Pose2D:
        from src.tactics.targeting.attack import goal_kick_delivery_target

        field = TeamFieldFrame(self.config)
        return goal_kick_delivery_target(self.config, field, ball)


class _Motion:
    def __init__(self, field: TeamFieldFrame) -> None:
        self.field = field

    def approach_target(
        self,
        ball: BallState,
        kick_theta: float,
        approach_offset: float,
    ) -> Pose2D:
        import math

        return self.field.clamp_inside_field(
            Pose2D(
                ball.x - approach_offset * math.cos(kick_theta),
                ball.y - approach_offset * math.sin(kick_theta),
                kick_theta,
            )
        )


class _Kit:
    def __init__(self) -> None:
        self.config = SoccerConfig()
        self.field = TeamFieldFrame(self.config)
        self.targeting = _Targeting(self.config)
        self.motion = _Motion(self.field)
        self.ready_stance = ReadyStance(self.config, self.field)


def _context(
    x: float,
    y: float = 0.0,
    *,
    player_x: float | None = None,
) -> PlayContext:
    teammates = {}
    if player_x is not None:
        teammates[2] = RobotState(2, Pose2D(player_x, y, 0.0), 1.0)
    return PlayContext(
        ball=BallState(x=x, y=y, last_seen_at=1.0),
        teammates=teammates,
    )


class GoalkeeperClearanceTests(unittest.TestCase):
    def test_keeper_does_not_abandon_goal_for_midfield_ball(self) -> None:
        config = SoccerConfig()
        self.assertFalse(
            predicates.ball_in_own_defensive_area(
                config,
                BallState(x=-3.0, y=0.0, last_seen_at=1.0),
            )
        )

    def test_keeper_challenges_close_centered_ball(self) -> None:
        config = SoccerConfig()
        self.assertTrue(
            predicates.ball_in_own_defensive_area(
                config,
                BallState(x=-5.2, y=0.0, last_seen_at=1.0),
            )
        )
        self.assertFalse(
            predicates.ball_in_own_defensive_area(
                config,
                BallState(x=-5.2, y=2.5, last_seen_at=1.0),
            )
        )

    def test_keeper_challenges_at_the_front_of_the_penalty_area(self) -> None:
        config = SoccerConfig()
        self.assertTrue(
            predicates.ball_in_own_defensive_area(
                config,
                BallState(x=-4.2, y=0.0, last_seen_at=1.0),
            )
        )
        self.assertFalse(
            predicates.ball_in_own_defensive_area(
                config,
                BallState(x=-3.8, y=0.0, last_seen_at=1.0),
            )
        )

    def test_keeper_clearance_uses_exit_hysteresis(self) -> None:
        role = GoalkeeperRole()
        kit = _Kit()

        self.assertTrue(role.wants_to_kick(kit, _context(-5.2)))
        # Outside the entry boundary (-4.8) but inside the active-clearance
        # exit boundary (-4.45), so the keeper finishes the intervention.
        self.assertTrue(role.wants_to_kick(kit, _context(-3.9)))
        self.assertFalse(role.wants_to_kick(kit, _context(-3.6)))

    def test_off_ball_roles_never_join_the_same_ball_duel(self) -> None:
        kit = _Kit()
        close = _context(0.0, player_x=-1.8)
        far = _context(0.0, player_x=-2.2)

        self.assertFalse(SupporterRole().wants_to_kick(kit, 2, close))
        self.assertFalse(DefenderRole().wants_to_kick(kit, 2, close))
        self.assertFalse(SupporterRole().wants_to_kick(kit, 2, far))
        self.assertFalse(DefenderRole().wants_to_kick(kit, 2, far))

    def test_field_players_do_not_press_keeper_claimed_ball(self) -> None:
        kit = _Kit()
        context = _context(-5.2, player_x=-5.0)

        self.assertFalse(SupporterRole().wants_to_kick(kit, 2, context))
        self.assertFalse(DefenderRole().wants_to_kick(kit, 2, context))

    def test_incoming_shot_is_tracked_at_predicted_intersection(self) -> None:
        kit = _Kit()
        planner = GoalkeeperStateMachine()
        ball = BallState(
            x=-4.0,
            y=0.10,
            last_seen_at=2.0,
            vx=-2.0,
            vy=0.20,
        )

        plan = planner.update(
            kit.config,
            kit.field,
            ball,
            Pose2D(-6.1, -0.8, 0.0),
            in_claim_area=True,
            in_clear_exit_area=True,
        )

        self.assertEqual(plan.phase, KeeperPhase.TRACK_SHOT)
        self.assertFalse(plan.wants_kick)
        self.assertIsNotNone(plan.move_target)
        assert plan.move_target is not None
        self.assertAlmostEqual(plan.move_target.x, -6.1)
        self.assertAlmostEqual(plan.move_target.y, 0.31, places=2)

    def test_close_incoming_ball_switches_to_emergency_clear(self) -> None:
        kit = _Kit()
        planner = GoalkeeperStateMachine()
        ball = BallState(
            x=-5.7,
            y=0.0,
            last_seen_at=2.0,
            vx=-2.0,
        )

        plan = planner.update(
            kit.config,
            kit.field,
            ball,
            Pose2D(-6.1, 0.0, 0.0),
            in_claim_area=True,
            in_clear_exit_area=True,
        )

        self.assertEqual(plan.phase, KeeperPhase.CLEAR)
        self.assertTrue(plan.wants_kick)

    def test_kickable_incoming_cross_is_cleared_before_reaching_goal_line(self) -> None:
        kit = _Kit()
        planner = GoalkeeperStateMachine()
        ball = BallState(
            x=-4.9,
            y=0.25,
            last_seen_at=2.0,
            vx=-2.0,
            vy=-0.1,
        )

        plan = planner.update(
            kit.config,
            kit.field,
            ball,
            Pose2D(-6.1, 0.25, 0.0),
            in_claim_area=True,
            in_clear_exit_area=True,
        )

        self.assertEqual(plan.phase, KeeperPhase.CLEAR)
        self.assertTrue(plan.wants_kick)

    def test_keeper_sweeps_only_when_ball_is_loose_and_teammates_are_late(self) -> None:
        config = SoccerConfig()
        loose = PlayContext(
            ball=BallState(x=-3.3, y=0.0, last_seen_at=2.0),
            teammates={
                1: RobotState(1, Pose2D(0.0, 0.0, 0.0), 2.0),
                2: RobotState(2, Pose2D(-0.5, 1.0, 0.0), 2.0),
                3: RobotState(3, Pose2D(-6.0, 0.0, 0.0), 2.0),
            },
            opponents={1: RobotState(1, Pose2D(-2.3, 0.0, 0.0), 2.0)},
        )
        controlled = PlayContext(
            ball=loose.known_ball,
            teammates=loose.teammates,
            opponents={1: RobotState(1, Pose2D(-3.0, 0.0, 0.0), 2.0)},
        )

        self.assertTrue(predicates.keeper_should_sweep_loose_ball(config, loose, 3))
        self.assertFalse(
            predicates.keeper_should_sweep_loose_ball(config, controlled, 3)
        )

    def test_keeper_does_not_sweep_when_field_teammate_arrives_first(self) -> None:
        config = SoccerConfig()
        context = PlayContext(
            ball=BallState(x=-3.3, y=0.0, last_seen_at=2.0),
            teammates={
                1: RobotState(1, Pose2D(-3.8, 0.0, 0.0), 2.0),
                3: RobotState(3, Pose2D(-6.0, 0.0, 0.0), 2.0),
            },
            opponents={1: RobotState(1, Pose2D(-2.2, 0.0, 0.0), 2.0)},
        )

        self.assertFalse(predicates.keeper_should_sweep_loose_ball(config, context, 3))

    def test_goal_line_clearance_approach_never_enters_net(self) -> None:
        kit = _Kit()
        role = GoalkeeperRole()
        context = PlayContext(
            ball=BallState(x=-6.92, y=0.0, last_seen_at=2.0),
            teammates={3: RobotState(3, Pose2D(-6.2, 0.0, 0.0), 2.0)},
        )

        target = role.target(kit, 3, context)

        self.assertGreater(target.x, kit.field.own_goal_x())
        self.assertGreaterEqual(target.x, -6.70)


if __name__ == "__main__":
    unittest.main()
