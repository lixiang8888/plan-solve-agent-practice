# -*- coding: utf-8 -*-
"""
planner.py —— Planner：把问题拆成计划
========================================

    输入：用户问题（+ 记忆里的历史）
    输出：PlanResult                     一次调用，只调一次

**不做的事**：不执行、不看工具返回。它的全部工作就是「看一眼问题，列出步骤」。

**也不做重试纠错。** 格式错了就返回 malformed，由 ps_loop 决定怎么办
（重问一次 / 退化成单步计划）。理由：重试策略属于控制流，散落到各部件里
就变成了「每个部件都有一套自己的重试逻辑」，改一处忘一处。
PS 循环是唯一知道「现在整体到哪一步了」的地方，所以纠错决策归它。
"""

from __future__ import annotations

from core.plan_protocol import PlanProtocol, PlanResult


class Planner:
    """一次调用，把问题拆成计划。不执行、不看工具结果。"""

    def __init__(self, llm, protocol: PlanProtocol, max_steps: int = 8):
        self.llm = llm
        self.protocol = protocol
        # max_steps 这里只用来**写进提示词**劝模型别写太长；
        # 真正的硬上限由 ps_loop 截断（提示词管不住不听话的模型）。
        self.max_steps = max_steps

    def make_plan(self, question: str, history: list | None = None,
                  correction: str | None = None) -> PlanResult:
        """产出计划。

        correction：上一次解析失败的纠正提示（格式错误的原文说明）。
        重试的**决策**在 ps_loop，这里只负责把纠正提示拼进消息——
        Planner 自己不判断「要不要重试」，它只是被调用时老实照做。
        """
        messages = [
            {"role": "system",
             "content": self.protocol.format_plan_instructions(self.max_steps)},
            {"role": "user", "content": question},
        ]
        if history:
            messages[1:1] = list(history)
        if correction:
            messages.append({"role": "user", "content": correction})

        reply = (self.llm(messages) or "").strip()
        return self.protocol.parse(reply)
