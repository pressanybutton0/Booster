"""A tiny, dependency-free goalkeeper environment for learning reset/step.

This is intentionally a teaching simulator, not the Booster physics engine.
It lets us validate observations, actions, rewards and episode boundaries before
we connect the same interface to Booster's ROS topics and reset services.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
import random
from typing import Any


class KeeperAction(IntEnum):
    """Four high-level actions that the existing motion layer can execute."""

    HOLD = 0
    MOVE_NEGATIVE_Y = 1
    MOVE_POSITIVE_Y = 2
    CLEAR = 3


@dataclass(frozen=True)
class Scenario:
    """One reproducible shot setup in team-view field coordinates."""

    keeper_y: float
    ball_x: float
    ball_y: float
    ball_vx: float
    ball_vy: float


class KeeperEnv:
    """Small Gymnasium-style environment implemented with Python only.

    Public contract:

    ``reset(seed, options) -> (observation, info)``
    ``step(action) -> (observation, reward, terminated, truncated, info)``

    The observation is an eight-number tuple. Every value is normalized to the
    approximate range [-1, 1], which is easier for a small neural network.
    """

    GOAL_LINE_X = -7.0
    KEEPER_X = -6.1
    GOAL_HALF_WIDTH = 1.30
    FIELD_HALF_WIDTH = 4.50
    DT = 0.05
    MAX_STEPS = 100
    KEEPER_STEP_M = 0.10
    BLOCK_HALF_WIDTH_M = 0.36
    CLEAR_DISTANCE_M = 0.65
    OBSERVATION_SIZE = 8
    ACTION_COUNT = 4

    def __init__(self) -> None:
        self._rng = random.Random()
        self.keeper_y = 0.0
        self.ball_x = -3.0
        self.ball_y = 0.0
        self.ball_vx = -2.0
        self.ball_vy = 0.0
        self.previous_action = KeeperAction.HOLD
        self.steps = 0
        self.finished = False

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[tuple[float, ...], dict[str, Any]]:
        """Start one new shot and return the first observation.

        ``seed`` makes a random scenario repeatable. ``options`` may contain a
        pre-built ``Scenario`` under the key ``scenario`` for an exact test.
        """

        if seed is not None:
            self._rng.seed(seed)
        scenario = None if options is None else options.get("scenario")
        if scenario is None:
            scenario = self._random_scenario()
        if not isinstance(scenario, Scenario):
            raise TypeError("options['scenario'] must be a Scenario")

        self.keeper_y = scenario.keeper_y
        self.ball_x = scenario.ball_x
        self.ball_y = scenario.ball_y
        self.ball_vx = scenario.ball_vx
        self.ball_vy = scenario.ball_vy
        self.previous_action = KeeperAction.HOLD
        self.steps = 0
        self.finished = False
        return self._observation(), self._info("running")

    def step(
        self,
        action: int | KeeperAction,
    ) -> tuple[tuple[float, ...], float, bool, bool, dict[str, Any]]:
        """Apply one action for 0.05 seconds and advance the toy physics."""

        if self.finished:
            raise RuntimeError("episode already ended; call reset() before step()")
        try:
            chosen = KeeperAction(int(action))
        except (TypeError, ValueError) as exc:
            raise ValueError("action must be an integer from 0 to 3") from exc

        old_error = abs(self.keeper_y - self._predicted_goal_y())
        reward = -0.002
        outcome = "running"

        if chosen == KeeperAction.MOVE_NEGATIVE_Y:
            self.keeper_y -= self.KEEPER_STEP_M
        elif chosen == KeeperAction.MOVE_POSITIVE_Y:
            self.keeper_y += self.KEEPER_STEP_M
        self.keeper_y = max(
            -self.FIELD_HALF_WIDTH,
            min(self.FIELD_HALF_WIDTH, self.keeper_y),
        )

        if chosen != self.previous_action:
            reward -= 0.003
        self.previous_action = chosen

        cleared = False
        if chosen == KeeperAction.CLEAR and self._distance_to_ball() <= self.CLEAR_DISTANCE_M:
            self.ball_vx = abs(self.ball_vx) + 1.2
            self.ball_vy = 0.7 if self.ball_y >= 0.0 else -0.7
            cleared = True

        self.ball_x += self.ball_vx * self.DT
        self.ball_y += self.ball_vy * self.DT
        self.steps += 1

        terminated = False
        truncated = False
        if cleared:
            reward += 4.0
            outcome = "cleared"
            terminated = True
        elif self.ball_x <= self.KEEPER_X:
            if abs(self.ball_y - self.keeper_y) <= self.BLOCK_HALF_WIDTH_M:
                reward += 6.0
                outcome = "saved"
            elif abs(self.ball_y) <= self.GOAL_HALF_WIDTH:
                reward -= 10.0
                outcome = "goal"
            else:
                reward += 1.0
                outcome = "wide"
            terminated = True
        elif self.ball_x > 0.0:
            reward += 3.0
            outcome = "safe_clearance"
            terminated = True
        elif self.steps >= self.MAX_STEPS:
            outcome = "time_limit"
            truncated = True

        new_error = abs(self.keeper_y - self._predicted_goal_y())
        reward += 0.20 * (old_error - new_error)
        self.finished = terminated or truncated
        return self._observation(), reward, terminated, truncated, self._info(outcome)

    def render(self) -> None:
        """Print a simple line of state for manual debugging."""

        print(
            f"step={self.steps:03d} keeper_y={self.keeper_y:+.2f} "
            f"ball=({self.ball_x:+.2f},{self.ball_y:+.2f}) "
            f"predicted_y={self._predicted_goal_y():+.2f}"
        )

    def _random_scenario(self) -> Scenario:
        return Scenario(
            keeper_y=self._rng.uniform(-0.8, 0.8),
            ball_x=self._rng.uniform(-4.2, -2.7),
            ball_y=self._rng.uniform(-1.8, 1.8),
            ball_vx=self._rng.uniform(-3.0, -1.2),
            ball_vy=self._rng.uniform(-0.65, 0.65),
        )

    def _predicted_goal_y(self) -> float:
        if self.ball_vx >= -1e-6:
            return self.ball_y
        seconds = (self.KEEPER_X - self.ball_x) / self.ball_vx
        if seconds <= 0.0:
            return self.ball_y
        return self.ball_y + self.ball_vy * seconds

    def _distance_to_ball(self) -> float:
        return math.hypot(self.ball_x - self.KEEPER_X, self.ball_y - self.keeper_y)

    def _observation(self) -> tuple[float, ...]:
        time_to_keeper = 0.0
        if self.ball_vx < -1e-6:
            time_to_keeper = max(0.0, (self.KEEPER_X - self.ball_x) / self.ball_vx)
        values = (
            self.keeper_y / self.FIELD_HALF_WIDTH,
            self.ball_x / abs(self.GOAL_LINE_X),
            self.ball_y / self.FIELD_HALF_WIDTH,
            self.ball_vx / 3.5,
            self.ball_vy / 1.5,
            self._predicted_goal_y() / self.FIELD_HALF_WIDTH,
            time_to_keeper / 5.0,
            float(self.previous_action) / float(self.ACTION_COUNT - 1),
        )
        return tuple(max(-1.0, min(1.0, value)) for value in values)

    def _info(self, outcome: str) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "steps": self.steps,
            "predicted_goal_y": self._predicted_goal_y(),
            "distance_to_ball": self._distance_to_ball(),
        }
