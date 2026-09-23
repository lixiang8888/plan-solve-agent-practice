# -*- coding: utf-8 -*-
"""
persona.py —— 人格定义。**换人格 = 只改这个文件。**
====================================================

这里有两个 dataclass，因为**这个项目有两层人格**：

    ReActPersona        Executor 的人格。就是 ReAct 版的 Persona，结构照用。
    PlanSolvePersona    整个 PS agent 的人格。规划层 + 策略旋钮，内嵌一个
                        `executor_persona` 字段。

**能直接复用 `ReActPersona` 是两层设计的好处**：Executor 的人格本来就是一个
ReAct 人格（它有什么工具、什么角色、什么输出协议），原样拿来就能用，
不需要为 Executor 发明一套新的人格结构（§7）。

内核（core/）不认识「人格」这个词，它只收拼装好的 system_prompt / 工具表 /
协议对象 / 几个数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.plan_protocol import PlanProtocol
from core.protocol import Protocol, TextReActProtocol
from core.ps_loop import PlanSolveLoop
from core.react_loop import ReActLoop
from llm import DeepSeekLLM, LLM


# ---------------------------------------------------------------------------
# 1. Executor 的人格（= ReAct 版的 Persona，从母本照搬）
# ---------------------------------------------------------------------------

@dataclass
class ReActPersona:
    # ---- 必填：这三样决定了「你是谁、你会什么、你多聪明」----
    name: str
    system_prompt: str                  # 只写角色与要求，不写输出格式
    tool_names: list[str]               # 名字要能在 tools.ALL_TOOLS 里找到
    llm: LLM

    # ---- 选填：想换就换，不换就用默认 ----
    protocol: Protocol = field(default_factory=TextReActProtocol)
    loop_cls: type = ReActLoop          # ← 换「循环策略」的入口：继承 ReActLoop 重写钩子
    max_steps: int = 6                  # 每问最大思考步数

    # 这两个字段在 PS 项目里的处境不同，用之前先看一眼：
    #   max_steps —— 被 PlanSolvePersona.max_react_steps 覆盖（单步内层的上限）。
    #                留在这里是为了「从 ReAct 版复制一段人格过来就能用」。
    #   max_rounds—— **PS 层不用它**。PS 没有「一个 ReAct agent 记多久」这回事，
    #                跨步骤的上下文由 ExecutionState 提供，跨轮的上下文由
    #                PlanSolvePersona.max_rounds 管。留作兼容，改了没效果。
    max_rounds: int = 10


# 从 ReAct 版复制过来的代码里写的可能是 Persona，留个别名省得改。
Persona = ReActPersona


# ---------------------------------------------------------------------------
# 2. PS 人格
# ---------------------------------------------------------------------------

@dataclass
class PlanSolvePersona:
    name: str
    # —— 规划层 ——
    planner_llm: LLM                    # 建议用强模型：这是全局决策
    executor_persona: ReActPersona      # ← 直接复用上面那个 ReActPersona！
    replanner_llm: LLM | None = None    # 不填就复用 planner_llm

    plan_protocol: PlanProtocol = field(default_factory=PlanProtocol)

    # —— 策略 ——
    replan_policy: str = "on_failure"   # always | on_failure | every_n | never
    replan_every: int = 3               # policy == "every_n" 时生效
    max_steps: int = 8                  # 计划最多几步
    max_replans: int = 3                # 最多换几次计划
    max_react_steps: int = 6            # 单步内部的 ReAct 上限
    max_rounds: int = 10                # 记忆保留最近多少轮 Q/A（PS 层的，非 executor 的）

    # 换「循环策略」的入口，和 ReActPersona.loop_cls 是同一个设计：
    # 继承 PlanSolveLoop 重写五个钩子，赋给这个字段。§13.1 的人工审核计划就用它。
    loop_cls: type = PlanSolveLoop

    def resolved_replanner_llm(self) -> LLM:
        """没单配 replanner 模型就用 planner 的。

        默认复用是有道理的：两者都是「看一眼全局、做一次判断」的活，
        对模型的要求同类。要省成本可以把 executor 换小模型（但见 MANUAL 里
        §12.3 那条警告：小模型当 Executor 会导致频繁重规划，反而更贵）。
        """
        return self.replanner_llm or self.planner_llm


# ---------------------------------------------------------------------------
# 3. 当前人格
# ---------------------------------------------------------------------------

PLAN_SOLVE_PERSONA = PlanSolvePersona(
    name="规划助手",
    planner_llm=DeepSeekLLM(),
    replanner_llm=DeepSeekLLM(),
    executor_persona=ReActPersona(
        name="执行者",
        system_prompt=(
            "你是「执行者」，负责完成上级分配给你的**一个具体步骤**。"
            "用中文工作。只关心这一步，做完就交差。"
        ),
        tool_names=["search"],
        llm=DeepSeekLLM(),
    ),
)
