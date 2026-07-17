"""Regression tests for conservative goalkeeper clearance boundaries."""

from __future__ import annotations

import unittest

from src.play.default_roles import GoalkeeperRole
from src.soccer_framework import BallState, PlayContext, SoccerConfig
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


class _Kit:
    def __init__(self) -> None:
        self.config = SoccerConfig()
        self.targeting = _Targeting(self.config)


def _context(x: float, y: float = 0.0) -> PlayContext:
    return PlayContext(ball=BallState(x=x, y=y, last_seen_at=1.0))


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

    def test_keeper_clearance_uses_exit_hysteresis(self) -> None:
        role = GoalkeeperRole()
        kit = _Kit()

        self.assertTrue(role.wants_to_kick(kit, _context(-5.2)))
        # Outside the entry boundary (-4.8) but inside the active-clearance
        # exit boundary (-4.45), so the keeper finishes the intervention.
        self.assertTrue(role.wants_to_kick(kit, _context(-4.6)))
        self.assertFalse(role.wants_to_kick(kit, _context(-4.3)))


if __name__ == "__main__":
    unittest.main()
