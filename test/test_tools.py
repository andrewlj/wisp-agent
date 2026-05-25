#!/usr/bin/env python3
"""
Wisp unit tests for tools.py — no LLM required.

Directly imports and calls tool functions to verify security scanners,
workspace guards, and core I/O tools.

Usage:
  python test/test_tools.py           # all tests
  python test/test_tools.py --module bash_scanner
  python test/test_tools.py --id u1.3

Run from the wisp-agent project root.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

# ── Bootstrap: add project root to import path ────────────────────────────────

AGENT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(AGENT_DIR))

import yaml as _yaml

def _get_workspace() -> Path:
    try:
        cfg = _yaml.safe_load((AGENT_DIR / "config.yaml").read_text())
        ws  = cfg["agent"].get("workspace", "~/wisp/workspace")
    except Exception:
        ws  = "~/wisp/workspace"
    return Path(ws).expanduser().resolve()

WORKSPACE = _get_workspace()

# Initialise tools with workspace so guards work
from tools import (
    init_workspace,
    _bash_risk,
    _guard_path,
    _parse_dt,
    tool_bash,
    tool_read_file,
    tool_write_file,
    tool_find_files,
    tool_clipboard_read,
    tool_clipboard_write,
    tool_reminders_list,
    tool_reminders_add,
    tool_calendar_list,
    tool_calendar_add,
)

init_workspace(str(WORKSPACE), strict=True)

# ── Colors ────────────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[32m"
    RED    = "\033[31m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"

def _c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"

# ── Unit test case ─────────────────────────────────────────────────────────────

@dataclass
class UnitTest:
    id:       str
    name:     str
    module:   str
    fn:       Callable[[], Optional[str]]   # returns None=PASS, str=FAIL reason
    setup:    Optional[Callable] = field(default=None, repr=False)
    teardown: Optional[Callable] = field(default=None, repr=False)


def run_unit_test(ut: UnitTest) -> tuple[str, str, float]:
    """Returns (status, detail, elapsed_s)."""
    if ut.setup:
        try:
            ut.setup()
        except Exception as e:
            return "FAIL", f"setup error: {e}", 0.0

    t0 = time.time()
    try:
        detail = ut.fn()
    except Exception as e:
        detail = f"exception: {e}"
    elapsed = time.time() - t0

    if ut.teardown:
        try:
            ut.teardown()
        except Exception:
            pass

    if detail is None:
        return "PASS", "", elapsed
    return "FAIL", detail, elapsed


# ── Helper assertions ─────────────────────────────────────────────────────────

def _assert_blocked(result: Optional[str], label: str = "") -> Optional[str]:
    """Expect _bash_risk to return a non-None string."""
    if result is None:
        return f"expected block{' for ' + label if label else ''}, got None (allowed)"
    return None


def _assert_allowed(result: Optional[str], label: str = "") -> Optional[str]:
    """Expect _bash_risk to return None (allowed)."""
    if result is not None:
        return f"expected allow{' for ' + label if label else ''}, got: {result}"
    return None


def _assert_contains(text: str, needle: str) -> Optional[str]:
    if needle not in text:
        return f'expected "{needle}" in: {text[:200]}'
    return None


def _assert_not_contains(text: str, needle: str) -> Optional[str]:
    if needle in text:
        return f'expected "{needle}" NOT in: {text[:200]}'
    return None


# ── Test workspace paths ───────────────────────────────────────────────────────

_UNIT_TEST_DIR  = WORKSPACE / "unit-test-tmp"
_UNIT_FILE      = _UNIT_TEST_DIR / "unit-test.txt"
_UNIT_FILE_COPY = _UNIT_TEST_DIR / "unit-test-copy.txt"


def _setup_unit_dir() -> None:
    _UNIT_TEST_DIR.mkdir(parents=True, exist_ok=True)

def _teardown_unit_dir() -> None:
    shutil.rmtree(_UNIT_TEST_DIR, ignore_errors=True)

def _write_unit_file() -> None:
    _setup_unit_dir()
    _UNIT_FILE.write_text("unit-test-content-2026")


# ═════════════════════════════════════════════════════════════════════════════
# TEST DEFINITIONS
# ═════════════════════════════════════════════════════════════════════════════

TESTS: list[UnitTest] = [

    # ── bash_scanner: destructive commands ────────────────────────────────────

    UnitTest(
        id="u1.1", module="bash_scanner",
        name="rm -rf 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("rm -rf ~/Documents"), "rm -rf"),
    ),
    UnitTest(
        id="u1.2", module="bash_scanner",
        name="rm -r 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("rm -r /tmp/test"), "rm -r"),
    ),
    UnitTest(
        id="u1.3", module="bash_scanner",
        name="sudo 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("sudo ls /private"), "sudo"),
    ),
    UnitTest(
        id="u1.4", module="bash_scanner",
        name="dd if= 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("dd if=/dev/zero of=/dev/disk0"), "dd"),
    ),
    UnitTest(
        id="u1.5", module="bash_scanner",
        name="chmod 777 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("chmod 777 /tmp/file"), "chmod 777"),
    ),

    # ── bash_scanner: workspace writes ────────────────────────────────────────

    UnitTest(
        id="u1.6", module="bash_scanner",
        name="重定向写入 /tmp 被拦截",
        fn=lambda: _assert_blocked(_bash_risk("echo hello > /tmp/out.txt"), "redirect /tmp"),
    ),
    UnitTest(
        id="u1.7", module="bash_scanner",
        name="重定向写入 ~/Desktop 被拦截",
        fn=lambda: _assert_blocked(
            _bash_risk("echo hello > ~/Desktop/out.txt"), "redirect ~/Desktop"),
    ),
    UnitTest(
        id="u1.8", module="bash_scanner",
        name=f"重定向写入 workspace 内路径允许",
        fn=lambda: _assert_allowed(
            _bash_risk(f"echo hello > {WORKSPACE}/out.txt"), "redirect in workspace"),
    ),

    # ── bash_scanner: safe commands not false-positived ────────────────────────

    UnitTest(
        id="u1.9", module="bash_scanner",
        name="2>/dev/null 不误报",
        fn=lambda: _assert_allowed(
            _bash_risk("ls /nonexistent 2>/dev/null"), "/dev/null redirect"),
    ),
    UnitTest(
        id="u1.10", module="bash_scanner",
        name="普通管道命令允许",
        fn=lambda: _assert_allowed(
            _bash_risk("ps aux | grep python"), "ps aux | grep"),
    ),
    UnitTest(
        id="u1.11", module="bash_scanner",
        name="普通 rm 单文件允许",
        fn=lambda: _assert_allowed(
            _bash_risk(f"rm {WORKSPACE}/file.txt"), "single file rm"),
    ),

    # ── workspace guard ────────────────────────────────────────────────────────

    UnitTest(
        id="u2.1", module="workspace_guard",
        name="workspace 内路径允许",
        fn=lambda: (
            None if _guard_path(str(WORKSPACE / "test.txt")) is None
            else f"expected None, got: {_guard_path(str(WORKSPACE / 'test.txt'))}"
        ),
    ),
    UnitTest(
        id="u2.2", module="workspace_guard",
        name="/tmp 路径被拦截",
        fn=lambda: (
            None if _guard_path("/tmp/blocked.txt") is not None
            else "expected block for /tmp path, got None"
        ),
    ),
    UnitTest(
        id="u2.3", module="workspace_guard",
        name="~/Desktop 路径被拦截",
        fn=lambda: (
            None if _guard_path(str(Path("~/Desktop/blocked.txt").expanduser())) is not None
            else "expected block for ~/Desktop path, got None"
        ),
    ),
    UnitTest(
        id="u2.4", module="workspace_guard",
        name="~/Documents 路径被拦截",
        fn=lambda: (
            None if _guard_path(str(Path("~/Documents/blocked.txt").expanduser())) is not None
            else "expected block for ~/Documents path, got None"
        ),
    ),

    # ── tool_bash ──────────────────────────────────────────────────────────────

    UnitTest(
        id="u3.1", module="tool_bash",
        name="简单命令执行成功",
        fn=lambda: _assert_contains(tool_bash("echo wisp-unit-test"), "wisp-unit-test"),
    ),
    UnitTest(
        id="u3.2", module="tool_bash",
        name="exit 0 被正确报告",
        fn=lambda: _assert_contains(tool_bash("true"), "[exit 0]"),
    ),
    UnitTest(
        id="u3.3", module="tool_bash",
        name="非零退出码被正确报告",
        fn=lambda: _assert_contains(tool_bash("false"), "[exit 1]"),
    ),
    UnitTest(
        id="u3.4", module="tool_bash",
        name="rm -rf 返回 security:",
        fn=lambda: _assert_contains(
            tool_bash("rm -rf ~/wisp-blocked-test"), "security:"),
    ),
    UnitTest(
        id="u3.5", module="tool_bash",
        name="写入 /tmp 返回 security:",
        fn=lambda: _assert_contains(
            tool_bash("echo test > /tmp/wisp-unit-block.txt"), "security:"),
    ),

    # ── tool_read_file ────────────────────────────────────────────────────────

    UnitTest(
        id="u4.1", module="tool_read_file",
        name="读取存在的文件",
        fn=lambda: _assert_contains(tool_read_file(str(_UNIT_FILE)), "unit-test-content-2026"),
        setup=_write_unit_file,
    ),
    UnitTest(
        id="u4.2", module="tool_read_file",
        name="读取不存在的文件返回 error:",
        fn=lambda: _assert_contains(
            tool_read_file("/wisp-nonexistent-99999.txt"), "error:"),
    ),
    UnitTest(
        id="u4.3", module="tool_read_file",
        name="读取 workspace 外路径不报 security (read is unrestricted)",
        fn=lambda: _assert_not_contains(
            tool_read_file("/etc/hostname"), "security:"),
    ),

    # ── tool_write_file ───────────────────────────────────────────────────────

    UnitTest(
        id="u5.1", module="tool_write_file",
        name="写入 workspace 内成功",
        fn=lambda: _assert_contains(
            tool_write_file(str(_UNIT_FILE), "written-by-unit-test"), "ok:"),
        setup=_setup_unit_dir,
        teardown=_teardown_unit_dir,
    ),
    UnitTest(
        id="u5.2", module="tool_write_file",
        name="写入 workspace 外返回 security:",
        fn=lambda: _assert_contains(
            tool_write_file("/tmp/wisp-unit-blocked.txt", "blocked"), "security:"),
    ),
    UnitTest(
        id="u5.3", module="tool_write_file",
        name="写入 ~/Desktop 返回 security:",
        fn=lambda: _assert_contains(
            tool_write_file(
                str(Path("~/Desktop/wisp-unit-blocked.txt").expanduser()), "blocked"),
            "security:"),
    ),

    # ── tool_clipboard ────────────────────────────────────────────────────────

    UnitTest(
        id="u6.1", module="tool_clipboard",
        name="clipboard 写入并读回",
        fn=lambda: (
            lambda w, r: None if "wisp-clip-unit-2026" in r
            else f"clipboard read back mismatch: {r[:100]}"
        )(
            tool_clipboard_write("wisp-clip-unit-2026"),
            tool_clipboard_read(),
        ),
    ),

    # ── tool_find_files ───────────────────────────────────────────────────────

    UnitTest(
        id="u7.1", module="tool_find_files",
        name="搜索存在的文件",
        fn=lambda: _assert_not_contains(
            tool_find_files("unit-test.txt", dir=str(_UNIT_TEST_DIR)), "error:"),
        setup=_write_unit_file,
        teardown=_teardown_unit_dir,
    ),
    UnitTest(
        id="u7.2", module="tool_find_files",
        name="搜索不存在的文件返回 no files found",
        fn=lambda: _assert_contains(
            tool_find_files("*.wisp-nonexistent-xq9z7m2p"), "no files found"),
    ),

    # ── _parse_dt ─────────────────────────────────────────────────────────────

    UnitTest(
        id="u8.1", module="parse_dt",
        name="解析 YYYY-MM-DD HH:MM",
        fn=lambda: (
            None if _parse_dt("2026-05-25 14:30") is not None
            and _parse_dt("2026-05-25 14:30").hour == 14
            and _parse_dt("2026-05-25 14:30").minute == 30
            else "failed to parse 2026-05-25 14:30"
        ),
    ),
    UnitTest(
        id="u8.2", module="parse_dt",
        name="解析 YYYY-MM-DD（无时间）",
        fn=lambda: (
            None if _parse_dt("2026-05-25") is not None
            and _parse_dt("2026-05-25").hour == 0
            else "failed to parse 2026-05-25"
        ),
    ),
    UnitTest(
        id="u8.3", module="parse_dt",
        name="非法日期返回 None",
        fn=lambda: (
            None if _parse_dt("not-a-date") is None
            else "expected None for invalid date"
        ),
    ),

    # ── tool_reminders ────────────────────────────────────────────────────────

    UnitTest(
        id="u9.1", module="tool_reminders",
        name="reminders_list — due_before 过滤不报错",
        fn=lambda: _assert_not_contains(
            tool_reminders_list(due_before=date.today().strftime("%Y-%m-%d")), "error:"),
    ),
    UnitTest(
        id="u9.2", module="tool_reminders",
        name="reminders_add — 添加提醒成功",
        setup=lambda: subprocess.run(["osascript", "-e",
            'tell application "Reminders" to tell list "Reminders" to '
            'delete (every reminder whose name is "wisp-unit-test-reminder")'],
            capture_output=True),
        fn=lambda: _assert_contains(
            tool_reminders_add("wisp-unit-test-reminder", due="2099-12-31 09:00",
                               list_name="Reminders"),
            "ok:"),
        teardown=lambda: subprocess.run(["osascript", "-e",
            'tell application "Reminders" to tell list "Reminders" to '
            'delete (every reminder whose name is "wisp-unit-test-reminder")'],
            capture_output=True),
    ),
    UnitTest(
        id="u9.3", module="tool_reminders",
        name="reminders_add — 非法日期返回 error",
        fn=lambda: _assert_contains(
            tool_reminders_add("bad-date-test", due="not-a-date"),
            "error:"),
    ),
    UnitTest(
        id="u9.4", module="tool_reminders",
        name="reminders_list — 指定不存在的清单返回 error",
        fn=lambda: _assert_contains(
            tool_reminders_list(list_name="wisp-nonexistent-list-zzzz"),
            "error:"),
    ),

    # ── tool_calendar ─────────────────────────────────────────────────────────

    UnitTest(
        id="u10.1", module="tool_calendar",
        name="calendar_list — 读取不报错",
        setup=lambda: subprocess.run(["open", "-a", "Calendar"], capture_output=True),
        fn=lambda: _assert_not_contains(tool_calendar_list(), "error:"),
    ),
    UnitTest(
        id="u10.2", module="tool_calendar",
        name="calendar_add — 添加事件成功",
        fn=lambda: _assert_contains(
            tool_calendar_add("wisp-unit-test-event", start="2099-12-31 10:00"),
            "ok:"),
        teardown=lambda: _run(["osascript", "-e",
            'tell application "Calendar" to repeat with cal in every calendar\n'
            '    try\n'
            '        delete (every event of cal whose summary is "wisp-unit-test-event")\n'
            '    end try\n'
            'end repeat']),
    ),
    UnitTest(
        id="u10.3", module="tool_calendar",
        name="calendar_add — 非法日期返回 error",
        fn=lambda: _assert_contains(
            tool_calendar_add("bad-start", start="not-a-date"),
            "error:"),
    ),
    UnitTest(
        id="u10.4", module="tool_calendar",
        name="calendar_add — end 自动补全为 start+1h",
        fn=lambda: (
            lambda r: None if "ok:" in r and "11:00" in r
            else f"expected 11:00 in result, got: {r}"
        )(tool_calendar_add("wisp-unit-test-duration", start="2099-12-31 10:00")),
        teardown=lambda: _run(["osascript", "-e",
            'tell application "Calendar" to repeat with cal in every calendar\n'
            '    try\n'
            '        delete (every event of cal whose summary is "wisp-unit-test-duration")\n'
            '    end try\n'
            'end repeat']),
    ),
    UnitTest(
        id="u10.5", module="tool_calendar",
        name="calendar_list — 指定不存在的日历返回 error",
        fn=lambda: _assert_contains(
            tool_calendar_list(calendar_name="wisp-nonexistent-cal-zzzz"),
            "error:"),
    ),
]


# ── Output ────────────────────────────────────────────────────────────────────

_WIDTH = 60


def _status_icon(status: str) -> str:
    return {
        "PASS": _c(C.GREEN, "✓"),
        "FAIL": _c(C.RED,   "✗"),
    }.get(status, status)


def print_results(results: list[tuple[UnitTest, str, str, float]]) -> None:
    print(f"\n{C.BOLD}══ Wisp Unit Tests (tools.py) {'═' * (_WIDTH - 29)}{C.RESET}\n")

    current_module = ""
    passed = failed = 0

    for ut, status, detail, elapsed in results:
        if ut.module != current_module:
            if current_module:
                print()
            current_module = ut.module
            print(f"  {C.BOLD}{C.CYAN}{ut.module}{C.RESET}")

        id_str   = _c(C.GRAY, f"{ut.id:<7}")
        name_str = f"{ut.name:<42}"
        time_str = _c(C.GRAY, f"[{elapsed*1000:.0f}ms]")
        print(f"  {_status_icon(status)} {id_str} {name_str} {time_str}")
        if detail:
            print(f"         {_c(C.GRAY, detail)}")

        if status == "PASS": passed += 1
        else:                failed += 1

    print(f"\n{'═' * (_WIDTH + 2)}")
    parts = [_c(C.GREEN, f"{passed} passed")]
    if failed:
        parts.append(_c(C.RED, f"{failed} failed"))
    print(f"  {' · '.join(parts)}\n")


def print_list(tests: list[UnitTest]) -> None:
    print(f"\n{C.BOLD}══ Wisp Unit Test Cases {'═' * (_WIDTH - 23)}{C.RESET}\n")
    current_module = ""
    for ut in tests:
        if ut.module != current_module:
            if current_module:
                print()
            current_module = ut.module
            print(f"  {C.BOLD}{C.CYAN}{ut.module}{C.RESET}")
        print(f"  {_c(C.GRAY, ut.id):<14} {ut.name}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wisp unit tests for tools.py — no LLM required",
    )
    parser.add_argument("--module", metavar="NAME",
                        help="Run only this module (e.g. bash_scanner, workspace_guard)")
    parser.add_argument("--id",     metavar="ID",
                        help="Run only this test id (e.g. u1.3)")
    parser.add_argument("--list",   action="store_true",
                        help="List all test cases without running")
    args = parser.parse_args()

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

    print(f"  {C.GRAY}running {len(tests)} unit test(s)…{C.RESET}\n")

    results: list[tuple[UnitTest, str, str, float]] = []
    for ut in tests:
        print(f"  {_c(C.GRAY, '…')} {ut.id:<7} {ut.name}", end="\r", flush=True)
        status, detail, elapsed = run_unit_test(ut)
        results.append((ut, status, detail, elapsed))
        print(" " * 70, end="\r")

    print_results(results)

    if any(s == "FAIL" for _, s, _, _ in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
