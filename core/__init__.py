# -*- coding: utf-8 -*-
"""core —— 内核：与主循环强耦合的东西。

**分两组，因为本项目真的有两个嵌套的循环**（§5）：

    # —— PS 内核（本项目的主角，与 PS 循环强耦合）——
    plan.py            Plan / PlanStep / StepOutcome / ExecutionState
    plan_protocol.py   PlanProtocol：计划的格式说明 + 解析
    ps_loop.py         PlanSolveLoop 骨架 + 五个钩子

    # —— ReAct 引擎（服务 Executor，从 ReAct 版整体搬来）——
    protocol.py        Protocol / StepResult / TextReActProtocol
    react_loop.py      ReActLoop + 三个钩子（原 core/loop.py，只改了文件名）

    PlanSolveLoop  ← 外层：按计划走，单位是「计划步骤」
       └─ ReActLoop  ← 内层：完成一步，单位是「thought/action」

两个循环的契约不同（外层吃 PlanStep、吐 StepOutcome；内层吃 question、吐
answer），所以必须各自成模块。**内核里唯一的耦合是 ps_loop 通过 `executor`
这个接缝去用内层**——它不认识 ReActLoop，只认识「有个东西能吃 step 吐 outcome」。

其余接缝（LLM / 工具 / 记忆 / 人格）都是**松耦合**的，各自待在仓库根目录的
自己文件里。改内核的边界是「耦合度」，不是「文件数」。
"""
