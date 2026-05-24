#!/usr/bin/env python3
"""
Wisp automated test suite — v0.6

Usage:
  python test/run_tests.py                     # all tests
  python test/run_tests.py --module bash       # one module
  python test/run_tests.py --id 1.4           # one test
  python test/run_tests.py --list             # list all test cases

Run from the wisp-agent project root.
Requires: local LLM server running (config.yaml).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

AGENT_DIR = Path(__file__).parent.parent.resolve()
AGENT_PY  = AGENT_DIR / "agent.py"


def _get_workspace() -> Path:
    """Read workspace path from config.yaml; fall back to default."""
    try:
        import yaml
        cfg = yaml.safe_load((AGENT_DIR / "config.yaml").read_text())
        ws = cfg["agent"].get("workspace", "~/wisp/workspace")
    except Exception:
        ws = "~/wisp/workspace"
    return Path(ws).expanduser().resolve()


WORKSPACE = _get_workspace()

# ── Colors ────────────────────────────────────────────────────────────────────


class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[32m"
    RED    = "\033[31m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"
    DIM    = "\033[2m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[^m]*m", "", text)


# ── Test Case ─────────────────────────────────────────────────────────────────


@dataclass
class TestCase:
    id:          str
    name:        str
    module:      str
    prompt:      str
    expect:      list[str]           = field(default_factory=list)
    # Strings that MUST ALL appear in output
    reject:      list[str]           = field(default_factory=list)
    # Strings that must NOT appear in output
    check_file:  str                 = ""
    # Path that must exist after the test runs
    timeout:     int                 = 90
    skip:        bool                = False
    skip_reason: str                 = ""
    setup:       Optional[Callable]  = field(default=None, repr=False)
    teardown:    Optional[Callable]  = field(default=None, repr=False)
    post_check:  Optional[Callable]  = field(default=None, repr=False)
    # post_check() → None means PASS, returns an error string means FAIL


# ── Runner ────────────────────────────────────────────────────────────────────


def run_wisp(prompt: str, timeout: int) -> tuple[str, bool]:
    """Run wisp one-shot.  Returns (stripped_output, timed_out)."""
    try:
        result = subprocess.run(
            [sys.executable, str(AGENT_PY), prompt],
            cwd=str(AGENT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw = result.stdout + result.stderr
        return _strip_ansi(raw), False
    except subprocess.TimeoutExpired:
        return "", True
    except Exception as e:
        return f"runner error: {e}", False


def run_test(tc: TestCase) -> tuple[str, str, float]:
    """Run one test.  Returns (status, detail, elapsed_s).
    Status is one of: PASS / FAIL / SKIP / TIMEOUT.
    """
    if tc.skip:
        return "SKIP", tc.skip_reason, 0.0

    if tc.setup:
        try:
            tc.setup()
        except Exception as e:
            return "FAIL", f"setup error: {e}", 0.0

    t0 = time.time()
    output, timed_out = run_wisp(tc.prompt, tc.timeout)
    elapsed = time.time() - t0

    if timed_out:
        return "TIMEOUT", f"exceeded {tc.timeout}s", elapsed

    failures: list[str] = []

    for s in tc.expect:
        if s not in output:
            failures.append(f'expect "{s}" not found')

    for s in tc.reject:
        if s in output:
            failures.append(f'reject "{s}" found in output')

    if tc.check_file:
        p = Path(tc.check_file).expanduser()
        if not p.exists():
            failures.append(f"file not found: {p}")

    if tc.post_check:
        try:
            err = tc.post_check()
            if err:
                failures.append(err)
        except Exception as e:
            failures.append(f"post_check error: {e}")

    if tc.teardown:
        try:
            tc.teardown()
        except Exception:
            pass

    if failures:
        return "FAIL", " · ".join(failures), elapsed
    return "PASS", "", elapsed


# ── Setup/teardown helpers ────────────────────────────────────────────────────

_TEST_FILE = WORKSPACE / "test-auto.txt"
_TEST_COPY = WORKSPACE / "test-auto-copy.txt"
_HELLO_TXT = WORKSPACE / "hello.txt"


def _ensure_test_file() -> None:
    """Create test-auto.txt in workspace if it doesn't exist."""
    if not _TEST_FILE.exists():
        _TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TEST_FILE.write_text("wisp-auto-test-2026")


