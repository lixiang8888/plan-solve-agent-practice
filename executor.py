# -*- coding: utf-8 -*-
"""
executor.py —— Executor：完成计划里的一步
============================================

    输入：当前步骤 + 之前步骤的结果（ExecutionState）
    输出：StepOutcome                    可能内部调很多次工具

**默认实现：包一个 ReAct 循环。** 把「完成这一步」当成一个子问题丢给 ReAct——
这是它和 Planner 的分工：Planner 负责**拆**，ReAct 负责**做**。
这也是 Plan-and-Execute 的标准做法，所以本项目的一大半「智能」其实是搬来的。

**不做的事**：不知道整个计划长什么样，也不知道后面还有什么步骤。
它只看到「我之前做了什么」和「现在要做什么」。这个信息隔离是刻意的——
让 Executor 专注当前步，避免它擅自跳步。

Executor 是个**接缝**：换实现只改构造参数，core/ 一行不动。
默认之外还能是什么，见 MANUAL 的「升级路径」。
"""

from __future__ import annotations

from core.plan import ExecutionState, PlanStep, StepOutcome
from core.protocol import Protocol
from core.react_loop import ReActLoop


class Executor:
    """完成计划里的一步。默认实现：包一个 ReAct 循环。"""

    def __init__(self, llm, tools: dict, protocol: Protocol,
                 system_prompt: str, max_react_steps: int = 6, verbose: bool = True,
                 loop_cls: type = ReActLoop):
        # loop_cls 让内层也能换策略（继承 ReActLoop 重写它的三个钩子）——
        # 和 PlanSolvePersona.loop_cls 换外层是同一套做法，两层各自可换。
        self.loop = loop_cls(llm=llm, tools=tools, protocol=protocol,
                             system_prompt=system_prompt,
                             max_steps=max_react_steps, verbose=verbose)
        self.max_react_steps = max_react_steps

    def execute(self, step: PlanStep, state: ExecutionState) -> StepOutcome:
        prompt = self._build_step_prompt(step, state)
        answer, trace = self.loop.run(prompt)
        return StepOutcome(
            step_id=step.id,
            desc=step.desc,
            ok=self._judge(answer, trace),      # ← 判定「这一步成功了没」
            text=answer or "(这一步没有产出)",
            steps_used=self._count_actions(trace),
            react_trace=trace,
        )

    # ------------------------------------------------------------------
    # 信息隔离点
    # ------------------------------------------------------------------

    def _build_step_prompt(self, step: PlanStep, state: ExecutionState) -> str:
        """**Executor 的信息隔离点**：只给模型三样东西。

           总任务：{state.question}
           已完成：{state.summary()}
           现在要做：第 {step.id} 步 —— {step.desc}

        **不给它看后面的步骤。** 这个隔离是刻意的：看到全计划，
        Executor 会忍不住跳步（把后面几步一起干了，或者提前下结论）。
        它只需要知道「之前做了什么」和「现在做什么」。
        """
        return f"""总任务：{state.question}

已完成：
{state.summary()}

现在要做：第 {step.id} 步 —— {step.desc}

请专注完成这一步，不要去考虑后面的步骤。
完成后用 Final Answer 给出**这一步的产出**（不要写成给用户的最终答复）。"""

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def _judge(self, answer: str | None, trace: list) -> bool:
        """判定这一步成没成。**必须是独立方法**——它是 on_failure 策略的唯一输入。

        刻意做得极简（§12.7）：只回答「这一步自己产出东西了吗」，
        不做任何全局判断。「做得够不够好」是 Replanner 的事，
        让它在这里掺和会导致「Executor 说自己成了、Replanner 说不行」的反复。

        判据两条，都只看自己这步：
          1. 有产出文本；
          2. 不以 ⚠ 开头 —— ReAct 循环拿不到 Final Answer 时的兜底文案就是这个前缀。
        """
        text = (answer or "").strip()
        if not text or text.startswith("⚠"):
            return False
        return True

    @staticmethod
    def _count_actions(trace: list) -> int:
        """这一步内部真跑了几轮工具（成本观测用）。"""
        return len([t for t in trace if t.get("type") == "action"])
