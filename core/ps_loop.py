# -*- coding: utf-8 -*-
"""
core/ps_loop.py —— Plan-and-Solve 主循环骨架 + 五个钩子
========================================================

这是内核的外层。骨架**固定**（规划 → 逐步执行 → 审视 → 收尾），
想改行为就重写钩子，而不是改这个文件。

    ┌─ on_plan_ready ─┐  计划刚出来。可以改、可以拒（置 ctx.stop）
    │                 │  ← 人工审核计划就在这里实现（§13.1）
    │   while cursor < len(plan):
    │     ① 取一步
    │     ② before_execute  │  ← 可跳可拦
    │     ③ Executor.execute│  ← 内层是 ReAct 循环，跑多久由 max_react_steps 管
    │     ④ 记结果、cursor++
    │     ⑤ should_replan? │  ← 钩子优先，其次 persona 的静态策略
    │          └ Replanner.review → final 收工 / plan 换掉剩余计划
    └─ 计划跑完 ────────┘  强制问一次 Replanner 要最终答案

它和内层 ReActLoop 的分工：外层单位是「计划步骤」，内层单位是「thought/action」。
两者契约不同（外层吃 PlanStep 吐 StepOutcome，内层吃 question 吐 answer），
所以各自成模块——这不是内核变厚了，是问题本身有两层。

**终止性**（三个硬上限合起来保证循环一定停）：
    max_steps      单份计划最长多少步（截断）
    max_replans    最多换几次计划 → 已执行步数 ≤ max_steps × (max_replans + 1)
    max_react_steps 单步内部 ReAct 的轮数上限（透传给 Executor）
任何一个触顶都走**兜底返回**，不抛异常——和 ReAct 版的失败兜底同一个原则。
"""

from __future__ import annotations

from core.plan import ExecutionState, Plan, PlanStep, StepOutcome, limit_steps
from core.plan_protocol import PlanResult

VALID_POLICIES = ("always", "on_failure", "every_n", "never")


