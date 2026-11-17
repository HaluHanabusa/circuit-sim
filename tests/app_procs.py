"""アプリの試験（tests/test_app_*.py）の部品: 起動したプロセスを、必ず止める。端末の出力を、待ち続けずに読む。

きっかけ（docs/verification/A0-verifier-5.md の問題2）: PO の PC（Windows 11）で、試験が起動したアプリ本体
（python -m circuit_sim.app --no-browser --port 50000）が、試験の終わった後も待ち受け続けていた。
「起動に失敗して終わるはず」の起動が成功し、試験は終わるのを待つだけで、止める手当てが無かった。

決まり:
1. 試験からプロセスを起動するときは、ここの spawn（または Launched）を通す。起動したものは控えに入る。
2. 試験ごとの見張り（各試験ファイルの no_process_left_behind。中身は reap）が、試験の終わりに動いている
   ものを止めて、その試験を失敗にする。試験の中の後始末（try/finally）を忘れても、プロセスは残らない。
   確かめ終えた（終わっている）ものは控えから外し、持っていたパイプを閉じる（控えが、試験の数に比例して
   パイプを開いたまま持ち続けない。レビュー 6回目の軽微7）。
3. 端末の出力は、別のスレッドが読み続ける（Launched）。試験は、待つ上限つきで行を受け取る。製品が何も
   出さなくなる後退が入っても、試験は止まらずに落ちる（readline で待ち続けない）。
4. 止めるときは、子のプロセスも止める（kill_tree）。ただし、子をたどれるのは親が動いている間だけ。
   親が先に終わった子は、ここでは止めない（下の「止められないもの」）。
5. 起動したものは、試験のプロセス（pytest）と同じプロセスのまとまりに置く（POSIX）。ハーネス
   （scripts/cycle.py の kill_tree。scripts/local_ci.py が、時間切れと中断で使う）が確認をまとまりごと
   止めると、試験が起動したものも一緒に止まる。新しいまとまりが要る起動（疑似端末を、そのプロセスの端末に
   する起動）は spawn(new_session=True) を使う。そのときは、試験のプロセスが終わったら起動したものを止める
   見張り（GUARD）を付ける（検証 6回目の問題2。前は、どの起動も新しいまとまりに入れていたので、ハーネスに
   止められた確認の後に、アプリ本体が待ち受けたまま残った）。ハーネスがまとまりごと止めるのは、時間切れと
   中断（Ctrl-C、終了の合図）のときだけ。

止められないもの（既知の限界）:
- 親が先に終わった子（孫）。POSIX では、試験のプロセスと同じまとまりにいれば、ハーネスがまとまりごと
  止めるとき（時間切れ、中断）には止まる。自分で新しいまとまりに移ったもの（webbrowser が、終わりに & を
  付けた BROWSER のコマンドを起動するときなど）は、親が動いている間に kill_tree が止めなければ、自分で
  終わるまで残る。
- pytest のプロセスだけが強制終了されたとき（メモリ不足など。まとまりごとでない止め方）。見張りは働かず、
  ふつうの起動は残る（新しいまとまりの起動は、命綱の見張りが止める）。
- kill_posix_tree の後始末（finally の SIGKILL）の最中に、さらに例外（3 回目の Ctrl+C など）が入ったとき。
  そこから先の SIGKILL は送られない（Python の合図の扱いは、命令の切れ目ごとに割り込める）。
- Windows の taskkill /T は、親の番号で子をたどる（Microsoft の文書 taskkill: 指定したプロセスと、
  それが起動した子を止める）。Windows は、親が終わった後も子の「親の番号」を書き換えないので、番号が
  使い回されていると、無関係のプロセスを子と見て止める可能性がある（作られた時刻で見分けているかは、
  文書に無い。確かめていない）。scripts/cycle.py の kill_tree も同じ。

標準ライブラリだけで動く。受け入れ試験の見張り（tests/acceptance/acceptance_watch.py）とは別に持つ
（受け入れ試験は、実装者の試験の部品に頼らない。逆も同じ）。
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time

WINDOWS = os.name == "nt"
KILL_WAIT_SECONDS = 20.0
# 子をたどる回数の上限（たどっている間に増えた子を拾い直す。止めた親は、もう子を増やせない）
TREE_ROUNDS = 8

# このプロセス（pytest）が、ここを通して起動し、まだ確かめ終えていないもの（起動した順）。
# 試験の終わりの reap が確かめて、終わっていれば外す（動いているものは外さない）
STARTED: list[subprocess.Popen] = []
_next_number = 0  # 次に起動するものの通し番号（mark が返す。控えから外しても、番号は詰めない）
_lock = threading.Lock()

# 新しいまとまりで起動するときに、本来のコマンドの前に流す見張り（POSIX）。
# 引数: 命綱のパイプの読む側の fd、本来のコマンド…
# 自分を2つに分け、片方（見張り）が命綱を見る。もう片方は、本来のコマンドに入れ替わる（番号はそのまま）。
# 命綱の書く側は、試験のプロセスだけが持つ。試験のプロセスが終わると（ハーネスに止められても）、読む側が
# 入力の終わりになる。そのとき、見張りの親（本来のコマンド）がまだ動いていれば、そのまとまりごと止める。
# 親が先に終わったら（親の番号が変わる）、見張りも終わる。見張りは自分だけのまとまりに移り、端末の合図
# （Ctrl+C など）を受けない。持っている入出力は閉じる（試験が、出力の終わりを待ち続けないように）。
# 命綱は select.poll で見る（select.select は 1024 以上の fd を扱えず、ValueError になる）。見張りの中で
# 思わぬ失敗が起きたら、黙って見張りをやめず、親がまだ動いていれば止める（試験が落ちて、気づける）。
GUARD = """\
import os, select, signal, sys

