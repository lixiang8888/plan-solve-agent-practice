# -*- coding: utf-8 -*-
"""
core/plan.py —— PS 的数据结构：计划、步骤产出、跨步骤状态
==========================================================

**这是 PS 与 ReAct 最大的结构差异所在。**

    ReAct 的「状态」就是 messages —— 循环自己攒，外面不用管，一步一回合。
    PS    必须显式维护「计划 + 走到哪了 + 每步产出了什么」—— 因为规划 / 执行 /
          重规划是三次**互相独立的模型调用**，它们之间没有共享的 messages，
          唯一的共享媒介就是这个 ExecutionState。

所以这个文件是 PS 内核的地基：改这里的字段 = 同时改 Planner / Executor /
Replanner / 循环四方的接口。加字段之前先想清楚是不是真需要（§8 的做法是
「想给钩子更多信息，就往 ExecutionState 上加字段」，但那是加法，不是改法）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. 计划
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """计划里的一步。desc 是给 Executor 看的自然语言描述。"""
    desc: str
    id: int = 0                     # 从 1 开始，供 prompt 里编号引用


@dataclass
class Plan:
    """一份计划。**编号的真源在这里**——模型给的是什么编号一律作废。"""

    steps: list[PlanStep] = field(default_factory=list)

    @classmethod
    def from_descs(cls, descs: list[str]) -> Plan:
        """按出现顺序重新编号 1..n。

        模型给的编号经常跳号、重号、从 0 开始、或者干脆没有。与其在解析器里
        逐个判这些坏情况，不如**一律按位置重编**——这样「编号不连续 / 重复
        怎么办」就有了唯一确定的答案（§11 要求的行为确定性），
        而且 `PlanProtocol.parse` 只管收集描述文本，不碰编号。
        """
        return cls([PlanStep(desc=d, id=i) for i, d in enumerate(descs, 1)])

    def descs(self) -> list[str]:
        return [s.desc for s in self.steps]

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)


def limit_steps(steps: list[PlanStep], max_steps: int) -> list[PlanStep]:
    """截断到 max_steps 步（返回新列表，不改原列表）。

    §4 的硬上限之一。**由循环调用，不放进 Planner**：Planner 只负责把
    max_steps 写进提示词（劝模型别写太长），真正兜底的是循环——
    「模型没听话」是常态，提示词管不住，硬上限才管得住。
    """
    return list(steps[:max(0, max_steps)])


# ---------------------------------------------------------------------------
# 2. 一步的产出
# ---------------------------------------------------------------------------

@dataclass
class StepOutcome:
    """Executor 执行完一步的结果。"""

    step_id: int
    desc: str
    ok: bool                        # Replanner 的策略（on_failure）依赖它
    text: str                       # 这一步产出了什么（会被喂给后续步骤）
    steps_used: int = 0             # 这一步内部跑了几轮 ReAct（成本观测）
    react_trace: list = field(default_factory=list)   # 内层 ReAct 的 trace，供排障
    skipped: bool = False           # 被 before_execute 钩子替掉了，没真跑 Executor

    # 后两个字段是对 §3 的补充，理由：
    #   react_trace —— §6 的 trace 形状里 `{"type":"step", ..., "react_trace":[...]}`
    #                  需要它。排障时「这一步为什么失败」只能从内层 trace 看出来
    #                  （§12.7 的 Executor/Replanner 扯皮就靠它定位）。
    #                  嫌占内存可以在 §13.3 落盘时剔掉。
    #   skipped     —— before_execute 的否决语义（§8）必须可观测，否则
    #                  「钩子到底生效没有」只能靠猜。


# ---------------------------------------------------------------------------
# 3. 跨步骤状态（PS 循环的 LoopContext）
# ---------------------------------------------------------------------------

@dataclass
class ExecutionState:
    """跨步骤的全部状态。PS 循环的 LoopContext 就是它（§8）。

    钩子拿到的就是**这个对象本身**，不是它的拷贝。想加信息就往这里加字段。
    """

    question: str
    plan: list[PlanStep]
    cursor: int                     # 下一个要执行的步骤下标
    results: list[StepOutcome]      # 已完成步骤的结果，按序
    replans: int = 0                # 重规划了几次（防死循环）
    stop: bool = False              # 钩子置 True → 本轮结束后收工
    trace: list = field(default_factory=list)   # 和 run() 返回的是同一个列表

    # ---- 给钩子和 Executor 用的便利方法 ----

    def done(self) -> list[PlanStep]:
        """当前计划里已走完的部分。

        注意：重规划会**整份换掉** plan 并把 cursor 归零（§4），
        所以这之后 done() 是空的——「历史上真跑过哪些步骤」要看 results，
        不要看这里。这是刻意的：done()/todo() 描述的是**当前这份计划**。
        """
        return self.plan[:self.cursor]

    def todo(self) -> list[PlanStep]:
        """当前计划里还没走的部分。"""
        return self.plan[self.cursor:]

    def summary(self) -> str:
        """把「已完成的步骤及产出」渲染成文本，喂给 Executor / Replanner。

        **不做截断**：§12.4 的教训是「外层统一截断会丢关键数据」，
        该由 Executor 自己决定 text 写什么（它最清楚什么重要）。
        真嫌长，就改 Executor 让它的产出更精炼，别在这里切。
        """
        if not self.results:
            return "（还没有完成任何步骤）"
        lines = []
        for o in self.results:
            lines.append(f"[{o.step_id}] {o.desc}（{'成功' if o.ok else '失败'}）")
            lines.append(f"    产出：{o.text}")
        return "\n".join(lines)

    def plan_text(self) -> str:
        """渲染当前计划并标出进度。重规划后 cursor 归零，所以整份都是「未开始」。"""
        if not self.plan:
            return "（计划为空）"
        lines = []
        for i, s in enumerate(self.plan):
            mark = "已完成" if i < self.cursor else "未开始"
            lines.append(f"[{s.id}] {s.desc}  ← {mark}")
        return "\n".join(lines)
