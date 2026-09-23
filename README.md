# Plan-and-Solve Agent 个人实践

> 复现 Plan-and-Solve 范式：**先规划、再执行、随时重规划**。DeepSeek 当大脑、Tavily 当手。

一个分层实现的 PS agent：一次调用把问题拆成计划，然后逐步执行（每一步内部是一个
完整的 ReAct 循环），执行过程中随时可以让 Replanner 审视进度、决定收工还是换计划。

**这个仓库的定位是模板母本**——其他项目要用这套框架时，复制出去改几个文件就是个
新 agent。所以代码的边界是按「改起来方不方便」来划的，不是按「跑起来对不对」来划的。

它的两个设计目标：

1. **换部件不用改内核**。换人格、换重规划策略、换 Executor 实现、换模型，
   全部从 [persona.py](persona.py) 或构造参数声明，`core/` 一行都不用动。
2. **能往上加东西**。人工审核计划、并行执行、断点续跑这类需求，是靠写新的子类
   挂上去的，不是改进内核里。

想动手改东西，看 **[MANUAL.md](MANUAL.md)**（操作手册：想改 X 就动哪个文件、
五个钩子怎么用、并行/持久化怎么升级、已知坑）。

它是 [react agent](../react%20agent/) 的姊妹项目：Executor 默认实现**就是**一个
ReAct 循环，`core/protocol.py`、`core/react_loop.py`、`llm.py`、`tools.py`、
`memory.py` 都是从那个母本搬来的（前四个一字未动）。

### 快速开始

依赖：**Python ≥ 3.10** + `requests`（唯一第三方依赖）。

```bash
uv sync          # 建 .venv 并安装依赖
```

配 key（优先级：环境变量 > 本目录 `keys.py`）：

```bash
export DEEPSEEK_API_KEY="sk-..."
export TAVILY_API_KEY="tvly-..."
```

跑起来：

```bash
python3 main.py "调研一下 2024 年中国新能源汽车销量，写一份摘要"   # 单次提问
python3 main.py --interactive                                    # 多轮对话（带记忆）
python3 main.py "问题" --policy always --max-steps 5              # 换策略 / 调上限
python3 main.py --selftest                                        # 离线自测（不联网，不用 key）
```

`--selftest` 有 54 条断言，全部用桩（桩 Planner / 桩 Executor / 桩 Replanner），
不联网、不用 key，改完代码先跑它。

> 安全：`keys.py` 已在 [.gitignore](.gitignore) 中忽略、不会进 git。若曾把真实 key 填进文件并外传过，请到 DeepSeek / Tavily 控制台轮换重置。

### 什么时候该用它

**目标明确、能拆成子任务、步骤可预测 → 用 PS；目标模糊、中间结果会改变下一步 → 用 ReAct。**

| 维度 | ReAct（摸着石头过河） | Plan-and-Solve（先算好再动） |
|---|---|---|
| 决策频率 | **每一步**都调大模型 | 计划一次，执行时按计划走 |
| 成本 | 随步数线性增长 | 规划贵、执行便宜（约省 30–60% token） |
| 长程任务的稳定性 | 弱：会**推理漂移**，跑着跑着忘了目标 | 强：目标固定在计划里 |
| 探索性任务的适应性 | 强：每步都重新判断 | 弱：计划可能**过期** |
| 可审计性 | 只有逐步 trace | **开局就有一份明确的计划**，可人工审 |
| 典型适用 | 「帮我找个我可能喜欢的开源项目」 | 「调研 X 并写一份报告」、「多步数据管道」 |

**说清楚边界**：简单任务（「今天几号」）走一遍 Planner → Executor → Replanner
是纯浪费，PS 架构在简单任务上天然亏本。这不是本项目要解决的问题——它是
「该用哪个 agent」的问题，不是「PS 怎么实现」的问题。见 MANUAL §已知坑。

### 怎么跑的

三个部件，各自一次模型调用，职责完全不同：

```
PlanSolveAgent
  ├─ Planner    ── 一次调用，问题 → 计划           （不执行、不看工具返回）
  ├─ Executor   ── 一个 ReAct 循环，完成「一步」    ← 搬自 ReAct 版
  └─ Replanner  ── 一次调用，审视进度 → 收工 / 换计划
```

