"""Regression tests for blocked-shot recycling and safe backpasses."""

from __future__ import annotations

import unittest

from src.soccer_framework import (
    BallState,
    GameControlState,
    GameState,
    PlayContext,
    Pose2D,
    RobotState,
    SetPlay,
    SoccerConfig,
)
from src.tactics.geometry import TeamFieldFrame
from src.tactics.navigation import ObstacleCollector
from src.tactics.targeting.attack import (
    goal_kick_delivery_target,
    kick_reason,
    select_clear_or_pass_target,
    select_kick_target,
)
from src.tactics.targeting.support import support_target


def _context(*, mark_receiver: bool, block_shot: bool) -> PlayContext:
    opponents = {}
    if block_shot:
        opponents[4] = RobotState(4, Pose2D(5.5, 0.0, 0.0), 1.0)
    if mark_receiver:
        opponents[5] = RobotState(5, Pose2D(2.5, 1.0, 0.0), 1.0)
    return PlayContext(
        game_state=GameControlState(state=GameState.PLAYING),
        ball=BallState(x=4.0, y=0.0, last_seen_at=1.0),
        teammates={
            1: RobotState(1, Pose2D(3.6, 0.0, 0.0), 1.0),
            2: RobotState(2, Pose2D(2.5, 1.0, 0.0), 1.0),
            3: RobotState(3, Pose2D(-5.5, 0.0, 0.0), 1.0),
        },
        opponents=opponents,
    )


class AttackTacticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SoccerConfig()
        self.field = TeamFieldFrame(self.config)
        self.obstacles = ObstacleCollector(self.config, self.field)

    def _target(self, context: PlayContext) -> Pose2D:
        return select_kick_target(
            self.config,
            self.field,
            self.obstacles,
            1,
            context,
            lambda _game, _player_id: True,
        )

    def test_blocked_shot_recycles_to_safe_teammate(self) -> None:
        target = self._target(_context(mark_receiver=False, block_shot=True))
        self.assertAlmostEqual(target.x, 2.5)
        self.assertAlmostEqual(target.y, 1.0)

    def test_clear_shot_is_not_replaced_by_backpass(self) -> None:
        target = self._target(_context(mark_receiver=False, block_shot=False))
        self.assertAlmostEqual(target.x, self.field.opponent_goal_x())
        self.assertAlmostEqual(target.y, 0.0)

    def test_marked_backpass_receiver_falls_back_to_dribble(self) -> None:
        target = self._target(_context(mark_receiver=True, block_shot=True))
        expected = self.field.clamp_inside_field(Pose2D(5.5, 0.0, 0.0))
        self.assertAlmostEqual(target.x, expected.x)
        self.assertAlmostEqual(target.y, expected.y)

    def test_backpass_has_an_explicit_runtime_reason(self) -> None:
        reason = kick_reason(
            self.config,
            Pose2D(2.5, 1.0, 0.0),
            default="center kick",
            ball=BallState(4.0, 0.0, 1.0),
        )
        self.assertEqual(reason, "center backpass")

    def test_midfield_open_lane_dribbles_instead_of_long_shot(self) -> None:
        context = PlayContext(
            game_state=GameControlState(state=GameState.PLAYING),
            ball=BallState(x=0.0, y=0.0, last_seen_at=1.0),
            teammates={1: RobotState(1, Pose2D(-0.4, 0.0, 0.0), 1.0)},
        )

        target = self._target(context)

        self.assertAlmostEqual(target.x, self.config.strategy.dribble_advance_m)
        self.assertAlmostEqual(target.y, 0.0)

    def test_face_to_face_short_pass_is_rejected(self) -> None:
        context = PlayContext(
            game_state=GameControlState(state=GameState.PLAYING),
            ball=BallState(x=0.0, y=0.0, last_seen_at=1.0),
            teammates={
                1: RobotState(1, Pose2D(-0.4, 0.0, 0.0), 1.0),
                2: RobotState(2, Pose2D(0.8, 0.0, 0.0), 1.0),
            },
        )

        target = self._target(context)

        self.assertNotAlmostEqual(target.x, 0.8)
        self.assertAlmostEqual(target.x, self.config.strategy.dribble_advance_m)

    def test_corner_is_cut_back_to_field_teammate_not_shot(self) -> None:
        context = PlayContext(
            game_state=GameControlState(
                state=GameState.PLAYING,
                set_play=SetPlay.CORNER_KICK,
                kicking_team=self.config.team_id,
            ),
            ball=BallState(x=6.7, y=4.25, last_seen_at=1.0),
            teammates={
                1: RobotState(1, Pose2D(6.3, 4.0, 0.0), 1.0),
                2: RobotState(2, Pose2D(4.85, 0.75, 0.0), 1.0),
                3: RobotState(3, Pose2D(-5.5, 0.0, 0.0), 1.0),
            },
        )

        center_target = self._target(context)
        side_target = select_clear_or_pass_target(
            self.config,
            self.field,
            self.obstacles,
            1,
            context,
            lambda _game, _player_id: True,
        )

        for target in (center_target, side_target):
            self.assertAlmostEqual(target.x, 4.85)
            self.assertAlmostEqual(target.y, 0.75)
            self.assertNotEqual(target.x, self.field.opponent_goal_x())

    def test_corner_without_receiver_uses_central_cutback_point(self) -> None:
        context = PlayContext(
            game_state=GameControlState(
                state=GameState.PLAYING,
                set_play=SetPlay.CORNER_KICK,
                kicking_team=self.config.team_id,
            ),
            ball=BallState(x=6.7, y=-4.25, last_seen_at=1.0),
            teammates={1: RobotState(1, Pose2D(6.3, -4.0, 0.0), 1.0)},
        )

        target = self._target(context)

        self.assertLess(target.x, self.field.opponent_goal_x() - 1.0)
        self.assertAlmostEqual(target.y, -0.75)

    def test_goal_kick_uses_a_stable_lane_between_wide_receivers(self) -> None:
        context = PlayContext(
            game_state=GameControlState(
                state=GameState.PLAYING,
                set_play=SetPlay.GOAL_KICK,
                kicking_team=self.config.team_id,
            ),
            ball=BallState(x=-5.8, y=0.0, last_seen_at=1.0),
            teammates={
                1: RobotState(1, Pose2D(-2.8, -2.65, 0.0), 1.0),
                2: RobotState(2, Pose2D(-2.8, 2.65, 0.0), 1.0),
                3: RobotState(3, Pose2D(-6.2, 0.0, 0.0), 1.0),
            },
        )

        selected = self._target(context)
        direct = goal_kick_delivery_target(
            self.config,
            self.field,
            context.known_ball,
        )

        self.assertEqual(selected, direct)
        self.assertLess(abs(selected.y), 2.65)

    def test_supporter_advances_and_occupies_a_separate_lane(self) -> None:
        context = PlayContext(
            game_state=GameControlState(state=GameState.PLAYING),
            ball=BallState(x=4.0, y=2.0, last_seen_at=1.0),
            teammates={
                1: RobotState(1, Pose2D(3.8, 2.0, 0.0), 1.0),
                2: RobotState(2, Pose2D(0.0, 0.0, 0.0), 1.0),
            },
        )

        target = support_target(
            self.config,
            self.field,
            2,
            context,
            lambda _game, _player_id: True,
        )

        self.assertGreater(target.x, 0.0)
        self.assertLess(target.y, context.known_ball.y)


if __name__ == "__main__":
    unittest.main()
