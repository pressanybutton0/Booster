# 踢球开发协议

本文档面向策略开发者，集中说明当前 3v3 仿真踢球会用到的数据 topic、裁判机 JSON 消息和 boosteros 控制接口。更详细的实现拆分见 `docs/implementation.md`；更详细的比赛规则见 `note/3v3_rule.md`。

## 1. 比赛身份和成员约定

正式比赛运行时会通过环境变量指定当前 Agent 的队伍身份和己方机器人列表：

| 环境变量 | 含义 | 默认比赛取值 |
|---|---|---|
| `SOCCER_TEAM_ID` | 当前 Agent 所属队伍编号 | team1 为 `1`，team2 为 `2` |
| `SOCCER_ROBOT_NAMES` | 当前 Agent 控制的己方机器人名，逗号分隔 | team1 为 `robot1,robot2,robot3`；team2 为 `robot4,robot5,robot6` |

`SOCCER_ROBOT_NAMES` 的顺序决定策略层的 `player_id`：第 1 个机器人是 `player_id=1`，第 2 个是 `player_id=2`，第 3 个是 `player_id=3`。运行时会用这些值推导 `/teamN/...` 真值 topic、己方 `RobotState` 映射和 `BoosterRobot.virtual_robot_name`。

参赛者如果自行修改代码，必须保留这个约定：

- 不要在策略或 runtime 中硬编码当前队伍号。
- 不要硬编码己方机器人一定是 `robot1,robot2,robot3`。
- 需要队伍号和己方成员时，从 `SOCCER_TEAM_ID` / `SOCCER_ROBOT_NAMES` 对应配置读取。
- 可以读取对手机器人位置，但本 Agent 只能控制 `SOCCER_ROBOT_NAMES` 指定的己方机器人。

## 2. 队伍视角真值数据

仿真真值通过 `/teamN/...` topic 前缀按队伍场地坐标系发布。这里的 `N` 是队伍号，通常是 `1` 或 `2`。`/teamN` 下的机器人位姿和球坐标都已经相对该队伍的场地坐标系表达，仿真赛策略可以直接订阅并使用这些真值，不需要参赛者再做坐标系转换。

队伍场地坐标系定义：

- 每支队伍都有自己的队伍场地坐标系；同一个 `/teamN` topic 前缀下的机器人和球使用同一套队伍坐标。
- 场地中心是 `(0, 0)`，默认 M-Field 尺寸为 `14.0m x 9.0m`。
- 对任意队伍，己方球门固定在 `x=-7.0`，对方球门固定在 `x=+7.0`。
- `+x` 永远表示本队进攻方向，`-x` 永远表示本队防守方向。
- 己方半场是 `x <= 0`，对方半场是 `x >= 0`。
- `robot_pose.theta` 是机器人朝向，单位为 rad；`theta=0` 表示面向 `+x` 进攻方向。
- 球 topic 复用 `geometry_msgs/msg/Pose2D`，当前只读取 `x` / `y`，忽略 `theta`。

这不同于“绝对场地坐标系”。在绝对场地坐标系里，team1 和 team2 可能共享同一个固定场地原点与方向，因此一队的进攻方向可能是 `+x`，另一队可能是 `-x`。但 `/teamN/...` 真值不是这样给策略用的：它已经按队伍视角归一化。例如同一个物理球如果正在 team1 的进攻半场，在 `/team1/.../ball` 中可能表现为 `x>0`；对 team2 来说这是它的防守半场，在 `/team2/.../ball` 中也会按 team2 视角表达。策略只需要记住：看到 `ball.x > 0` 就表示球在本队进攻方向一侧，看到 `ball.x < 0` 就表示球在本队防守方向一侧。

因此，策略层不根据 `team_id`、上下半场或 topic 前缀再镜像坐标。如果未来输入源改成绝对场地坐标，镜像/旋转归一化应在 `PlayContextProvider` 这类 adapter 层完成，进入 `PlayContext` 后仍保持上述队伍场地坐标系约定。

### Topic 列表

3v3 默认共有 6 台机器人。每个队伍视角都会发布 6 台机器人的位姿，以及 1 个队伍级足球位置 topic。