lifeline = int(sys.argv[1])
command = sys.argv[2:]
target = os.getpid()
if os.fork() == 0:
    try:
        try:
            os.setpgid(0, 0)
            for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            os.closerange(3, lifeline)
            os.closerange(lifeline + 1, 65536)
            poller = select.poll()
            poller.register(lifeline, select.POLLIN)
            while os.getppid() == target:
                if poller.poll(200) and not os.read(lifeline, 1):
                    break
            else:
                os._exit(0)
        except BaseException:
            pass
        if os.getppid() == target:
            os.killpg(target, signal.SIGKILL)
    finally:
        os._exit(0)
os.close(lifeline)
os.execvp(command[0], command)
"""


def spawn(cmd: list[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """subprocess.Popen と同じに起動し、控えに入れる。

    起動したものは、このプロセスと同じプロセスのまとまりに置く（決まり5）。new_session が真なら
    （POSIX だけ）、新しいまとまり（セッション）の先頭で起動し、見張り（GUARD）を付ける。番号（pid）は、
    本来のコマンドのもの。まとまりの指定（start_new_session、process_group）は、ここでは受けない
    （見張りの無い新しいまとまりを作らないため）。"""
    for key in ("start_new_session", "process_group", "preexec_fn"):
        if key in kwargs:
            raise ValueError(f"{key} は使わない（新しいまとまりは new_session=True で作る）")
    lifeline = None
    if new_session:
        if WINDOWS:
            raise ValueError("new_session は POSIX だけ")
        read_end, lifeline = os.pipe()  # どちらも、ほかの子には受け継がれない（O_CLOEXEC）
        cmd = [sys.executable, "-c", GUARD, str(read_end), *cmd]
        kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), read_end)
        kwargs["start_new_session"] = True
    global _next_number
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except BaseException:
        if lifeline is not None:
            os.close(lifeline)
        raise
    finally:
        if new_session:
            os.close(read_end)
    proc.app_procs_lifeline = lifeline  # type: ignore[attr-defined]
    with _lock:
        proc.app_procs_number = _next_number  # type: ignore[attr-defined]
        _next_number += 1
        STARTED.append(proc)
    return proc


def children_table() -> dict[int, list[int]] | None:
    """動いているプロセスの、親の番号 → 子の番号の並び（POSIX）。作れなければ None。

    Linux は /proc、そのほか（macOS など）は ps。"""
    table: dict[int, list[int]] = {}
    if sys.platform.startswith("linux"):
        try:
            names = os.listdir("/proc")
        except OSError:
            return None
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as f:
                    fields = f.read().rsplit(")", 1)[1].split()  # 名前（かっこの中）の後ろ: 状態、親の番号、…
                table.setdefault(int(fields[1]), []).append(int(name))
            except (OSError, IndexError, ValueError):
                continue  # 読む間に終わった
        return table
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table.setdefault(int(parts[1]), []).append(int(parts[0]))
    return table


def kill_posix_tree(root: int) -> None:
    """POSIX: root（このプロセスの子で、まだ受け取っていないもの）と、その子・孫を止める。

    番号を使い回されたほかの人のプロセスを止めないよう、次の順にする:
    1. root を一時停止する（SIGSTOP）。止まっている親は、子を増やせず、終わった子を受け取れない。だから、
       止まっている親の子の番号は、その子が終わっても、受け取られるまでゾンビとして押さえられ、使い回され
       ない。ただし、次の条件のもとでだけ成り立つ:
       - 親が SIGCHLD を無視（SIG_IGN）していない（無視していると、終わった子は止まっている親の下でも
         片付けられ、番号が空く）
       - 一時停止が効いた後（SIGSTOP は送ってすぐには効かない。効くまでのわずかな間は、親が子を受け取り
         うる。一覧を作る間に効く）
       - ほかの者が、その親を止めない（親が終わると、子は別の親に移って受け取られうる）
       試験が起動するもの（アプリ本体、偽のブラウザ、眠るだけの Python）は、どれも SIGCHLD を無視しない
    2. 止めた親の子を一覧から探し、一時停止する。増えた分を拾い直す（TREE_ROUNDS まで）。一時停止が失敗した
       番号（もう無い）は、後で SIGKILL を送らない（その番号は、もう別のプロセスのものかもしれない）
    3. 一時停止したものに、止めた順の逆（子から先）で SIGKILL を送る。どの番号も、送る時点では、止まって
       いる親かこのプロセスが受け取っていない（上の条件のもとで）
    2 の途中で例外（後始末の最中の Ctrl+C など）が起きても、3 は必ず行う（finally）。一時停止したものを、
    止まったまま残さない（残すと、見張りも止まって命綱が効かない。レビュー 7回目の中1）。
    親が先に終わった子（親の番号が 1 などに変わったもの）は、たどれない（モジュールの説明の「止められない
    もの」）。一覧を作れなければ、root だけを止める。"""
    try:
        os.kill(root, signal.SIGSTOP)
    except OSError:
        return  # もう無い
    stopped = [root]  # 一時停止したもの（止めた順）
    seen = {root}
    try:
        for _ in range(TREE_ROUNDS):
            table = children_table()
            if table is None:
                break
            new = [child for parent in stopped for child in table.get(parent, []) if child not in seen]
            if not new:
                break
            for child in new:
                seen.add(child)
                try:
                    os.kill(child, signal.SIGSTOP)
                except OSError:
                    continue  # もう無い
                stopped.append(child)
    finally:
        for pid in reversed(stopped):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def kill_tree(proc: subprocess.Popen) -> None:
    """プロセスを、その子ごと止める（もう終わっていれば、何もしない）。終わった後、見張りの命綱を閉じる。

    子ごと止めるのは、親がまだ動いているときだけ（親が終わった後は、子をたどれない。モジュールの説明）。
    POSIX は kill_posix_tree、Windows は taskkill /T（番号の使い回しについての限界は、モジュールの説明）。"""
    if proc.poll() is None:
        if WINDOWS:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            kill_posix_tree(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=KILL_WAIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is not None:
        close_lifeline(proc)


def close_lifeline(proc: subprocess.Popen) -> None:
    """見張り（GUARD）の命綱を閉じる（見張りは、親がもう終わっていれば、何もせずに終わる）。"""
    lifeline = getattr(proc, "app_procs_lifeline", None)
    if lifeline is not None:
        proc.app_procs_lifeline = None  # type: ignore[attr-defined]
        try:
            os.close(lifeline)
        except OSError:
            pass


def release(proc: subprocess.Popen) -> None:
    """終わったプロセスについて、このプロセスが持っているものを閉じる: 標準入力へのパイプ、命綱。

    出力のパイプは、読んでいるスレッドが、終わりまで読んでから閉じる（Launched）。ほかのスレッドが読んで
    いる最中に閉じると、そのスレッドが読み終えるまで待たされることがあるので、ここでは閉じない
    （控えから外せば、読み終えた後に Python が閉じる）。"""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
    close_lifeline(proc)


def mark() -> int:
    """いまの控えの位置（reap に渡す）。この後に起動したものの通し番号は、この値から。"""
    with _lock:
        return _next_number


def shown(proc: subprocess.Popen) -> str:
    args = proc.args if isinstance(proc.args, (list, tuple)) else [proc.args]
    if args[:2] == [sys.executable, "-c"] and len(args) > 3 and args[2] == GUARD:
        args = args[4:]  # 見張りの分は見せない
    text = " ".join(" ".join(str(arg).split()) for arg in args)
    return f"PID {proc.pid}: {text if len(text) <= 160 else text[:160] + '…'}"


def reap(since: int, keep: frozenset[int] | set[int] = frozenset()) -> list[str]:
    """since（mark の値）より後に起動して、まだ動いているものを止める。止めたものの説明を返す。

    keep は、止めないプロセスの番号（モジュールの間だけ動かすフィクスチャの持ち物。試験をまたいで動かす）。
    確かめたもののうち、終わっているものは控えから外し、持っているもの（release）を閉じる。"""
    with _lock:
        mine = [proc for proc in STARTED
                if getattr(proc, "app_procs_number", 0) >= since and proc.pid not in keep]
    left = []
    for proc in mine:
        if proc.poll() is None:
            left.append(shown(proc))
            kill_tree(proc)
    ended = [proc for proc in mine if proc.poll() is not None]
    with _lock:
        gone = {id(proc) for proc in ended}
        STARTED[:] = [proc for proc in STARTED if id(proc) not in gone]
    for proc in ended:
        release(proc)
    return left


class Launched:
    """起動したプロセスと、その端末の出力（標準出力と標準エラーを合わせたもの）。

    出力は、別のスレッドが読み続けて控え、終わりまで読んだら閉じる。試験は、待つ上限つきで行を受け取る。
    proc の持ち物（pid、poll、wait、send_signal、stdin、returncode など）は、そのまま使える。
    new_session は spawn と同じ（疑似端末を、そのプロセスの端末にする起動だけが使う）。"""

    def __init__(self, cmd: list[str], *, cwd, env: dict[str, str], stdin=subprocess.DEVNULL,
                 creationflags: int = 0, new_session: bool = False) -> None:
        self.proc = spawn(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=stdin, text=True, encoding="utf-8", errors="replace",
                          creationflags=creationflags, new_session=new_session)
        self.seen: list[str] = []
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.pump = threading.Thread(target=self._read, daemon=True)
        self.pump.start()

    def _read(self) -> None:
        stdout = self.proc.stdout
        try:
            for line in stdout:
                self.seen.append(line)
                self.lines.put(line)
        except ValueError:
            pass  # 読んでいる間に閉じられた
        finally:
            try:
                stdout.close()  # 読むのは、このスレッドだけ。終わりまで読んだので閉じる
            except (OSError, ValueError):
                pass
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
        """止める（子も。親が動いている間にたどれる子だけ）。出力の全部を返す。もう終わっていても呼べる。"""
        kill_tree(self.proc)
        self.pump.join(timeout=10)
        release(self.proc)
        return self.out
