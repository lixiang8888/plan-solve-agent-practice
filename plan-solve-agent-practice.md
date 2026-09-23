# Plan-and-Solve Agent 实践 —— 设计蓝图

> **状态：蓝图。还没有代码。** 这份文档定义目录结构、模块职责、关键接口与实施顺序，
> 照着它可以直接把项目写出来。
>
> 姊妹篇：[react agent 的 MANUAL.md](../react%20agent/MANUAL.md)（ReAct 版的操作手册）。
> 本项目**沿用它的分层哲学**（薄内核 + 根目录模块、边界按耦合度切、无全局状态），
> 但把循环从"思考-行动"换成"先规划-再执行"。

---

## 目录

- [0. 决策速览](#0-决策速览)
- [1. 为什么要有这一篇](#1-为什么要有这一篇)
- [2. 三个部件](#2-三个部件)
- [3. 数据结构](#3-数据结构)
- [4. 控制流](#4-控制流)
- [5. 目录结构](#5-目录结构)
- [6. 关键接口](#6-关键接口)
- [7. 人格与可配项](#7-人格与可配项)
- [8. 五个钩子](#8-五个钩子)
- [9. 从 ReAct 版搬什么](#9-从-react-版搬什么)
- [10. 实施顺序](#10-实施顺序)
- [11. 验收方式](#11-验收方式)
- [12. 已知坑与设计取舍](#12-已知坑与设计取舍)
- [13. 升级路径](#13-升级路径)
- [参考](#参考)

---

## 0. 决策速览

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 项目定位 | 与 ReAct 版同级的**模板母本**，复制出去再改 |
| 2 | 分层策略 | 沿用：薄内核 + 根目录模块，边界按耦合度切 |
| 3 | 内核边界 | `core/` 分**两组**：PS 内核（本项目主角）+ ReAct 引擎（服务 Executor） |
| 4 | 三大部件 | Planner / Executor / Replanner，各自一个文件，都是可替换接缝 |
| 5 | Executor 默认实现 | **包一个 ReAct 循环**（Plan-and-Execute 的标准做法） |
| 6 | Planner 与 Replanner | 共用同一个 `PlanProtocol`（输出的都是"计划"），提示词各写各的 |
| 7 | 重规划策略 | **可配**：`always` / `on_failure` / `every_n` / `never`，默认 `on_failure` |
| 8 | 收尾方式 | 由 Replanner 判定（`final` 分支），不额外做"拼接所有结果" |
| 9 | 全局状态 | **零**。与 ReAct 版同标准——多 agent 并存的前提 |
| 10 | 依赖 | 仍是 `requests` 一个，不新增 |
| 11 | 测试 | 离线自测（桩 Planner/Executor/Replanner），沿用 `--selftest` 形式 |
| 12 | 文档 | README（定位）+ MANUAL（操作手册），沿用 ReAct 版的两篇制 |

---

## 1. 为什么要有这一篇

ReAct 版已经能跑，为什么还要做 Plan-and-Solve 版？**因为它俩擅长的任务不一样，
而且这个差异是可验证的工程差异，不是风格偏好。**

| 维度 | ReAct（摸着石头过河） | Plan-and-Solve（先算好再动） |
|---|---|---|
| 决策频率 | **每一步**都调大模型 | 计划一次，执行时按计划走 |
| 成本 | 随步数线性增长 | 规划贵、执行便宜（约省 30–60% token） |
| 长程任务的稳定性 | 弱：会**推理漂移**，跑着跑着忘了目标 | 强：目标固定在计划里 |
| 探索性任务的适应性 | 强：每步都重新判断 | 弱：计划可能**过期** |
| 可审计性 | 只有逐步 trace | **开局就有一份明确的计划**，可人工审 |
| 典型适用 | "帮我找个我可能喜欢的开源项目" | "调研 X 并写一份报告"、"多步数据管道" |

一句话：**目标明确、能拆成子任务、步骤可预测 → 用 PS；目标模糊、中间结果会改变
下一步 → 用 ReAct。**

### 最关键的工程洞察

**Plan-and-Execute 的 Executor，标准做法就是一个 ReAct agent。**

也就是说这个项目**不是从零开始**——它是"在 ReAct 内核外面套一层规划层"：

```
PlanSolveAgent
  ├─ Planner    ── 一次大模型调用，产出计划
  ├─ Executor   ── 一个 ReAct agent，负责完成「一步」      ← ReAct 版直接复用
  └─ Replanner  ── 一次大模型调用，判断继续/重规划/收尾
```

这同时是 ReAct 版"模板母本"定位的**第一次实战检验**：如果那套分层真的可复用，
这个项目的一大半代码应该是搬过来的，而不是重写的。**如果实施时发现搬不动，
说明 ReAct 版的分层有问题，应该回头修母本，而不是在这里打补丁。**

---

## 2. 三个部件

三个部件形式相同（都是"调一次 LLM、解析出结构化结果"），但职责完全不同，
所以是三个独立的可替换接缝。

### Planner —— 把问题拆成计划

```
输入：用户问题（+ 记忆里的历史）
输出：Plan(steps=[...])            一次调用，只调一次
```

**不做的事**：不执行、不看工具返回。它的全部工作就是"看一眼问题，列出步骤"。

### Executor —— 完成计划里的一步

```
输入：当前步骤 + 之前步骤的结果（ExecutionState）
输出：StepOutcome(ok, text)        可能内部调很多次工具
```

**默认实现**：包一个 ReAct 循环。把"完成这一步"当成一个子问题丢给 ReAct——
这是它和 Planner 的分工：Planner 负责**拆**，ReAct 负责**做**。

**不做的事**：不知道整个计划长什么样，也不知道后面还有什么步骤。
它只看到"我之前做了什么"和"现在要做什么"。这个信息隔离是刻意的——
让 Executor 专注当前步，避免它擅自跳步。

### Replanner —— 审视进度，决定下一步

```
输入：完整 ExecutionState（问题 + 当前计划 + 已完成的步骤及结果）
输出：PlanResult(kind="final" | "plan" | ...)
      kind="final" → 信息够了，这是最终答案，收工
      kind="plan"  → 剩下的步骤改成这样
```

**这是 Plan-and-Solve 相对原始 PS 提示法的关键增量**：原始 PS 没有反馈回路，
计划错了就一路错到底。Replanner 就是那个恢复机制。

---

## 3. 数据结构

这是整个项目的核心——**PS 与 ReAct 最大的结构差异，是 PS 有一个跨步骤的状态**。
ReAct 的"状态"就是 messages；PS 必须显式维护"计划 + 走到哪了 + 每步结果"。

```python
# core/plan.py

@dataclass
class PlanStep:
    """计划里的一步。desc 是给 Executor 看的自然语言描述。"""
    desc: str
    id: int = 0                     # 从 1 开始，供 prompt 里编号引用

@dataclass
class Plan:
    steps: list[PlanStep]

@dataclass
class StepOutcome:
    """Executor 执行完一步的结果。"""
    step_id: int
    desc: str
    ok: bool                        # Replanner 的策略（on_failure）依赖它
    text: str                       # 这一步产出了什么（会被喂给后续步骤）
    steps_used: int = 0             # 这一步内部跑了几轮 ReAct（成本观测）

@dataclass
class ExecutionState:
    """跨步骤的全部状态。PS 循环的 LoopContext 就是它。"""
    question: str
    plan: list[PlanStep]
    cursor: int                     # 下一个要执行的步骤下标
    results: list[StepOutcome]      # 已完成步骤的结果，按序
    replans: int = 0                # 重规划了几次（防死循环）
    stop: bool = False

    # ---- 给钩子和 Executor 用的便利方法 ----
    def done(self) -> list[PlanStep]:    return self.plan[:self.cursor]
    def todo(self) -> list[PlanStep]:    return self.plan[self.cursor:]
    def summary(self) -> str:
        """把「已完成的步骤及结果」渲染成一段文本，喂给 Executor / Replanner。"""
```

```python
# core/plan_protocol.py

@dataclass
class PlanResult:
    """PlanProtocol.parse() 的返回值。"""
    kind: str                       # "plan" | "final" | "malformed"
    steps: list[PlanStep] = field(default_factory=list)
    final: str | None = None        # kind == "final" 时：给用户的最终答案
    error: str | None = None        # kind == "malformed" 时：喂回给模型的纠正提示
```

**为什么 `StepOutcome` 要有 `ok`**：`on_failure` 重规划策略靠它决定要不要叫 Replanner。
没有它，"失败"就只能靠字符串匹配判断，那是脆的。

**为什么 `ExecutionState` 要有 `replans` 计数器**：Replanner 在嘈杂环境下会
**几乎每步都触发**。没有硬上限就是死循环。

---

## 4. 控制流

```
                       question
                          │
                    ┌─────▼─────┐
                    │  Planner  │  一次调用，产出初始计划
                    └─────┬─────┘
                          │  Plan
              ┌───────────▼───────────┐
              │  on_plan_ready 钩子    │  ← 人工审核 / 自动修剪计划在这里
              └───────────┬───────────┘
                          │
        ┌─────────────────▼─────────────────┐
        │  while cursor < len(plan):        │
        │    step = plan[cursor]            │
        │    outcome = Executor.execute(step)│  ← 内部是 ReAct 循环
        │    state.results.append(outcome)  │
        │    cursor += 1                    │
        │                                   │
        │    if should_replan(state):       │  ← 策略：always/on_failure/every_n
        │        r = Replanner.review(state)│
        │        if r.kind == "final":      │
        │            return r.final  ───────┼──→ 收工
        │        if r.kind == "plan":       │
        │            state.plan = r.steps   │  ← 换掉剩余计划
        │            state.replans += 1     │
        └─────────────────┬─────────────────┘
                          │ 计划跑完但没人说收工
                          ▼
              ┌───────────────────────┐
              │ 收尾：强制问一次       │
              │ Replanner 要最终答案   │
              └───────────┬───────────┘
                          ▼
                      final answer
```

### 三个必须写在循环里的硬上限

| 上限 | 防的是 |
|---|---|
| `max_steps` | 计划本身太长 / Executor 每步都在跑 |
| `max_replans` | Replanner 每步都触发，无限重规划 |
| `max_react_steps` | 单步内部 ReAct 跑飞（透传给 Executor） |

任何一个触顶，都要走**兜底返回**（把已有结果拼成一段"未完成"的说明），
而不是抛异常——和 ReAct 版的失败兜底同一个原则。

---

## 5. 目录结构

```
core/                         内核：分两组
  # —— PS 内核（本项目的主角，与 PS 循环强耦合）——
  plan.py                       Plan / PlanStep / StepOutcome / ExecutionState
  plan_protocol.py              PlanProtocol：计划的格式说明 + 解析
  ps_loop.py                    PlanSolveLoop 骨架 + 五个钩子
  # —— ReAct 引擎（服务 Executor，从 ReAct 版整体搬来）——
  protocol.py                   Protocol / StepResult / TextReActProtocol
  react_loop.py                 ReActLoop + 三个钩子
planner.py                    Planner：问题 → 计划
executor.py                   Executor：一步 → 结果（默认包一个 ReAct 循环）
replanner.py                  Replanner：执行状态 → 新计划 / 最终答案
persona.py                    PlanSolvePersona（内含 executor_persona）
agent.py                      PlanSolveAgent 门面
llm.py                        ← 从 ReAct 版原样搬
tools.py                      ← 从 ReAct 版原样搬
memory.py                     ← 从 ReAct 版原样搬
main.py                       CLI + 离线自测
README.md / MANUAL.md         两篇制文档
```

### `core/` 里为什么有两个循环

因为**这个项目真的有两个嵌套的循环**，而且它们跑在不同的层级上：

```
PlanSolveLoop  ← 外层：按计划走，单位是「计划步骤」
   └─ ReActLoop  ← 内层：完成一步，单位是「thought/action」
```

这两个循环的契约不同（外层吃 `PlanStep`、吐 `StepOutcome`；内层吃 `question`、
吐 `answer`），所以必须各自成模块。**这不是"内核变厚了"，是问题本身有两层。**

`react_loop.py` 相对 ReAct 版的 `core/loop.py` 只是**改了文件名**（内容一字不动），
目的是让 `core/` 里两个循环平级、一眼看得出分工。改名是为了可读性，不是为了藏差异。

---

## 6. 关键接口

### Planner

```python
# planner.py
class Planner:
    """一次调用，把问题拆成计划。不执行、不看工具结果。"""

    def __init__(self, llm, protocol: PlanProtocol, max_steps: int = 8):
        self.llm = llm
        self.protocol = protocol
        self.max_steps = max_steps        # 计划最多几步，防「过度规划」

    def make_plan(self, question: str, history: list | None = None) -> PlanResult:
        messages = [
            {"role": "system", "content": self.protocol.format_plan_instructions(self.max_steps)},
            {"role": "user", "content": question},
        ]
        if history:
            messages[1:1] = list(history)
        reply = (self.llm(messages) or "").strip()
        return self.protocol.parse(reply)
```

**注意**：Planner 和 Replanner 都不做重试纠错。格式错了就返回 `malformed`，
由外层循环决定怎么办（重问一次 or 兜底）。这和 ReAct 循环里那套"格式错误喂回重写"
的处理放在**同一个地方**（PS 循环），不散落到各部件里。

### Executor

```python
# executor.py
class Executor:
    """完成计划里的一步。默认实现：包一个 ReAct 循环。"""

    def __init__(self, llm, tools: dict, protocol: Protocol,
                 system_prompt: str, max_react_steps: int = 6, verbose: bool = True):
        self.loop = ReActLoop(llm=llm, tools=tools, protocol=protocol,
                              system_prompt=system_prompt,
                              max_steps=max_react_steps, verbose=verbose)

    def execute(self, step: PlanStep, state: ExecutionState) -> StepOutcome:
        prompt = self._build_step_prompt(step, state)
        answer, trace = self.loop.run(prompt)
        return StepOutcome(
            step_id=step.id, desc=step.desc,
            ok=self._judge(answer, trace),      # ← 判定"这一步成功了没"
            text=answer or "(这一步没有产出)",
            steps_used=len([t for t in trace if t.get("type") == "action"]),
        )
```

`_build_step_prompt` 是 **Executor 的信息隔离点**，它只给模型三样东西：

```
总任务：{state.question}
已完成：{state.summary()}
现在要做：第 {step.id} 步 —— {step.desc}
```

**不给它看后面的步骤**。这个隔离是刻意的：看到全计划，Executor 会忍不住跳步。

`_judge` 判定这一步成没成。默认实现可以很简单（比如答案非空且不以 `⚠` 开头），
但**必须是独立方法**，因为它是 `on_failure` 策略的唯一输入。

### Replanner

```python
# replanner.py
class Replanner:
    """审视进度，决定：收工 / 换计划。"""

    def __init__(self, llm, protocol: PlanProtocol, max_steps: int = 8):
        ...

    def review(self, state: ExecutionState) -> PlanResult:
        messages = [
            {"role": "system", "content": self.protocol.format_replan_instructions()},
            {"role": "user", "content": self._render_state(state)},   # 问题+计划+已完成结果
        ]
        reply = (self.llm(messages) or "").strip()
        return self.protocol.parse(reply)
```

### PlanProtocol —— 成对打包（沿用 ReAct 版的教训）

```python
# core/plan_protocol.py
class PlanProtocol:
    """计划的格式说明 + 解析器。两者必须一致，所以放同一个对象里。

    参考 ReAct 版 core/protocol.py 的 TextReActProtocol —— 那是这个模式的样板。
    """

    def format_plan_instructions(self, max_steps: int) -> str:
        """给 Planner：怎么列计划。"""

    def format_replan_instructions(self) -> str:
        """给 Replanner：怎么在『继续 / 换计划 / 收尾』之间选。"""

    def parse(self, text: str) -> PlanResult:
        """两个部件共用。识别编号步骤列表，以及可选的 Final Answer。"""
```

**Planner 和 Replanner 共用 `parse`，各写各的 `format_*`**——因为它们输出的
是同一种东西（一份计划 / 一个终局答案），只是问法不同。这是"成对打包"原则
在有两个提问方时的自然延伸：**格式说明可以有多个，解析器只能有一个。**

### PlanSolveLoop

```python
# core/ps_loop.py
class PlanSolveLoop:
    def __init__(self, planner, executor, replanner, replan_policy: str = "on_failure",
                 max_steps: int = 8, max_replans: int = 3, verbose: bool = True): ...

    # ---- 五个钩子，默认空实现 ----
    def on_plan_ready(self, ctx): ...                       # 计划刚出，可改可拒
    def before_execute(self, ctx, step) -> str | None: ...  # 能否决这一步
    def on_step_end(self, ctx, outcome): ...                # 一步之后
    def should_replan(self, ctx) -> bool: ...               # 覆盖静态策略
    def on_replan(self, ctx, result): ...                   # 重规划之后

    def run(self, question, history=None) -> tuple[str | None, list]: ...
```

`trace` 的形状（供展示与测试）：

```python
{"type": "plan",   "steps": [...]}
{"type": "step",   "step_id": 1, "desc": "...", "ok": True,
                   "outcome": "...", "react_trace": [...]}
{"type": "replan", "replans": 1, "steps": [...]}      # 或 {"type":"replan", "final": "..."}
{"type": "final",  "answer": "..."}
{"type": "fallback", "answer": "⚠ ..."}                # 触顶兜底
```

---

## 7. 人格与可配项

```python
# persona.py
@dataclass
class PlanSolvePersona:
    name: str
    # —— 规划层 ——
    planner_llm: LLM                    # 建议用强模型：这是全局决策
    executor_persona: ReActPersona      # ← 直接复用 ReAct 版的 Persona！
    replanner_llm: LLM                  # 可与 planner_llm 不同
    plan_protocol: PlanProtocol = PlanProtocol()
    # —— 策略 ——
    replan_policy: str = "on_failure"   # always | on_failure | every_n | never
    replan_every: int = 3               # policy == "every_n" 时生效
    max_steps: int = 8                  # 计划最多几步
    max_replans: int = 3
    max_react_steps: int = 6            # 单步内部的 ReAct 上限
```

### `replan_policy` 是这个项目最重要的可配项

| 策略 | 行为 | 成本 | 适用 |
|---|---|---|---|
| `always` | 每步后都问 Replanner | 最高（N+1 次大模型调用） | 环境嘈杂、计划容易过期 |
| `on_failure` | 只在某步失败时问 | 低 | **默认**。多数任务的性价比拐点 |
| `every_n` | 每 N 步问一次 | 中 | 长计划、想要周期性检查 |
| `never` | 从不问，计划跑完直接收尾 | 最低 | 纯 PS 提示法、任务简单确定 |

**这个可配项就是"原始 Plan-and-Solve 提示法"和"Plan-and-Execute 工程架构"之间的
连续调节旋钮**——`never` 那端是论文里的 PS，`always` 那端是 LangGraph 的标准形态。

### `executor_persona` 复用 ReAct 版的 `Persona`

这是两层设计的好处：**Executor 的人格本来就是一个 ReAct 人格**（它有什么工具、
什么角色、什么输出协议），原样拿来就能用。不需要为 Executor 发明新的人格结构。

---

## 8. 五个钩子

沿用 ReAct 版"固定骨架 + 钩子"的做法，钩子深度同样做到**否决级**。

```
① on_plan_ready(ctx)              计划刚出来。可以改、可以拒（置 ctx.stop）
                                   ← 人工审核计划就在这里实现
② before_execute(ctx, step)       执行一步之前。返回字符串 = 跳过执行，
                                   拿它当 outcome（沿用 ReAct 版 before_tool 的语义）
③ on_step_end(ctx, outcome)       一步完成。埋点、日志
④ should_replan(ctx) -> bool      动态决定要不要调 Replanner，
                                   覆盖 persona 里的静态 replan_policy
⑤ on_replan(ctx, result)          重规划之后。可以在这里记录/拦截换计划
```

**`before_execute` 的语义刻意和 ReAct 版的 `before_tool` 保持一致**：
`None` = 照常执行；返回字符串 = 跳过，拿它当结果；抛异常 = 拦截，异常信息当结果。
**同一个模式在两个项目里复用，复制出去的人不用重新学。**

钩子能碰到的是 `ExecutionState`（见 §3），它同时充当 `LoopContext` 的角色。
如果要给钩子更多信息，就往 `ExecutionState` 上加字段——和 ReAct 版
往 `LoopContext` 上加字段是同一个做法。

---

## 9. 从 ReAct 版搬什么

| ReAct 版的文件 | 搬到哪 | 改动 |
|---|---|---|
| `core/protocol.py` | `core/protocol.py` | **一字不动**（Executor 和 PS 都可能用到） |
| `core/loop.py` | `core/react_loop.py` | **只改文件名** |
| `llm.py` | `llm.py` | 一字不动 |
| `tools.py` | `tools.py` | 一字不动 |
| `memory.py` | `memory.py` | 一字不动 |
| `persona.py` 的 `Persona` | `persona.py` 的 `executor_persona` 字段 | 结构照用，作为 Executor 的人格 |
| `agent.py` 的 `ReActAgent` | **不搬** | Executor 直接用 `ReActLoop`（纯内核），不用门面——步骤间上下文由 `ExecutionState` 提供，不需要门面那套自动记忆 |
| `main.py` 的 `--selftest` 框架 | `main.py` | 照搬 `check(name, cond)` 的结构 |

**能搬这么多，是因为 ReAct 版当初就是按"复制出去再改"设计的。**
如果实施时发现某个文件搬不动（比如又冒出全局状态），**回头修 ReAct 母本**，
不要在这边打补丁——那会让两个仓库慢慢分叉，母本失去意义。

---

## 10. 实施顺序

依赖方向：契约 → 部件 → 循环 → 门面 → CLI。**先把搬的搬完，再写新的。**

1. **建目录，搬 ReAct 件**：`core/protocol.py`、`core/react_loop.py`（改名）、
   `llm.py`、`tools.py`、`memory.py`。搬完先跑一遍 ReAct 版的自测确认没搬坏。
2. `core/plan.py`：`PlanStep` / `Plan` / `StepOutcome` / `ExecutionState`
3. `core/plan_protocol.py`：`PlanResult` + `PlanProtocol`（先只写 `format_plan_instructions` + `parse`）
4. `planner.py`：能根据一个问题产出计划
5. `executor.py`：**先写一个不调工具的桩 Executor**（直接返回"做完了"），
   把外层循环跑通，再换成真 ReAct 版
6. `replanner.py`：`format_replan_instructions` + `review`
7. `core/ps_loop.py`：主循环 + 五个钩子 + 三个硬上限
8. `persona.py` / `agent.py`：组装
9. `main.py`：CLI + `--selftest`
10. `README.md` + `MANUAL.md`

**第 5 步的顺序是刻意的**：先用桩 Executor 把 PS 循环本身跑通，
这样一旦出问题，你能确定是循环的问题还是 ReAct 的问题——不要让两层循环同时调试。

---

## 11. 验收方式

沿用 ReAct 版的 `python3 main.py --selftest` 形式（无新依赖、不用 key）。
**桩 Planner / 桩 Executor / 桩 Replanner**，断言行为而非文本。

必须覆盖的：

**契约与解析**
- `PlanProtocol.parse` 能拆出编号步骤列表
- `parse` 能识别 `Final Answer` 分支 → `kind == "final"`
- 无编号、无 Final → `kind == "malformed"`
- 编号不连续 / 重复编号的处理是确定的（不靠运气）

**控制流**
- 计划被完整执行（桩 Executor 收到全部步骤，顺序正确）
- Replanner 说 `final` → 循环立刻收工，**剩余步骤不再执行**
- Replanner 给新计划 → 后续步骤换成新的
- 计划跑完但没人说 final → 强制问一次 Replanner 要收尾

**四处硬上限（每条都要有独立断言）**
- `max_steps`：计划超长被截断
- `max_replans`：Replanner 一直说"再改改"时能停下来
- 步数耗尽 / 重规划耗尽 → 走兜底返回，**不抛异常**

**四种 replan 策略**
- `always`：N 步 → N 次 review
- `on_failure`：全成功 → 0 次 review；有失败 → 触发
- `every_n`：步数对得上
- `never`：0 次 review

**换部件不碰内核**（对应 ReAct 版的"换人格不碰内核"）
- 两个不同的 `PlanSolvePersona` 同进程并存，工具调用与状态互不串
- 换 `replan_policy` 只改 persona，`core/` 不动
- 换 Executor 实现（桩 → 真 ReAct）只改构造参数，`core/` 不动
- **没有模块级全局状态**（`not hasattr(tools_mod, "TOOL_REGISTRY")` 那类断言）

**钩子**
- `on_plan_ready` 能改计划（改完后续按新计划走）
- `on_plan_ready` 里 `ctx.stop` 能中止
- `before_execute` 返回字符串 → 真 Executor 未被调用（沿用 ReAct 版那条断言的做法）
- `should_replan` 为 True 时，**即使策略是 `never` 也会触发 review**

---

## 12. 已知坑与设计取舍

按"这个坑有多容易踩到"排序。

### 12.1 步粒度（最容易踩，且没有银弹）

计划步太粗（"调研市场"）→ Executor 不知道从哪下手，等于没规划；
太细（"打开浏览器"）→ 计划本身就快成答案了，Executor 沦为复述机。

**应对**：① Planner 提示词里明确要求"每一步是一个可以用工具完成的**具体动作**"；
② `max_steps` 上限防过度规划；③ 自测里断言计划步数落在合理区间。
**但要说清楚：这最终是靠提示词工程调的，不是靠代码解决的。**

### 12.2 计划过期（plan brittleness）

初始计划是基于"当时以为的情况"定的。第 2 步的结果可能让第 5 步变得毫无意义。

**应对**：Replanner + `replan_policy`。**默认给 `on_failure` 而不是 `never`**——
纯 PS 没有恢复机制，一个错计划会一路错到底。

### 12.3 Replan 成本失控

Replanner 在嘈杂环境下会**几乎每步都触发**，每个 replan 都是一次大模型调用。
文档里明确记载了"小模型当 Executor 会导致频繁重规划"。

**应对**：`max_replans` 硬上限 + 默认 `on_failure`（而不是 `always`）+
在 trace 里记录 `replans` 次数让人看得见成本。

### 12.4 信息传递丢数据

步骤结果用一个字符串传给后续步骤，太长要截断，截断就丢关键数据。
这是被记录过的真实失败模式。

**应对**：`StepOutcome.text` 由 **Executor 自己决定**写什么（它最清楚什么重要），
而不是由外层统一截断。蓝图阶段先只留 `text` 一个字段；
如果实践中发现丢数据，再扩 `artifacts: dict` 存结构化产物（见 §13）。

### 12.5 简单任务上过度规划

"今天几号"这种问题走一遍 Planner → Executor → Replanner 是纯浪费。
PS 架构在简单任务上天然亏本。

**应对**：这一条**不在本项目解决**——它是"该用哪个 agent"的问题，不是"PS 怎么实现"
的问题。README 里要写清楚适用边界（见 §1 那张表）。

### 12.6 无并行（与 ReAct 版同病）

计划是个列表，顺序执行。真正的并行需要 DAG 调度（LLMCompiler 那条路线）。

**应对**：不做，但在 MANUAL 里写清升级路径（见 §13.2），
和 ReAct 版对"并行工具"的处理方式保持一致。

### 12.7 Executor 与 Replanner 的职责重叠

"这一步做得够不够好" 由 Executor 的 `_judge` 判定，"要不要换计划"由 Replanner 判定。
两者边界模糊时，会出现"Executor 说自己成了、Replanner 说不行"的反复。

**应对**：明确分工——**`_judge` 只看这一步自己的产出，Replanner 才看全局**。
`_judge` 的实现必须简单（非空、不含兜底标记），不要让它做全局判断。

---

## 13. 升级路径

### 13.1 人工审核计划

`on_plan_ready` 钩子就是为此留的：

```python
class HumanReviewLoop(PlanSolveLoop):
    def on_plan_ready(self, ctx):
        print("计划：")
        for s in ctx.plan:
            print(f"  {s.id}. {s.desc}")
        if input("执行吗？(y/n/edit) ") != "y":
            ctx.plan = 手动改过的计划
```

### 13.2 并行执行计划步骤

需要三处一起改（和 ReAct 版并行工具的思路一致）：

1. `PlanStep` 加 `depends_on: list[int]`；
2. `Plan` 从"列表"升级为"有向图"，加拓扑排序；
3. `PlanSolveLoop` 把"取 `plan[cursor]`"换成"取所有依赖已满足的步骤"，
   用线程池并发跑 `Executor.execute`。

**注意**：并发跑多个 Executor 时，每个 Executor 内部的 ReAct 循环必须**互相独立**
（各自的 tools 字典、各自的 trace）。这正好是"无全局状态"这个约束在还债。

### 13.3 计划持久化 / 断点续跑

`ExecutionState` 是个普通 dataclass，序列化它就是断点。
存盘 → 进程重启 → 读回来接着跑，不需要动内核。

### 13.4 结构化产物传递

`StepOutcome` 扩一个 `artifacts: dict`，让 Executor 除了自然语言摘要还能交出
结构化数据（表格、代码、文件路径），后续步骤按需取用而不是去解析摘要文本。
这是 §12.4 那个坑的正解。

### 13.5 换掉 Executor 引擎

Executor 是接缝，默认包 ReAct，但不一定非得是 ReAct：

- **单次 LLM 调用**：步骤简单、不需要工具时，最便宜
- **另一个 PlanSolveAgent**：递归分解，处理"某一层还是太复杂"的情况
- **直接调工具**：步骤本身就是确定的工具调用时，跳过 LLM

换法都是"传一个自己实现的 `Executor` 进去"，`core/` 不动。

---

## 参考

- Wang et al., *Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought
  Reasoning by Large Language Models* (ACL 2023) —— PS / PS+ 提示法的原始论文
  <https://arxiv.org/abs/2305.04091>
- [Agent Series (3): Plan-and-Solve — Think First, Then Act](https://dev.to/wonderlab/agent-series-3-plan-and-solve-think-first-then-act-1e14)
- [Plan-and-Execute: Separating Planning from Execution](https://blckalpaca.at/en/knowledge-base/ai-agents/agent-architectures-overview/plan-and-execute-architektur)
- [Agent Plan 完全指南：Plan-and-Execute、ReWOO、LLMCompiler 深度解析](https://segmentfault.com/a/1190000047737848)
- [plan_and_solve.md — Interview_Agentic_AI](https://github.com/BrendanJamesLynskey/Interview_Agentic_AI/blob/dc151e0edcab8b0362b9269f88228b5fceb4880a/02_reasoning_and_planning/plan_and_solve.md)

姊妹项目：[react agent](../react%20agent/)（ReAct 版，含 [README](../react%20agent/README.md)
与 [MANUAL](../react%20agent/MANUAL.md)）
