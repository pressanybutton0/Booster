"""Chase/kick decisions and pass scoring.

Callers first use :mod:`predicates` to rule out sideline/recovery cases, then use
this module to choose this tick's kick target:

center view: pass, then shoot, then dribble
side view: pass if possible, otherwise clear, without shooting
choose best passing teammate by score
generic lane-obstacle score
"""

from __future__ import annotations

import math
from collections.abc import Callable

from ...soccer_framework import (
    BallState,
    GameControlState,
    Pose2D,
    ReadySlot,
    SetPlay,
    SoccerConfig,
    PlayContext,
)
from ..geometry import clamp
from ..navigation import Obstacle, ObstacleCollector
from ..geometry import TeamFieldFrame
from . import recovery
from .predicates import ball_near_sideline


__all__ = [
    "PlayerAllowed",
    "best_backpass_target",
    "best_corner_receiver_target",
    "best_pass_target",
    "corner_delivery_target",
    "dribble_target",
    "goal_kick_delivery_target",
    "kick_reason",
    "lane_clear_score",
    "select_clear_or_pass_target",
    "select_kick_target",
    "shot_lane_is_clear",
    "should_make_restart_touch",
]


# Shared with upper layers: test whether a teammate can legally join tactics, excluding penalized/substitute players.
PlayerAllowed = Callable[[GameControlState, int], bool]


# Top-level selection


def select_kick_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D:
    """Decide this tick's aim target for a center chaser.

    Decision order: corner cutback, sideline recovery, restart touch, a close
    clear shot, useful pass, safe backpass, and finally dribble forward.
    """

    ball = context.known_ball
    game = context.known_game
    if _is_own_corner(config, game):
        return corner_delivery_target(
            config, field, obstacles,
            player_id, context, is_player_allowed,
        )

    if _is_own_goal_kick(config, game):
        return goal_kick_delivery_target(config, field, ball)

    if ball_near_sideline(config, ball):
        return recovery.sideline_recovery_target(config, field, ball)

    if should_make_restart_touch(config, game):
        teammate = best_pass_target(
            config, obstacles,
            player_id, context, is_player_allowed,
        )
        if teammate is not None:
            return Pose2D(teammate.x, teammate.y, 0.0)
        return dribble_target(config, field, ball)

    distance_to_goal = math.hypot(
        field.opponent_goal_x() - ball.x,
        ball.y,
    )
    if (
        distance_to_goal <= config.strategy.shot_max_distance_m
        and shot_lane_is_clear(config, field, obstacles, context)
    ):
        return Pose2D(field.opponent_goal_x(), 0.0, 0.0)

    teammate = best_pass_target(
        config, obstacles,
        player_id, context, is_player_allowed,
    )
    if teammate is not None:
        return Pose2D(teammate.x, teammate.y, 0.0)

    backpass = best_backpass_target(
        config, field, obstacles,
        player_id, context, is_player_allowed,
    )
    if backpass is not None:
        return Pose2D(backpass.x, backpass.y, 0.0)
    return dribble_target(config, field, ball)


def select_clear_or_pass_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D:
    """Side-lane view: pass if possible, otherwise clear toward the opponent goal without shooting or dribbling.

    Side chasers prefer clearing toward the middle rather than dribbling along the sideline into pressure.
    """

    ball = context.known_ball
    if _is_own_corner(config, context.known_game):
        return corner_delivery_target(
            config, field, obstacles,
            player_id, context, is_player_allowed,
        )

    if _is_own_goal_kick(config, context.known_game):
        return goal_kick_delivery_target(config, field, ball)

    if ball_near_sideline(config, ball):
        return recovery.sideline_recovery_target(config, field, ball)

    teammate = best_pass_target(
        config, obstacles,
        player_id, context, is_player_allowed,
    )
    if teammate is not None:
        return Pose2D(teammate.x, teammate.y, 0.0)
    return Pose2D(field.opponent_goal_x(), 0.0, 0.0)


