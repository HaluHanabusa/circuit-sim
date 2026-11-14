#!/usr/bin/env python3
"""W1 の確認用（一時的なファイル。確認が済んだら消す）。

Windows でしか実行されない分岐を1か所ずつ壊して、ハーネスの試験が落ちることを確かめる。
Linux の上では、これらの分岐は実行されないので、壊しても試験は落ちない。GitHub上の Windows で1回だけ流す。

使い方: python scripts/w1_windows_mutants.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "cycle.py"
TESTS = ["tests/test_harness.py", "tests/test_dev_tools.py"]

# （名前、元の文字列、壊した後の文字列）。名前は、実行ログで読めるように英数字にする
MUTANTS = [
    # ロックを掛けずに進む
    ("lock-skip",
     "                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)\n                return\n",
     "                return\n"),
    # 他の実行がロックを持っていても、待たずに進む
    ("lock-no-wait",
     "            except PermissionError:     # 他の実行が持っている\n"
     "                time.sleep(POLL_SECONDS)\n",
     "            except PermissionError:\n                return\n"),
    # Ctrl-Break を終了の合図として扱わない
    ("signal-no-ctrl-break",
     'STOP_SIGNAL_NAMES = ("SIGTERM", "SIGHUP", "SIGBREAK")',
     'STOP_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")'),
    # 拡張子の無い同名のファイル（起動できない）を、そのまま使う
    ("find-bare-file",
     "    if WINDOWS and found and not found.lower().endswith(LAUNCH_SUFFIXES):\n",
     "    if False:\n"),
    # 起動したプロセスの先（claude.cmd が呼んだ本体）を止めない
    ("kill-no-tree",
     '        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)\n',
     "        pass\n"),
    # 開かれているファイルの置き換えを、待たずにあきらめる
    ("busy-no-wait",
     "BUSY_WAIT_SECONDS = 5.0 if WINDOWS else 0.0",
     "BUSY_WAIT_SECONDS = 0.0"),
    # 仕様の場所を、OSの区切り（\\）のまま記録する
    ("path-spec",
     '            "spec": spec.relative_to(root).as_posix(),',
     '            "spec": str(spec.relative_to(root)),'),
    # 確認のログの場所を、OSの区切りのまま記録する
    ("path-check-log",
     '    report = log.relative_to(root).as_posix()\n    adopted = add_record(root, args.id, "check"',
     '    report = str(log.relative_to(root))\n    adopted = add_record(root, args.id, "check"'),
    # ヘッドレス実行のログの場所を、OSの区切りのまま記録する
    ("path-run-log",
     '    report = log.relative_to(root).as_posix()\n    adopted = add_record(root, args.id, args.gate',
     '    report = str(log.relative_to(root))\n    adopted = add_record(root, args.id, args.gate'),
]


def run_tests() -> tuple[bool, str, float]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
    tail = (lines or proc.stdout.strip().splitlines()[-1:] or [""])[0]
    tail = tail.encode("ascii", "backslashreplace").decode("ascii")
    return proc.returncode == 0, tail, time.monotonic() - started


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    passed, tail, seconds = run_tests()
    print(f"baseline: {'pass' if passed else 'FAIL'} ({seconds:.0f}s) {tail}", flush=True)
    if not passed:
        return 2
    survivors = 0
    for number, (name, old, new) in enumerate(MUTANTS, 1):
        assert original.count(old) == 1, f"mutant {number}: the original text was not found exactly once"
        try:
            TARGET.write_text(original.replace(old, new), encoding="utf-8", newline="\n")
            passed, tail, seconds = run_tests()
        finally:
            TARGET.write_text(original, encoding="utf-8", newline="\n")
        survivors += passed
        outcome = "SURVIVED" if passed else "killed"
        print(f"mutant {number} {name}: {outcome} ({seconds:.0f}s) | {tail}", flush=True)
    print(f"mutants: {len(MUTANTS)}, survivors: {survivors}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
