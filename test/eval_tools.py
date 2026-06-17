#!/usr/bin/env python3
"""
Tool-selection eval — measures how reliably the configured (small) model picks
the RIGHT tool for a natural-language instruction. This is the thing that keeps
breaking on a 9B (e.g. "简报" → wrong daily_briefing, deleting to the wrong
folder, hallucinating instead of calling a tool).

For each case it sends `system + user` to the real model, captures the FIRST
tool call, and compares it to the expected set. It does NOT execute the tool —
no side effects. Run it, get an accuracy %, tweak tool descriptions / the system
prompt, re-run, and see if the number moved. (Requires the LLM server up; the
9B is non-deterministic so treat each run as a snapshot.)

Usage:  python test/eval_tools.py            # all cases
        python test/eval_tools.py --only mail
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
import agent

# (category, prompt, {acceptable tools})   None in the set = "should NOT call a tool"
CASES = [
    # Action-on-unspecified-target cases accept a look-up first (mail_list /
    # mail_read) — "list then act" is correct multi-step behaviour, not a miss.
    ("mail", "看看我的未读邮件", {"mail_list"}),
    ("mail", "我配置了哪些邮箱账户", {"mail_accounts"}),
    ("mail", "读一下苹果发来的那封邮件", {"mail_read", "mail_list"}),
    ("mail", "把这封广告邮件删掉", {"mail_delete", "mail_list"}),
    ("mail", "帮我清理收件箱里的垃圾邮件", {"mail_delete", "mail_move", "mail_move_all", "mail_list"}),
    ("mail", "给 leejun527@gmail.com 发一封测试邮件", {"mail_send", "mail_draft"}),
    ("mail", "帮我起草一封给老板的请假邮件", {"mail_draft", "mail_send"}),
    ("mail", "回复刚才那封邮件，说我同意", {"mail_reply", "mail_list"}),

    ("pim", "我今天和明天有什么安排", {"calendar_list"}),
    ("pim", "明天下午三点加一个牙医预约到日历", {"calendar_add"}),
    ("pim", "提醒我今晚八点吃药", {"reminders_add"}),
    ("pim", "我有哪些待办事项", {"reminders_list"}),
    ("pim", "记一条笔记：周末去爬山", {"notes_create", "notes_append"}),

    ("schedule", "每天早上八点给我汇总邮件", {"schedule_add"}),
    ("schedule", "看看我有哪些定时任务", {"schedule_list"}),
    ("schedule", "取消那个早安简报任务", {"schedule_remove", "schedule_list"}),

    ("memory", "记住我住在都柏林", {"remember"}),
    ("memory", "忘记我说过关于都柏林的事", {"forget"}),

    ("web", "搜一下都柏林明天的天气", {"web_search"}),
    ("web", "查一下后天从都柏林到伦敦的机票", {"flight_search"}),

    ("briefing", "给我来一份今天的新闻简报", {"daily_briefing"}),

    ("system", "我桌面上有多少个文件", {"bash", "find_files"}),
    ("system", "帮我截个屏", {"screenshot"}),

    ("none", "你好", {None}),
    ("none", "谢谢你", {None}),
]


def first_tool(prompt: str) -> str | None:
    """Return the name of the first tool the model would call, or None."""
    messages = [{"role": "system", "content": agent._build_system_prompt(agent._profile or {})},
                {"role": "user", "content": prompt}]
    with contextlib.redirect_stdout(io.StringIO()):   # swallow streamed tokens
        _content, tool_calls, _usage = agent.call_api_stream(messages)
    return tool_calls[0]["name"] if tool_calls else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run only one category")
    ap.add_argument("--runs", type=int, default=1,
                    help="repeat each case N times (the 9B is stochastic; "
                         "use 3-5 for a robust per-case pass-rate)")
    args = ap.parse_args()

    if agent._profile is None:
        agent._profile = agent._load_profile() if agent._PROFILE_PATH.exists() else {}

    cases = [c for c in CASES if not args.only or c[0] == args.only]
    G, R, Y, B, DIM, RESET = ("\033[32m", "\033[31m", "\033[33m",
                              "\033[1m", "\033[90m", "\033[0m")
    total_pass = total = 0
    flaky = []
    cur = None
    for cat, prompt, expected in cases:
        if cat != cur:
            print(f"\n{B}{cat}{RESET}")
            cur = cat
        hits, gots = 0, []
        for _ in range(args.runs):
            try:
                got = first_tool(prompt)
            except Exception as e:
                got = f"<error: {e}>"
            hits += got in expected
            gots.append(got)
        total_pass += hits
        total += args.runs
        rate = hits / args.runs
        mark = (f"{G}✓{RESET}" if rate == 1 else
                f"{R}✗{RESET}" if rate == 0 else f"{Y}~{RESET}")
        suffix = f"  {DIM}{hits}/{args.runs}{RESET}" if args.runs > 1 else ""
        print(f"  {mark} {prompt}{suffix}")
        if rate < 1:
            wrong = {g if g is not None else "(none)" for g in gots if g not in expected}
            exp = "/".join(sorted(str(e) for e in expected))
            print(f"      {DIM}want {exp}; got {', '.join(sorted(wrong))}{RESET}")
            flaky.append((prompt, rate))

    pct = 100 * total_pass // total if total else 0
    color = G if pct >= 85 else (R if pct < 70 else Y)
    print(f"\n{'─' * 50}")
    print(f"tool-selection pass-rate: {color}{total_pass}/{total} ({pct}%){RESET}"
          + (f"  over {args.runs} runs/case" if args.runs > 1 else ""))
    if flaky:
        print(f"{DIM}needs attention (failed or flaky):{RESET}")
        for p, rate in flaky:
            print(f"  • {p}  ({int(rate * 100)}% correct)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
