# Mamba 门将强化学习：从零开始的完整操作手册

这份手册按“第一次接触强化学习也能照着操作”的标准编写。先给出最重要的结论：

- 不需要新建另一个 Booster Agent 项目。
- 继续使用当前项目 `E:\Pressanybutton\Booster\mamba`。
- 比赛代码仍放在 `src/`；训练代码单独放在 `training/`。
- Booster Studio 用于编辑、构建和进行最终比赛验证。
- Python 虚拟环境用于运行强化学习训练。
- Docker Desktop 目前用于运行 Booster 仿真；它是否能批量训练，要等我们确认仿真提供了哪些自动重置接口。
- 不要靠反复手动点击“开始”来训练。手动比赛适合验证，不适合生成几十万次训练步骤。
- 第一阶段只训练门将的短时扑救选择；比赛规则、唯一球权、门框脱困、犯规保护继续由现有确定性代码负责。

本项目已经建立了一个可以立即运行的“教学环境”。它先让我们学会并验证 `reset/step`、观察、动作、奖励和回合结束条件。它不是 Booster 的真实物理仿真，不能把它的成绩当作比赛成绩。等 Booster 的自动化接口确认后，我们会保留同样的外部接口，把内部的教学物理替换为 Booster 仿真。

官方参考：

- [Gymnasium 自定义环境教程](https://gymnasium.farama.org/introduction/create_custom_env/)
- [Stable-Baselines3 PPO 文档](https://stable-baselines3.readthedocs.io/en/master/modules/ppo.html)

## 1. 先理解五个最基础的词

### 1.1 智能体 Agent

“智能体”就是正在学习做决定的程序。在第一版训练里，智能体只代表门将的短时扑救决策，不代表三台机器人，也不负责整场战术。

不要把两个含义混淆：

- Booster 的 `.agent`：最终提交和运行的软件包。
- 强化学习里的 agent：根据观察选择动作的学习算法。

### 1.2 环境 Environment

环境不是 Windows、Docker 或 Python 的“运行环境”。这里的环境是一个可以反复出题并给分的程序。

门将训练环境每一小步做五件事：

1. 告诉学习算法球和门将在哪里，这叫“观察”。
2. 接收学习算法选出的动作。
3. 让仿真向前运行一小段时间。
4. 判断这一步做得好不好并给分，这叫“奖励”。
5. 判断这次射门是否已经结束。

### 1.3 观察 Observation

观察是算法在做决定前能够看到的信息。例如：

- 门将的横向位置；
- 球的位置和速度；
- 球按当前速度会从门线的什么位置通过；
- 预计还有多久到达门线；
- 上一步做了什么动作。

观察不是视频画面。第一版使用一组数字，因为数字更容易训练和排错。

### 1.4 动作 Action

动作是算法可以选择的有限指令。教学环境先使用四个高层动作：

| 编号 | 动作 | 含义 |
| ---: | --- | --- |
| `0` | `HOLD` | 保持当前位置 |
| `1` | `MOVE_NEGATIVE_Y` | 向球门一侧横移 |
| `2` | `MOVE_POSITIVE_Y` | 向球门另一侧横移 |
| `3` | `CLEAR` | 球足够近时尝试解围 |

这样设计的原因是现有 Agent 已经验证过 `vx + vyaw`、靠近球和踢球管理器。强化学习先做“选择哪种高层行为”，不直接控制每个关节，也不绕过速度限制和比赛规则。

### 1.5 回合 Episode

一个回合就是一次很短的射门题目，而不是一整场 10 分钟比赛。

例如：随机摆好门将和球，球开始射向球门；门将做若干步动作；球被扑出、解围、射进或达到时间上限后，这个回合结束。然后环境立即重新摆放并开始下一题。

一次训练通常会自动运行成千上万个这样的回合。

## 2. “Gymnasium 风格的最小接口”到底是什么

它指环境至少提供两个函数：`reset` 和 `step`。

```python
observation, info = env.reset(seed=42)
observation, reward, terminated, truncated, info = env.step(action)
```

### 2.1 `reset()` 做什么

`reset()` 开始一道新题：

1. 清除上一回合的状态。
2. 放置门将、球和射手。
3. 给球设置初速度。
4. 把计时器清零。
5. 返回第一帧观察。

返回的两个值：

- `observation`：算法现在能看到的数字。
- `info`：给人排错看的额外信息，算法通常不使用。

`seed=42` 叫随机种子。同一个环境使用相同种子，应该生成相同题目。这样修改代码前后可以考同一套试题。

### 2.2 `step(action)` 做什么

`step(action)` 让环境执行一个动作并前进一小步。它返回五个值：

- `observation`：动作执行后的新观察。
- `reward`：这一步获得的分数，可以是正数或负数。
- `terminated`：因为成功或失败而自然结束，例如扑救或失球。
- `truncated`：因为时间上限或数据中断而被迫结束。
- `info`：结果原因、步数等排错信息。

最容易混淆的是最后两个布尔值：

- 球进了，`terminated=True`。
- 球被成功解围，`terminated=True`。
- 5 秒内什么结果也没发生，`truncated=True`。

训练程序看到任意一个为 `True`，就调用 `reset()` 开始下一题。

## 3. 要用哪些软件

### 必需软件

1. **Booster Studio**

   继续打开现有的 Mamba 项目。它负责编辑代码、构建 `.agent`、部署和最终比赛测试。不需要再新建 Booster 项目。

2. **Docker Desktop**

   继续运行本地虚拟机器人和比赛仿真。以后如果 Booster 支持无界面批量重置，Docker 内的仿真会成为真实训练环境。

3. **Python 3.14（64 位）**

   可以直接使用你能安装的 Python 3.14，不必再寻找 3.11。训练仍放在独立虚拟环境中，不把依赖塞进比赛 `.agent`。当前 PyTorch 官方安装页已把 Python 3.10–3.14 列为推荐范围，Stable-Baselines3 当前要求 Python 3.10 以上；本项目只安装训练所需的核心包，不再安装容易引入额外二进制兼容问题的 Atari、OpenCV 等 `extra` 组件。

### 不必另外安装的软件

- 不必再安装 VS Code；Booster Studio 已能编辑项目文件。
- 目前不需要 Anaconda 或 Miniconda。
- 目前不需要购买云服务器或显卡。教学环境用 CPU 就能开始；真实 Booster 仿真是否需要加速以后再判断。

## 4. 项目放在哪里，是否要新建项目

不新建 Booster 项目。当前目录已经这样分工：

```text
E:\Pressanybutton\Booster\mamba\
├─ src\                         # 正式比赛代码，会进入 .agent
├─ tests\                       # 规则和战术回归测试
├─ training\                    # 训练工具，不由比赛代码导入
│  ├─ keeper_env.py             # 不依赖第三方库的教学环境
│  ├─ gym_keeper_env.py         # 把教学环境包装成 Gymnasium 环境
│  ├─ check_env.py              # 最先运行的基础检查
│  ├─ train_keeper.py           # PPO 训练入口
│  ├─ evaluate_keeper.py        # 固定 500 个种子的评估入口
│  └─ requirements-training.txt # 训练专用依赖
├─ res\models\                 # 以后保存通过验收的模型
├─ docs\reinforcement_learning.md
└─ .venv-training\             # 稍后创建，不提交，也不打进 .agent
```

训练目录和比赛目录放在同一个仓库的好处是：坐标定义、规则包装器和测试能共用版本记录；但 `src/` 不导入 `training/`，因此 Gymnasium、PyTorch 和 Stable-Baselines3 不会意外进入比赛运行路径。

## 5. 第一次操作：只运行教学环境，不安装任何库

这一节的目的是确认你能找到目录、打开终端、运行 Python 文件。

### 5.1 在 Booster Studio 打开项目

1. 打开 Booster Studio。
2. 打开文件夹 `E:\Pressanybutton\Booster\mamba`。
3. 在左侧文件树展开 `training`。
4. 确认能看到 `keeper_env.py` 和 `check_env.py`。

### 5.2 打开终端

在 Booster Studio 顶部菜单选择“终端”→“新建终端”。终端底部应该出现 PowerShell 提示符。

输入：

```powershell
cd E:\Pressanybutton\Booster\mamba
python -m training.check_env
```

预期结果：

```text
环境基础检查通过：20 个 episode 都能 reset、step 并正常结束。
```

这条命令不会训练模型，只检查：

- `reset()` 能生成新题；
- 观察固定为 8 个数字；
- 每个数字都在约定范围内；
- `step()` 能接收动作；
- 每个回合最终都能结束。

## 6. 逐行理解已经建立的教学环境

打开 `training/keeper_env.py`。

### 6.1 `KeeperAction`

```python
class KeeperAction(IntEnum):
    HOLD = 0
    MOVE_NEGATIVE_Y = 1
    MOVE_POSITIVE_Y = 2
    CLEAR = 3
```

算法最终只输出 `0` 到 `3`。枚举只是给这些数字起人能读懂的名字。

### 6.2 `Scenario`

```python
@dataclass(frozen=True)
class Scenario:
    keeper_y: float
    ball_x: float
    ball_y: float
    ball_vx: float
    ball_vy: float
```

这是一道射门题的初始条件。当前只建模门将横向位置和球的二维运动。真实 Booster 连接版以后还会包含门将朝向、射手位置、门框接触状态和数据有效掩码。

### 6.3 `_random_scenario()`

这个函数随机出题。例如球的横向位置、速度和射门角度都在合理区间内变化。随机化的目的不是制造混乱，而是防止模型只背会一条固定射门路线。

### 6.4 `_observation()`

当前返回 8 个数字：

| 下标 | 内容 | 为什么需要 |
| ---: | --- | --- |
| `0` | 门将 `y` | 知道自己站在哪里 |
| `1` | 球 `x` | 知道球离门多远 |
| `2` | 球 `y` | 知道球在左、中还是右 |
| `3` | 球 `vx` | 知道球是否向球门以及有多快 |
| `4` | 球 `vy` | 知道球横向飞向哪边 |
| `5` | 预测的门线交点 `y` | 直接表达应该封堵的位置 |
| `6` | 预计到达门将线的时间 | 决定还能走几步 |
| `7` | 上一步动作 | 帮助减少左右抖动 |

每个数被缩放到大约 `[-1, 1]`。这种处理叫归一化。不同量纲如果一个是 `0.05`、另一个是 `7.0`，神经网络更难学习；缩放后更稳定。

### 6.5 `step()`

教学物理每步推进 `0.05` 秒。它先移动门将，再尝试解围，再移动球，最后判断扑救、失球、射偏或超时。

这一版很简单是有意的。先把接口和测试做对，再连接复杂仿真；否则出问题时无法判断是奖励、算法、ROS 连接还是仿真重置造成的。

## 7. 创建训练专用 Python 虚拟环境

“虚拟环境”可以理解为当前项目自己的 Python 工具箱。它不会改动 Booster Agent 的 Python 依赖。

### 7.1 安装 Python 3.14

从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)安装 64 位 Python 3.14。新版 Windows 安装的是 Python Install Manager；如果它询问是否把命令目录加入 PATH，选择允许。安装后完全关闭并重新打开 Booster Studio，再打开新终端，输入：

```powershell
py -V:3.14 --version
```

应该看到 `Python 3.14.x`。最后一位小版本不同没有关系。`-V:3.14` 是新版 Python Install Manager 选择指定运行时的官方写法；旧启动器通常也接受 `py -3.14`。

如果提示找不到 `py`，先在 Windows 自带的 PowerShell 中执行同一条命令。如果那里可用，说明只是 Booster Studio 尚未刷新 PATH，重启 Studio 即可。如果两处都找不到，重新打开 Python Install Manager，确认已安装 3.14 runtime，并允许添加命令目录到 PATH。

### 7.2 在项目根目录创建虚拟环境

```powershell
cd E:\Pressanybutton\Booster\mamba
py -V:3.14 -m venv .venv-training
```

执行后左侧文件树可能出现 `.venv-training`。这是正常的。本项目的 `.gitignore` 已排除它，不会提交，也不会打包进 `.agent`。

### 7.3 激活虚拟环境

```powershell
.\.venv-training\Scripts\Activate.ps1
```

激活成功后，终端行首会出现 `(.venv-training)`。

如果 PowerShell 提示禁止运行脚本，只在当前终端执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv-training\Scripts\Activate.ps1
```

`-Scope Process` 表示关闭这个终端后设置就失效，不会永久修改系统策略。

### 7.4 安装训练库

```powershell
python -m pip install --upgrade pip
python -m pip install -r training\requirements-training.txt
```

安装结束后马上确认实际使用的版本：

```powershell
python --version
python -c "import torch, gymnasium, stable_baselines3; print('torch', torch.__version__); print('gymnasium', gymnasium.__version__); print('sb3', stable_baselines3.__version__)"
```

第一行必须是 `Python 3.14.x`。如果不是，说明虚拟环境没有激活；不要在错误的 Python 上继续安装。

这会安装：

- Gymnasium：规定环境接口和数据空间；
- Stable-Baselines3：提供 PPO；
- PyTorch：Stable-Baselines3 使用的神经网络库；
- TensorBoard：查看训练曲线。

安装可能需要数分钟。以后打开一个新终端时，要重新运行激活命令，但不需要重复安装。

## 8. 检查 Gymnasium 包装是否正确

激活虚拟环境后输入：

```powershell
python -c "from gymnasium.utils.env_checker import check_env; from training.gym_keeper_env import GymKeeperEnv; check_env(GymKeeperEnv()); print('Gymnasium 检查通过')"
```

Gymnasium 会检查：

- 动作空间是否声明正确；
- 观察的形状和数据类型是否一致；
- `reset()` 和 `step()` 返回值数量是否正确；
- 观察是否落在允许范围内。

如果这一步失败，不要开始训练。先修环境，因为算法无法弥补一个返回格式错误的环境。

## 9. 第一次 PPO 训练如何运行

确认终端行首有 `(.venv-training)`，然后输入：

```powershell
python -m training.train_keeper
```

训练脚本做的事情：

1. 创建 `GymKeeperEnv`。
2. 再运行一次环境检查。
3. 创建 PPO 的小型多层感知机策略 `MlpPolicy`。
4. 自动执行 200,000 个环境步骤。
5. 将模型保存到 `res\models\keeper_toy_ppo.zip`。
6. 将 TensorBoard 日志保存到 `training\runs`。

这个模型只是在教学物理里学会接口闭环，不应放进正式比赛 Agent。

### 查看训练曲线

保持虚拟环境激活，另开一个终端：

```powershell
tensorboard --logdir training\runs
```

终端会显示一个本地地址，通常是 `http://localhost:6006`。在浏览器打开它。

初学阶段只先看两项：

- `rollout/ep_rew_mean`：平均每回合奖励，整体应逐渐上升；
- `rollout/ep_len_mean`：平均回合长度，用来发现模型是否一直拖到超时。

曲线上下波动是正常的；不要只看最后一个点。

## 10. 如何评估，而不是只相信训练分数

运行：

```powershell
python -m training.evaluate_keeper
```

评估脚本用固定的 `1000` 到 `1499` 共 500 个种子。这 500 道题在不同版本间保持一致，因此可以公平比较。

至少记录：

- 扑救或解围率；
- 失球数；
- 超时数；
- 动作切换频率；
- 门将进入球门内部次数；
- 与 robot2 或门框碰撞次数。

教学环境目前只能提供前三类基础结果。后三类需要真实 Booster 连接版。

## 11. 奖励函数应该怎样理解

奖励不是“写得越多越聪明”，而是告诉算法最终目标。第一版坚持少而清楚：

| 事件 | 教学环境分数 | 意图 |
| --- | ---: | --- |
| 扑救 | `+6` | 首要成功事件 |
| 主动解围 | `+4` | 球离开危险区 |
| 失球 | `-10` | 最大失败 |
| 射偏 | `+1` | 没失球，但不是门将主动贡献 |
| 每一步 | `-0.002` | 不鼓励无限等待 |
| 切换动作 | `-0.003` | 减少左右抖动 |
| 更接近预测封堵点 | 小额正分 | 帮助早期学习移动方向 |

真实版本还会增加：

- 倒地或进入球门内部：负分；
- 撞门框或队友：负分；
- 将球挡回危险的正前方：不能按成功解围计分；
- 球进入安全边路或被队友控制：成功解围。

不要持续奖励“离球近”。否则模型可能学会站在球边卡住，却不把球解围。

## 12. 从教学环境换成 Booster 环境，具体要换哪里

对训练程序来说，外部接口不变：仍然是 `reset()` 和 `step()`。变化只发生在环境内部。

### 教学版 `reset()`

直接修改 Python 变量：

```text
keeper_y = 随机值
ball_x = 随机值
ball_vx = 随机值
```

### Booster 版 `reset()`

需要向 Booster 仿真发命令：

1. 暂停或重置场景。
2. 把门将、射手和球放到指定姿态。
3. 清除上一回合速度、踢球状态和惩罚状态。
4. 给球初速度，或命令射手执行可重复的射门。
5. 等待所有真值 topic 确认新姿态已经生效。
6. 返回从 topic 读取的第一帧观察。

### 教学版 `step(action)`

直接用几行 Python 更新位置。

### Booster 版 `step(action)`

需要：

1. 把动作转换为现有运动控制器允许的高层命令。
2. 让仿真运行固定时间，例如 `0.05` 秒。
3. 从 topic 读取球和机器人新状态。
4. 由赛事规则判断进球、出界、倒地、碰撞和超时。
5. 计算奖励并返回。

所以“构建真实强化学习环境”的核心不是再画一个球场，而是找到 Booster 提供的**自动重置、设置姿态、推进和读取状态**接口。

## 13. 现在怎样确认 Booster 是否支持自动训练

这一步不修改代码，只收集接口清单。

### 方法 A：使用 Docker Desktop

1. 启动一次本地 3v3 仿真，使虚拟机器人容器正在运行。
2. 打开 Docker Desktop。
3. 点击左侧“Containers”。
4. 找到正在运行 Booster 仿真的容器。
5. 点击容器，再打开“Exec”或“Terminal”。
6. 在容器终端依次运行：

```bash
ros2 topic list -t
ros2 service list -t
ros2 action list -t
```

7. 将三段完整输出复制保存为文本文件。

### 方法 B：使用 Booster Studio 已连接的容器终端

如果 Studio 终端已经进入 Booster 容器并且 `ros2` 命令可用，可以直接执行同样三条命令。

我们重点寻找：

- 球和机器人姿态 topic；
- 设置模型姿态的 service；
- 重置世界或重置比赛的 service/action；
- 暂停、继续或按固定步长推进仿真的接口；
- 给球速度或触发射门的接口。

只看到 ground-truth topic 还不够。topic 能“读”，训练还需要可靠地“重置”和“推进”。

## 14. 如果没有自动 reset 接口怎么办

有三种路线，按推荐顺序排列：

1. **查找官方场景脚本或 headless runner**

   这是最理想方案。它通常能快速重复同一场景，适合大量训练。

2. **使用官方允许的仿真 service/action 组合出 reset**

   例如暂停世界、设置姿态、清速度、恢复运行。必须只使用官方公开并允许的接口。

3. **先做离线/替代仿真训练，Booster Studio 只用于评估**

   这种模型存在“仿真差距”：在简化物理里有效，到了 Booster 未必有效。因此必须做随机化，并用真实固定测试集筛选。

不推荐把“人工点开始、等一场结束、再点开始”当训练。200,000 步训练如果每次都要人工操作，既慢又无法精确复现初始条件。

## 15. 训练课程不要一开始就上 3v3

推荐由简单到复杂：

1. **课程 1：无射手的轨迹球**

   直接给球初速度，只学左右封堵。

2. **课程 2：单射手点球**

   随机射门方向、速度和门将初始位置。

3. **课程 3：慢球、假动作和二次射门**

   防止门将只按第一帧速度提前猜边。

4. **课程 4：加入 robot2**

   验证门将唯一球权、robot2 不重叠、解围方向和二点保护。

5. **课程 5：完整 3v3**

   学习策略只在 `TRACK_SHOT / BLOCK_LINE` 这类短时阶段提供动作建议；现有状态机继续负责规则和安全。

每升一级都要保留上一级固定测试集，防止学会复杂场景后把基础扑救忘掉。

## 16. 模型以后怎样进入 `.agent`

训练完成不等于可以直接放入比赛包。正确流程是：

1. 在训练目录训练模型。
2. 用未参与训练的固定种子评估。
3. 在 Booster 仿真里与当前规则门将做 A/B 对照。
4. 只有扑救率提高且碰撞、犯规、进门次数没有增加，才冻结该版本。
5. 将冻结模型转换为比赛运行环境支持的格式。
6. 在 `src/learning/` 编写只负责加载和推理的包装器。
7. 输出仍通过现有规则状态机、速度限制、唯一球权和门框安全层。
8. 重新构建 `.agent` 并做完整比赛回归。

正式比赛期间不继续更新权重。`.agent` 内只做前向推理。

不要现在就决定使用 ONNX。先确认 `onnxruntime` 能否被 Booster 构建器同时打包到 `sim_x86_64`、`sim_aarch64` 和 `real_jetson`。如果不能，第一版可以导出一个很小的 MLP 权重并用纯 Python 前向计算。

## 17. 你现在只需要做的准备

按顺序完成以下事项即可：

1. 在 Booster Studio 终端运行：

   ```powershell
   cd E:\Pressanybutton\Booster\mamba
   python -m training.check_env
   ```

2. 安装 Python 3.14 并创建 `.venv-training`。
Test-Path .\.venv-training\Scripts\Activate.ps13. 安装 `training\requirements-training.txt`。
4. 运行 Gymnasium `check_env`。
5. 暂时不急着长时间训练；先确认教学环境的概念和输出看得懂。
6. 启动一次 Booster 仿真，从容器保存三份 ROS 清单：

   ```bash
   ros2 topic list -t
   ros2 service list -t
   ros2 action list -t
   ```

7. 将三份输出交给我。下一步我会判断能否直接构建 `BoosterKeeperEnv`，并明确 `reset()` 和 `step()` 中每一行应该调用哪个官方接口。

## 18. 常见问题

### “训练是不是一直点开始？”

不是。人工点击用于最终观察和验收；强化训练必须由程序自动重复 `reset → step → 结束 → reset`。

### “要不要复制一份 mamba 项目？”

不要。训练代码已隔离在当前仓库的 `training/`，虚拟环境已隔离在 `.venv-training/`。

### “教学环境训练出的模型能直接参赛吗？”

不能。它只证明环境接口和训练流程能跑通。真实模型至少要用 Booster 物理数据训练或校准，并通过真实仿真 A/B 测试。

### “强化学习会取代现在所有规则吗？”

不会。第一版只增强门将的短时封堵动作。规则、定位球、唯一追球者、门框脱困、速度上限和犯规保护都继续由确定性代码控制。

### “为什么不直接训练三台机器人整场比赛？”

因为那会同时混入站位、传球、射门、犯规、门框、队友协作和稀疏进球奖励。问题太大时，模型失败后很难定位原因。先把点球扑救学会，才有可比较的基线。

### “什么时候才算环境构造完成？”

当下面这些条件全部满足：

- `reset(seed)` 能重复同一初始场景；
- `step(action)` 每次推进固定时间；
- 观察没有缺失值冒充真实的零；
- 动作经过安全限制；
- 进球、解围、倒地和超时判断可靠；
- 训练可以无人值守连续运行；
- 固定测试集在代码修改前后产生可比较结果。