def _ensure_test_copy() -> None:
    """Create test-auto-copy.txt in workspace if it doesn't exist."""
    _ensure_test_file()
    if not _TEST_COPY.exists():
        import shutil
        shutil.copy2(_TEST_FILE, _TEST_COPY)


def _cleanup_hello() -> None:
    _HELLO_TXT.unlink(missing_ok=True)


# ── Test Cases ────────────────────────────────────────────────────────────────

TESTS: list[TestCase] = [

    # ── bash ─────────────────────────────────────────────────────────────────

    TestCase(
        id="1.1", module="bash",
        name="基础执行 — whoami",
        prompt="用bash工具运行whoami命令，告诉我结果",
        expect=["exit 0"],
        reject=["error:"],
        timeout=60,
    ),

    TestCase(
        id="1.2", module="bash",
        name="/dev/null 不触发沙箱警告",
        prompt="用bash工具运行这条命令并告诉我输出：ls /nonexistent-wisp-path 2>/dev/null",
        reject=["security:"],
        timeout=60,
    ),

    TestCase(
        id="1.3", module="bash",
        name="写入 workspace 外路径被拦截",
        prompt='用bash工具把字符串"wisp-sandbox-test"用shell重定向写入文件'
               '/tmp/wisp-sandbox-test.txt',
        # LLM 拒绝 或 bash scanner 拦截都算通过；验证文件未被创建
        post_check=lambda: (
            "file was created at /tmp/wisp-sandbox-test.txt — sandbox failed"
            if Path("/tmp/wisp-sandbox-test.txt").exists() else None
        ),
        timeout=60,
    ),

    TestCase(
        id="1.4", module="bash",
        name="危险命令 rm -rf 被拦截",
        prompt="用bash工具运行 rm -rf 命令删除目录 ~/wisp/rm-safety-test-dir",
        # LLM 拒绝 或 bash scanner 拦截都算通过；验证目录未被删除
        setup=lambda: (
            Path("~/wisp/rm-safety-test-dir").expanduser().mkdir(parents=True, exist_ok=True)
        ),
        post_check=lambda: (
            None if Path("~/wisp/rm-safety-test-dir").expanduser().exists()
            else "directory was deleted — rm -rf was not blocked"
        ),
        teardown=lambda: (
            __import__("shutil").rmtree(
                Path("~/wisp/rm-safety-test-dir").expanduser(), ignore_errors=True
            )
        ),
        timeout=60,
    ),

    TestCase(
        id="1.5", module="bash",
        name="危险命令 sudo 被拦截",
        prompt="用bash工具运行 sudo ls /private",
        expect=["security:"],
        timeout=60,
    ),

    # ── read_file / write_file ────────────────────────────────────────────────

    TestCase(
        id="2.1", module="files",
        name="read_file — 读知识库",
        prompt="用read_file工具读一下~/wisp/knowledge.md的内容",
        expect=["工具知识库"],
        timeout=60,
    ),

    TestCase(
        id="2.2", module="files",
        name="write_file — 写入 workspace 成功",
        prompt=f'用write_file工具把内容"wisp-auto-test-2026"写入文件{_TEST_FILE}',
        expect=["ok:"],
        timeout=60,
    ),

    TestCase(
        id="2.3", module="files",
        name="write_file — workspace 外被拦截",
        prompt='用write_file工具把内容"blocked"写入文件~/Desktop/wisp-blocked-test.txt',
        expect=["security:"],
        timeout=60,
    ),

    TestCase(
        id="2.4", module="files",
        name="read_file — 不存在的文件返回 error",
        prompt="用read_file工具读一下/wisp-nonexistent-99999.txt",
        expect=["error:"],
        timeout=60,
    ),

    # ── mac system ────────────────────────────────────────────────────────────

    TestCase(
        id="3.1", module="mac",
        name="screenshot — 截全屏保存到 workspace",
        prompt="用screenshot工具截一张全屏，保存到workspace目录",
        expect=["ok:"],
        timeout=60,
    ),

    TestCase(
        id="3.2", module="mac",
        name="list_apps — 列出运行中的应用",
        prompt="用list_apps工具列出现在运行中的应用",
        reject=["error:"],
        timeout=60,
    ),

    TestCase(
        id="3.3", module="mac",
        name="osascript — 系统通知（人工验证）",
        skip=True,
        skip_reason="通知弹窗无法程序验证，请手动测试",
        prompt="",
    ),

    TestCase(
        id="3.4", module="mac",
        name="clipboard_write + clipboard_read",
        prompt='用clipboard_write工具把文字"wisp-clip-test-2026"写入剪贴板，再用clipboard_read读出来',
        expect=["wisp-clip-test-2026"],
        timeout=60,
    ),

    # ── file operations ───────────────────────────────────────────────────────

    TestCase(
        id="4.1", module="fileops",
        name="find_files — 搜索 workspace 中的 txt",
        prompt=f"用find_files工具搜索目录{WORKSPACE}下的txt文件",
        reject=["error:"],
        timeout=60,
    ),

    TestCase(
        id="4.2", module="fileops",
        name="copy_file — workspace 内复制",
        prompt=f"用copy_file工具把文件{_TEST_FILE}复制到{_TEST_COPY}",
        expect=["ok:"],
        timeout=60,
        setup=_ensure_test_file,
    ),

    TestCase(
        id="4.3", module="fileops",
        name="delete_file — 删除 workspace 内文件",
        prompt=f"用delete_file工具删除文件{_TEST_COPY}",
        expect=["ok:"],
        timeout=60,
        setup=_ensure_test_copy,
    ),

    TestCase(
        id="4.4", module="fileops",
        name="delete_file — workspace 外被拦截",
        prompt="用delete_file工具删除文件~/Desktop/wisp-nonexistent-blocked.txt",
        expect=["security:"],
        timeout=60,
    ),

    TestCase(
        id="4.5", module="fileops",
        name="move_file — 目标在 workspace 外被拦截",
        prompt=f"用move_file工具把{_TEST_FILE}移动到~/Documents/wisp-test-moved.txt",
        expect=["security:"],
        timeout=60,
        setup=_ensure_test_file,
    ),

    # ── task checkpoint ───────────────────────────────────────────────────────

    TestCase(
        id="5.1", module="task",
        name="task_init — 多步任务完整流程",
        prompt=f"帮我做一个3步测试任务："
               f"1.用write_file在{WORKSPACE}/hello.txt写入内容'hello wisp' "
               f"2.用read_file验证文件内容 "
               f"3.用bash输出文件大小。"
               f"必须用task_init创建任务并追踪每一步",
        expect=["task"],
        timeout=150,
        teardown=_cleanup_hello,
    ),

    # ── learn ─────────────────────────────────────────────────────────────────

    TestCase(
        id="6.1", module="learn",
        name="learn — 记录工具规则",
        prompt="用learn工具记录一条规则：'自动化测试验证文件存在应该用bash的test命令，"
               "不要用read_file'，类别是bash",
        expect=["ok:"],
        timeout=60,
    ),

    # ── browser (SKIP — 需人工) ────────────────────────────────────────────────

    TestCase(
        id="7.1", module="browser",
        name="browser_open — 打开网页",
        skip=True,
        skip_reason="browser 测试需人工验证（速度慢 + 需要显示器）",
        prompt="",
    ),

    TestCase(
        id="7.2", module="browser",
        name="browser_get_text + browser_close",
        skip=True,
        skip_reason="browser 测试需人工验证",
        prompt="",
    ),

    # ── daily_briefing (SKIP — 太慢) ──────────────────────────────────────────

    TestCase(
        id="8.1", module="briefing",
        name="daily_briefing — 完整晨报",
        skip=True,
        skip_reason="耗时 60-90s，依赖网络，建议手动运行",
        prompt="",
    ),
]

