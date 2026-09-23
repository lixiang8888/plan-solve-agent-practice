# -*- coding: utf-8 -*-
"""
core/plan_protocol.py —— 计划的格式说明 + 解析器
==================================================

沿用 ReAct 版「成对打包」的教训（见 core/protocol.py 的模块说明）：
格式说明和解析器必须一起改，所以打包在同一个对象里。分家就会静默出错——
模型照着旧格式输出、解析器按新格式拆，最后表现为「模型不听话」。

和 ReAct 版唯一的结构差异：**这里有两个提问方（Planner / Replanner），
却只有一个解析器。**

    format_plan_instructions(max_steps)   → 给 Planner：怎么列计划
    format_replan_instructions(max_steps) → 给 Replanner：继续 / 换计划 / 收尾
    parse(text)                           → 两边共用

因为它们输出的**是同一种东西**（一份计划，或一个终局答案），只是问法不同。
这是「成对打包」原则在有两个提问方时的自然延伸：
**格式说明可以有多个，解析器只能有一个。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.plan import Plan, PlanStep
# 剥代码围栏是通用的文本归一化，直接复用 ReAct 版那一份，不再抄一遍。
# 跨模块 import 一个下划线开头的函数确实不漂亮，但比复制一份「哪天改了一边
# 忘了另一边」的重复实现要好——这正是「成对打包」要防的那类错误。
from core.protocol import _strip_code_fence


# ---------------------------------------------------------------------------
# 1. 契约
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    """PlanProtocol.parse() 的返回值。"""
    kind: str                       # "plan" | "final" | "malformed"
    steps: list[PlanStep] = field(default_factory=list)
    final: str | None = None        # kind == "final" 时：给用户的最终答案
    error: str | None = None        # kind == "malformed" 时：喂回给模型的纠正提示


# ---------------------------------------------------------------------------
# 2. 解析用的行模式
# ---------------------------------------------------------------------------

# 编号步骤：`1. xxx` / `2、xxx` / `3) xxx` / `4：xxx`
# 已知的脆弱点：`1.5 米` 这种以数字开头的正文行会被误判成第 1 步。
# 计划文本里这种行极罕见，为了多兼容几种编号写法，接受这个代价。
_STEP_RE = re.compile(r"^[ \t]*(\d+)[ \t]*[.、)）:：][ \t]*(\S.*?)[ \t]*$")
# 无编号的项目符号：`- xxx` / `* xxx` / `• xxx`
_BULLET_RE = re.compile(r"^[ \t]*[-*•][ \t]+(\S.*?)[ \t]*$")
# Final Answer 标记（行首、允许全角冒号）
_FINAL_RE = re.compile(r"^[ \t]*Final[ \t]*Answer[ \t]*[:：][ \t]*(.*)$", re.I)
# 纯噪声行：`Thought: ...` / `计划：` / `Plan:` 之类的小标题，丢弃不影响语义
_NOISE_RE = re.compile(
    r"^[ \t]*(?:Thought|计划|规划|Plan|Planning|Steps?|步骤|行动方案)[ \t]*[:：]", re.I)
# 空编号行：只有 `2.` / `-` 没有任何内容。**必须单独识别**，否则会被下面的
# 「续行」规则接到上一步描述后面，把上一步的描述污染成「甲 2.」。
_BARE_MARKER_RE = re.compile(r"^[ \t]*(?:\d+[ \t]*[.、)）:：]|[-*•])[ \t]*$")


# ---------------------------------------------------------------------------
# 3. 协议
# ---------------------------------------------------------------------------

class PlanProtocol:
    """计划的格式说明 + 解析器。参考 ReAct 版 core/protocol.py 的 TextReActProtocol——
    那是这个模式的样板。"""

    # ---- 给 Planner 的格式说明 ----

    def format_plan_instructions(self, max_steps: int = 8) -> str:
        """给 Planner：怎么列计划。"""
        return f"""请把下面的问题拆成一份执行计划，供后续逐步执行。

要求：
1. 最多 {max_steps} 步，宁少勿多；一步能做完就只写一步。
2. 每一步是一个**可以用工具完成的具体动作**，不是一句泛泛的目标。
   反例：调研市场
   正例：用 search 查「2024 年中国新能源汽车销量」
3. 步骤按执行顺序排列，后面的步骤可以使用前面步骤的产出。
4. 只输出编号步骤列表。不要写解释、不要输出 Final Answer、不要调用工具。

