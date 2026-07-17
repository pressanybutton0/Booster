# `src/` 代码地图

详细架构、行为树和策略说明见 [docs/implementation.md](../docs/implementation.md)；运行配置见 [note/operation.md](../note/operation.md)。本文只回答两个问题：**代码长这样**、**改东西从哪进**。

## 目录结构

```
src/
├── soccer_framework/      # Soccer environment framework and public competitor API
│   ├── __init__.py        # Public entry for PlayContext, RobotCommand, SoccerConfig, etc.
│   ├── types.py           # Data types plus PlayContextProvider abstraction
│   ├── config.py          # SoccerConfig, SoccerStrategyTuning, SoccerDebugConfig, and from_env
│   ├── game_state.py      # GameController JSON codec
│   ├── ros_truth.py       # RosTruthProvider ROS truth adapter
│   ├── robot.py           # TeamRobotManager plus kick/control adapter
│   ├── game_controller.py # GameControllerRosProvider GC topic provider
│   ├── ros_adapter.py     # SoccerRosAdapter node/subscription/executor owner
│   └── telemetry.py       # SoccerLogger plus JSONL structured logging plugin
├── runtime.py             # SoccerKit and SoccerTeamRuntime assembly/control loop with ROS adapters
├── main.py                # Booster Agent entry owning the Agent lifecycle
├── tactics/               # Pure model layer without BT or ROS
│   ├── geometry.py        # Coordinate transforms plus team-view field geometry
│   ├── navigation.py      # ObstacleCollector for obstacle gathering
│   ├── targeting/         # Tactic targets split by responsibility
│   │   └── __init__.py    # Targeting facade with stable external API
│   ├── motion.py          # MotionController for avoidance, walking, and kicking commands
│   ├── kick_hysteresis.py # Kick enter/exit hysteresis model
│   └── ready_stance.py    # READY positioning calculation
├── behavior_tree/         # BT infrastructure for blackboard, nodes, subtrees, assembly
│   ├── __init__.py        # Exports TeamStrategyTree, TeamCommandExecutor, create_team_tree
│   ├── blackboard.py      # BlackboardKeys table plus BlackboardClient
│   ├── tree.py            # TeamStrategyTree plus create_team_tree top-level assembly
│   ├── ready_subtree.py   # READY subtree factory
│   ├── safety_subtree.py  # SafetyGuards and SafetyOverrides subtree factory
│   └── nodes/
│       ├── data.py        # Data leaves that write the blackboard
│       ├── conditions.py  # Common condition leaves for ball, rules, hardware
│       └── actions.py     # Common action leaves such as StopAll, READY, CommitTeamCommands
└── play/                  # Template core with all PLAY-stage strategy code
    ├── __init__.py        # Registers the default Playbook visibly at the end
    ├── playbook.py        # Playbook, DefaultPlaybook, RoleAssignment, select_chaser
    ├── registry.py        # PlaybookRegistry plus global PLAYBOOKS registry
    ├── role.py            # Role abstractions and registry
    ├── default_roles.py   # Default dynamic roles, with defender for extension
    ├── play_subtree.py    # create_play_subtree PLAY subtree factory
    └── nodes.py           # Shared leaves and attack-subtree builder
```

## 依赖方向

```
play            ─> behavior_tree + tactics + soccer_framework (+ runtime 里的 SoccerKit)
behavior_tree   ─> runtime(仅类型) + tactics + soccer_framework
tactics         ─> soccer_framework
runtime         ─> play + behavior_tree + tactics + soccer_framework
soccer_framework ─> (无内部依赖)
```

`soccer_framework` 不会反向 import 任何上层包——这是"参赛者只用关心 framework
提供的数据和命令"这个口径成立的前提。`runtime` 里的 :class:`SoccerKit` 不依赖
`play`：二者通过 :class:`Playbook` 协议解耦，`SoccerKit` 提供能力工具，`play`
实现 PLAY 阶段决策。

## 行为树总览

```text
TeamRoot
├── DataLayer
│   ├── UpdateClock
│   ├── UpdatePlayContext
│   ├── UpdateGameState / UpdateRecentBall / UpdateRobotPoses
│   └── UpdateRobotStatus(N)
├── MatchControl
│   ├── SafetyGuards
│   │   ├── no game controller -> StopAll
│   │   ├── all inactive -> StopAll
│   │   ├── stopped=true -> StopAll
│   │   ├── non-playing state -> StopAll
│   │   └── PLAYING without ball -> StopAll
│   ├── ReadyPhase
│   │   └── ReadySlots: GoReadyTarget(N)
│   ├── PlayingPhase
│   │   └── PlaybookCore: AssignRoles + Roles(Player(N))
│   └── unsupported state -> StopAll
├── SafetyOverrides
│   └── PlayerSafety(N): allowed / fall-down / walk-mode overlays
└── CommitTeamCommands
```

