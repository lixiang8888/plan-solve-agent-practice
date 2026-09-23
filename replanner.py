# -*- coding: utf-8 -*-
"""
replanner.py —— Replanner：审视进度，决定收工还是换计划
==========================================================

    输入：完整 ExecutionState（问题 + 当前计划 + 已完成的步骤及结果）
    输出：PlanResult
          kind="final" → 信息够了，这是最终答案，收工
          kind="plan"  → 剩下的步骤改成这样

**这是 Plan-and-Solve 相对原始 PS 提示法的关键增量。**
原始 PS（Wang et al. 2023）没有反馈回路，计划错了就一路错到底。
Replanner 就是那个恢复机制——也是 §12.2「计划过期」的正解。

和 Planner 一样**不做重试纠错**，格式错了返回 malformed 由循环处置。

它和 Executor 的边界（§12.7）：Executor 的 `_judge` 只看**这一步自己的产出**，
Replanner 才看**全局**——「这条链走下来信息够不够答问题了」。
"""

from __future__ import annotations

from core.plan import ExecutionState
from core.plan_protocol import PlanProtocol, PlanResult


class Replanner:
    """审视进度，决定：收工 / 换计划。"""

    def __init__(self, llm, protocol: PlanProtocol, max_steps: int = 8):
        self.llm = llm
        self.protocol = protocol
        self.max_steps = max_steps

    def review(self, state: ExecutionState) -> PlanResult:
        messages = [
            {"role": "system",
             "content": self.protocol.format_replan_instructions(self.max_steps)},
            {"role": "user", "content": self._render_state(state)},
        ]
        reply = (self.llm(messages) or "").strip()
        return self.protocol.parse(reply)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _render_state(state: ExecutionState) -> str:
        """把状态渲染成给 Replanner 看的文本。

        三块：**问题 / 当前计划 / 已完成的产出**。
        产出用 state.summary()，不截断（§12.4）——Replanner 要靠它判断
        「信息够不够答问题」，把关键数据截掉了它就判错。
        """
        return f"""用户的问题：
{state.question}

当前计划：
{state.plan_text()}

已完成的步骤及产出：
{state.summary()}

已重规划次数：{state.replans}"""