class PlanSolveLoop:
    """默认循环。想换策略就继承它、重写钩子（见 persona.PlanSolvePersona）。"""

    def __init__(self, planner, executor, replanner, replan_policy: str = "on_failure",
                 replan_every: int = 3, max_steps: int = 8, max_replans: int = 3,
                 verbose: bool = True):
        if replan_policy not in VALID_POLICIES:
            raise ValueError(
                f"未知的 replan_policy {replan_policy!r}；"
                f"可选：{', '.join(VALID_POLICIES)}")
        self.planner = planner
        self.executor = executor
        self.replanner = replanner
        self.replan_policy = replan_policy
        self.replan_every = max(1, replan_every)
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.verbose = verbose

    # ------------------------------------------------------------------
    # 五个钩子：默认什么都不做 / 不干预。子类重写。
    # ------------------------------------------------------------------

    def on_plan_ready(self, ctx: ExecutionState) -> None:
        """计划刚出来。可以改 `ctx.plan`、也可以置 `ctx.stop` 拒掉。

        典型用法：人工审核计划（§13.1）、按白名单修剪超纲步骤。
        改完之后记得保持 `ctx.cursor` 与 `ctx.plan` 自洽（通常 cursor 还是 0）。
        """

    def before_execute(self, ctx: ExecutionState, step: PlanStep) -> str | None:
        """拦在 Executor 执行之前。语义刻意和 ReAct 版 before_tool 对齐（§8）：

            return None      → 照常交给 Executor 执行
            return "文本"    → **跳过**执行，拿这个字符串当这一步的产出（判为成功）
            raise Exception  → 不执行，异常信息当产出（**判为失败**，会触发 on_failure）

        返回 = 你替它把活干了；抛异常 = 你不让它干。所以一个是成功一个是失败。
        典型用法：人工确认、步骤白名单、结果缓存。
        """
        return None

    def on_step_end(self, ctx: ExecutionState, outcome: StepOutcome) -> None:
        """一步完成（产出已经写进 ctx.results）。典型用法：埋点、日志。"""

    def should_replan(self, ctx: ExecutionState) -> bool:
        """动态决定要不要调 Replanner，**覆盖** persona 里的静态 replan_policy。

        返回 True 时即使策略是 `never` 也会触发一次 review（§11 有断言）。
        """
        return False

    def on_replan(self, ctx: ExecutionState, result: PlanResult) -> None:
        """Replanner 每给一次结果就调一次——**换计划和收工都会调**。

        这样一处就能记全「重规划花了多少钱」（§12.3 关心的成本可见性）。
        想区分两种结局看 `result.kind`。
        """

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self, question: str, history: list | None = None) -> tuple[str | None, list]:
        """返回 (最终回答, trace)。

        - history：拼在问题之前的消息前缀，只影响 Planner（Executor 的上下文
          由 ExecutionState 提供，**不给它看历史**——§6 的信息隔离）。
        - trace：整个 PS 循环的事件流，供展示与测试（形状见 §6）。
        """
        state = ExecutionState(question=question, plan=[], cursor=0, results=[])
        trace = state.trace

        # ---- ① 规划：一次调用，产出初始计划 ----
        result = self._make_plan(question, history, trace)
        if result.kind == "final":
            return self._finish(result.final, trace)

        state.plan = limit_steps(result.steps, self.max_steps)
        if len(result.steps) > len(state.plan):
            trace.append({"type": "plan_truncated",
                          "was": len(result.steps), "now": len(state.plan)})
        trace.append({"type": "plan", "steps": _brief(state.plan)})
        self._log_plan(state)

        # ---- ② 钩子①：计划刚出来，可以改可以拒 ----
        self.on_plan_ready(state)
        if state.stop:
            return self._fallback(state, "计划被 on_plan_ready 钩子中止")

        # ---- ③ 主循环 ----
        capped_warned = False
        while True:
            # 计划跑完但没人说收工 → 强制问一次 Replanner 要最终答案（§4 收尾分支）
            if state.cursor >= len(state.plan):
                forced = self._force_final(state)
                if forced is not None:
                    return self._finish(forced, trace)
                if state.stop:
                    return self._fallback(state, "循环被钩子提前终止")
                return self._fallback(state, "计划已执行完，但没能得到最终答案")

            step = state.plan[state.cursor]
            outcome = self._run_step(state, step)
            state.results.append(outcome)
            state.cursor += 1
            trace.append({"type": "step", "step_id": step.id, "desc": step.desc,
                          "ok": outcome.ok, "outcome": outcome.text,
                          "react_trace": outcome.react_trace,
                          "skipped": outcome.skipped})
            self._log_step(state, outcome)
            self.on_step_end(state, outcome)          # 钩子③

            if state.stop:
                break

            # ---- ④ 要不要叫 Replanner ----
            if not self._should_replan(state):
                continue
            if state.replans >= self.max_replans:
                if not capped_warned:      # 只记一次，别每步都刷
                    trace.append({"type": "replan_capped",
                                  "max_replans": self.max_replans})
                    capped_warned = True
                continue

            result = self.replanner.review(state)
            self.on_replan(state, result)             # 钩子⑤
            if state.stop:
                break

            if result.kind == "final" and result.final:
                trace.append({"type": "replan", "final": result.final})
                return self._finish(result.final, trace)

            if result.kind == "plan" and result.steps:
                state.plan = limit_steps(result.steps, self.max_steps)   # 换掉剩余计划
                state.cursor = 0                                         # 新计划从头走
                state.replans += 1
                trace.append({"type": "replan", "replans": state.replans,
                              "steps": _brief(state.plan)})
                self._log_replan(state)
                continue

            # malformed / 空计划：装上会死循环（空计划立刻又触发收尾），所以不动计划。
            # 不涨 replans 是安全的：计划没换，剩余步数不会变多。
            trace.append({"type": "replan",
                          "malformed": result.error or "Replanner 的输出无法解析"})

        # 只有钩子置 ctx.stop 才会走到这
        return self._fallback(state, "循环被钩子提前终止")

    # ------------------------------------------------------------------
    # 内部：规划
    # ------------------------------------------------------------------

    def _make_plan(self, question: str, history: list | None,
                   trace: list) -> PlanResult:
        """拿初始计划。重试纠错的**决策**在这里，不在 Planner 里（§6）。"""
        result = self.planner.make_plan(question, history)
        if result.kind != "malformed":
            return result

        trace.append({"type": "plan_malformed", "error": result.error})
        if self.verbose:
            print(f"\n[!] 计划格式不对，重问一次：{result.error}")

        retry = self.planner.make_plan(question, history, correction=result.error)
        if retry.kind != "malformed":
            return retry

        # 还是不听话 → 兜底成「一步计划 = 直接回答这个问题」。
        # 这在语义上等于「跳过规划，让 Executor 用 ReAct 自己去处理」，
        # 比直接报错有用得多——PS 的退化形态就是 ReAct，退化总比罢工好。
        trace.append({"type": "plan_fallback", "error": retry.error})
        if self.verbose:
            print(f"\n[!] 计划仍然解析不了，退化成单步计划：{retry.error}")
        return PlanResult(kind="plan", steps=Plan.from_descs([question]).steps)

    # ------------------------------------------------------------------
    # 内部：执行一步
    # ------------------------------------------------------------------

    def _run_step(self, state: ExecutionState, step: PlanStep) -> StepOutcome:
        """过一遍 before_execute 钩子，再交给 Executor。"""
        try:
            override = self.before_execute(state, step)
        except Exception as e:      # 钩子抛异常 = 否决这一步，且判为失败
            return StepOutcome(step_id=step.id, desc=step.desc, ok=False,
                               text=f"这一步被拦截：{e}", skipped=True)
        if override is not None:    # 返回字符串 = 替 Executor 给出产出，判为成功
            return StepOutcome(step_id=step.id, desc=step.desc, ok=True,
                               text=override, skipped=True)
        return self.executor.execute(step, state)

    # ------------------------------------------------------------------
    # 内部：重规划策略
    # ------------------------------------------------------------------

    def _should_replan(self, state: ExecutionState) -> bool:
        """钩子④优先于静态策略（§11 的断言靠这个）。"""
        if self.should_replan(state):
            return True

        policy = self.replan_policy
        if policy == "never":
            return False
        if policy == "always":
            return True
        if policy == "every_n":
            # 用 len(results) 而不是 cursor：重规划会把 cursor 归零，
            # 用它计数会让 every_n 在换过计划之后重新开始数。results 只增不减。
            return len(state.results) % self.replan_every == 0
        # on_failure（默认）
        return bool(state.results) and not state.results[-1].ok

    def _force_final(self, state: ExecutionState) -> str | None:
        """计划跑完后的强制收尾：**只要最终答案，不要新计划**。

        §4 的收尾分支问的就是「要最终答案」。Replanner 若在这里给新计划，
        当作没听懂走兜底——它每步都有机会换计划，计划跑完了才说「还要做」
        只会把循环拖长。
        """
        if not state.results:       # 一步都没跑（空计划），没什么可收尾的
            return None
        result = self.replanner.review(state)
        self.on_replan(state, result)
        if state.stop:
            return None
        if result.kind == "final" and result.final:
            state.trace.append({"type": "replan", "final": result.final,
                                "forced": True})
            return result.final
        state.trace.append({"type": "replan", "forced": True,
                            "malformed": result.error
                            or "收尾时 Replanner 没给出 Final Answer"})
        return None

    # ------------------------------------------------------------------
    # 内部：收尾
    # ------------------------------------------------------------------

    @staticmethod
    def _finish(answer: str | None, trace: list) -> tuple[str | None, list]:
        trace.append({"type": "final", "answer": answer})
        return answer, trace

    def _fallback(self, state: ExecutionState, reason: str) -> tuple[str, list]:
        """触顶 / 中止时的兜底返回。**不抛异常**，把已有产出拼给用户看。"""
        text = f"⚠ {reason}，未能给出最终答案。以下是已完成的步骤及产出：\n{state.summary()}"
        state.trace.append({"type": "fallback", "answer": text})
        if self.verbose:
            print(f"\n[!] {text}")
        return text, state.trace

    # ------------------------------------------------------------------
    # 内部：打印
    # ------------------------------------------------------------------

    def _log_plan(self, state: ExecutionState) -> None:
        if not self.verbose:
            return
        print(f"\n{'=' * 56}\n计划（{len(state.plan)} 步）：")
        for s in state.plan:
            print(f"  {s.id}. {s.desc}")
        print("=" * 56)

    def _log_step(self, state: ExecutionState, o: StepOutcome) -> None:
        if not self.verbose:
            return
        tag = "被钩子替换" if o.skipped else ("成功" if o.ok else "失败")
        print(f"\n[计划 {state.cursor}/{len(state.plan)}] 第 {o.step_id} 步 —— "
              f"{o.desc}  [{tag}]")
        text = (o.text or "").replace("\n", "\n      ")
        print(f"      产出：{text[:600]}")     # 只展示前 600 字符

    def _log_replan(self, state: ExecutionState) -> None:
        if not self.verbose:
            return
        print(f"\n{'·' * 56}\n已重规划 {state.replans} 次，换成新计划：")
        for s in state.plan:
            print(f"  {s.id}. {s.desc}")
        print("·" * 56)


def _brief(steps: list[PlanStep]) -> list[dict]:
    """把步骤列表渲染成 trace 里的紧凑形式。"""
    return [{"id": s.id, "desc": s.desc} for s in steps]
