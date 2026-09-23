# -*- coding: utf-8 -*-
"""
agent.py —— 门面层：把 persona / llm / 工具 / 记忆 / 三个部件 组装成能直接 run 的 agent
========================================================================================

两层设计（想用哪层用哪层）：

    core/ps_loop.py  PlanSolveLoop   纯骨架。收 history 参数，**不知道「记忆」是什么**
    agent.py         PlanSolveAgent  门面。持有 memory，自动读写，一步到位

    # 单 agent 应用：用门面，最省事
    agent = PlanSolveAgent()
    answer, trace = agent.run("调研 X 并写一份报告")

    # 编排 / 自定义：绕过门面，直接用内核
    loop = PlanSolveLoop(planner=..., executor=..., replanner=...)
    answer, trace = loop.run("问题", history=[...])

门面做的事情只有一件：**把 persona 里的声明变成三个部件的实例**。
它不含任何决策逻辑——策略在 persona 的字段里，骨架在 core/ps_loop.py 里。

**「什么算成功的一轮」的策略在这个文件里**，只有这一处：
只有真正给出 Final Answer 的轮次才写进记忆；兜底返回（⚠ 开头）一律不记——
避免把「没答完」当成「聊过的事」。
"""

from __future__ import annotations

from core.plan_protocol import PlanProtocol, PlanResult
from core.ps_loop import PlanSolveLoop
from executor import Executor
from memory import Memory, WindowMemory
from persona import PLAN_SOLVE_PERSONA, PlanSolvePersona, ReActPersona
from planner import Planner
from replanner import Replanner
from tools import build_registry

_DEFAULT = object()      # 哨兵：区分「没传」和「显式传 None（=不要记忆）」


def build_system_prompt(persona: ReActPersona, tools: dict) -> str:
    """Executor 的 system prompt = 角色提示词 + 工具清单（从注册表渲染）+ 协议格式说明。

    加工具不用改提示词——工具清单是渲染出来的，不是手写的。
    （这个函数和 ReAct 版 agent.py 里的同名函数是同一份，原样搬来。）
    """
    if not tools:
        raise ValueError(f"人格 {persona.name!r} 没配任何工具，无法生成提示词")
    tools_desc = "\n".join(f"- {t.usage_hint}：{t.description}" for t in tools.values())
    return f"""{persona.system_prompt}

可用工具：
{tools_desc}

{persona.protocol.format_instructions(tools)}"""


class PlanSolveAgent:
    """门面。一个实例 = 一套人格 + 一份记忆 + 四个部件，**没有全局状态**。

    所以同一个进程里可以并存多个实例：两套规划策略、两套工具、两份记忆，
    互不干扰。这是多 agent 能落地的前提。
    """

    def __init__(self, persona: PlanSolvePersona = PLAN_SOLVE_PERSONA,
                 executor=None, planner=None, replanner=None,
                 llm=None, memory: Memory | None = _DEFAULT,
                 verbose: bool = True):
        """
        executor / planner / replanner：**接缝**。传了就用你的，没传就按 persona 造。
            换 Executor 实现（桩 / 单次 LLM / 嵌套 PS）只改这个参数，core/ 不动。
        llm：懒人参数，一次替换 planner/executor/replanner **三个**模型。
            要分别配就改 persona 的字段，别用这个。
        """
        self.persona = persona
        self.verbose = verbose

        # ---- Planner：问题 → 计划 ----
        self.planner = planner or Planner(
            llm=persona.planner_llm if llm is None else llm,
            protocol=persona.plan_protocol,
            max_steps=persona.max_steps,
        )

        # ---- Executor：一步 → 结果 ----
        if executor is not None:
            self.executor = executor
        else:
            ep = persona.executor_persona
            # 每次 build_registry 都造新字典新实例 —— 没有模块级全局注册表
            tools = build_registry(ep.tool_names)
            self.executor = Executor(
                llm=ep.llm if llm is None else llm,
                tools=tools,
                protocol=ep.protocol,
                system_prompt=build_system_prompt(ep, tools),
                max_react_steps=persona.max_react_steps,
                verbose=verbose,
                loop_cls=ep.loop_cls,   # 想换内层 ReAct 的行为，改 executor_persona 就行
            )

        # ---- Replanner：执行状态 → 新计划 / 最终答案 ----
        self.replanner = replanner or Replanner(
            llm=persona.resolved_replanner_llm() if llm is None else llm,
            protocol=persona.plan_protocol,
            max_steps=persona.max_steps,
        )

        # ---- 记忆：PS 层的跨轮上下文（每步之间的上下文是 ExecutionState，不是它）----
        self.memory = WindowMemory(persona.max_rounds) if memory is _DEFAULT else memory

        # ---- 骨架：五个钩子的挂载点（persona.loop_cls 可换，见 §13.1 人工审核）----
        self.loop = persona.loop_cls(
            planner=self.planner,
            executor=self.executor,
            replanner=self.replanner,
            replan_policy=persona.replan_policy,
            replan_every=persona.replan_every,
            max_steps=persona.max_steps,
            max_replans=persona.max_replans,
            verbose=verbose,
        )

    def run(self, question: str) -> tuple[str | None, list]:
        """提问一轮。带记忆时自动读历史、写回结果。

        history 只喂给 Planner——Executor 的上下文由 ExecutionState 提供，
        不给它看历史（§6 的信息隔离）。
        """
        history = self.memory.history() if self.memory is not None else None
        answer, trace = self.loop.run(question, history=history)
        # 「什么算成功的一轮」—— 唯一真源：拿到 final 才记
        if self.memory is not None and trace and trace[-1].get("type") == "final":
            self.memory.record(question, answer)
        return answer, trace

    # 旧名字（ReAct 版门面用的是 Session.ask），保留以免已有的调用方断掉
    ask = run