行为树整体长什么样、每帧怎么走见 [implementation.md §7](../docs/implementation.md#7-策略与规则层流转)；READY / PLAYING 阶段策略见 [§8](../docs/implementation.md#8-ready-策略) / [§9](../docs/implementation.md#9-playing-策略)。

策略代码统一使用队伍场地坐标系：仿真真值里的机器人位姿和球坐标已经相对当前队伍表达，己方球门在 `x=-field_length/2`，对方球门在 `x=+field_length/2`，`+x` 是本队进攻方向，`-x` 是本队防守方向。仿真赛可以直接使用 `/teamN/...` 真值，不要在 `play/` 或 `tactics/` 里根据 `team_id` 再镜像坐标；如果输入源是绝对场地坐标，应在 `PlayContextProvider` 适配层归一化。

## 改东西从哪进

入门时优先看 [play/](play/) 下的三个文件：

| 想改的事 | 入口 |
| --- | --- |
| **角色分配（落后全员出击 / 双人进攻等）** | [play/playbook.py](play/playbook.py) 的 `Playbook.assign_roles` |
| **进攻 / 传球目标点** | [play/default_roles.py](play/default_roles.py) 的 `ChaserRole.kick_target` |
| **支援站位** | [play/default_roles.py](play/default_roles.py) 的 `SupporterRole.target` |
| **防守站位（自定义扩展）** | [play/default_roles.py](play/default_roles.py) 的 `DefenderRole.target` |
| **门将守位** | [play/default_roles.py](play/default_roles.py) 的 `GoalkeeperRole.target` |
| **加新角色（interceptor / 双前锋…）** | 派生 [play/role.py](play/role.py) 的 `RoleStrategy`，在 `Playbook.__init__` 里 `register_role(...)`；纯站位角色覆写 `target()` + 用 `MoveToTarget`，复合角色用 `build_attack_subtree` |
| **注册新 Playbook（指定为默认或按名字调用）** | [play/registry.py](play/registry.py) 的 `PLAYBOOKS.register(name, factory)`；改默认见 [play/__init__.py](play/__init__.py) 末尾 |
| **抢球评分（谁去追球）** | [play/playbook.py](play/playbook.py) 的 `DefaultPlaybook.select_chaser` |
| **PLAY 子树形态** | [play/play_subtree.py](play/play_subtree.py) |
| **缺球 / 无裁判数据的全队停机** | [behavior_tree/safety_subtree.py](behavior_tree/safety_subtree.py) 的 `SafetyGuards` |
| **未分配角色时的球员兜底动作** | [play/playbook.py](play/playbook.py) 的 `Playbook.waiting_command` |
| 门将公式 | [tactics/ready_stance.py](tactics/ready_stance.py) 的 `ReadyStance.goalkeeper_guard_target` |
| 进攻评分细节（射门 lane / 传球评分 / 带球） | [tactics/targeting/attack.py](tactics/targeting/attack.py) |
| 支援站位算法（队友间距推开） | [tactics/targeting/support.py](tactics/targeting/support.py) |
| 重启避让 / 边线恢复目标 | [tactics/targeting/restart.py](tactics/targeting/restart.py) / [tactics/targeting/recovery.py](tactics/targeting/recovery.py) |
| 避障 / 队友避让 | [tactics/navigation.py](tactics/navigation.py) |
| 队伍场地坐标几何 / 坐标变换 | [tactics/geometry.py](tactics/geometry.py) |
| 踢球进 / 出场迟滞 | [tactics/kick_hysteresis.py](tactics/kick_hysteresis.py) |
| READY / SafetyGuards / SafetyOverrides | [behavior_tree/ready_subtree.py](behavior_tree/ready_subtree.py) / [behavior_tree/safety_subtree.py](behavior_tree/safety_subtree.py) |
| 一帧数据怎么写到黑板 | [behavior_tree/nodes/data.py](behavior_tree/nodes/data.py) |

## 示例模板：派生 Playbook

`TeamStrategyTree` 不会自带任何默认 Playbook——所有 Playbook（包括
`DefaultPlaybook`）都在构造时显式传入，并通过
[play/registry.py](play/registry.py) 的 `PLAYBOOKS` 注册表统一登记。
`DefaultPlaybook` 的注册写在 [play/__init__.py](play/__init__.py) 末尾——和参赛者
注册自己的 Playbook 走完全相同的接口。

跑一场默认打法：

```python
from src.behavior_tree import TeamStrategyTree
from src.play import PLAYBOOKS
from src.runtime import SoccerKit
from src.soccer_framework import SoccerConfig

kit = SoccerKit(SoccerConfig())
tree = TeamStrategyTree(kit, PLAYBOOKS.create_default(kit), context_provider)
```

派生一份自己的 Playbook，覆写两三个方法即可：

```python
from src.behavior_tree import TeamStrategyTree
from src.play import DefaultPlaybook, PLAYBOOKS, PlayContext, RoleAssignment
from src.runtime import SoccerKit
from src.soccer_framework import SoccerConfig

class AggressivePlaybook(DefaultPlaybook):
    def assign_roles(self, context: PlayContext):
        base = super().assign_roles(context)
        # Temporarily count the goalkeeper as a supporter for all-out attack.
        game = context.known_game
        own_team = game.get_team_state(self.kit.config.team_id)
        other_team = next(
            (
                team
                for team in game.teams
                if team.team_number != self.kit.config.team_id
            ),
            None,
        )
        if (
            own_team is not None
            and other_team is not None
            and own_team.score + 1 < other_team.score
        ):
            mapping = dict(base.by_player)
            goalkeeper = next(
                (
                    player_id
                    for player_id, role in base.by_player.items()
                    if role == "goalkeeper"
                ),
                None,
            )
            if goalkeeper is not None:
                mapping[goalkeeper] = "supporter"
            return RoleAssignment(mapping)
        return base


# Register one line in your entry module, same as DefaultPlaybook registration in play/__init__.py.
PLAYBOOKS.register("aggressive", AggressivePlaybook)

kit = SoccerKit(SoccerConfig())
tree = TeamStrategyTree(kit, PLAYBOOKS.create("aggressive", kit), context_provider)
```

或者不走注册表，直接传对象（适合一次性用法）：

```python
tree = TeamStrategyTree(kit, AggressivePlaybook(kit), context_provider)
```

PLAY 子树形态、命令下发链路、READY/Safety 阶段都不用改。

## 加新角色（3 步）

想引入一个 chaser/supporter/defender/goalkeeper 之外的角色（比如「interceptor」拦传位）：

```python
from src.play import (
    DefaultPlaybook, RoleStrategy, RoleAssignment, PlayContext, MoveToTarget,
)
from src.soccer_framework import Pose2D


class InterceptorRole(RoleStrategy):
    name = "interceptor"

    def target(self, kit, player_id: int, context: PlayContext) -> Pose2D:
        # Compute a positioning Pose2D, such as on an opponent passing lane.
        ...

    def build_subtree(self, kit, player_id: int):
        return MoveToTarget(
            kit,
            player_id,
            lambda context: self.target(kit, player_id, context),
            reason_fn=lambda: "interceptor hold",
        )


class TacticalPlaybook(DefaultPlaybook):
    def __init__(self, kit):
        super().__init__(kit)
        self.register_role(InterceptorRole())   # Registration order is Selector branch priority.

    def assign_roles(self, context):
        # Mark any player_id as "interceptor" to activate it.
        return RoleAssignment({1: "chaser", 2: "interceptor", 3: "goalkeeper"})


kit = SoccerKit(SoccerConfig())
tree = TeamStrategyTree(kit, TacticalPlaybook(kit), context_provider)
```

想加「条件性踢球」的角色（如球进禁区时门将出击解围），用 `build_attack_subtree` 组装「条件踢球 → 否则跑位」子树：

```python
from src.play import AttackSubtreeConfig, RoleStrategy, build_attack_subtree
from src.soccer_framework import Pose2D


class GoalkeeperRole(RoleStrategy):
    """门将默认守位；球滚进我方危险区时主动出击解围。 / Default goalkeeper guard; actively clears when the ball enters our danger area."""

    name = "goalkeeper"

    def target(self, kit, player_id, context):
        return kit.ready_stance.goalkeeper_guard_target(context.known_ball)

    def wants_to_kick(self, kit, player_id, context):
        return kit.targeting.ball_in_own_defensive_area(context.known_ball)

    def kick_target(self, kit, player_id, context):
        return Pose2D(kit.field.opponent_goal_x(), 0.0, 0.0)

    def build_subtree(self, kit, player_id):
        return build_attack_subtree(
            kit,
            player_id,
            AttackSubtreeConfig(
                target_fn=lambda context: self.target(kit, player_id, context),
                kick_target_fn=lambda context: self.kick_target(kit, player_id, context),
                wants_kick_fn=lambda context: self.wants_to_kick(kit, player_id, context),
                reason_fn=lambda: "goalkeeper guard",
                kick_reason_fn=lambda target, context: kit.targeting.kick_reason(
                    target,
                    default="goalkeeper clear",
                    ball=context.known_ball,
                ),
            ),
        )
```

行为树会看 `wants_to_kick` + `IsInKickRange` 决定本帧踢还是走守位点。这种写法的好处是**球员在黑板上的角色名始终是 `"goalkeeper"`**，不再需要在 `Playbook.assign_roles` 里把守门员临时改写成 `"chaser"`，调试日志也好读。