```
                       question
                          │
                    ┌─────▼─────┐
                    │  Planner  │  一次调用，产出初始计划
                    └─────┬─────┘
              ┌───────────▼───────────┐
              │  on_plan_ready 钩子    │  ← 人工审核 / 自动修剪计划在这里
              └───────────┬───────────┘
        ┌─────────────────▼─────────────────┐
        │  while cursor < len(plan):        │
        │    outcome = Executor.execute(step)│ ← 内部是 ReAct 循环
        │    if should_replan(state):        │ ← always/on_failure/every_n/never
        │        r = Replanner.review(state) │
        │        r.kind == "final" → 收工    │
        │        r.kind == "plan"  → 换计划  │
        └─────────────────┬─────────────────┘
                          │ 计划跑完但没人说收工
                          ▼
              强制问一次 Replanner 要最终答案 → final answer
```

### 协议

**Planner 输出**（一次调用，只列步骤）：

```
1. 用 search 查「2024 年中国新能源汽车销量」
2. 用 search 查「比亚迪 2024 年销量」
3. 把两步结果整理成 300 字摘要
```

**Replanner 输出**（三选一）：

```
Final Answer: 给用户的完整回答          ← 收工
```
```
1. 新的第一步
2. 新的第二步                          ← 换计划（只列剩下的，不列已完成的）
```
```
1. 剩下那一步（原样再列一遍）           ← 照旧
```

编号一律**按位置重编**（`Plan.from_descs`），模型给什么编号都不算数——所以
「跳号、重号、从 0 开始」这些情况有唯一确定的处理方式，不靠运气。

格式说明和解析逻辑打包在同一个协议对象里（[core/plan_protocol.py](core/plan_protocol.py)），
所以换协议时格式说明和 parser 不会脱节。**Planner 和 Replanner 共用同一个
`parse`，各写各的 `format_*`**——它们输出的是同一种东西（一份计划或一个终局答案），
只是问法不同。

### 结构一览

```
core/                    内核：分两组（本项目真的有两个嵌套的循环）
  plan.py                  Plan / PlanStep / StepOutcome / ExecutionState
  plan_protocol.py         PlanProtocol：计划的格式说明 + 解析
  ps_loop.py               PlanSolveLoop 骨架 + 五个钩子   ← 外层：单位是「计划步骤」
  protocol.py              协议契约 + 默认文本协议（搬自 ReAct 版，一字未动）
  react_loop.py            ReActLoop + 三个钩子（原 core/loop.py，只改了文件名）
                                                          ← 内层：单位是 thought/action
planner.py                 Planner：问题 → 计划
executor.py                Executor：一步 → 结果（默认包一个 ReAct 循环）
replanner.py               Replanner：执行状态 → 新计划 / 最终答案
persona.py                 两套人格：ReActPersona（给 Executor）+ PlanSolvePersona
agent.py                   门面：把 persona 的声明变成三个部件的实例
llm.py                     LLM 基类 + DeepSeekLLM + 密钥加载（搬自 ReAct 版）
tools.py                   Tool 基类 + SearchTool + 注册表工厂（搬自 ReAct 版）
memory.py                  Memory 基类 + WindowMemory（搬自 ReAct 版）
main.py                    CLI + 离线自测
```

内核和外围的分界线是**耦合度**：`core/` 里放的是「接口必须一起改」的东西
（两个循环各和自己的协议、自己的状态强耦合）；LLM / 工具 / 记忆 / 人格跟循环
只有松耦合，各自待在根目录自己的文件里。

`core/` 里有**两个循环**不是内核变厚了，是问题本身有两层：

```
PlanSolveLoop  ← 外层：按计划走，单位是「计划步骤」，吃 PlanStep 吐 StepOutcome
   └─ ReActLoop  ← 内层：完成一步，单位是「thought/action」，吃 question 吐 answer
```

### 文档

| 文件 | 内容 |
| --- | --- |
| 本文件 | 定位、适用边界、快速开始、控制流、协议、结构一览 |
| [MANUAL.md](MANUAL.md) | 操作手册：改哪里、五个钩子、升级路径、已知坑与设计取舍 |
