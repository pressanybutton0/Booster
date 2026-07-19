"""Stateless field predicates and player scoring functions.

This module only performs pure calculations from ``BallState`` / ``PlayContext``
plus geometry thresholds to bool/float results. It does not create :class:`Pose2D`
or dispatch commands; other targeting modules call these predicates before higher-level decisions.
"""

from __future__ import annotations

import math

from ...soccer_framework import (
    BallState,
    Pose2D,
    ReadySlot,
    SoccerConfig,
    PlayContext,
)


__all__ = [
    "ball_beyond_goal_line",
    "ball_beyond_own_goal_line",
    "ball_claim_score",
    "ball_in_own_defensive_area",
    "ball_is_in_midfield_or_own_half",
    "ball_near_sideline",
    "keeper_should_sweep_loose_ball",
    "pose_for_slot",
    "side_should_challenge",
    "sideline_sign",
]


# Field-geometry predicates


def ball_in_own_defensive_area(
    config: SoccerConfig,
    ball: BallState,
    extra_margin_m: float = 0.0,
) -> bool:
    """Whether the ball is close enough to our goal for a keeper clearance.

    The old boundary started at ``-0.18 * field_length`` (``x=-2.52`` on the
    adult field), which let the goalkeeper abandon its guard point for balls
    almost at midfield. Anchor the zone to the actual goal-area geometry and
    keep it centred on the goal mouth instead. ``extra_margin_m`` is used only
    as an exit band after a clearance has already begun.
    """

    # Start the intervention near the front of the penalty area, not only once
    # the ball reaches the one-metre goal area.  The previous boundary was so
    # deep that a direct shot could cross it less than two seconds before the
    # goal, leaving the keeper time only to turn in place.
    own_goal_x = -config.field_length / 2.0
    area_x = (
        own_goal_x
        + config.penalty_area_length
        - 0.80
        + config.strategy.goalkeeper_challenge_margin_m
        + max(0.0, extra_margin_m)
    )
    area_y = min(
        config.goal_area_width / 2.0 + 0.35,
        config.goal_width / 2.0
        + 0.35
        + config.strategy.goalkeeper_challenge_margin_m
        + max(0.0, extra_margin_m),
    )
    return ball.x < area_x and abs(ball.y) <= area_y


def ball_beyond_goal_line(config: SoccerConfig, ball: BallState) -> bool:
    """Whether the ball crossed either goal line, ours or the opponent's."""

    half_length = config.field_length / 2.0
    margin = config.strategy.goal_line_recovery_margin_m
    return abs(ball.x) > half_length + margin


def ball_beyond_own_goal_line(config: SoccerConfig, ball: BallState) -> bool:
    """Whether the ball crossed our goal line in the ``-x`` direction."""

    half_length = config.field_length / 2.0
    margin = config.strategy.goal_line_recovery_margin_m
    return ball.x < -half_length - margin


def ball_near_sideline(config: SoccerConfig, ball: BallState) -> bool:
    """Whether the ball is close enough to the sideline to trigger sideline recovery."""

    sideline_y = config.field_width / 2.0
    return (
        abs(ball.y)
        >= sideline_y - config.strategy.sideline_recovery_margin_m
    )


def keeper_should_sweep_loose_ball(
    config: SoccerConfig,
    context: PlayContext,
    keeper_id: int,
    *,
    continuing: bool = False,
) -> bool:
    """Let the keeper claim a genuinely loose one-on-one ball outside its box.

    A fixed penalty-area predicate made the keeper wait on its line even when an
    attacker had knocked the ball well clear of its feet.  Sweeping is allowed
    only in the central defensive channel, only when an observed opponent is
    separated from the ball, and only when the keeper is clearly the fastest
    teammate.  The continuing band prevents a rush from being cancelled by one
    noisy frame after it has begun.
    """

    keeper = context.teammates.get(keeper_id)
    if keeper is None or keeper.pose is None:
        return False
    opponent_poses = tuple(
        robot.pose
        for robot in context.opponents.values()
        if robot.pose is not None
    )
    if not opponent_poses:
        # Missing opponent truth must not be interpreted as an uncontested ball.
        return False

    ball = context.known_ball
    strategy = config.strategy
    exit_margin = strategy.goalkeeper_sweep_exit_margin_m if continuing else 0.0
    own_goal_x = -config.field_length / 2.0
    front_x = (
        own_goal_x
        + config.penalty_area_length
        + strategy.goalkeeper_sweep_front_extension_m
        + exit_margin
    )
    if ball.x > front_x:
        return False
    if abs(ball.y) > config.penalty_area_width / 2.0 + 0.35:
        return False

    keeper_distance = math.hypot(
        keeper.pose.x - ball.x,
        keeper.pose.y - ball.y,
    )
    if keeper_distance > strategy.goalkeeper_sweep_max_distance_m + exit_margin:
        return False

    nearest_opponent = min(
        math.hypot(pose.x - ball.x, pose.y - ball.y)
        for pose in opponent_poses
    )
    free_ball_threshold = max(
        0.55,
        strategy.goalkeeper_sweep_ball_free_m - 0.45 * exit_margin,
    )
    if nearest_opponent < free_ball_threshold:
        return False

    nearest_field_teammate = min(
        (
            math.hypot(robot.pose.x - ball.x, robot.pose.y - ball.y)
            for teammate_id, robot in context.teammates.items()
            if teammate_id != keeper_id and robot.pose is not None
        ),
        default=math.inf,
    )
    required_advantage = max(
        0.0,
        strategy.goalkeeper_sweep_teammate_advantage_m - exit_margin,
    )
    return keeper_distance + required_advantage <= nearest_field_teammate


def ball_is_in_midfield_or_own_half(config: SoccerConfig, ball: BallState) -> bool:
    """Whether the ball is around midfield or our half; SIDE uses this to decide whether to challenge."""

    return ball.x < config.field_length * 0.12


def sideline_sign(y: float) -> float:
    """Return +1 for the upper side of the field and -1 for the lower side."""

    return 1.0 if y >= 0.0 else -1.0


# Player scoring


def ball_claim_score(
    config: SoccerConfig,
    slot: ReadySlot,
    pose: Pose2D,
    ball: BallState,
) -> float:
    """Estimate the cost for a player to claim the current ball; lower score is better for chaser.

    KEEPER gets a high field-length cost unless the ball is dangerous, pushing it
    to the end. CENTER is slightly cheaper than SIDE to prefer central challenges.
    """

    distance = math.hypot(ball.x - pose.x, ball.y - pose.y)
    if slot == ReadySlot.KEEPER:
        if ball_in_own_defensive_area(config, ball):
            return distance - 0.75
        return distance + config.field_length
    if slot == ReadySlot.CENTER:
        return distance - 0.20
    return distance - 0.10


def pose_for_slot(
    config: SoccerConfig,
    context: PlayContext,
    slot: ReadySlot,
) -> Pose2D | None:
    """Find the teammate pose currently assigned to a ReadySlot; return ``None`` if missing."""

    for player_id, robot in context.teammates.items():
        if config.ready_slot_for_player(player_id) == slot:
            return robot.pose
    return None


def side_should_challenge(
    config: SoccerConfig,
    context: PlayContext,
) -> bool:
    """Allow SIDE into arbitration; the playbook still selects exactly one chaser.

    The former final-third gate made robot2 wait until it was 0.35 m closer than
    robot1.  With atomic single-ball ownership now in place that extra gate is
    unnecessary and was the source of visible "polite deferral".
    """

    del config, context
    return True
