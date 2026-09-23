# 操作手册

> 本文是 [README.md](README.md) 的展开：**想改什么、动哪个文件、怎么加东西**。
> 只想知道怎么跑、什么时候该用它，看 README 就够了。

## 目录

- [目录结构与职责](#目录结构与职责)
- [想改 X，就动 Y](#想改-x就动-y)
- [两层设计：门面 vs 内核](#两层设计门面-vs-内核)
- [三个部件](#三个部件)
- [五个钩子](#五个钩子)
- [replan_policy：这个项目最重要的可配项](#replan_policy这个项目最重要的可配项)
- [三个硬上限与终止性](#三个硬上限与终止性)
- [升级路径](#升级路径)
- [离线自测](#离线自测)
- [已知坑与设计取舍](#已知坑与设计取舍)
- [和 ReAct 版的对照](#和-react-版的对照)

---

## 目录结构与职责

```
core/                       内核：与主循环强耦合的东西，分两组
  # —— PS 内核（本项目的主角）——
  plan.py                     Plan / PlanStep / StepOutcome / ExecutionState
  plan_protocol.py            PlanResult + PlanProtocol（格式说明 + 解析）
  ps_loop.py                  PlanSolveLoop 骨架 + 五个钩子
  # —— ReAct 引擎（服务 Executor，从 ReAct 版整体搬来）——
  protocol.py                 Protocol / StepResult / TextReActProtocol
  react_loop.py               ReActLoop + 三个钩子（原 core/loop.py，只改文件名）
planner.py                   Planner：问题 → 计划
executor.py                  Executor：一步 → 结果（默认包一个 ReAct 循环）
replanner.py                 Replanner：执行状态 → 新计划 / 最终答案
persona.py                   ReActPersona（Executor 的人格）+ PlanSolvePersona
agent.py                     门面：组装三个部件 + 记忆
llm.py                       LLM 基类 + DeepSeekLLM + 密钥加载
tools.py                     Tool 基类 + SearchTool + 注册表工厂
memory.py                    Memory 基类 + WindowMemory + NoMemory
main.py                      CLI + 离线自测
```

**边界是按耦合度切的，不是按文件数。** `ps_loop` 通过 `executor` 这个接缝用内层——
它不认识 `ReActLoop`，只认识「有个东西能吃 step 吐 outcome」。而 `protocol.parse()`
的返回值和循环的下一步动作是绑死的，两者必须一起改，所以都在 `core/`。

## 想改 X，就动 Y

| 想做什么 | 动哪个文件 |
| --- | --- |
| 换角色、语气、用哪些工具、Executor 用哪个模型 | [persona.py](persona.py) —— `executor_persona` |
| 换重规划策略（`always` / `on_failure` / `every_n` / `never`） | [persona.py](persona.py) —— `replan_policy`，**内核不动** |
| 调计划步数 / 重规划次数 / 单步 ReAct 上限 | [persona.py](persona.py) —— `max_steps` / `max_replans` / `max_react_steps` |
| 换规划层用的模型（Planner / Replanner） | [persona.py](persona.py) —— `planner_llm` / `replanner_llm` |
| 加 / 改工具 | [tools.py](tools.py) —— 写 `Tool` 子类，登记进 `ALL_TOOLS`，再填进 `executor_persona.tool_names` |
| 换计划的输出格式（JSON、带依赖的步骤…） | [core/plan_protocol.py](core/plan_protocol.py) —— 写 `PlanProtocol` 子类，赋给 `plan_protocol` |
| 换循环行为（人工审核、步骤白名单、动态重规划） | 继承 [core/ps_loop.py](core/ps_loop.py) 的 `PlanSolveLoop` 重写钩子，赋给 `loop_cls` |
| 换 Executor 实现（单次 LLM / 嵌套 PS / 直接调工具） | 写一个带 `execute(step, state)` 的类，`PlanSolveAgent(executor=...)`，**内核不动** |
| 换内层 ReAct 的行为 | 继承 [core/react_loop.py](core/react_loop.py) 的 `ReActLoop`，赋给 `executor_persona.loop_cls` |
| 换记忆策略（摘要、落盘、检索） | [memory.py](memory.py) —— 写 `Memory` 子类，传 `PlanSolveAgent(memory=...)` |
| 换模型 / 供应商 | [llm.py](llm.py) —— 写 `LLM` 子类，赋给 persona 里对应的 `*_llm` |
| 改骨架本身（很少需要） | [core/ps_loop.py](core/ps_loop.py) |

**加一个 PS agent 的最小改法**：复制整个仓库 → 改 [persona.py](persona.py) 里的
`PLAN_SOLVE_PERSONA` → 按需加工具。内核（`core/`）通常一行都不用动。

### 人格里有什么

```python
@dataclass
class PlanSolvePersona:
    name: str
    planner_llm: LLM                    # 建议用强模型：这是全局决策
    executor_persona: ReActPersona      # Executor 的人格（就是 ReAct 版的 Persona）
    replanner_llm: LLM | None = None    # 不填就复用 planner_llm
    plan_protocol: PlanProtocol = ...
    replan_policy: str = "on_failure"
    replan_every: int = 3
    max_steps: int = 8
    max_replans: int = 3
    max_react_steps: int = 6
    max_rounds: int = 10                # 记忆保留最近多少轮（PS 层的）
    loop_cls: type = PlanSolveLoop      # 换骨架策略的入口
```

`executor_persona` 里的 `system_prompt` **不要写格式要求**——那是
`protocol.format_instructions()` 的活。分开写，换协议时格式说明会自动跟着变。

> 注意 `ReActPersona` 的两个字段在 PS 项目里处境不同（文件里有注释）：
> `max_steps` 被 `PlanSolvePersona.max_react_steps` 覆盖；
> `max_rounds` 在 PS 层**不生效**——PS 没有「一个 ReAct agent 记多久」这回事，
> 跨步骤的上下文由 `ExecutionState` 提供，跨轮的由 `PlanSolvePersona.max_rounds` 管。

## 两层设计：门面 vs 内核

```
core/ps_loop.py  PlanSolveLoop   纯骨架。收 history 参数，**不知道「记忆」是什么**
agent.py         PlanSolveAgent  门面。持有 memory，自动读写，一步到位
```

```python
# 单 agent 应用：用门面，最省事
agent = PlanSolveAgent()
answer, trace = agent.run("调研 X 并写一份报告")

# 编排 / 自定义：绕过门面，直接用内核
loop = PlanSolveLoop(planner=..., executor=..., replanner=...)
answer, trace = loop.run("问题", history=[...])
```

门面做的事情只有一件：**把 persona 里的声明变成三个部件的实例**。它不含任何
决策逻辑——策略在 persona 的字段里，骨架在 `core/ps_loop.py` 里。

**「什么算成功的一轮」的策略在 [agent.py](agent.py) 里，只有这一处**：
只有真正给出 `Final Answer` 的轮次才写进记忆；兜底返回（`⚠` 开头）一律不记——
避免把「没答完」当成「聊过的事」。

`history` **只喂给 Planner**。Executor 的上下文由 `ExecutionState` 提供，
不给它看历史也不给它看历史问答——§信息隔离见下。

## 三个部件

三个部件形式相同（都是「调一次 LLM、解析出结构化结果」），职责完全不同，
所以是三个独立的可替换接缝。换哪个都是改构造参数，`core/` 不动。

### Planner —— 把问题拆成计划

```
输入：用户问题（+ 记忆里的历史）
输出：PlanResult(kind="plan", steps=[...])     一次调用，只调一次
```

**不做的事**：不执行、不看工具返回。它的全部工作就是「看一眼问题，列出步骤」。

**也不做重试纠错。** 格式错了返回 `malformed`，由 `ps_loop` 决定怎么办。
理由：重试策略属于控制流，散落到各部件里就变成「每个部件一套自己的重试逻辑」，
改一处忘一处。循环是唯一知道「整体到哪一步了」的地方，所以纠错决策归它。

循环的处置（`PlanSolveLoop._make_plan`）：

1. 第一次 `malformed` → **把错误提示喂回去重问一次**；
2. 还是 `malformed` → **退化成单步计划 =「直接回答这个问题」**。
   这在语义上等于「跳过规划，让 Executor 用 ReAct 自己去处理」——
   PS 的退化形态就是 ReAct，退化总比罢工好。

### Executor —— 完成计划里的一步

```
输入：当前步骤 + 之前步骤的结果（ExecutionState）
输出：StepOutcome(ok, text)                    可能内部调很多次工具
```

**默认实现：包一个 ReAct 循环。** 这是 Plan-and-Execute 的标准做法——
也是它和 Planner 的分工：Planner 负责**拆**，ReAct 负责**做**。

**不做的事**：不知道整个计划长什么样，也不知道后面还有什么步骤。

`_build_step_prompt` 是 **Executor 的信息隔离点**，它只给模型三样东西：

```
总任务：{state.question}
已完成：{state.summary()}
现在要做：第 {step.id} 步 —— {step.desc}
```

**不给它看后面的步骤**。这个隔离是刻意的：看到全计划，Executor 会忍不住跳步
（把后面几步一起干了，或者提前下结论）。

`_judge` 判定这一步成没成。默认实现极简（有产出文本、且不以 `⚠` 开头），
但**必须是独立方法**，因为它是 `on_failure` 策略的唯一输入。要改判定口径
（比如要求产出里带来源链接），重写它就行。

### Replanner —— 审视进度，决定下一步

```
输入：完整 ExecutionState（问题 + 当前计划 + 已完成的步骤及结果）
输出：PlanResult(kind="final" | "plan" | "malformed")
```

**这是 Plan-and-Solve 相对原始 PS 提示法的关键增量**：原始 PS 没有反馈回路，
计划错了就一路错到底。Replanner 就是那个恢复机制。

输出「换计划」时**只列剩下的步骤**，循环会整份替换 `plan` 并把 `cursor` 归零。
所以换过计划之后 `state.done()` 是空的——「历史上真跑过哪些步骤」要看
`state.results`，不是看 `done()`。

## 五个钩子

沿用「固定骨架 + 钩子」的做法，钩子深度做到**否决级**。钩子拿到的是
`ExecutionState` 本身（它同时充当 `LoopContext` 的角色）。

```
① on_plan_ready(ctx)              计划刚出来。可以改、可以拒（置 ctx.stop）
                                   ← 人工审核计划就在这里实现
② before_execute(ctx, step)       执行一步之前。返回字符串 = 跳过执行，
                                   拿它当 outcome
③ on_step_end(ctx, outcome)       一步完成。埋点、日志
④ should_replan(ctx) -> bool      动态决定要不要调 Replanner，
                                   覆盖 persona 里的静态 replan_policy
⑤ on_replan(ctx, result)          Replanner 每给一次结果就调一次
                                   （换计划和收工都会调，看 result.kind 区分）
```

### `before_execute` 的语义

**刻意和 ReAct 版的 `before_tool` 保持一致**，复制出去的人不用重新学：

| 你做什么 | 循环怎么做 | `ok` |
| --- | --- | --- |
| `return None` | 照常交给 Executor 执行 | — |
| `return "文本"` | **跳过**执行，拿它当这一步的产出 | **True** |
| `raise Exception` | 不执行，异常信息当产出 | **False** |

返回 = 你替它把活干了（所以判成功）；抛异常 = 你不让它干（所以判失败，
会触发 `on_failure`）。这个区分是有意义的：缓存命中该算成功，
白名单拦截该算失败。

`StepOutcome.skipped` 会标出「这一步没真跑 Executor」，方便排障。

### 钩子写在哪

继承 `PlanSolveLoop` 重写，然后赋给 `persona.loop_cls`：

```python
from core.plan import PlanStep
from core.ps_loop import PlanSolveLoop

class MyLoop(PlanSolveLoop):
    def on_plan_ready(self, ctx):
        print("计划：")
        for s in ctx.plan:
            print(f"  {s.id}. {s.desc}")
        if input("执行吗？(y/n) ") != "y":
            ctx.stop = True            # 拒掉

PLAN_SOLVE_PERSONA.loop_cls = MyLoop
```

**想给钩子更多信息，就往 `ExecutionState` 上加字段**——和 ReAct 版往
`LoopContext` 上加字段是同一个做法。

## replan_policy：这个项目最重要的可配项

| 策略 | 行为 | 成本 | 适用 |
| --- | --- | --- | --- |
| `always` | 每步后都问 Replanner | 最高（N+1 次大模型调用） | 环境嘈杂、计划容易过期 |
| `on_failure` | 只在某步失败时问 | 低 | **默认**。多数任务的性价比拐点 |
| `every_n` | 每 N 步问一次 | 中 | 长计划、想要周期性检查 |
| `never` | 从不问，计划跑完直接收尾 | 最低 | 纯 PS 提示法、任务简单确定 |

**这个可配项就是「原始 Plan-and-Solve 提示法」和「Plan-and-Execute 工程架构」之间
的连续调节旋钮**——`never` 那端是论文里的 PS，`always` 那端是 LangGraph 的标准形态。

实现要点（`_should_replan`）：

- 钩子 `should_replan` **优先于**静态策略。返回 True 时即使策略是 `never`
  也会触发一次 review——这是「动态覆盖静态」的口子。
- `every_n` 用 `len(state.results)` 计数，**不是 `cursor`**：重规划会把 `cursor`
  归零，用它计数会让 `every_n` 在换过计划之后重新开始数。`results` 只增不减。
- `every_n` 纯按步数，**失败不额外触发**。要「每 N 步 + 失败也查」，
  用钩子：`return super().should_replan(ctx) or not ctx.results[-1].ok`。

## 三个硬上限与终止性

| 上限 | 防的是 | 在哪 |
| --- | --- | --- |
| `max_steps` | 计划本身太长（模型不听劝写 20 步） | 循环截断，记进 trace 的 `plan_truncated` |
| `max_replans` | Replanner 每步都触发，无限重规划 | 触顶后**停止重规划但继续执行**，记进 `replan_capped` |
| `max_react_steps` | 单步内部 ReAct 跑飞 | 透传给 Executor 的内层循环 |

**任何一个触顶，或者计划跑完拿不到 final，都走兜底返回**（把已有产出拼成一段
「未完成」的说明），**而不是抛异常**——和 ReAct 版的失败兜底同一个原则。

终止性是能证明的：已执行步数 ≤ `max_steps × (max_replans + 1)`。
每换一次计划最多再走 `max_steps` 步，而换计划次数被 `max_replans` 卡死。

注意 `max_steps` 是**单份计划**的长度上限，不是全局步数预算。想要全局预算，
在钩子里数 `len(ctx.results)` 并置 `ctx.stop`。

## 升级路径

### 1. 人工审核计划

`on_plan_ready` 钩子就是为此留的：

```python
class HumanReviewLoop(PlanSolveLoop):
    def on_plan_ready(self, ctx):
        print("计划：")
        for s in ctx.plan:
            print(f"  {s.id}. {s.desc}")
        if input("执行吗？(y/n/edit) ") != "y":
            ctx.plan = 手动改过的计划      # 改计划
            # 或者 ctx.stop = True        # 整个拒掉
```

### 2. 并行执行计划步骤

需要三处一起改（和 ReAct 版并行工具的思路一致）：

1. `PlanStep` 加 `depends_on: list[int]`；
2. `Plan` 从「列表」升级为「有向图」，加拓扑排序；
3. `PlanSolveLoop` 把「取 `plan[cursor]`」换成「取所有依赖已满足的步骤」，
   用线程池并发跑 `Executor.execute`。

**注意**：并发跑多个 Executor 时，每个 Executor 内部的 ReAct 循环必须**互相独立**
（各自的 tools 字典、各自的 trace）。这正好是「无全局状态」这个约束在还债——
`tools.build_registry` 每次返回新字典新实例，就是为了这一天。

### 3. 计划持久化 / 断点续跑

`ExecutionState` 是个普通 dataclass，序列化它就是断点：

```python
import dataclasses, json
snapshot = json.dumps(dataclasses.asdict(state), ensure_ascii=False)
```

存盘 → 进程重启 → 读回来接着跑，**不需要动内核**。
`StepOutcome.react_trace` 通常占大头，落盘前可以剔掉。

### 4. 结构化产物传递

`StepOutcome` 扩一个 `artifacts: dict`，让 Executor 除了自然语言摘要还能交出
结构化数据（表格、代码、文件路径），后续步骤按需取用而不是去解析摘要文本。
这是 §已知坑-「信息传递丢数据」的正解。

### 5. 换掉 Executor 引擎

Executor 是接缝，默认包 ReAct，但不一定非得是 ReAct：

- **单次 LLM 调用**：步骤简单、不需要工具时，最便宜；
- **另一个 PlanSolveAgent**：递归分解，处理「某一层还是太复杂」的情况；
- **直接调工具**：步骤本身就是确定的工具调用时，跳过 LLM。

换法都是「传一个自己实现的 `Executor` 进去」，`core/` 不动：

```python
class SingleShotExecutor:
    """一步 = 一次 LLM 调用，不调工具。"""
    def __init__(self, llm, system_prompt): ...
    def execute(self, step, state) -> StepOutcome:
        reply = self.llm([...])
        return StepOutcome(step_id=step.id, desc=step.desc, ok=bool(reply),
                           text=reply, steps_used=0)

agent = PlanSolveAgent(executor=SingleShotExecutor(...))
```

## 离线自测

```bash
python3 main.py --selftest      # 54 条断言，不联网、不用 key
```

改完任何代码先跑它。**全部用桩**（桩 Planner / 桩 Executor / 桩 Replanner /
桩 LLM / 桩工具），断言行为而非文本。覆盖：

- **契约与解析**：编号列表 / `Final Answer` 分支 / `malformed` / 编号跳号重号的确定行为 /
  项目符号 / 续行 / 噪声行 / 代码围栏 / 空描述行
- **控制流**：计划被完整执行 / Replanner 说 final 立刻收工 / 换计划后旧步骤不再跑 /
  计划跑完强制收尾 / trace 形状
- **三个硬上限**：分别有独立断言，且都验证「走兜底不抛异常」
- **四种 replan 策略**：`always` 数得清 review 次数 / `on_failure` 全成功 0 次、有失败触发 /
  `every_n` 步数对得上 / `never` 0 次 / 非法策略构造时就报错
- **规划失败的处置**：重问一次带上纠正提示 / 退化单步计划 / Planner 直接给答案则跳过执行
- **五个钩子**：改计划 / `ctx.stop` 中止 / 返回字符串则真 Executor 未被调用 /
  抛异常则判失败 / `should_replan` 覆盖 `never` / `on_step_end`、`on_replan` 的调用时机
- **换部件不碰内核**：两个循环同进程互不串 / 换策略内核不动 /
  真 ReAct Executor 端到端跑通 / 没有模块级全局状态
- **记忆**：前缀喂给 Planner / 只有 final 才入记忆 / 兜底轮次不入 / `memory=None`

## 已知坑与设计取舍

按「这个坑有多容易踩到」排序。

### 步粒度（最容易踩，且没有银弹）

计划步太粗（「调研市场」）→ Executor 不知道从哪下手，等于没规划；
太细（「打开浏览器」）→ 计划本身就快成答案了，Executor 沦为复述机。

**应对**：① Planner 提示词里明确要求「每一步是一个可以用工具完成的**具体动作**」；
② `max_steps` 上限防过度规划。
**但要说清楚：这最终是靠提示词工程调的，不是靠代码解决的。**

### 计划过期（plan brittleness）

初始计划是基于「当时以为的情况」定的。第 2 步的结果可能让第 5 步变得毫无意义。

**应对**：Replanner + `replan_policy`。**默认给 `on_failure` 而不是 `never`**——
纯 PS 没有恢复机制，一个错计划会一路错到底。

### Replan 成本失控

Replanner 在嘈杂环境下会**几乎每步都触发**，每个 replan 都是一次大模型调用。
文档里明确记载了「小模型当 Executor 会导致频繁重规划」。

**应对**：`max_replans` 硬上限 + 默认 `on_failure`（而不是 `always`）+
trace 里记录 `replans` 次数让人看得见成本。

> 推论：**别为了省钱把 Executor 换成小模型**。Executor 产出质量差 → `_judge`
> 判失败 → 频繁重规划 → 省下的钱在 Replanner 那儿加倍花回去。

### 信息传递丢数据

步骤结果用一个字符串传给后续步骤，太长要截断，截断就丢关键数据。
这是被记录过的真实失败模式。

**应对**：`StepOutcome.text` 由 **Executor 自己决定**写什么（它最清楚什么重要），
**外层一律不截断**——`state.summary()` 是全文渲染的。真嫌长，就改 Executor
让它产出更精炼，别在外层切。彻底解决见「升级路径 4」。

### Executor 与 Replanner 的职责重叠

「这一步做得够不够好」由 Executor 的 `_judge` 判定，「要不要换计划」由 Replanner
判定。两者边界模糊时，会出现「Executor 说自己成了、Replanner 说不行」的反复。

**应对**：明确分工——**`_judge` 只看这一步自己的产出，Replanner 才看全局**。
`_judge` 的实现必须简单，不要让它做全局判断。

### 简单任务上过度规划

「今天几号」这种问题走一遍 Planner → Executor → Replanner 是纯浪费。
PS 架构在简单任务上天然亏本。

**应对**：这一条**不在本项目解决**——它是「该用哪个 agent」的问题，不是
「PS 怎么实现」的问题。README 里写清了适用边界。

> 相关：`PlanProtocol.parse` 接受 Planner 直接输出 `Final Answer`（会跳过执行
> 直接返回），这是留给「模型自己判断这题不用规划」的口子。但 `format_plan_instructions`
> 明确要求「不要输出 Final Answer」，所以正常情况不会走到——它只是不让这种
> 输出变成 `malformed` 而被浪费掉。

### 无并行（与 ReAct 版同病）

计划是个列表，顺序执行。真正的并行需要 DAG 调度（LLMCompiler 那条路线）。

**应对**：不做，但升级路径见上。

### 解析器是启发式的

`PlanProtocol.parse` 靠正则识别编号行，有些边界情况只能靠权衡：

- `1.5 米` 这种以数字开头的正文行会被误判成「第 1 步」。
  计划文本里极罕见，为了多兼容几种编号写法（`1.` `2、` `3)` `4：`）接受这个代价。
- 同时输出编号步骤和 `Final Answer` 时，**谁先出现谁说话**。
- 步骤后面的续行会被并入上一步的描述。空编号行（只有 `2.` 没有内容）
  单独识别并丢弃，不会被当成续行污染上一步。

换成 JSON 协议（写 `PlanProtocol` 子类）可以彻底摆脱这些启发式，
`plan_protocol` 就是为了这个留的接缝。

## 和 ReAct 版的对照

| ReAct 版的文件 | 搬到哪 | 改动 |
| --- | --- | --- |
| `core/protocol.py` | `core/protocol.py` | **一字不动** |
| `core/loop.py` | `core/react_loop.py` | **只改文件名**（+ 模块 docstring 里的自引用） |
| `llm.py` | `llm.py` | 一字不动 |
| `tools.py` | `tools.py` | 一字不动 |
| `memory.py` | `memory.py` | 一字不动 |
| `persona.py` 的 `Persona` | `persona.py` 的 `ReActPersona` | 结构照用，名字改了（留了 `Persona` 别名） |
| `agent.py` 的 `ReActAgent` | **不搬** | Executor 直接用 `ReActLoop`（纯内核），不用门面——步骤间上下文由 `ExecutionState` 提供，不需要门面那套自动记忆 |
| `agent.py` 的 `build_system_prompt` | `agent.py` | 一字不动 |
| `main.py` 的 `--selftest` 框架 | `main.py` | 照搬 `check(name, cond)` 的结构 |

**能搬这么多，是因为 ReAct 版当初就是按「复制出去再改」设计的。**
如果发现某个文件搬不动（比如又冒出全局状态），应该**回头修 ReAct 母本**，
不要在这边打补丁——那会让两个仓库慢慢分叉，母本失去意义。

### 两处刻意的复用

1. **`core/plan_protocol.py` 用了 `core/protocol.py` 的 `_strip_code_fence`**。
   跨模块 import 一个下划线开头的函数确实不漂亮，但比复制一份
   「哪天改了一边忘了另一边」的重复实现要好——这正是「成对打包」要防的那类错误。
2. **`before_execute` 的语义和 `before_tool` 完全一致**（返回=替换，抛异常=拦截）。
   同一个模式在两个项目里复用，复制出去的人不用重新学。
