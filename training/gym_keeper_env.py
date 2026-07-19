"""Gymnasium adapter around the dependency-free teaching environment."""

from __future__ import annotations

import gymnasium as gym
import numpy as np

from training.keeper_env import KeeperEnv


class GymKeeperEnv(gym.Env):
    """Expose :class:`KeeperEnv` to Stable-Baselines3."""

    metadata = {"render_modes": ["human"]}

    def __init__(self, render_mode: str | None = None) -> None:
        super().__init__()
        self.core = KeeperEnv()
        self.render_mode = render_mode
        self.action_space = gym.spaces.Discrete(KeeperEnv.ACTION_COUNT)
        self.observation_space = gym.spaces.Box(
            low=-1.0,
            high=1.0,
            shape=(KeeperEnv.OBSERVATION_SIZE,),
            dtype=np.float32,
        )

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        observation, info = self.core.reset(seed=seed, options=options)
        return np.asarray(observation, dtype=np.float32), info

    def step(self, action: int):
        observation, reward, terminated, truncated, info = self.core.step(action)
        if self.render_mode == "human":
            self.core.render()
        return (
            np.asarray(observation, dtype=np.float32),
            reward,
            terminated,
            truncated,
            info,
        )

    def render(self) -> None:
        self.core.render()