# ── Output ────────────────────────────────────────────────────────────────────

_WIDTH = 60


def _status_icon(status: str) -> str:
    return {
        "PASS":    _c(C.GREEN,  "✓"),
        "FAIL":    _c(C.RED,    "✗"),
        "SKIP":    _c(C.GRAY,   "·"),
        "TIMEOUT": _c(C.YELLOW, "⏱"),
    }.get(status, status)


def print_results(results: list[tuple[TestCase, str, str, float]]) -> None:
    print(f"\n{C.BOLD}══ Wisp Test Suite v0.6 {'═' * (_WIDTH - 23)}{C.RESET}\n")

    current_module = ""
    passed = failed = skipped = timedout = 0

    for tc, status, detail, elapsed in results:
        if tc.module != current_module:
            if current_module:
                print()
            current_module = tc.module
            print(f"  {C.BOLD}{C.CYAN}{tc.module}{C.RESET}")

        id_str   = _c(C.GRAY, f"{tc.id:<5}")
        name_str = f"{tc.name:<40}"
        time_str = _c(C.GRAY, f"[{elapsed:.1f}s]") if elapsed > 0 else ""
        print(f"  {_status_icon(status)} {id_str} {name_str} {time_str}")
        if detail:
            print(f"         {_c(C.GRAY, detail)}")

        if status == "PASS":        passed   += 1
        elif status == "FAIL":      failed   += 1
        elif status == "SKIP":      skipped  += 1
        elif status == "TIMEOUT":   timedout += 1

    print(f"\n{'═' * (_WIDTH + 2)}")
    parts = [_c(C.GREEN, f"{passed} passed")]
    if failed:   parts.append(_c(C.RED,    f"{failed} failed"))
    if timedout: parts.append(_c(C.YELLOW, f"{timedout} timeout"))
    if skipped:  parts.append(_c(C.GRAY,   f"{skipped} skipped"))
    print(f"  {' · '.join(parts)}\n")


