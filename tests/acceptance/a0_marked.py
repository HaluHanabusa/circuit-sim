"""受け入れ試験の部品: 番号からプロセスに合図を送る前に、印で自分のものと確かめる（D-078）。

きっかけ（A0 の検証 8回目、レビュー 8回目の中1・軽微3）: 後始末で、ファイルや一覧から読んだ番号に、そのまま
SIGTERM / SIGKILL を送る所があった。その番号のプロセスが先に終わって受け取られていると、番号は別の
プロセスに使い回されうる（この作業環境は pid_max が 32768 で、試験の間に番号が一周する。Windows は番号を
すぐに使い回す）。そのときは、試験と関係の無いプロセスに本物の合図が届く。

決まり:
- Linux: /proc の環境変数（environ。"名前=値" の 1 項目として）と、引数（cmdline）で確かめる。与えた条件の
  どれか 1 つでも合わなければ送らない。ゾンビ（終わって受け取られていないもの）は、環境変数も引数も読めない
  ので、送らない（止める必要も無い）。
- ほかの OS: 環境変数は読めないので、引数（acceptance_watch.list_processes のコマンドライン）だけで確かめる。
  引数の条件を与えなかったときは送らない（止め損ねたものは、試験の部品が自分で終わる形にしてある）。

部品の止め方そのもの（tests/app_procs.py）を確かめる試験では、これではなく、許す番号の一覧を持つ gate
（test_a0_fix7_procs.py）や、合図を送らない身代わり（test_a0_fix8_procs_browser.py）を使う。
"""
from __future__ import annotations

import sys
from pathlib import Path

import acceptance_watch as watch

LINUX = sys.platform.startswith("linux")


def _linux_read(pid: int, name: str) -> bytes | None:
    try:
        return Path(f"/proc/{pid}/{name}").read_bytes()
    except OSError:
        return None


def is_marked(pid: int, *, environ: str | None = None, command: str | None = None) -> bool:
    """pid のプロセスが、与えた印をどれも持つか（どれも与えなければ偽）。

    environ は "名前=値"（Linux だけで確かめる。ほかの OS では、これだけでは偽）。command は、引数（コマンド
    ライン）に含まれる文字列。"""
    if environ is None and command is None:
        return False
    if LINUX:
        if environ is not None:
            data = _linux_read(pid, "environ")
            if not data or environ.encode() not in data.split(b"\0"):
                return False
        if command is not None:
            data = _linux_read(pid, "cmdline")
            if not data or command not in data.replace(b"\0", b" ").decode("utf-8", "replace"):
                return False
        return True
    if command is None:
        return False
    listed = watch.list_processes()
    if listed is None:
        return False
    return any(process.pid == pid and command in process.command for process in listed[0])


def kill_if_marked(pid: int, *, environ: str | None = None, command: str | None = None) -> bool:
    """印を確かめられたときだけ、番号で止める（acceptance_watch.kill_pid）。止めたら真。"""
    if not is_marked(pid, environ=environ, command=command):
        return False
    return watch.kill_pid(pid)