| 队伍视角 | Topic | 消息类型 | 说明 |
|---|---|---|---|
| team1 | `/team1/robot1/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot1` 在 team1 视角下的位姿 |
| team1 | `/team1/robot2/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot2` 在 team1 视角下的位姿 |
| team1 | `/team1/robot3/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot3` 在 team1 视角下的位姿 |
| team1 | `/team1/robot4/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot4` 在 team1 视角下的位姿 |
| team1 | `/team1/robot5/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot5` 在 team1 视角下的位姿 |
| team1 | `/team1/robot6/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot6` 在 team1 视角下的位姿 |
| team1 | `/team1/soccer/sim/ground_truth/ball` | `geometry_msgs/msg/Pose2D` | 足球在 team1 视角下的位置 |
| team2 | `/team2/robot1/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot1` 在 team2 视角下的位姿 |
| team2 | `/team2/robot2/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot2` 在 team2 视角下的位姿 |
| team2 | `/team2/robot3/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot3` 在 team2 视角下的位姿 |
| team2 | `/team2/robot4/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot4` 在 team2 视角下的位姿 |
| team2 | `/team2/robot5/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot5` 在 team2 视角下的位姿 |
| team2 | `/team2/robot6/soccer/sim/ground_truth/robot_pose` | `geometry_msgs/msg/Pose2D` | `robot6` 在 team2 视角下的位姿 |
| team2 | `/team2/soccer/sim/ground_truth/ball` | `geometry_msgs/msg/Pose2D` | 足球在 team2 视角下的位置 |

当前 `RosTruthProvider` 会在同一个 `/teamN` topic 前缀下读取本队机器人和对手机器人。例如 team1 运行时默认订阅 `/team1/robot1..robot6/.../robot_pose` 和 `/team1/soccer/sim/ground_truth/ball`；team2 运行时默认订阅 `/team2/robot1..robot6/.../robot_pose` 和 `/team2/soccer/sim/ground_truth/ball`。

### 消息结构

`geometry_msgs/msg/Pose2D`：

```text
float64 x
float64 y
float64 theta
```

机器人位姿映射到策略层：

```text
Pose2D.x     -> RobotState.pose.x
Pose2D.y     -> RobotState.pose.y
Pose2D.theta -> RobotState.pose.theta
```

足球位置映射到策略层：

```text
Pose2D.x -> BallState.x
Pose2D.y -> BallState.y
theta    -> ignored
```

### 订阅示例

```python
from geometry_msgs.msg import Pose2D
from rclpy.node import Node


class TruthDebugNode(Node):
    def __init__(self) -> None:
        super().__init__("truth_debug")
        self.create_subscription(
            Pose2D,
            "/team1/robot1/soccer/sim/ground_truth/robot_pose",
            self._robot_cb,
            1,
        )
        self.create_subscription(
            Pose2D,
            "/team1/soccer/sim/ground_truth/ball",
            self._ball_cb,
            1,
        )

    def _robot_cb(self, msg: Pose2D) -> None:
        self.get_logger().info(
            f"robot1 x={msg.x:.2f} y={msg.y:.2f} theta={msg.theta:.2f}"
        )

    def _ball_cb(self, msg: Pose2D) -> None:
        self.get_logger().info(f"ball x={msg.x:.2f} y={msg.y:.2f}")
```

## 3. 裁判机消息 `/soccer/game_controller`

裁判机状态通过 ROS2 topic `/soccer/game_controller` 发布。消息类型是 `std_msgs/msg/String`，`data` 字段是 GameController v19 语义的 JSON 表示。当前模板只订阅这个 ROS topic，不直接消费裁判机 UDP binary packet。

运行时通过 `game_control_state_from_json()` 解析 JSON，得到 `GameControlState`。如果超过 2 秒没有收到新的裁判 topic，控制循环会把机器人停住。

### Topic 与 ROS 消息

| Topic | 消息类型 | 说明 |
|---|---|---|
| `/soccer/game_controller` | `std_msgs/msg/String` | `data` 是 GameController v19 JSON payload |

`std_msgs/msg/String`：

```text
string data
```

### JSON 示例

```json
{
  "version": 19,
  "packetNumber": 1024,
  "playersPerTeam": 3,
  "competitionType": "MIDDLE",
  "stopped": false,
  "gamePhase": "NORMAL",
  "state": "PLAYING",
  "setPlay": "NONE",
  "firstHalf": true,
  "kickingTeam": 255,
  "secsRemaining": 540,
  "secondaryTime": 0,
  "teams": [
    {
      "teamNumber": 1,
      "fieldPlayerColour": 0,
      "goalkeeperColour": 0,
      "goalkeeper": 3,
      "score": 0,
      "penaltyShot": 0,
      "singleShots": 0,
      "messageBudget": 0,
      "players": [
        {
          "penalty": "NONE",
          "secsTillUnpenalised": 0,
          "warnings": 0,
          "cautions": 0
        }
      ]
    },
    {
      "teamNumber": 2,
      "fieldPlayerColour": 1,
      "goalkeeperColour": 1,
      "goalkeeper": 3,
      "score": 0,
      "penaltyShot": 0,
      "singleShots": 0,
      "messageBudget": 0,
      "players": [
        {
          "penalty": "NONE",
          "secsTillUnpenalised": 0,
          "warnings": 0,
          "cautions": 0
        }
      ]
    }
  ]
}
```

