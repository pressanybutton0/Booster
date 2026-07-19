"""Train a PPO policy on the teaching environment, not Booster physics."""

from __future__ import annotations

import argparse
from pathlib import Path

from gymnasium.utils.env_checker import check_env
from stable_baselines3 import PPO

from training.gym_keeper_env import GymKeeperEnv


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="训练教学用门将 PPO 模型")
    parser.add_argument(
        "--timesteps",
        type=_positive_int,
        default=200_000,
        help="训练步数；第一次冒烟测试建议使用 4096（默认：200000）",
    )
    parser.add_argument(
        "--model-name",
        default="keeper_toy_ppo",
        help="保存到 res/models 下的模型名（默认：keeper_toy_ppo）",
    )
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认：42）")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if Path(args.model_name).name != args.model_name:
        raise ValueError("--model-name 只能是文件名，不能包含目录")

    env = GymKeeperEnv()
    check_env(env)

    root = Path(__file__).resolve().parents[1]
    model_dir = root / "res" / "models"
    log_dir = root / "training" / "runs"
    model_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    model = PPO(
        "MlpPolicy",
        env,
        n_steps=1024,
        batch_size=64,
        verbose=1,
        seed=args.seed,
        device="cpu",
        tensorboard_log=str(log_dir),
    )
    print(f"开始离线训练：{args.timesteps} 步；不会连接或控制 Booster 比赛。")
    model.learn(total_timesteps=args.timesteps)

    model_path = model_dir / args.model_name
    model.save(model_path)
    print(f"训练完成：{model_path.with_suffix('.zip')}")


if __name__ == "__main__":
    main()
