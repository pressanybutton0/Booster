"""Evaluate baseline or learned policies on identical fixed scenarios."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import random

from stable_baselines3 import PPO

from training.gym_keeper_env import GymKeeperEnv
from training.keeper_env import KeeperAction, KeeperEnv


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="在固定场景中评估门将策略")
    parser.add_argument(
        "--policy",
        choices=("hold", "random", "heuristic", "model"),
        default="model",
        help="要评估的策略（默认：model）",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=root / "res" / "models" / "keeper_toy_ppo.zip",
        help="--policy model 时加载的模型路径",
    )
    parser.add_argument(
        "--episodes",
        type=_positive_int,
        default=500,
        help="固定测试场景数（默认：500）",
    )
    return parser.parse_args()


def _baseline_action(
    policy: str,
    env: GymKeeperEnv,
    info: dict,
    rng: random.Random,
) -> int:
    if policy == "hold":
        return int(KeeperAction.HOLD)
    if policy == "random":
        return rng.randrange(KeeperEnv.ACTION_COUNT)

    if info["distance_to_ball"] <= KeeperEnv.CLEAR_DISTANCE_M:
        return int(KeeperAction.CLEAR)
    target_y = float(info["predicted_goal_y"])
    if target_y < env.core.keeper_y - KeeperEnv.KEEPER_STEP_M / 2.0:
        return int(KeeperAction.MOVE_NEGATIVE_Y)
    if target_y > env.core.keeper_y + KeeperEnv.KEEPER_STEP_M / 2.0:
        return int(KeeperAction.MOVE_POSITIVE_Y)
    return int(KeeperAction.HOLD)


def main() -> None:
    args = _parse_args()
    env = GymKeeperEnv()
    model = PPO.load(args.model) if args.policy == "model" else None
    rng = random.Random(20260719)
    outcomes: Counter[str] = Counter()
    total_reward = 0.0

    for seed in range(1000, 1000 + args.episodes):
        observation, info = env.reset(seed=seed)
        while True:
            if model is None:
                action = _baseline_action(args.policy, env, info, rng)
            else:
                predicted_action, _ = model.predict(observation, deterministic=True)
                action = int(predicted_action)
            observation, reward, terminated, truncated, info = env.step(action)
            total_reward += reward
            if terminated or truncated:
                outcomes[str(info["outcome"])] += 1
                break

    defended = outcomes["saved"] + outcomes["cleared"]
    print(f"策略：{args.policy}")
    print(f"固定测试场景：{args.episodes}")
    print(f"扑救或解围：{defended} ({defended / args.episodes:.1%})")
    print(f"失球：{outcomes['goal']} ({outcomes['goal'] / args.episodes:.1%})")
    print(f"射偏：{outcomes['wide']} ({outcomes['wide'] / args.episodes:.1%})")
    print(f"平均回合奖励：{total_reward / args.episodes:.3f}")
    print(f"完整结果：{dict(sorted(outcomes.items()))}")


if __name__ == "__main__":
    main()
