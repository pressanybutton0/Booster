"""Motion controller that combines avoidance, walking control, and kick commands.

The controller translates "where to go" into a :class:`RobotCommand` while
keeping path detours, yaw-avoidance bias, and unicycle walking control together.
It is a reactive controller with no global path planning: each tick computes
detour points from current obstacles and outputs velocity directly.

The biped base is most stable with ``vx + vyaw`` commands. Lateral ``vy`` is
left at zero in combined movement commands, so avoidance is split into path
detours that change the target and yaw avoidance that changes angular velocity.

PLAY, READY, and recovery share the same nearby-robot avoidance.  Opponents must
remain enabled during PLAY as well; disabling them creates a collision blind spot
when the path layer ignores an obstacle already close to the robot.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..soccer_framework import (
    BallState,
    GameState,
    KickIntent,
    MoveIntent,
    Pose2D,
    RobotCommand,
    SetPlay,
    SoccerConfig,
    PlayContext,
)
from .geometry import clamp, field_to_relative, normalize_angle
from .kick_hysteresis import KickHysteresis
from .navigation import Obstacle, ObstacleCollector
from .geometry import TeamFieldFrame


# Walking-control parameters shared by PLAY and READY
# Use a compromise of the old PLAY/READY presets: arrive_distance in the middle,
# turn_threshold and floor from the more stable READY values. During chasing,
# small angle error will not trigger floor; near the ball,
# distance < arrive_distance goes through align unaffected by floor.
#
# 2026-06-26 adjustment: lower floor, relax arrival angle, and add dead zone to fix
# extra spinning in Ready; see note/motion_spinning_issue_analysis.md.
_ARRIVE_DISTANCE = 0.15
_ARRIVE_ANGLE = 0.30  # Relaxed to ~17 deg to avoid repeated micro-adjustments near target.
_TURN_THRESHOLD = 0.5
_ANGULAR_SPEED_FLOOR = 0.25  # Lower floor to reduce overshoot for small angle errors.
_ANGULAR_DEAD_ZONE = 0.15  # No floor below this angle error; pure proportional control.
_LINEAR_SPEED_FLOOR = 0.3
_LINEAR_GAIN = 0.9
_MAX_ALIGN_TIME = 2.0  # Maximum time spent aligning before forcing arrival

# The goal is a U-shaped trap.  Once the robot overlaps it, ordinary tangent
# avoidance cannot recover because close obstacles are deliberately ignored to
# suppress robot-vs-robot jitter.  These values put the temporary target safely
# inside the pitch (or outside the side net) before normal planning resumes.
_GOAL_DEPTH = 0.60
_GOAL_ESCAPE_INFIELD_M = 0.65
_GOAL_ESCAPE_LATERAL_M = 0.55
_GOAL_ESCAPE_SPEED_MPS = 0.55
# Goal obstacle radii already include the robot footprint.  Escape is an
# emergency contact recovery, so only add a small perception/contact allowance
# here.  Reusing the normal path-planning margin (0.22 m) made ordinary keeper
# positions roughly half a metre from the frame look like collisions.
_GOAL_ESCAPE_TRIGGER_MARGIN_M = 0.08
_GOAL_ESCAPE_PROGRESS_M = 0.12
_GOAL_ESCAPE_STALL_SEC = 2.0
_GOAL_ESCAPE_ROUTE_MAX_SEC = 4.0
# A rear-corner waypoint is much farther away than a post-contact waypoint.
# Flipping to the opposite rear corner after only four seconds makes a robot
# behind the back net traverse left/right forever instead of clearing one side.
# Actual no-progress still uses the two-second stall detector; this longer cap
# only allows a robot that is genuinely translating to finish the chosen side.
_GOAL_ESCAPE_BEHIND_NET_ROUTE_MAX_SEC = 12.0


@dataclass(frozen=True)
class _GoalEscapeProgress:
    """Progress anchor used to rotate recovery routes after a real stall."""

    phase: str
    anchor: Pose2D
    since: float
    route_index: int = 0
    route_since: float = 0.0


class MotionController:
    """Reactive motion controller that integrates avoidance, walking control, and kicking.

    A full :meth:`move_to_target` call follows this flow:

    1. **Path detour**: find the first blocking obstacle on the original path and
    create a side via point as the new target; side choice is remembered across ticks.
    2. **Walking control**: compute unicycle velocity, ``vx + vyaw``, toward the possibly adjusted target.
    3. **Yaw avoidance**: add vyaw bias when nearby teammates or opponents require extra turning.

    The three layers change different quantities, target / velocity / angular
    velocity, and never touch ``vy``, satisfying biped constraints.
    """

    def __init__(
        self,
        config: SoccerConfig,
        field: TeamFieldFrame,
        kicker: KickHysteresis,
        obstacles: ObstacleCollector,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._field = field
        self._kicker = kicker
        self._obstacles = obstacles
        self._clock = clock
        self._avoid_side_by_player: dict[int, float] = {}
        self._goal_escape_plan_by_player: dict[int, tuple[str, Pose2D]] = {}
        self._goal_escape_progress_by_player: dict[int, _GoalEscapeProgress] = {}

    # Public interface

    def move_to_target(
        self,
        player_id: int,
        context: PlayContext,
        target: Pose2D,
        reason: str,
        arrive_distance: float | None = None,
        hold_vyaw: float = 0.0,
    ) -> RobotCommand:
        """Generate a movement command with avoidance applied.

        ``arrive_distance`` overrides the arrival threshold; ``hold_vyaw`` keeps a
        nonzero turn rate after arrival. Nearby teammates and opponents are always
        included in yaw avoidance in every moving phase.
        """
        robot = context.teammates.get(player_id)
        if robot is None or robot.pose is None:
            return RobotCommand.stop(f"{reason}: waiting for pose")

        # Never pursue a point inside the U-shaped goal.  If the robot overlaps
        # the frame or is outside the goal line, run topology-safe recovery before
        # normal tangent avoidance.  In particular, a robot behind the back net
        # must go around a rear corner rather than drive straight toward the ball.
        safe_target = self._project_out_of_goal(target)
        escape_target = self._goal_escape_target(player_id, robot.pose, context)
        if escape_target is not None:
            self._avoid_side_by_player.pop(player_id, None)
            adjusted_target = escape_target
            progress = self._goal_escape_progress_by_player.get(player_id)
            route_detail = (
                f" {progress.phase} r{progress.route_index}"
                if progress is not None
                else ""
            )
            adjusted_reason = f"{reason} escape goal frame{route_detail}"
        else:
            adjusted_target = self._avoidance_target(
                player_id, robot.pose, safe_target, context
            )
            if adjusted_target != safe_target:
                adjusted_reason = f"{reason} via obstacle"
            elif safe_target != target:
                adjusted_reason = f"{reason} goal-safe target"
            else:
                adjusted_reason = reason

        # Walking control: compute vx + vyaw
        arrive_dist = _ARRIVE_DISTANCE if arrive_distance is None else arrive_distance
        if escape_target is not None:
            # Translating immediately is essential when another robot pins us
            # against the frame: an in-place turn cannot break contact.  Reverse
            # is allowed so a robot facing the net can still move infield now.
            command = self._compute_goal_escape_velocity(
                robot.pose, adjusted_target, adjusted_reason
            )
        else:
            command = self._compute_velocity(
                robot.pose,
                adjusted_target,
                adjusted_reason,
                arrive_dist,
                hold_vyaw,
            )

        # Goal recovery already scores routes against teammates and opponents.
        # Applying the generic yaw bias again can steer the robot back into the
        # post/net, particularly in the two-robot pinch this path handles.
        if escape_target is not None:
            return command

        # Yaw avoidance: add vyaw bias
        return self._apply_yaw_avoidance(
            player_id,
            context,
            command,
        )

    def kick_command(
        self,
        player_id: int,
        context: PlayContext,
        kick_theta: float,
        reason: str,
        now: float = 0.0,
    ) -> RobotCommand:
        """Generate a kick command and mark the player as kicking to trigger kick hysteresis."""
        ball = context.known_ball
        robot = context.teammates.get(player_id)
        if robot is None or robot.pose is None:
            return RobotCommand.stop(f"{reason}: waiting for pose")
        self._kicker.mark_kicking(player_id, now=now)
        rel_ball = field_to_relative(ball.x, ball.y, robot.pose)
        return RobotCommand(
            intent=KickIntent(
                direction=normalize_angle(kick_theta - robot.pose.theta),
                power=self._config.strategy.soccer_kick_power,
                ball_x=rel_ball.x,
                ball_y=rel_ball.y,
            ),
            reason=reason,
        )

    def approach_target(
        self,
        ball: BallState,
        kick_theta: float,
        approach_offset: float = 0.4,
    ) -> Pose2D:
        """Approach target behind the ball: step back by ``approach_offset`` opposite the kick direction, then clamp inside the field."""
        return self._field.clamp_inside_field(
            Pose2D(
                x=ball.x - approach_offset,
                y=ball.y,
                theta=kick_theta,
            )
        )

    # Path detour

    def _project_out_of_goal(self, target: Pose2D) -> Pose2D:
        """Project targets in either goal enclosure back onto the playable field.

        Targets outside the goal mouth are left alone: they may be intermediate
        recovery points around the outside of a side net.  Only the U-shaped
        enclosure and its immediate frame clearance are rejected.
        """
        half_length = self._config.field_length / 2.0
        half_goal_width = self._config.goal_width / 2.0
        abs_x = abs(target.x)
        if not (
            half_length <= abs_x
            and abs(target.y)
            <= half_goal_width + 0.30 + self._config.strategy.obstacle_safety_margin
        ):
            return target

        sign_x = 1.0 if target.x >= 0.0 else -1.0
        inner_y = max(0.0, half_goal_width - _GOAL_ESCAPE_LATERAL_M)
        return Pose2D(
            x=sign_x * (half_length - _GOAL_ESCAPE_INFIELD_M),
            y=clamp(target.y, -inner_y, inner_y),
            theta=target.theta,
        )

    def _goal_escape_target(
        self,
        player_id: int,
        start: Pose2D,
        context: PlayContext,
    ) -> Pose2D | None:
        """Return a topology-safe recovery point around the goal frame.

        Several infield/centre/outside candidates are scored against nearby
        teammates and opponents.  This matters when two robots and a post form a
        three-body pinch.  A robot behind the back net first moves around a rear
        corner, then returns infield outside the side net; it must never target a
        ball through the net wall.
        """
        goal_obstacles = self._obstacles.goal_structure_obstacles()
        frame_overlap = any(
            math.hypot(start.x - obstacle.x, start.y - obstacle.y)
            < obstacle.radius + _GOAL_ESCAPE_TRIGGER_MARGIN_M
            for obstacle in goal_obstacles
        )

        # The full planning margin is still appropriate for recovery waypoints:
        # after contact is confirmed, leave enough room to clear the frame.
        margin = self._config.strategy.obstacle_safety_margin

        half_length = self._config.field_length / 2.0
        half_width = self._config.field_width / 2.0
        half_goal_width = self._config.goal_width / 2.0
        abs_x = abs(start.x)
        if not frame_overlap and abs_x <= half_length:
            self._goal_escape_plan_by_player.pop(player_id, None)
            self._goal_escape_progress_by_player.pop(player_id, None)
            return None

        sign_x = 1.0 if start.x >= 0.0 else -1.0
        sign_y = 1.0 if start.y >= 0.0 else -1.0
        escape_x = sign_x * (half_length - _GOAL_ESCAPE_INFIELD_M)
        near_line_x = sign_x * (half_length - 0.20)
        inner_y = max(0.0, half_goal_width - _GOAL_ESCAPE_LATERAL_M)
        frame_clearance = 0.30 + margin
        outer_y = min(
            half_width - 0.25,
            half_goal_width
            + max(_GOAL_ESCAPE_LATERAL_M, frame_clearance + 0.15),
        )
        back_x = half_length + _GOAL_DEPTH
        rear_route_x = sign_x * (back_x + frame_clearance + 0.20)

        if abs_x >= back_x and abs(start.y) < outer_y:
            # Behind the back net: stay behind it while moving around either
            # rear corner.  Any infield target from here crosses solid netting.
            escape_phase = "behind_net"
            candidates = (
                (rear_route_x, sign_y * outer_y),
                (rear_route_x, -sign_y * outer_y),
            )
        elif abs_x > half_length and abs(start.y) >= half_goal_width:
            # We have cleared a side of the U.  Now return infield while keeping
            # enough lateral clearance from the side net and front post.
            escape_phase = "around_side"
            route_sign_y = 1.0 if start.y >= 0.0 else -1.0
            candidates = (
                (escape_x, route_sign_y * outer_y),
                (near_line_x, route_sign_y * outer_y),
            )
        elif abs(start.y) <= half_goal_width:
            escape_phase = "mouth"
            candidates = (
                (escape_x, clamp(start.y, -inner_y, inner_y)),
                (escape_x, start.y),
                (escape_x, -sign_y * inner_y),
                (near_line_x, sign_y * outer_y),
                (near_line_x, -sign_y * outer_y),
            )
        else:
            escape_phase = "side_contact"
            candidates = (
                (escape_x, sign_y * outer_y),
                (escape_x, clamp(start.y, -half_width + 0.25, half_width - 0.25)),
                (near_line_x, sign_y * outer_y),
                (escape_x, sign_y * inner_y),
            )

        dynamic_obstacles = (
            self._obstacles.opponent_obstacles(context)
            + self._obstacles.teammate_obstacles(player_id, context)
        )
        game = context.game_state
        ball = context.ball
        if (
            game is not None
            and ball is not None
            and game.state == GameState.PLAYING
            and not game.stopped
            and game.set_play != SetPlay.NONE
            and game.has_kicking_team()
            and game.kicking_team == self._config.opponent_team_id()
        ):
            # Goal-frame recovery temporarily overrides the normal opponent-
            # restart target. Without carrying the ball exclusion into this
            # emergency planner, a robot beside/behind the net can choose the
            # physically clearest route while remaining illegally close to the
            # restart ball. Model the rule radius as another escape obstacle;
            # subtract the common planner margin so zero sampled clearance is
            # exactly the configured legal distance.
            dynamic_obstacles += (
                Obstacle(
                    ball.x,
                    ball.y,
                    max(
                        0.0,
                        self._config.strategy.opponent_restart_avoid_distance_m
                        - self._config.strategy.obstacle_safety_margin,
                    ),
                ),
            )
        scored_candidates = tuple(
            (
                self._goal_escape_candidate_score(
                    start,
                    candidate[0],
                    candidate[1],
                    dynamic_obstacles,
                    goal_obstacles,
                ),
                candidate,
            )
            for candidate in candidates
        )
        ranked_candidates = sorted(
            scored_candidates,
            key=lambda item: item[0],
            reverse=True,
        )
        now = self._clock()
        progress = self._goal_escape_progress_by_player.get(player_id)
        stalled = False
        if progress is None or progress.phase != escape_phase:
            progress = _GoalEscapeProgress(
                escape_phase,
                start,
                now,
                route_since=now,
            )
        elif now - progress.route_since >= (
            _GOAL_ESCAPE_BEHIND_NET_ROUTE_MAX_SEC
            if escape_phase == "behind_net"
            else _GOAL_ESCAPE_ROUTE_MAX_SEC
        ):
            # Small back-and-forth motion can exceed the displacement threshold
            # without getting out of the frame. Cap total time on one route so
            # that apparent progress cannot keep r0 forever.
            stalled = True
            progress = _GoalEscapeProgress(
                escape_phase,
                start,
                now,
                route_index=(progress.route_index + 1) % len(ranked_candidates),
                route_since=now,
            )
        elif (
            math.hypot(start.x - progress.anchor.x, start.y - progress.anchor.y)
            >= _GOAL_ESCAPE_PROGRESS_M
        ):
            progress = _GoalEscapeProgress(
                escape_phase,
                start,
                now,
                route_index=progress.route_index,
                route_since=progress.route_since,
            )
        elif now - progress.since >= _GOAL_ESCAPE_STALL_SEC:
            stalled = True
            progress = _GoalEscapeProgress(
                escape_phase,
                start,
                now,
                route_index=(progress.route_index + 1) % len(ranked_candidates),
                route_since=now,
            )
        self._goal_escape_progress_by_player[player_id] = progress

        best_score, (escape_x, escape_y) = ranked_candidates[
            progress.route_index % len(ranked_candidates)
        ]

        # Keep the previous route through small perception changes.  Without
        # this hysteresis, two nearly equal openings can make the target flip
        # every tick and turn a valid escape into another stationary oscillation.
        previous_plan = self._goal_escape_plan_by_player.get(player_id)
        if (
            not stalled
            and progress.route_index == 0
            and previous_plan is not None
            and previous_plan[0] == escape_phase
        ):
            previous = previous_plan[1]
            previous_score = self._goal_escape_candidate_score(
                start,
                previous.x,
                previous.y,
                dynamic_obstacles,
                goal_obstacles,
            )
            if previous_score >= best_score - 0.75:
                escape_x, escape_y = previous.x, previous.y

        escape = Pose2D(
            x=escape_x,
            y=escape_y,
            theta=math.atan2(escape_y - start.y, escape_x - start.x),
        )
        self._goal_escape_plan_by_player[player_id] = (escape_phase, escape)
        return escape

    def _goal_escape_candidate_score(
        self,
        start: Pose2D,
        target_x: float,
        target_y: float,
        dynamic_obstacles: tuple[Obstacle, ...],
        goal_obstacles: tuple[Obstacle, ...],
    ) -> float:
        """Prefer segments that open space from both robots and the touched frame."""
        dx = target_x - start.x
        dy = target_y - start.y
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return -math.inf
        dir_x = dx / length
        dir_y = dy / length
        margin = self._config.strategy.obstacle_safety_margin
        score = 0.0

        for obstacle in dynamic_obstacles:
            rel_away_x = start.x - obstacle.x
            rel_away_y = start.y - obstacle.y
            start_distance = math.hypot(rel_away_x, rel_away_y)
            influence = obstacle.radius + margin + 0.80
            if start_distance >= influence:
                continue

            end_distance = math.hypot(target_x - obstacle.x, target_y - obstacle.y)
            separation_gain = end_distance - start_distance
            away_alignment = (
                rel_away_x * dir_x + rel_away_y * dir_y
            ) / max(start_distance, 0.05)

            # Ignore the unavoidable overlap at t=0 and inspect whether the
            # first steps actually leave the neighbor instead of crossing it.
            sampled_clearance = min(
                math.hypot(
                    start.x + dx * fraction - obstacle.x,
                    start.y + dy * fraction - obstacle.y,
                )
                - obstacle.radius
                - margin
                for fraction in (0.25, 0.50, 0.75, 1.0)
            )
            score += 5.0 * separation_gain + 2.0 * away_alignment
            score += 2.0 * clamp(sampled_clearance, -1.0, 1.0)

        # Dynamic-obstacle scoring alone can select a route that moves away from
        # a pinning opponent but slides along the post/net.  Explicitly reward
        # leaving the nearest touched frame element, and inspect the first part
        # of the segment so a target on the far side of the net cannot look safe.
        if goal_obstacles:
            nearest_frame = min(
                goal_obstacles,
                key=lambda obstacle: math.hypot(
                    start.x - obstacle.x,
                    start.y - obstacle.y,
                )
                - obstacle.radius,
            )
            frame_start_distance = math.hypot(
                start.x - nearest_frame.x,
                start.y - nearest_frame.y,
            )
            frame_end_distance = math.hypot(
                target_x - nearest_frame.x,
                target_y - nearest_frame.y,
            )
            away_x = start.x - nearest_frame.x
            away_y = start.y - nearest_frame.y
            away_norm = max(frame_start_distance, 0.05)
            away_alignment = (away_x * dir_x + away_y * dir_y) / away_norm
            score += 8.0 * clamp(
                frame_end_distance - frame_start_distance,
                -0.50,
                0.80,
            )
            score += 3.0 * away_alignment

            followup_clearance = min(
                math.hypot(
                    start.x + dx * fraction - obstacle.x,
                    start.y + dy * fraction - obstacle.y,
                )
                - obstacle.radius
                - _GOAL_ESCAPE_TRIGGER_MARGIN_M
                for fraction in (0.35, 0.65, 1.0)
                for obstacle in goal_obstacles
            )
            score += 12.0 * clamp(followup_clearance, -0.50, 0.50)
            # Once multiple candidates clear the touched frame, prefer the
            # shortest one.  Without this term a cross-mouth route can win only
            # because its endpoint is very far from the original post.
            score -= 1.5 * length

        return score

    def _avoidance_target(
        self,
        player_id: int,
        start: Pose2D,
        target: Pose2D,
        context: PlayContext,
    ) -> Pose2D:
        """Insert a detour via point on the original path.

        Finds the first blocking obstacle and generates a side via point as the
        new target. With no obstacle, returns target and clears the player's side memory.
        The via point is clamped inside the field.
        """
        obstacle = self._first_blocking_obstacle(player_id, start, target, context)
        if obstacle is None:
            self._avoid_side_by_player.pop(player_id, None)
            return target
        side_sign = self._avoid_side_by_player.get(player_id)
        if side_sign is None:
            side_sign = self._choose_avoid_side(start, target, obstacle)
        self._avoid_side_by_player[player_id] = side_sign
        via = self._via_pose(start, target, obstacle, side_sign)
        return self._field.clamp_inside_field(via)

    def _first_blocking_obstacle(
        self,
        player_id: int,
        start: Pose2D,
        target: Pose2D,
        context: PlayContext,
    ) -> Obstacle | None:
        """Find the first obstacle that truly blocks the start-to-target segment.

        Decision logic, projected in the path-local frame:

        ``along`` must fall in the middle of the path; obstacles near start or target are ignored.
        ``lateral`` must be smaller than obstacle radius plus safety margin, meaning the obstacle intrudes into the corridor.
        Among blockers, return the one with the smallest ``along`` value, nearest the start.
        """
        seg_dx = target.x - start.x
        seg_dy = target.y - start.y
        seg_len = math.hypot(seg_dx, seg_dy)
        if seg_len < 1e-6:
            return None
        dir_x = seg_dx / seg_len
        dir_y = seg_dy / seg_len
        left_x = -dir_y
        left_y = dir_x
        best: tuple[float, Obstacle] | None = None
        for obstacle in self._obstacles.collect_all(player_id, context):
            rel_x = obstacle.x - start.x
            rel_y = obstacle.y - start.y
            along = rel_x * dir_x + rel_y * dir_y
            if (
                along <= self._config.strategy.obstacle_start_ignore_distance
                or along
                >= seg_len - self._config.strategy.obstacle_target_ignore_distance
            ):
                continue
            lateral = abs(rel_x * left_x + rel_y * left_y)
            corridor = obstacle.radius + self._config.strategy.obstacle_safety_margin
            if lateral >= corridor:
                continue
            if best is None or along < best[0]:
                best = (along, obstacle)
        return best[1] if best is not None else None

    def _choose_avoid_side(
        self,
        start: Pose2D,
        target: Pose2D,
        obstacle: Obstacle,
    ) -> float:
        """Decide whether to detour left (+1) or right (-1) the first time an obstacle is seen.

        Project the obstacle onto the path's left normal: obstacle on the left
        means detour right (-1), obstacle on the right means detour left (+1), minimizing the detour.
        """
        seg_dx = target.x - start.x
        seg_dy = target.y - start.y
        seg_len = math.hypot(seg_dx, seg_dy)
        if seg_len < 1e-6:
            return 1.0
        left_x = -seg_dy / seg_len
        left_y = seg_dx / seg_len
        lateral = (obstacle.x - start.x) * left_x + (obstacle.y - start.y) * left_y
        return -1.0 if lateral > 0.0 else 1.0

    def _via_pose(
        self,
        start: Pose2D,
        target: Pose2D,
        obstacle: Obstacle,
        side_sign: float,
    ) -> Pose2D:
        """Generate a side detour via point around an obstacle as the new target.

        1. Project the obstacle onto the path to get ``closest``.
        2. Offset along the path's left normal by radius plus margin, with ``side_sign`` choosing left/right.
        3. Point the via pose back toward the original target so walking naturally returns to path.
        """
        seg_dx = target.x - start.x
        seg_dy = target.y - start.y
        seg_len = math.hypot(seg_dx, seg_dy)
        if seg_len < 1e-6:
            return target
        dir_x = seg_dx / seg_len
        dir_y = seg_dy / seg_len
        left_x = -dir_y
        left_y = dir_x
        rel_x = obstacle.x - start.x
        rel_y = obstacle.y - start.y
        along = rel_x * dir_x + rel_y * dir_y
        closest_x = start.x + dir_x * along
        closest_y = start.y + dir_y * along
        offset = obstacle.radius + self._config.strategy.obstacle_safety_margin
        via_x = closest_x + left_x * side_sign * offset
        via_y = closest_y + left_y * side_sign * offset
        return Pose2D(via_x, via_y, math.atan2(target.y - via_y, target.x - via_x))

    # Walking control

    def _compute_goal_escape_velocity(
        self,
        pose: Pose2D,
        target: Pose2D,
        reason: str,
    ) -> RobotCommand:
        """Translate immediately toward an escape target, forward or backward.

        Normal navigation turns in place for large heading errors.  That is safe
        in open play but ineffective in a goal-frame pinch, where rotation alone
        cannot create clearance from either contact body.
        """
        angle_to_target = math.atan2(target.y - pose.y, target.x - pose.x)
        forward_error = normalize_angle(angle_to_target - pose.theta)
        if abs(forward_error) <= math.pi / 2.0:
            drive_sign = 1.0
            steering_error = forward_error
        else:
            drive_sign = -1.0
            steering_error = normalize_angle(forward_error - math.pi)

        speed = min(
            _GOAL_ESCAPE_SPEED_MPS,
            self._config.strategy.max_linear_speed,
        )
        return RobotCommand(
            intent=MoveIntent(
                vx=drive_sign * speed,
                vy=0.0,
                vyaw=self._angular_velocity(steering_error),
            ),
            reason=reason,
        )

    def _compute_velocity(
        self,
        pose: Pose2D,
        target: Pose2D,
        reason: str,
        arrive_distance: float,
        hold_vyaw: float,
        now: float = 0.0,
    ) -> RobotCommand:
        """Unicycle-style movement: pure turning at long angles, then vx plus small vyaw when aligned.

        Output always satisfies one of:

        Pure stop: ``vx=vy=vyaw=0``
        Pure turn: ``vyaw != 0`` and ``vx=vy=0``
        Forward plus turn: ``vx > 0``, ``vy=0``, and small ``vyaw``
        """
        dx = target.x - pose.x
        dy = target.y - pose.y
        distance = math.hypot(dx, dy)
        final_theta_error = normalize_angle(target.theta - pose.theta)

        # Arrival check
        if distance < arrive_distance and abs(final_theta_error) < _ARRIVE_ANGLE:
            if abs(hold_vyaw) > 1e-6:
                return RobotCommand(
                    intent=MoveIntent(vyaw=hold_vyaw),
                    reason=f"{reason}: active hold",
                )
            return RobotCommand.stop(f"{reason}: arrived")

        # Already close but misaligned: turn in place to target theta to avoid overshooting.
        # But limit alignment time to prevent dead-lock
        if distance < arrive_distance:
            # If angle error is small enough, consider arrived anyway
            if abs(final_theta_error) < _ARRIVE_ANGLE * 1.5:
                return RobotCommand.stop(f"{reason}: arrived (close enough)")
            return RobotCommand(
                intent=MoveIntent(vyaw=self._angular_velocity(final_theta_error)),
                reason=f"{reason} align",
            )

        angle_to_target = math.atan2(dy, dx)
        angle_error = normalize_angle(angle_to_target - pose.theta)

        # Large angle error: pure turn instead of walking while turning.
        if abs(angle_error) > _TURN_THRESHOLD:
            return RobotCommand(
                intent=MoveIntent(vyaw=self._angular_velocity(angle_error)),
                reason=f"{reason} turn",
            )

        # Forward plus tracking turn; vx is cosine-scaled to reduce lateral drift, and vy is forced to 0.
        vx = self._linear_speed(distance, angle_error)
        vyaw = self._angular_velocity(angle_error)
        return RobotCommand(intent=MoveIntent(vx=vx, vy=0.0, vyaw=vyaw), reason=reason)

    def _linear_speed(self, distance: float, angle_error: float) -> float:
        """vx equals gain * distance * cos(err), then applies floor and max clamps."""
        if distance <= 1e-6:
            return 0.0
        raw = _LINEAR_GAIN * distance * math.cos(angle_error)
        magnitude = abs(raw)
        floor = min(_LINEAR_SPEED_FLOOR, self._config.strategy.max_linear_speed)
        if magnitude < floor:
            return floor if raw >= 0.0 else -floor
        return clamp(raw, -self._config.strategy.max_linear_speed, self._config.strategy.max_linear_speed)

    def _angular_velocity(self, angle_error: float) -> float:
        """omega equals clamp(2 * err, +/-max), then applies a floor.

        Small-angle dead zone: when error is below ``_ANGULAR_DEAD_ZONE``, skip the
        floor and use pure proportional control to avoid floor-amplified overshoot near target angle.
        """
        if abs(angle_error) <= 1e-6:
            return 0.0
        omega = clamp(
            2.0 * angle_error,
            -self._config.strategy.max_angular_speed,
            self._config.strategy.max_angular_speed,
        )
        # Inside the dead zone, use pure proportional control and do not raise to floor.
        if abs(angle_error) < _ANGULAR_DEAD_ZONE:
            return omega
        floor = min(_ANGULAR_SPEED_FLOOR, self._config.strategy.max_angular_speed)
        if abs(omega) < floor:
            return floor if omega > 0.0 else -floor
        return omega

    # Yaw avoidance

    def _apply_yaw_avoidance(
        self,
        player_id: int,
        context: PlayContext,
        command: RobotCommand,
    ) -> RobotCommand:
        """Map nearby-neighbor threats to a vyaw bias added onto the command.

        A biped cannot add field-frame lateral velocity like an omni robot; instead
        it turns a bit more and walks along the new direction around the neighbor.

        Both teammates and opponents count as neighbors.  Excluding opponents in
        PLAY leaves no avoidance layer when one is already inside the path
        planner's start-ignore distance.
        """
        intent = command.intent
        # Apply bias only to forward MoveIntent commands; kick, pure turn, pure strafe, and stop are unchanged.
        if not isinstance(intent, MoveIntent) or abs(intent.vx) < 1e-6:
            return command

        robot = context.teammates.get(player_id)
        if robot is None or robot.pose is None:
            return command

        # Field-frame forward direction equals robot heading because vx is body-forward and vy is assumed 0.
        forward_vx = intent.vx * math.cos(robot.pose.theta)
        forward_vy = intent.vx * math.sin(robot.pose.theta)
        max_bias_per_neighbor = self._config.strategy.yaw_avoid_bias_max

        def neighbor_yaw_contribution(rel_x: float, rel_y: float) -> float:
            scale = self._yaw_avoid_scale(rel_x, rel_y, forward_vx, forward_vy)
            if scale <= 0.0:
                return 0.0
            speed = math.hypot(forward_vx, forward_vy)
            if speed <= 1e-6:
                return 0.0
            forward_unit_x = forward_vx / speed
            forward_unit_y = forward_vy / speed
            left_unit_x = -forward_unit_y
            left_unit_y = forward_unit_x
            rel_lateral = rel_x * left_unit_x + rel_y * left_unit_y
            # Neighbor on the left means turn right; directly behind is resolved by player_id parity.
            if abs(rel_lateral) < 0.05:
                side_sign = 1.0 if player_id % 2 == 0 else -1.0
            else:
                side_sign = -1.0 if rel_lateral > 0.0 else 1.0
            return side_sign * scale * max_bias_per_neighbor

        yaw_bias = 0.0
        for teammate_id, teammate in context.teammates.items():
            if teammate_id == player_id or teammate.pose is None:
                continue
            yaw_bias += neighbor_yaw_contribution(
                teammate.pose.x - robot.pose.x,
                teammate.pose.y - robot.pose.y,
            )

        for opponent in context.opponents.values():
            if opponent.pose is None:
                continue
            yaw_bias += neighbor_yaw_contribution(
                opponent.pose.x - robot.pose.x,
                opponent.pose.y - robot.pose.y,
            )

        if abs(yaw_bias) < 1e-6:
            return command

        new_vyaw = clamp(
            intent.vyaw + yaw_bias,
            -self._config.strategy.max_angular_speed,
            self._config.strategy.max_angular_speed,
        )
        return RobotCommand(
            intent=MoveIntent(vx=intent.vx, vy=intent.vy, vyaw=new_vyaw),
            reason=f"{command.reason} yaw avoid",
        )

    def _yaw_avoid_scale(
        self,
        rel_x: float,
        rel_y: float,
        field_vx: float,
        field_vy: float,
    ) -> float:
        """Compute one neighbor's avoidance-bias strength in [0, 1].

        Consider both current distance and predicted closest distance:

        Currently inside ``min_distance`` -> positive scale proportional to closeness.
        Predicted closest distance within ``horizon`` below ``min_distance`` -> positive scale for early avoidance.

        Take the max so both "already inside" and "will enter" trigger avoidance.
        With zero speed this falls back to pure distance; 0 means no current threat.
        """
        min_distance = self._config.strategy.yaw_avoid_min_distance_m
        horizon = self._config.strategy.yaw_avoid_horizon_sec
        distance = math.hypot(rel_x, rel_y)
        if min_distance <= 0.0 or horizon <= 0.0 or distance <= 1e-6:
            return 0.0

        speed_sq = field_vx * field_vx + field_vy * field_vy
        if speed_sq <= 1e-9:
            if distance >= min_distance:
                return 0.0
            return clamp((min_distance - distance) / min_distance, 0.0, 1.0)

        closing_time = clamp(
            (rel_x * field_vx + rel_y * field_vy) / speed_sq,
            0.0,
            horizon,
        )
        closest_x = rel_x - field_vx * closing_time
        closest_y = rel_y - field_vy * closing_time
        closest_distance = math.hypot(closest_x, closest_y)
        scale = 0.0
        if closest_distance < min_distance:
            scale = max(scale, (min_distance - closest_distance) / min_distance)
        if distance < min_distance:
            scale = max(scale, (min_distance - distance) / min_distance)
        return clamp(scale, 0.0, 1.0)