# Restart touch


def should_make_restart_touch(
    config: SoccerConfig,
    game: GameControlState,
) -> bool:
    """Whether our team should actively touch the ball on restart: kickoff, throw-in, or indirect free kick."""

    return game.is_kickoff_for_team(config.team_id) or (
        game.is_restart_for_team(config.team_id)
        and game.set_play in {SetPlay.THROW_IN, SetPlay.INDIRECT_FREE_KICK}
    )


def _is_own_corner(config: SoccerConfig, game: GameControlState) -> bool:
    return (
        game.is_restart_for_team(config.team_id)
        and game.set_play == SetPlay.CORNER_KICK
    )


def _is_own_goal_kick(config: SoccerConfig, game: GameControlState) -> bool:
    return (
        game.is_restart_for_team(config.team_id)
        and game.set_play == SetPlay.GOAL_KICK
    )


def goal_kick_delivery_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    ball: BallState,
) -> Pose2D:
    """Return a stable one-touch goal-kick lane clear of both receivers.

    The aim depends only on the restart ball placement, not on per-tick obstacle
    scores, so the kick manager cannot keep steering left/right while active.
    Field players occupy wider lanes in :func:`support_target`.
    """

    if ball.y > 0.25:
        side = 1.0
    elif ball.y < -0.25:
        side = -1.0
    else:
        side = 1.0
    return field.clamp_inside_field(
        Pose2D(
            min(field.opponent_goal_x() - 0.75, ball.x + 4.2),
            side * 1.55,
            field.attack_theta(),
        ),
        margin=0.35,
    )


def corner_delivery_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D:
    """Return a straight, low corner delivery instead of aiming at goal.

    SoccerSim robots cannot bend a shot around the goal frame. Prefer the
    active field teammate in the central receiving lane; if that player is
    unavailable or marked, cut the ball back to an open point near the top of
    the penalty area. The fallback is deliberately outside the goal mouth, so
    this branch can never become a direct corner shot.
    """

    receiver = best_corner_receiver_target(
        config,
        field,
        obstacles,
        player_id,
        context,
        is_player_allowed,
    )
    if receiver is not None:
        return Pose2D(receiver.x, receiver.y, 0.0)

    ball = context.known_ball
    side = 1.0 if ball.y >= 0.0 else -1.0
    return field.clamp_inside_field(
        Pose2D(
            field.opponent_goal_x() - 2.15,
            side * 0.75,
            field.attack_theta(),
        ),
        margin=0.35,
    )


def best_corner_receiver_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D | None:
    """Pick a legal non-keeper teammate for a corner cutback."""

    ball = context.known_ball
    game = context.known_game
    opponents = obstacles.opponent_obstacles(context)
    ideal_x = field.opponent_goal_x() - 2.15
    candidates: list[tuple[float, Pose2D]] = []
    for teammate_id, robot in context.teammates.items():
        if teammate_id == player_id or robot.pose is None:
            continue
        if config.ready_slot_for_player(teammate_id) == ReadySlot.KEEPER:
            continue
        if not is_player_allowed(game, teammate_id):
            continue

        distance = math.hypot(robot.pose.x - ball.x, robot.pose.y - ball.y)
        if distance < config.strategy.pass_min_distance_m:
            continue
        lane_score = lane_clear_score(
            config,
            ball.x,
            ball.y,
            robot.pose.x,
            robot.pose.y,
            opponents,
        )
        if lane_score < config.strategy.pass_min_score:
            continue
        nearest_opponent = min(
            (
                math.hypot(robot.pose.x - obstacle.x, robot.pose.y - obstacle.y)
                - obstacle.radius
                for obstacle in opponents
            ),
            default=float("inf"),
        )
        if nearest_opponent < config.strategy.backpass_receiver_clearance_m:
            continue

        centrality = 1.0 - clamp(
            abs(robot.pose.y) / (config.field_width / 2.0),
            0.0,
            1.0,
        )
        top_of_box = 1.0 - clamp(
            abs(robot.pose.x - ideal_x) / 4.0,
            0.0,
            1.0,
        )
        score = 0.55 * lane_score + 0.25 * centrality + 0.20 * top_of_box
        candidates.append((score, robot.pose))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


