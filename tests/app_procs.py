"""アプリの試験（tests/test_app_*.py）の部品: 起動したプロセスを、必ず止める。端末の出力を、待ち続けずに読む。

きっかけ（docs/verification/A0-verifier-5.md の問題2）: PO の PC（Windows 11）で、試験が起動したアプリ本体
（python -m circuit_sim.app --no-browser --port 50000）が、試験の終わった後も待ち受け続けていた。
「起動に失敗して終わるはず」の起動が成功し、試験は終わるのを待つだけで、止める手当てが無かった。

決まり:
1. 試験からプロセスを起動するときは、ここの spawn（または Launched）を通す。起動したものは控えに入る。
2. 試験ごとの見張り（各試験ファイルの no_process_left_behind。中身は reap）が、試験の終わりに動いている
   ものを止めて、その試験を失敗にする。試験の中の後始末（try/finally）を忘れても、プロセスは残らない。
3. 端末の出力は、別のスレッドが読み続ける（Launched）。試験は、待つ上限つきで行を受け取る。製品が何も
   出さなくなる後退が入っても、試験は止まらずに落ちる（readline で待ち続けない）。
4. 止めるときは、子のプロセスも止める（kill_tree）。

標準ライブラリだけで動く。受け入れ試験の見張り（tests/acceptance/acceptance_watch.py）とは別に持つ
（受け入れ試験は、実装者の試験の部品に頼らない。逆も同じ）。
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time

WINDOWS = os.name == "nt"
KILL_WAIT_SECONDS = 20.0

# このプロセス（pytest）が、ここを通して起動したもの（起動した順）
STARTED: list[subprocess.Popen] = []


def spawn(cmd: list[str], **kwargs) -> subprocess.Popen:
    """subprocess.Popen と同じに起動し、控えに入れる。

    POSIX では、自分だけのプロセスのまとまり（セッション）で起動する。止めるときに、その子（アプリ本体が
    起動した偽のブラウザなど）ごと止められる。Windows は、止めるときに taskkill /T で子をたどる。"""
    if not WINDOWS:
        kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(cmd, **kwargs)
    STARTED.append(proc)
    return proc


def kill_tree(proc: subprocess.Popen) -> None:
    """プロセスを、その子ごと止める（もう終わっていれば、何もしない）。

    子ごと止めるのは、親がまだ動いているときだけ。親が終わった後は、まとまりの番号（POSIX）もプロセスの
    番号（Windows）も、ほかのプロセスに使い回されうるので、番号で止めない（ほかの人のプロセスを止めない）。"""
    if proc.poll() is None:
        if WINDOWS:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # spawn が、自分だけのまとまりで起動している
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=KILL_WAIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass


def mark() -> int:
    """いまの控えの位置（reap に渡す）。"""
    return len(STARTED)


def shown(proc: subprocess.Popen) -> str:
    args = proc.args if isinstance(proc.args, (list, tuple)) else [proc.args]
    text = " ".join(" ".join(str(arg).split()) for arg in args)
    return f"PID {proc.pid}: {text if len(text) <= 160 else text[:160] + '…'}"


def reap(since: int, keep: frozenset[int] | set[int] = frozenset()) -> list[str]:
    """since（mark の値）より後に起動して、まだ動いているものを止める。止めたものの説明を返す。

    keep は、止めないプロセスの番号（モジュールの間だけ動かすフィクスチャの持ち物。試験をまたいで動かす）。"""
    left = []
    for proc in STARTED[since:]:
        if proc.poll() is None and proc.pid not in keep:
            left.append(shown(proc))
            kill_tree(proc)
    return left


class Launched:
    """起動したプロセスと、その端末の出力（標準出力と標準エラーを合わせたもの）。

    出力は、別のスレッドが読み続けて控える。試験は、待つ上限つきで行を受け取る。proc の持ち物
    （pid、poll、wait、send_signal、stdin、returncode など）は、そのまま使える。"""

    def __init__(self, cmd: list[str], *, cwd, env: dict[str, str], stdin=subprocess.DEVNULL,
                 creationflags: int = 0) -> None:
        self.proc = spawn(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=stdin, text=True, encoding="utf-8", errors="replace",
                          creationflags=creationflags)
        self.seen: list[str] = []
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.pump = threading.Thread(target=self._read, daemon=True)
        self.pump.start()

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                self.seen.append(line)
                self.lines.put(line)
        except ValueError:
            pass  # 読んでいる間に閉じられた
        self.lines.put(None)

    def __getattr__(self, name: str):
        if name == "proc":  # 起動に失敗して、まだ持っていない
            raise AttributeError(name)
        return getattr(self.proc, name)

    @property
    def out(self) -> str:
        """ここまでに出た出力の全部。"""
        return "".join(self.seen)

    def next_line(self, timeout: float = 10.0) -> str:
        """次の行。timeout 秒のうちに出なければ（アプリ本体が終わっても）、それまでの出力を添えて落とす。"""
        try:
            line = self.lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            raise AssertionError(
                f"{timeout:.1f} 秒たっても、次の行が出ない。ここまでの出力: {self.out!r}"
            ) from None
        if line is None:
            self.lines.put(None)
            raise AssertionError(f"次の行が出ないまま、出力が終わった。ここまでの出力: {self.out!r}")
        return line

    def wait_line(self, start: str = "", *, contains: str = "", timeout: float = 10.0) -> str:
        """start で始まり、contains を含む行が出るまで待つ。出なければ、それまでの出力を添えて落とす。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.next_line(deadline - time.monotonic())
            except AssertionError as exc:
                wanted = f"{start!r} で始まる行" if start else f"{contains!r} を含む行"
                raise AssertionError(f"{wanted}が出ない。{exc}") from None
            if line.startswith(start) and contains in line:
                return line

    def end(self, timeout: float = 30.0) -> tuple[int, str]:
        """終わるのを待ち、（終了コード、出力の全部）を返す。

        timeout 秒たっても終わらなければ（「起動に失敗して終わるはず」が成功した、など）、止めてから
        落とす。"""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            out = self.stop()
            raise AssertionError(
                f"終わるはずのプロセスが {timeout} 秒たっても終わらない（止めた）。出力: {out!r}"
            ) from None
        self.pump.join(timeout=10)
        return code, self.out

    def stop(self) -> str:
        """止める（子も）。出力の全部を返す。もう終わっていても呼べる。"""
        kill_tree(self.proc)
        self.pump.join(timeout=10)
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        return self.out
