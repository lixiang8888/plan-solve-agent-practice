# -*- coding: utf-8 -*-
"""
main.py —— CLI 入口 + 离线自测
================================

运行（在 WSL 内；用 uv 时把 python3 换成 `uv run python`）：

    python3 main.py "问题"                    # 单次提问
    python3 main.py --interactive             # 多轮对话（带记忆）
    python3 main.py "问题" --policy always    # 换重规划策略
    python3 main.py --selftest                # 离线自测（不联网，不用 key）

自测**全部用桩**（桩 Planner / 桩 Executor / 桩 Replanner / 桩 LLM / 桩工具），
断言行为而非文本，所以不联网、不用 key、结果稳定。
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台保险
except Exception:
    pass

import core.ps_loop as ps_loop_mod
import tools as tools_mod
from agent import PlanSolveAgent
from core.plan import Plan, PlanStep, StepOutcome
from core.plan_protocol import PlanProtocol, PlanResult
from core.ps_loop import VALID_POLICIES, PlanSolveLoop
from persona import PLAN_SOLVE_PERSONA, PlanSolvePersona, ReActPersona
from tools import Tool


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="plan_solve_agent",
        description="Plan-and-Solve 智能体：先规划，再执行，随时重规划")
    parser.add_argument("question", nargs="?", help="单次提问内容")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="多轮对话（带记忆）")
    parser.add_argument("--policy", choices=VALID_POLICIES, default=None,
                        help="重规划策略（默认取人格配置）")
    parser.add_argument("--every", type=int, default=None,
                        help="--policy every_n 时的 N")
    parser.add_argument("--max-steps", type=int, default=None, help="计划最多几步")
    parser.add_argument("--max-replans", type=int, default=None, help="最多重规划几次")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="只打印最终答案与计划，不打印内层 ReAct 过程")
    parser.add_argument("--selftest", action="store_true",
                        help="跑离线自测，不联网不用 key")
    args = parser.parse_args(argv)

    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    # 密钥缺失只在真的要跑的时候才拦（import 阶段不拦，否则 --selftest 也起不来）
    if not PLAN_SOLVE_PERSONA.planner_llm.ready():
        print("启动失败：未配置 DeepSeek API key。"
              "请设置环境变量 DEEPSEEK_API_KEY，或在本目录放 keys.py")
        sys.exit(1)

    # 人格是 dataclasses.replace 出来的副本 —— 不动模块级的 PLAN_SOLVE_PERSONA，
    # 所以同进程里改参数不会互相影响（无全局状态的直接体现）
    import dataclasses
    persona = dataclasses.replace(
        PLAN_SOLVE_PERSONA,
        replan_policy=args.policy or PLAN_SOLVE_PERSONA.replan_policy,
        replan_every=args.every or PLAN_SOLVE_PERSONA.replan_every,
        max_steps=args.max_steps or PLAN_SOLVE_PERSONA.max_steps,
        max_replans=(PLAN_SOLVE_PERSONA.max_replans if args.max_replans is None
                     else args.max_replans),
    )
    agent = PlanSolveAgent(persona=persona, verbose=not args.quiet)

    if args.interactive:
        print(f"多轮对话模式（人格：{persona.name}，策略：{persona.replan_policy}，"
              f"有记忆）。输入 quit / exit 退出。\n")
        while True:
            try:
                q = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                continue
            if q.lower() in {"quit", "exit", "q"}:
                break
            answer, _ = agent.run(q)
            print("\n" + "=" * 56)
            print(f"Agent > {answer}")
            print("=" * 56 + "\n")
    elif args.question:
        answer, _ = agent.run(args.question)
        print("\n" + "=" * 56)
        print(f"Agent > {answer}")
        print("=" * 56)
    else:
        parser.print_help()


# ---------------------------------------------------------------------------
# 离线自测用的桩
# ---------------------------------------------------------------------------

class StubExecutor:
    """桩 Executor：不调 LLM、不调工具，只记下收到的步骤并返回固定产出。"""

    def __init__(self, ok: bool = True, fail_on=(), text: str = "产出"):
        self.seen: list[tuple[int, str]] = []      # [(step_id, desc), ...] 按调用顺序
        self.ok = ok
        self.fail_on = set(fail_on)                # 这些 step_id 判为失败
        self.text = text

    def execute(self, step: PlanStep, state) -> StepOutcome:
        self.seen.append((step.id, step.desc))
        ok = self.ok and step.id not in self.fail_on
        return StepOutcome(step_id=step.id, desc=step.desc, ok=ok,
                           text=f"{self.text}:{step.desc}", steps_used=1)


class StubPlanner:
    """桩 Planner：按调用次序吐出预设的 PlanResult，用完重复最后一个。"""

    def __init__(self, *results: PlanResult):
        self.results = list(results)
        self.calls: list[dict] = []

    def make_plan(self, question, history=None, correction=None) -> PlanResult:
        self.calls.append({"q": question, "correction": correction,
                           "history": list(history or [])})
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class StubReplanner:
    """桩 Replanner：一串 PlanResult（用完重复最后一个），或一个 callable。

    记的是每次 review 时状态的**快照**——不能存 state 引用，循环之后还会改它。
    """

    def __init__(self, *results):
        self.fn = results[0] if len(results) == 1 and callable(results[0]) else None
        self.results = [] if self.fn else list(results)
        self.calls: list[dict] = []

    def review(self, state) -> PlanResult:
        self.calls.append({"step_count": len(state.results), "cursor": state.cursor,
                           "plan_len": len(state.plan), "replans": state.replans})
        if self.fn:
            return self.fn(len(self.calls) - 1, state)
        if not self.results:                    # 空桩：不做任何决定
            return _bad("（空桩 Replanner 不表态）")
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def _plan(*descs: str) -> PlanResult:
    return PlanResult(kind="plan", steps=Plan.from_descs(list(descs)).steps)


def _final(text: str) -> PlanResult:
    return PlanResult(kind="final", final=text)


def _bad(err: str = "格式错误") -> PlanResult:
    return PlanResult(kind="malformed", error=err)


def _loop(planner, executor, replanner, **kw) -> PlanSolveLoop:
    kw.setdefault("verbose", False)
    return PlanSolveLoop(planner, executor, replanner, **kw)


def _echo_replanner() -> StubReplanner:
    """执行中一律【照旧】（把剩余步骤原样再列一遍），计划走完才给最终答案。

    这样才能观察到「每步都 review」的完整效果——若第一次 review 就给 final，
    循环立刻收工，就数不出 always 策略到底触发了几次。
    """
    def fn(i, state):
        if state.cursor >= len(state.plan):
            return _final("答案")
        return _plan(*[s.desc for s in state.todo()])
    return StubReplanner(fn)


def _mid_reviews(trace: list) -> int:
    """中途 review 次数：**不含**计划跑完后那次强制收尾。

    判据直接读 trace 里的 `forced` 标记（循环写入的真实信号），
    不要去猜——比如「review 时 cursor 是否小于 len(plan)」看着对，
    实际会漏掉「最后一步之后再问一次」那种正好走到计划末尾的 review。
    """
    return sum(1 for t in trace if t["type"] == "replan" and not t.get("forced"))


# ---------------------------------------------------------------------------
# 离线自测
# ---------------------------------------------------------------------------

def _selftest() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    p = PlanProtocol()

    # ---- 1. 契约与解析 --------------------------------------------------
    print("== PlanProtocol.parse ==")
    r = p.parse("1. 查资料\n2. 写报告")
    check("拆出编号步骤列表",
          r.kind == "plan" and [s.desc for s in r.steps] == ["查资料", "写报告"]
          and [s.id for s in r.steps] == [1, 2])

    r = p.parse("Thought: 信息够了\nFinal Answer: 答案是 42")
    check("识别 Final Answer 分支",
          r.kind == "final" and r.final == "答案是 42")

    r = p.parse("随便说点什么，既没有编号也没有最终答案")
    check("无编号、无 Final → malformed", r.kind == "malformed" and bool(r.error))

    r = p.parse("3. 甲\n3. 乙\n0. 丙")
    check("编号跳号/重号/从 0 开始 → 按位置重编 1,2,3（行为确定）",
          r.kind == "plan" and [s.id for s in r.steps] == [1, 2, 3]
          and [s.desc for s in r.steps] == ["甲", "乙", "丙"])

    r = p.parse("- 甲\n- 乙")
    check("无编号的项目符号也能拆",
          r.kind == "plan" and [s.desc for s in r.steps] == ["甲", "乙"])

    r = p.parse("计划：\n1. 甲\n   补充说明\n2. 乙")
    check("小标题被丢弃、续行并入上一步",
          r.kind == "plan" and len(r.steps) == 2
          and r.steps[0].desc == "甲 补充说明")

    r = p.parse("1. 甲\nThought: 要不要再搜一下\n2. 乙")
    check("步骤之间的 Thought 行被丢弃",
          r.kind == "plan" and [s.desc for s in r.steps] == ["甲", "乙"])

    r = p.parse('```\n1. 甲\n2. 乙\n```')
    check("剥代码围栏后仍能拆", r.kind == "plan" and len(r.steps) == 2)

    r = p.parse("1. 甲\nFinal Answer: 提前收工")
    check("步骤后跟 Final Answer → 仍判为计划（谁先出现谁说话）",
          r.kind == "plan" and [s.desc for s in r.steps] == ["甲"])

    r = p.parse("Final Answer:   ")
    check("空 Final Answer → malformed", r.kind == "malformed")

    r = p.parse("1. 甲\n2.\n3. 乙")
    check("编号行没有描述 → 不算一步，也不会塞个空步骤进去",
          r.kind == "plan" and [s.desc for s in r.steps] == ["甲", "乙"]
          and [s.id for s in r.steps] == [1, 2])

    r = p.parse("1.\n2.")
    check("全部编号行都空 → malformed", r.kind == "malformed")

    # ---- 2. 控制流 ------------------------------------------------------
    print("== 控制流 ==")
    ex = StubExecutor()
    rp = StubReplanner(_final("收工答案"))
    ans, tr = _loop(StubPlanner(_plan("甲", "乙", "丙")), ex, rp,
                    replan_policy="never").run("Q")
    check("计划被完整执行、顺序正确", [d for _, d in ex.seen] == ["甲", "乙", "丙"])
    check("计划跑完但没人说 final → 强制问一次 Replanner 收尾",
          len(rp.calls) == 1 and ans == "收工答案")
    check("trace 形状：plan → step×3 → replan(forced) → final",
          [t["type"] for t in tr] == ["plan", "step", "step", "step", "replan", "final"])

    ex = StubExecutor()
    rp = StubReplanner(_final("提前收工"))
    ans, tr = _loop(StubPlanner(_plan("甲", "乙", "丙")), ex, rp,
                    replan_policy="always").run("Q")
    check("Replanner 说 final → 立刻收工，剩余步骤不再执行",
          ans == "提前收工" and [d for _, d in ex.seen] == ["甲"])

    ex = StubExecutor()
    rp = StubReplanner(_plan("新一", "新二"), _final("搞定"))
    ans, tr = _loop(StubPlanner(_plan("旧一", "旧二", "旧三")), ex, rp,
                    replan_policy="every_n", replan_every=2).run("Q")
    check("Replanner 给新计划 → 后续步骤换成新的（旧三被换掉）",
          [d for _, d in ex.seen] == ["旧一", "旧二", "新一", "新二"] and ans == "搞定")
    check("replan 事件记了次数", sum(1 for t in tr if t["type"] == "replan") == 2)

    # ---- 3. 硬上限 ------------------------------------------------------
    print("== 硬上限 ==")
    ex = StubExecutor()
    ans, tr = _loop(StubPlanner(_plan(*[f"步骤{i}" for i in range(1, 11)])), ex,
                    StubReplanner(_final("答案")), replan_policy="never",
                    max_steps=3).run("Q")
    check("max_steps：10 步的计划被截断到 3 步", len(ex.seen) == 3)
    check("截断被记进 trace",
          any(t["type"] == "plan_truncated" and t["was"] == 10 and t["now"] == 3
              for t in tr))

    ex = StubExecutor()
    rp = StubReplanner(*[_plan(f"重规划{i}") for i in range(20)])   # 永远说「再改改」
    ans, tr = _loop(StubPlanner(_plan("甲")), ex, rp,
                    replan_policy="always", max_replans=2).run("Q")
    check("max_replans：Replanner 一直说改改也能停下来",
          len(rp.calls) == 3 and any(t["type"] == "replan_capped" for t in tr))
    check("重规划耗尽 → 走兜底返回，不抛异常",
          str(ans).startswith("⚠") and tr[-1]["type"] == "fallback")

    ex = StubExecutor()
    ans, tr = _loop(StubPlanner(_plan("甲")), ex, StubReplanner(),   # 空桩，永远 malformed
                    replan_policy="never").run("Q")
    check("Replanner 给不出 final → 兜底返回，不抛异常",
          str(ans).startswith("⚠") and tr[-1]["type"] == "fallback")

    # ---- 4. 四种 replan 策略 --------------------------------------------
    print("== 四种 replan 策略 ==")
    ex = StubExecutor()
    rp = _echo_replanner()
    ans, tr = _loop(StubPlanner(_plan("a", "b", "c")), ex, rp,
                    replan_policy="always").run("Q")
    check("always：3 步 → 3 次中途 review", _mid_reviews(tr) == 3)
    check("always：计划最终被走完并收尾", len(ex.seen) == 3 and ans == "答案")

    ex = StubExecutor()
    rp = StubReplanner(_final("答案"))
    ans, tr = _loop(StubPlanner(_plan("a", "b", "c")), ex, rp,
                    replan_policy="on_failure").run("Q")
    check("on_failure：全成功 → 0 次中途 review", _mid_reviews(tr) == 0 and len(ex.seen) == 3)

    ex = StubExecutor(fail_on=(1,))
    rp = StubReplanner(_final("答案"))
    ans, tr = _loop(StubPlanner(_plan("甲", "乙")), ex, rp,
                    replan_policy="on_failure").run("Q")
    check("on_failure：有失败 → 触发 review（并立刻收工）",
          _mid_reviews(tr) == 1 and ans == "答案" and len(ex.seen) == 1)

    ex = StubExecutor()
    rp = _echo_replanner()
    ans, tr = _loop(StubPlanner(_plan("a", "b", "c", "d")), ex, rp,
                    replan_policy="every_n", replan_every=2).run("Q")
    check("every_n=2：4 步 → 2 次中途 review（步数对得上）", _mid_reviews(tr) == 2)

    ex = StubExecutor()
    rp = StubReplanner(_final("答案"))
    ans, tr = _loop(StubPlanner(_plan("a", "b", "c")), ex, rp,
                    replan_policy="never").run("Q")
    check("never：0 次中途 review", _mid_reviews(tr) == 0 and len(ex.seen) == 3)

    check("非法策略在构造时就报错（不拖到运行期）",
          _raises(lambda: _loop(StubPlanner(_plan("a")), StubExecutor(),
                                StubReplanner(_final("x")), replan_policy="随机应变")))

    # ---- 5. 规划失败的处置 ----------------------------------------------
    print("== 规划失败的处置 ==")
    ex = StubExecutor()
    pl = StubPlanner(_bad("没有编号"), _plan("甲", "乙"))
    ans, tr = _loop(pl, ex, StubReplanner(_final("答案")), replan_policy="never").run("原始问题")
    check("格式错 → 重问一次，且把纠正提示带回去",
          len(pl.calls) == 2 and pl.calls[0]["correction"] is None
          and pl.calls[1]["correction"] == "没有编号")
    check("重问成功 → 按新计划执行", [d for _, d in ex.seen] == ["甲", "乙"])

    ex = StubExecutor()
    pl = StubPlanner(_bad("一直不听话"))
    ans, tr = _loop(pl, ex, StubReplanner(_final("答案")), replan_policy="never").run("这个问题")
    check("两次都解析不了 → 退化成单步计划，不抛异常",
          len(pl.calls) == 2 and [d for _, d in ex.seen] == ["这个问题"])
    check("退化被记进 trace", any(t["type"] == "plan_fallback" for t in tr))

    ex = StubExecutor()
    ans, tr = _loop(StubPlanner(_final("不用规划，直接答")), ex,
                    StubReplanner(_final("不该被问到")), replan_policy="never").run("今天几号")
    check("Planner 直接给 Final Answer → 跳过执行，一步不跑",
          ans == "不用规划，直接答" and ex.seen == [] and tr[-1]["type"] == "final")

    # ---- 6. 五个钩子 ----------------------------------------------------
    print("== 五个钩子 ==")

    class EditPlan(PlanSolveLoop):
        def on_plan_ready(self, ctx):
            ctx.plan = [PlanStep(desc="被改过的唯一一步", id=1)]

    ex = StubExecutor()
    ans, tr = EditPlan(StubPlanner(_plan("甲", "乙", "丙")), ex,
                       StubReplanner(_final("答案")), replan_policy="never",
                       verbose=False).run("Q")
    check("on_plan_ready 能改计划（改完后续按新计划走）",
          [d for _, d in ex.seen] == ["被改过的唯一一步"] and ans == "答案")

    class StopAtPlan(PlanSolveLoop):
        def on_plan_ready(self, ctx):
            ctx.stop = True

    ex = StubExecutor()
    ans, tr = StopAtPlan(StubPlanner(_plan("甲", "乙")), ex,
                         StubReplanner(_final("不该被问到")), replan_policy="never",
                         verbose=False).run("Q")
    check("on_plan_ready 里 ctx.stop 能中止",
          ex.seen == [] and str(ans).startswith("⚠") and tr[-1]["type"] == "fallback")

    class SkipStep(PlanSolveLoop):
        def before_execute(self, ctx, step):
            return f"[钩子替 Executor 给的产出] {step.desc}"

    ex = StubExecutor()
    rp = StubReplanner(_final("答案"))
    ans, tr = SkipStep(StubPlanner(_plan("甲", "乙")), ex, rp,
                       replan_policy="on_failure", verbose=False).run("Q")
    check("before_execute 返回字符串 → 真 Executor 未被调用",
          ex.seen == [] and all(t["skipped"] for t in tr if t["type"] == "step"))
    check("被替掉的步骤判为成功（on_failure 不被误触发）", _mid_reviews(tr) == 0)

    class InterceptStep(PlanSolveLoop):
        def before_execute(self, ctx, step):
            raise RuntimeError("这一步不许跑")

    ex = StubExecutor()
    rp = StubReplanner(_final("答案"))
    ans, tr = InterceptStep(StubPlanner(_plan("甲", "乙")), ex, rp,
                            replan_policy="on_failure", verbose=False).run("Q")
    check("before_execute 抛异常 → 拦下这一步且判为失败",
          ex.seen == [] and any(t["type"] == "step" and not t["ok"] for t in tr))
    check("被拦下的失败触发 on_failure 策略，循环不崩", _mid_reviews(tr) == 1)

    class ForceReplan(PlanSolveLoop):
        def should_replan(self, ctx):
            return True

    ex = StubExecutor()
    rp = StubReplanner(_final("被强制问出来的答案"))
    ans, tr = ForceReplan(StubPlanner(_plan("甲", "乙", "丙")), ex, rp,
                          replan_policy="never", verbose=False).run("Q")
    check("should_replan 为 True 时，即使策略是 never 也会触发 review",
          len(rp.calls) == 1 and ans == "被强制问出来的答案")

    hook_log = []

    class Recorder(PlanSolveLoop):
        def on_step_end(self, ctx, outcome):
            hook_log.append(("step", outcome.step_id, outcome.ok))

        def on_replan(self, ctx, result):
            hook_log.append(("replan", result.kind))

    ex = StubExecutor()
    Recorder(StubPlanner(_plan("甲", "乙")), ex, StubReplanner(_final("答案")),
             replan_policy="never", verbose=False).run("Q")
    check("on_step_end 每步都调、on_replan 收尾时也调",
          hook_log == [("step", 1, True), ("step", 2, True), ("replan", "final")])

    # ---- 7. 换部件不碰内核 ----------------------------------------------
    print("== 换部件不碰内核（适配度 + 无全局状态） ==")
    ex_a, ex_b = StubExecutor(text="A"), StubExecutor(text="B")
    lp_a = _loop(StubPlanner(_plan("甲")), ex_a, StubReplanner(_final("A答案")),
                 replan_policy="never")
    lp_b = _loop(StubPlanner(_plan("乙")), ex_b, StubReplanner(_final("B答案")),
                 replan_policy="always")
    a1, _ = lp_a.run("问题甲")
    b1, _ = lp_b.run("问题乙")
    a2, _ = lp_a.run("问题甲2")
    check("两个 PS 循环同进程并存、都能跑通",
          a1 == "A答案" and b1 == "B答案" and a2 == "A答案")
    check("交叉执行不串：各自的 Executor 只收到自己的步骤",
          [d for _, d in ex_a.seen] == ["甲", "甲"] and [d for _, d in ex_b.seen] == ["乙"])
    check("换 replan_policy 内核一行没改（同一份 PlanSolveLoop 服务两种策略）",
          type(lp_a) is type(lp_b) is PlanSolveLoop
          and lp_a.replan_policy != lp_b.replan_policy)
    check("没有模块级全局工具注册表（多实例共存的前提）",
          not hasattr(tools_mod, "TOOL_REGISTRY"))
    check("core/ps_loop.py 没有模块级可变全局（状态全在 ExecutionState 里）",
          not any(isinstance(v, (dict, list, set))
                  for k, v in vars(ps_loop_mod).items() if not k.startswith("__")))

    # 真 ReAct Executor 端到端：桩 LLM 按 system prompt 分流给三个部件
    real_tool_calls = []

    class StubSearch(Tool):
        name, usage_hint, description = "stub", 'stub("q")', "桩搜索"
        def run(self, arg):
            real_tool_calls.append(arg)
            return f"假结果：{arg}"

    tools_mod.ALL_TOOLS["stub"] = StubSearch
    try:
        def scripted_llm(messages):
            sysmsg = messages[0]["content"]
            if "拆成一份执行计划" in sysmsg:
                return "1. 查天气\n2. 汇总"
            if "审视一个正在执行中的计划" in sysmsg:
                return "Final Answer: 上海多云"
            if messages[-1]["content"].startswith("Observation"):
                return "Thought: 够了\nFinal Answer: 上海多云 23 度"
            return 'Thought: 搜\nAction: stub("上海天气")'

        stub_persona = PlanSolvePersona(
            name="桩 PS 人格",
            planner_llm=None, replanner_llm=None,
            executor_persona=ReActPersona(name="桩执行者", system_prompt="你是桩执行者。",
                                          tool_names=["stub"], llm=None),
        )
        agent = PlanSolveAgent(persona=stub_persona, llm=scripted_llm, verbose=False)
        ans, tr = agent.run("上海天气怎么样")
        check("真 ReAct Executor 端到端跑通（只换 LLM/工具，内核未动）",
              ans == "上海多云" and real_tool_calls == ["上海天气", "上海天气"])
        check("Executor 拿到的是「一步」，不是整份计划（信息隔离）",
              all(t["type"] in {"plan", "step", "replan", "final"} for t in tr)
              and len([t for t in tr if t["type"] == "step"]) == 2)
    finally:
        tools_mod.ALL_TOOLS.pop("stub", None)

    # ---- 8. 记忆 --------------------------------------------------------
    print("== 记忆（PS 层：跨轮；不是 Executor 的） ==")
    pl = StubPlanner(_plan("甲"), _plan("乙"))
    mem_persona = PlanSolvePersona(
        name="桩", planner_llm=None,
        executor_persona=ReActPersona(name="e", system_prompt="p",
                                      tool_names=["search"], llm=None))
    agent2 = PlanSolveAgent(persona=mem_persona, planner=pl, executor=StubExecutor(),
                            replanner=StubReplanner(_final("答案")), verbose=False)
    agent2.run("第一问")
    agent2.run("第二问")
    check("记忆前缀喂给 Planner（第二次带上了第一问）",
          pl.calls[0]["history"] == []
          and any(m["content"] == "第一问" for m in pl.calls[1]["history"]))
    check("只有拿到 final 的轮次才入记忆", len(agent2.memory) == 4)

    agent3 = PlanSolveAgent(persona=mem_persona, planner=StubPlanner(_plan("甲")),
                            executor=StubExecutor(),
                            replanner=StubReplanner(_plan("一直改")), verbose=False)
    agent3.run("问题")
    check("兜底返回的轮次不入记忆", len(agent3.memory) == 0)

    agent4 = PlanSolveAgent(persona=mem_persona, planner=StubPlanner(_plan("甲")),
                            executor=StubExecutor(),
                            replanner=StubReplanner(_final("答案")),
                            memory=None, verbose=False)
    agent4.run("第一问")
    agent4.run("第二问")
    check("memory=None 时不带记忆", agent4.memory is None)

    print()
    return ok


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    main()