# Pass scoring


def best_pass_target(
    config: SoccerConfig,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D | None:
    """Select this tick's best legal passing target; return ``None`` when no candidate qualifies.

    Score weights are lane clearance 0.55, forward gain 0.30, center pull 0.15,
    minus distance penalty. Candidates below ``pass_min_score`` are discarded.
    """

    if not config.strategy.pass_enabled:
        return None
    ball = context.known_ball
    game = context.known_game
    candidates: list[tuple[float, Pose2D]] = []
    for teammate_id, robot in context.teammates.items():
        if teammate_id == player_id or robot.pose is None:
            continue
        if not is_player_allowed(game, teammate_id):
            continue
        forward_gain = robot.pose.x - ball.x
        if forward_gain < config.strategy.pass_min_forward_m:
            continue
        lane_score = lane_clear_score(
            config,
            ball.x,
            ball.y,
            robot.pose.x,
            robot.pose.y,
            obstacles.opponent_obstacles(context),
        )
        distance = math.hypot(robot.pose.x - ball.x, robot.pose.y - ball.y)
        if distance < config.strategy.pass_min_distance_m:
            continue
        field_score = clamp(
            forward_gain / max(1.0, config.field_length),
            0.0,
            1.0,
        )
        center_score = 1.0 - clamp(
            abs(robot.pose.y) / (config.field_width / 2.0),
            0.0,
            1.0,
        )
        score = 0.55 * lane_score + 0.30 * field_score + 0.15 * center_score
        score -= clamp(distance / 12.0, 0.0, 0.25)
        if score >= config.strategy.pass_min_score:
            candidates.append((score, robot.pose))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def best_backpass_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    player_id: int,
    context: PlayContext,
    is_player_allowed: PlayerAllowed,
) -> Pose2D | None:
    """Select a safe relief receiver behind the ball after a blocked shot.

    This is intentionally narrower than :func:`best_pass_target`: it excludes
    the configured goalkeeper, keeps the receiver outside our penalty area,
    limits how far possession may retreat, and rejects both blocked lanes and
    receivers already marked by an opponent. The caller only invokes it after
    the normal forward pass and direct-shot options have failed.
    """

    if not config.strategy.pass_enabled or not config.strategy.backpass_enabled:
        return None

    ball = context.known_ball
    if _is_own_corner(config, context.known_game):
        return corner_delivery_target(
            config, field, obstacles,
            player_id, context, is_player_allowed,
        )

    game = context.known_game
    opponent_obstacles = obstacles.opponent_obstacles(context)
    own_penalty_front = field.own_goal_x() + config.penalty_area_length
    candidates: list[tuple[float, Pose2D]] = []

    for teammate_id, robot in context.teammates.items():
        if teammate_id == player_id or robot.pose is None:
            continue
        if config.ready_slot_for_player(teammate_id) == ReadySlot.KEEPER:
            continue
        if not is_player_allowed(game, teammate_id):
            continue

        retreat = ball.x - robot.pose.x
        if not (
            config.strategy.backpass_min_retreat_m
            <= retreat
            <= config.strategy.backpass_max_retreat_m
        ):
            continue
        if robot.pose.x <= own_penalty_front + 0.35:
            continue

        lane_score = lane_clear_score(
            config,
            ball.x,
            ball.y,
            robot.pose.x,
            robot.pose.y,
            opponent_obstacles,
        )
        if lane_score < config.strategy.pass_min_score:
            continue

        nearest_opponent = min(
            (
                math.hypot(robot.pose.x - obstacle.x, robot.pose.y - obstacle.y)
                - obstacle.radius
                for obstacle in opponent_obstacles
            ),
            default=float("inf"),
        )
        if nearest_opponent < config.strategy.backpass_receiver_clearance_m:
            continue

        receiver_space = clamp(
            nearest_opponent / max(1.0, config.strategy.backpass_receiver_clearance_m * 2.0),
            0.0,
            1.0,
        )
        center_score = 1.0 - clamp(
            abs(robot.pose.y) / (config.field_width / 2.0),
            0.0,
            1.0,
        )
        retreat_cost = clamp(
            retreat / max(1.0, config.strategy.backpass_max_retreat_m),
            0.0,
            1.0,
        )
        score = 0.60 * lane_score + 0.25 * receiver_space + 0.15 * center_score
        score -= 0.15 * retreat_cost
        candidates.append((score, robot.pose))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def shot_lane_is_clear(
    config: SoccerConfig,
    field: TeamFieldFrame,
    obstacles: ObstacleCollector,
    context: PlayContext,
) -> bool:
    """Treat a shot lane as shootable at the active strategy profile threshold."""

    ball = context.known_ball
    return lane_clear_score(
        config,
        ball.x,
        ball.y,
        field.opponent_goal_x(),
        0.0,
        obstacles.opponent_obstacles(context),
    ) >= config.strategy.shot_lane_min_score