上面的 `players` 只展示了每队第 1 个球员。实际消息会包含该队球员数组，策略按 `player_id` 使用 `players[player_id - 1]`。

### 顶层字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `version` | int | GameController 协议版本。当前为 `19`。 |
| `packetNumber` | int | 裁判机消息序号，用于观察消息是否持续更新。 |
| `playersPerTeam` | int | 每队场上球员数。3v3 中通常为 `3`。 |
| `competitionType` | string | 比赛组别。当前枚举：`SMALL`、`MIDDLE`、`LARGE`。 |
| `stopped` | bool | `state=PLAYING` 下的暂停/摆球状态。为 `true` 时机器人不应继续明显移动。 |
| `gamePhase` | string | 比赛阶段。当前枚举：`NORMAL`、`PENALTY_SHOOT_OUT`、`EXTRA_TIME`、`TIMEOUT`。 |
| `state` | string | 主比赛状态。当前枚举：`INITIAL`、`READY`、`SET`、`PLAYING`、`FINISHED`。 |
| `setPlay` | string | 当前定位球/重开球类型。当前枚举：`NONE`、`DIRECT_FREE_KICK`、`INDIRECT_FREE_KICK`、`PENALTY_KICK`、`THROW_IN`、`GOAL_KICK`、`CORNER_KICK`。 |
| `firstHalf` | bool | 是否上半场。当前策略不依赖上下半场翻转场地。 |
| `kickingTeam` | int | 当前 kick-off 的开球队伍号，或 set play 的主罚队伍号。无队伍时为 `255`。 |
| `secsRemaining` | int | 主比赛剩余秒数。 |
| `secondaryTime` | int | 当前阶段或 set play 的辅助倒计时，例如 Ready、Set、kick-off 球权开放前窗口、重开球窗口。 |
| `teams` | array | 两队状态数组，每个元素是一个 `TeamState` JSON 对象。 |

### `teams[]` 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `teamNumber` | int | 队伍号，通常为 `1` 或 `2`。 |
| `fieldPlayerColour` | int | 场上球员颜色编号，含义由 GameController/仿真端定义。 |
| `goalkeeperColour` | int | 守门员颜色编号，含义由 GameController/仿真端定义。 |
| `goalkeeper` | int | 守门员球员号，按 1-based player id 表示。 |
| `score` | int | 当前比分。 |
| `penaltyShot` | int | 点球相关计数，保留 GameController v19 字段。 |
| `singleShots` | int | 单次射门相关计数，保留 GameController v19 字段。 |
| `messageBudget` | int | 队伍通信预算字段，当前策略不使用。 |
| `players` | array | 本队球员状态数组，按 `player_id - 1` 下标访问。 |

### `teams[].players[]` 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `penalty` | string | 球员当前处罚类型。`NONE` 表示未被罚下。 |
| `secsTillUnpenalised` | int | 距离解除处罚的剩余秒数。为正数时该球员应视为不可参与比赛。 |
| `warnings` | int | warning 次数。 |
| `cautions` | int | caution 次数。 |

当前模板识别的 `penalty` 枚举包括：

```text
NONE
ILLEGAL_POSITIONING
MOTION_IN_SET
LOCAL_GAME_STUCK
INCAPABLE_ROBOT
PICKED_UP
BALL_HOLDING
LEAVING_THE_FIELD
PLAYING_WITH_ARMS_HANDS
PUSHING
SENT_OFF
SUBSTITUTE
```

策略层通常只需要判断：

- `state`：决定当前是 Ready、Set、Playing 还是结束。
- `stopped`：`PLAYING` 中是否必须暂停移动。
- `setPlay` + `kickingTeam` + `secondaryTime`：判断是我方 / 对方 kick-off，还是我方 / 对方定位球或重开球。
- `teams[].score`：比分。
- `teams[].players[player_id - 1].penalty` 与 `secsTillUnpenalised`：判断本方球员是否可参与比赛。

