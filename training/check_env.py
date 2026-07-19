"""Run this first: it checks the environment without training a model."""

from __future__ import annotations

import random

from training.keeper_env import KeeperEnv


def main() -> None:
    env = KeeperEnv()
    completed = 0
    for seed in range(20):
        observation, info = env.reset(seed=seed)
        assert len(observation) == KeeperEnv.OBSERVATION_SIZE
        assert all(-1.0 <= value <= 1.0 for value in observation)
        for _ in range(KeeperEnv.MAX_STEPS):
            observation, reward, terminated, truncated, info = env.step(
                random.randrange(KeeperEnv.ACTION_COUNT)
            )
            assert isinstance(reward, float)
            if terminated or truncated:
                completed += 1
                break
    assert completed == 20
    print("环境基础检查通过：20 个 episode 都能 reset、step 并正常结束。")


if __name__ == "__main__":
    main()