def print_list(tests: list[TestCase]) -> None:
    print(f"\n{C.BOLD}══ Wisp Test Cases {'═' * (_WIDTH - 19)}{C.RESET}\n")
    current_module = ""
    for tc in tests:
        if tc.module != current_module:
            if current_module:
                print()
            current_module = tc.module
            print(f"  {C.BOLD}{C.CYAN}{tc.module}{C.RESET}")
        skip_tag = _c(C.GRAY, "  [skip]") if tc.skip else ""
        print(f"  {_c(C.GRAY, tc.id):<14} {tc.name}{skip_tag}")
    print()


# ── LLM connectivity check ────────────────────────────────────────────────────


def _check_llm_reachable() -> bool:
    """Quick connectivity check against the LLM server."""
    try:
        import yaml, requests
        cfg     = yaml.safe_load((AGENT_DIR / "config.yaml").read_text())
        url     = cfg["server"]["base_url"]
        api_key = cfg["server"]["api_key"]
        # Use a minimal non-streaming call
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": cfg["model"]["name"],
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 5, "stream": False},
            timeout=(5, 15),
        )
        return resp.status_code < 500
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wisp automated test suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--module", metavar="NAME",
                        help="Run only tests in this module (bash, files, mac, fileops, task, learn)")
    parser.add_argument("--id",     metavar="ID",
                        help="Run only the test with this id (e.g. 1.4)")
    parser.add_argument("--list",   action="store_true",
                        help="List all test cases without running them")
    parser.add_argument("--no-check", action="store_true",
                        help="Skip LLM connectivity check")
    args = parser.parse_args()

    # Filter
    tests = TESTS
    if args.module:
        tests = [t for t in tests if t.module == args.module]
    if args.id:
        tests = [t for t in tests if t.id == args.id]

    if not tests:
        print("No tests matched.")
        sys.exit(0)

    if args.list:
        print_list(tests)
        return

    # Sanity checks
    if not AGENT_PY.exists():
        print(f"{C.RED}error: agent.py not found at {AGENT_PY}{C.RESET}")
        sys.exit(1)

    if not args.no_check:
        print(f"  {C.DIM}checking LLM server…{C.RESET}", end="\r", flush=True)
        if not _check_llm_reachable():
            print(f"  {C.RED}✗  LLM server unreachable — is it running? (skip with --no-check){C.RESET}")
            sys.exit(1)
        print(f"  {C.GREEN}✓  LLM server OK{C.RESET}           ")

    n_run = sum(1 for t in tests if not t.skip)
    print(f"  {C.DIM}running {n_run} test(s) from {AGENT_DIR.name}…{C.RESET}\n")

    results: list[tuple[TestCase, str, str, float]] = []
    for tc in tests:
        # Inline progress
        print(f"  {_c(C.GRAY, '…')} {tc.id:<5} {tc.name}", end="\r", flush=True)
        status, detail, elapsed = run_test(tc)
        results.append((tc, status, detail, elapsed))
        # Clear progress line
        print(" " * 70, end="\r")

    print_results(results)

    if any(s in ("FAIL", "TIMEOUT") for _, s, _, _ in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