### 订阅示例

```python
import json

from rclpy.node import Node
from std_msgs.msg import String


class GameControllerDebugNode(Node):
    def __init__(self) -> None:
        super().__init__("gc_debug")
        self.create_subscription(
            String,
            "/soccer/game_controller",
            self._gc_cb,
            10,
        )

    def _gc_cb(self, msg: String) -> None:
        payload = json.loads(msg.data)
        self.get_logger().info(
            "state=%s stopped=%s setPlay=%s kickingTeam=%s"
            % (
                payload.get("state"),
                payload.get("stopped"),
                payload.get("setPlay"),
                payload.get("kickingTeam"),
            )
        )
```

## 4. boosteros 控制接口清单

当前项目把 boosteros 访问集中封装在 `src/soccer_framework/robot.py` 的 `TeamRobotManager` 和 `PlayerKickStateMachine` 中。策略代码不要直接调用 boosteros；策略只输出 `RobotCommand`，由 runtime 统一下发。

boosteros 官方详细接口文档：[boosteros 接口文档](https://booster.feishu.cn/wiki/FV4SwjEeXiGJ1wkZJEacT3kCniQ)。

| 类/接口 | 当前用法 | 简要说明 |
|---|---|---|
| `BoosterRobot(virtual_robot_name=..., enable_tf_listener=False, timeout=10.0)` | 启动时为每个 `player_id` 创建一个机器人对象 | `virtual_robot_name` 来自 `SOCCER_ROBOT_NAMES` / ROS 参数 `robot_names`；写 `default` 时 runtime 会传空字符串。 |
| `BoosterRobot.list_gaits()` | 保留为硬件适配辅助接口 | 可用于检查 SDK 暴露的 gait 名称。 |
| `BoosterRobot.set_gait(gait)` | READY/PLAYING 阶段需要行走命令且当前不是 walk mode 时调用 | 切换到足球 gait。 |
| `BoosterRobot.set_mode("walk")` | READY/PLAYING 阶段需要行走命令且当前不是 walk mode 时调用 | 切换到 walk mode。 |
| `BoosterRobot.get_mode()` | READY/PLAYING 控制 tick 读取 SDK 快照 | 检查当前模式是否仍为 walk；裁判机可能异步切到 prepare，需要在可运动阶段发现。 |
| `BoosterRobot.get_fall_down_state()` | READY/PLAYING 且非 walk 模式下读取 SDK 快照 | 读取跌倒状态；当前使用返回对象的 `state` 和 `recoverable` 字段。walk 模式默认视为 normal。 |
| `BoosterRobot.get_up()` | 摔倒恢复时调用 | 触发起身；manager 内部做 1 秒重试节流。 |
| `BoosterRobot.set_velocity(vx, vy, vyaw)` | 普通移动/停止命令 | 双足底盘速度接口。当前导航层主要使用 `vx` 和 `vyaw`，横移 `vy` 通常保持 `0`。 |
| `SoccerKickManager(robot)` | 启动时为每台机器人创建 | boosteros 自动踢球管理器，和 `set_velocity` 共享底盘控制通道。 |
| `SoccerKickManager.start()` | 进入踢球意图时调用 | 开始自动踢球控制；项目会保持最小活跃时间，避免频繁 start/stop。 |
| `SoccerKickManager.update_command(direction, power)` | 踢球期间每个控制 tick 更新 | `direction` 是机器人本体坐标系下的踢球方向，`power` 被限制在 `[1.0, 10.0]`。 |
| `SoccerKickManager.update_ball(x, y)` | 踢球期间每个控制 tick 更新 | `x` / `y` 是球在机器人本体坐标系下的位置，不是场地坐标。 |
| `SoccerKickManager.stop()` | 离开踢球意图或强制停止时调用 | 停止自动踢球控制，释放底盘给 `set_velocity`。 |

控制通道约束：

- 同一台机器人同一时刻只能由 `set_velocity` 或 `SoccerKickManager` 其中一个通道控制底盘。
- 普通移动命令会先尝试停止 `SoccerKickManager`；若仍处于最小踢球活跃时间内，本帧会跳过 `set_velocity`。
- 罚下、GameController 过期、runtime closing 等停止语义会输出零速度，并在必要时强制释放踢球通道。
- 进入 `SoccerKickManager` 前，策略使用队伍视角的场地坐标接近足球；进入后，runtime 会把球和目标方向转到机器人本体坐标系再调用 `update_ball()` / `update_command()`。