def lane_clear_score(
    config: SoccerConfig,
    start_x: float,
    start_y: float,
    target_x: float,
    target_y: float,
    obstacles: tuple[Obstacle, ...],
) -> float:
    """Generic lane-obstacle score in [0, 1], where 1 is clear and 0 is blocked.

    Project each obstacle onto the lane; if it lies within the segment and inside
    lateral clearance, reduce score proportionally. Multiple obstacles take the worst score.
    """

    if not obstacles:
        return 1.0
    seg_dx = target_x - start_x
    seg_dy = target_y - start_y
    seg_len = math.hypot(seg_dx, seg_dy)
    if seg_len < 1e-6:
        return 0.0
    dir_x = seg_dx / seg_len
    dir_y = seg_dy / seg_len
    left_x = -dir_y
    left_y = dir_x
    score = 1.0
    for obstacle in obstacles:
        rel_x = obstacle.x - start_x
        rel_y = obstacle.y - start_y
        along = rel_x * dir_x + rel_y * dir_y
        if along <= 0.0 or along >= seg_len:
            continue
        lateral = abs(rel_x * left_x + rel_y * left_y)
        clearance = max(config.strategy.pass_lane_clearance, obstacle.radius)
        if lateral < clearance:
            score = min(score, clamp(lateral / clearance, 0.0, 1.0))
    return score


# Dribble and reason text


def dribble_target(
    config: SoccerConfig,
    field: TeamFieldFrame,
    ball: BallState,
) -> Pose2D:
    """Simple dribble target: advance by ``dribble_advance_m`` along ``+x`` and pull ``y`` toward center."""

    target_x = ball.x + config.strategy.dribble_advance_m
    target_y = ball.y * config.strategy.dribble_center_pull
    return field.clamp_inside_field(
        Pose2D(target_x, target_y, field.attack_theta())
    )


def kick_reason(
    config: SoccerConfig,
    target: Pose2D,
    default: str = "chaser kick",
    ball: BallState | None = None,
) -> str:
    """Choose the reason suffix from whether target points at the opponent goal center.

    When target is near the goal mouth, keep ``default``; otherwise append
    ``" to target"`` so logs distinguish shooting from pass/clear targets.
    """

    if (
        ball is not None
        and target.x <= ball.x - config.strategy.backpass_min_retreat_m
    ):
        return f"{default.removesuffix(' kick')} backpass"
    if abs(target.x) >= config.field_length / 2.0 - 0.2 and abs(target.y) < 0.2:
        return default
    return f"{default} to target"