格式（严格照此输出，一行一步）：
1. 第一步的具体动作
2. 第二步的具体动作"""

    # ---- 给 Replanner 的格式说明 ----

    def format_replan_instructions(self, max_steps: int = 8) -> str:
        """给 Replanner：怎么在「继续 / 换计划 / 收尾」之间选。"""
        return f"""你在审视一个正在执行中的计划的进度，需要做出判断。

你会看到：用户的问题、当前计划、已完成的步骤及产出。

只输出下面三种之一，不要写任何解释：

【收工】已有信息足够回答用户的问题了：
Final Answer: 给用户的完整回答

【换计划】还需要继续做，但剩下的步骤不合适了，换成新的（最多 {max_steps} 步）：
1. 新的第一步
2. 新的第二步

【照旧】剩下的步骤仍然有效，不需要改动：
把它们按原顺序、原措辞再列一遍即可。

判断原则：
- 只要已完成的步骤合起来已经能回答问题，就选【收工】。
  不要为了「把计划走完」而继续执行——计划的目的是答案，不是执行本身。
- 只有在某一步失败、产出与预期不符、或后续步骤已经没意义时才选【换计划】。
- 换计划时**不要再列出已完成的步骤**，只列剩下的。
- 拿不准就选【照旧】，不要凭感觉重组计划。"""

    # ---- 唯一的解析器（两边共用）----

    def parse(self, text: str) -> PlanResult:
        """识别编号步骤列表，或 Final Answer，或 malformed。

        裁决规则：**谁先出现谁说话**（沿用 ReAct 版 parse_output 的做法）。
        第一个结构标记（编号步骤行 / Final Answer 行）的位置决定走哪个分支。

        编号一律作废、按位置重编（见 Plan.from_descs），所以「编号跳号、重号、
        从 0 开始」这些情况不需要单独判断，行为天然确定。
        """
        body = _strip_code_fence(text or "")
        if not body:
            return PlanResult(
                kind="malformed",
                error="回复是空的。请按格式输出编号步骤列表，或者输出 Final Answer。")

        lines = body.splitlines()

        first_step_at = None
        first_final_at = None
        for i, ln in enumerate(lines):
            if first_step_at is None and (_STEP_RE.match(ln) or _BULLET_RE.match(ln)):
                first_step_at = i
            if first_final_at is None and _FINAL_RE.match(ln):
                first_final_at = i
            if first_step_at is not None and first_final_at is not None:
                break

        # ---- 分支 A：Final Answer ----
        if first_final_at is not None and (
                first_step_at is None or first_final_at < first_step_at):
            head = _FINAL_RE.match(lines[first_final_at]).group(1)
            payload = "\n".join([head] + lines[first_final_at + 1:]).strip()
            if not payload:
                return PlanResult(
                    kind="malformed",
                    error="Final Answer 后面是空的。要么把回答写完整，要么改列编号步骤。")
            return PlanResult(kind="final", final=payload)

        # ---- 分支 B：逐行收集步骤描述 ----
        if first_step_at is None:
            return PlanResult(
                kind="malformed",
                error="解析不到编号步骤，也解析不到 Final Answer。"
                      "请严格按格式输出「1. 具体动作」这样的编号列表，"
                      "或者输出「Final Answer: 回答」。")

        descs: list[str] = []
        for ln in lines[first_step_at:]:
            if not ln.strip():
                continue
            if _FINAL_RE.match(ln):
                # 步骤列表后面跟了个 Final Answer —— 属于「同时输出两种」的
                # 含糊情况。已按「谁先出现谁说话」判成计划了，这里把它当收尾语截断。
                break
            sm = _STEP_RE.match(ln)
            if sm is not None:
                desc = sm.group(2).strip()
            else:
                bm = _BULLET_RE.match(ln)
                desc = bm.group(1).strip() if bm else None

            if desc is not None:
                if desc:
                    descs.append(desc)
                continue
            if _NOISE_RE.match(ln) or _BARE_MARKER_RE.match(ln):
                continue
            # 续行：非空、又不匹配任何标记 → 接到上一步后面。
            # 模型常把一步写成「标题行 + 缩进补充行」，丢掉续行会削掉一半信息。
            if descs:
                descs[-1] = f"{descs[-1]} {ln.strip()}"

        if not descs:
            return PlanResult(
                kind="malformed",
                error="编号步骤都是空的。每一步都要写一句具体动作。")

        return PlanResult(kind="plan", steps=Plan.from_descs(descs).steps)
