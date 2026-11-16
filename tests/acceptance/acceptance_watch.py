"""受け入れ試験の見張りの中身: 試験が起動したプロセスを、残さない（配線は conftest.py）。

きっかけ（A0 の検証 5回目、docs/verification/A0-verifier-5.md）: PO の PC（Windows 11）で、試験が起動した
アプリ本体（python -m circuit_sim.app --no-browser --port 50000）が、試験の終わった後も 127.0.0.1:50000 で
待ち受け続けていた。「起動に失敗して終わるはず」の起動が成功し、試験は終わるのを待つだけで、止める手当てが
無かった。利用者の PC に、待ち受けるプロセスを残すのは、試験として許されない。

ここが受け持つこと:
1. 受け入れ試験が subprocess.Popen で起動したプロセスを、すべて控える（Tracker）。試験の終わりに動いて
   いるものは、止めて、その試験を失敗にする。モジュールの終わりにも、同じことをする（モジュールの間だけ
   動かすフィクスチャの分）。準備の途中で失敗したフィクスチャが起動したものは、その場で止める。
2. 試験の実行の終わりに、この実行から生まれたプロセス（子、孫、親が先に終わったもの）のうち、アプリ本体
   らしいものが残っていないかを調べる（leftover_apps）。残っていたら、実行の全体を失敗にする。
3. 試験を始める前から動いているアプリ本体らしいプロセスを、知らせる（前の実行の残りに気づけるように。
   利用者が自分で動かしているものかもしれないので、止めない。失敗にもしない）。

「この実行から生まれた」の見分け方（ほかの人やほかの実行のプロセスを、止めないため）:
- Linux: 実行ごとの印を環境変数（MARK_VAR）に置く。子も孫も引き継ぐので、/proc/<pid>/environ に印がある
  プロセスだけが、この実行のもの（親が先に終わっていても分かる）。
- そのほかの OS: プロセスの一覧（Windows は PowerShell の Get-CimInstance、macOS などは ps）の親子の関係を
  たどる。試験のプロセス（pytest）から、動いている親をたどって届くものは「確か」。親がもう無く、その番号が
  この実行で控えたプロセスの番号のものは「たぶん」（番号は使い回されるので、止めずに知らせる）。

標準ライブラリだけで動く。調べられなかったときは、調べられなかったと知らせる（黙って合格にしない）。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
# この実行の印を入れる環境変数（値は、実行ごとに違う）
MARK_VAR = "CIRCUIT_SIM_ACCEPTANCE_RUN"
# アプリ本体が残っていた実行の終了コード（tests/conftest.py の 70、71 の続き）
LEFTOVER_EXIT_CODE = 72
# 「止めた直後で、終わりかけている」プロセスを、残りと数えないための猶予
GRACE_SECONDS = 3.0
KILL_WAIT_SECONDS = 20.0
LIST_TIMEOUT_SECONDS = 60.0

# アプリ本体らしいコマンドライン:
# - python -m circuit_sim.app、circuit_sim.app を起動する -c の入口（偽のブラウザの見張りつきの起動など）
# - circuit_sim/app/ の中のファイルを直接
# - コマンドそのものが circuit-sim（Windows は circuit-sim.exe。場所に空白があれば引用符つき）
# - python と、その次に circuit-sim のスクリプト（POSIX の circuit-sim コマンドは、こう見える）
# リポジトリの場所の名前（…/circuit-sim）が引数に出るだけのものは、数えない
APP_LIKE = re.compile(
    r"circuit_sim\.app"
    r"|circuit_sim[\\/]app[\\/]"
    r"|^(?:\"[^\"]*[\\/]|[^\s\"]*[\\/])?circuit-sim(?:\.exe)?\"?(?:\s|$)"
    r"|^\S*python\S*\s+(?:\"[^\"]*[\\/]|\S*[\\/])?circuit-sim(?:-script\.pyw?)?\"?(?:\s|$)"
)

REAL_POPEN = subprocess.Popen


def shown(text: str, limit: int = 200) -> str:
    """表示するコマンドライン。利用者のホームは ~ にし（記録に利用者名を残さない）、長いものは切る。"""
    try:
        home = str(Path.home())
    except RuntimeError:
        home = ""
    if home and len(home) > 1:
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def command_text(args: object) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        return os.fsdecode(args)
    try:
        return " ".join(os.fsdecode(part) for part in args)  # type: ignore[union-attr]
    except TypeError:
        return repr(args)


@dataclass
class Started:
    """受け入れ試験が起動したプロセス。owner は、後始末を受け持つ側: test（その試験）、scope（モジュールの間
    だけ動かすフィクスチャ）。"""

    proc: subprocess.Popen
    owner: str
    where: str
    number: int     # 起動した順の通し番号

    def describe(self) -> str:
        where = self.where or "不明"
        return f"PID {self.proc.pid}: {shown(command_text(self.proc.args))}（起動した所: {where}）"


def run_helper(cmd: list[str], timeout: float, capture: bool = False) -> tuple[int, bytes, int] | None:
    """見張りが使う道具（taskkill、PowerShell、ps）を流す。（終了コード、出力、道具の番号）。

    流せなければ None。控えを取らない Popen で起動する。時間が切れたら、道具を止めて None（待ち続けない）。"""
    try:
        helper = REAL_POPEN(cmd, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = helper.communicate(timeout=timeout)
        return helper.returncode, out or b"", helper.pid
    except (OSError, subprocess.SubprocessError):
        try:
            helper.kill()
            helper.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    finally:
        if helper.stdout is not None:
            helper.stdout.close()


def kill_tree(proc: subprocess.Popen) -> None:
    """プロセスを止める。Windows では、その子（アプリ本体が起動したブラウザのコマンドなど）も止める。

    Windows は、親を止めても子は残る。親が動いている間なら、taskkill /T が子をたどれる（scripts/cycle.py の
    kill_tree と同じ）。POSIX では親だけを止める（子は、実行の終わりの確かめが、印で見つける）。"""
    if WINDOWS and proc.poll() is None:
        run_helper(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=30)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_WAIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass


class Tracker:
    """受け入れ試験が subprocess.Popen で起動したプロセスの控え。

    install している間、subprocess.Popen を、控えを取るだけの子クラスに差し替える（起動のしかたは変えない）。
    subprocess.run も、この名前を呼ぶときに引くので、控えに入る。先に自分の子クラスを作っているもの
    （Windows の asyncio の子プロセス。playwright が使う）は、入らない。"""

    def __init__(self) -> None:
        self.started: list[Started] = []    # まだ確かめていないもの
        self.pids: set[int] = set()         # 控えたことのあるプロセスの番号（確かめた後も残す）
        self.next_number = 0
        self.where = ""
        self.scoped = 0      # モジュールの間だけ動かすフィクスチャの準備の中にいる深さ
        self.installed = 0
        tracker = self

        class TrackedPopen(REAL_POPEN):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                tracker.add(self)

        self.popen_class = TrackedPopen

    def install(self) -> None:
        if self.installed == 0:
            subprocess.Popen = self.popen_class  # type: ignore[misc]
        self.installed += 1

    def uninstall(self) -> None:
        self.installed -= 1
        if self.installed == 0 and subprocess.Popen is self.popen_class:
            subprocess.Popen = REAL_POPEN  # type: ignore[misc]

    def add(self, proc: subprocess.Popen) -> None:
        owner = "scope" if self.scoped else "test"
        self.started.append(Started(proc, owner, self.where, self.next_number))
        self.next_number += 1
        self.pids.add(proc.pid)

    def count(self) -> int:
        """次に起動するプロセスの通し番号（ここから後に起動したものを、since で取り出す）。"""
        return self.next_number

    def since(self, mark: int, owner: str | None = None) -> list[Started]:
        found = [item for item in self.started if item.number >= mark]
        return [item for item in found if owner is None or item.owner == owner]

    def reap(self, items: list[Started], grace: float = GRACE_SECONDS) -> list[str]:
        """items のうち、まだ動いているものを止める。止めたものの説明を返す（何も残っていなければ空）。

        止める合図を受けて終わりかけているものは、grace 秒まで待つ（残りと数えない）。
        確かめ終えた items は、控えから外す（番号だけ pids に残す）。控えが Popen を持ち続けると、試験が
        閉じなかったパイプが実行の終わりまで開いたままになる。"""
        alive = [item for item in items if item.proc.poll() is None]
        deadline = time.monotonic() + grace
        while alive and time.monotonic() < deadline:
            time.sleep(0.05)
            alive = [item for item in alive if item.proc.poll() is None]
        for item in alive:
            kill_tree(item.proc)
        done = {id(item) for item in items}
        self.started = [item for item in self.started if id(item) not in done]
        return [item.describe() for item in alive]


@dataclass(frozen=True)
class Process:
    """プロセスの一覧の1行。created は作られた時刻（秒。分からなければ None）。"""

    pid: int
    parent: int
    command: str
    created: float | None = None


def proc_processes(proc_dir: str = "/proc") -> list[Process] | None:
    """Linux: /proc から、動いているプロセスの一覧を作る（読めるものだけ）。/proc が無ければ None。"""
    try:
        names = os.listdir(proc_dir)
    except OSError:
        return None
    found = []
    for name in names:
        if not name.isdigit():
            continue
        base = Path(proc_dir, name)
        try:
            raw = base.joinpath("cmdline").read_bytes()
            stat = base.joinpath("stat").read_text(encoding="utf-8", errors="replace")
            fields = stat.rsplit(")", 1)[1].split()   # 名前（かっこの中）の後ろ: 状態、親の番号、…
            command = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, IndexError):
            continue
        if fields[0] != "Z":        # 終わっていて、親の受け取りを待つだけのものは数えない
            found.append(Process(int(name), int(fields[1]), command.strip()))
    return found or None


def marked_processes(token: str, proc_dir: str = "/proc") -> list[Process] | None:
    """Linux: この実行の印（MARK_VAR=token）を環境変数に持つ、動いているプロセス（自分を除く）。

    /proc が無ければ None。読めないプロセス（ほかの利用者のもの、終わったもの）は、この実行のものではない。"""
    needle = f"{MARK_VAR}={token}".encode()
    listed = proc_processes(proc_dir)
    if listed is None:
        return None
    found = []
    for process in listed:
        if process.pid == os.getpid():
            continue
        try:
            environ = Path(proc_dir, str(process.pid), "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(process)
    return found


# Windows: プロセスの番号、親の番号、作られた時刻（1970年からのミリ秒）、コマンドラインを、1行ずつ出す。
# 区切りは、コマンドラインに出ない文字（U+001F）。文字コードは UTF-8 にそろえる
POWERSHELL_LIST = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$t = 0; if ($_.CreationDate) { $t = ([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds() }; "
    "$c = ''; if ($_.CommandLine) { $c = $_.CommandLine -replace '[\\r\\n\\x1f]', ' ' }; "
    "'{0}{4}{1}{4}{2}{4}{3}' -f $_.ProcessId, $_.ParentProcessId, $t, $c, [char]0x1f }"
)


def parse_windows_list(text: str) -> list[Process]:
    found = []
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        try:
            pid, parent, created = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        found.append(Process(pid, parent, parts[3].strip(), created / 1000 if created else None))
    return found


def parse_ps_list(text: str) -> list[Process]:
    found = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            found.append(Process(int(parts[0]), int(parts[1]), parts[2].strip() if len(parts) > 2 else ""))
    return found


def list_processes(windows: bool = WINDOWS, linux: bool = LINUX) -> tuple[list[Process], int] | None:
    """動いているプロセスの一覧と、一覧を作ったコマンド自身の番号（コマンドを使わなければ 0）。

    作れなければ None。

    Linux は /proc、Windows は PowerShell（Windows 10 / 11 に入っている 5.1 で動く書き方）、そのほかは ps。"""
    if linux and not windows:
        found = proc_processes()
        if found is not None:
            return found, 0
    if windows:
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        exe = str(Path(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
        cmd = [exe, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_LIST]
        parse = parse_windows_list
    else:
        cmd = ["ps", "-eo", "pid=,ppid=,args="]
        parse = parse_ps_list
    done = run_helper(cmd, LIST_TIMEOUT_SECONDS, capture=True)
    if done is None or done[0] != 0:
        return None
    found = parse(done[1].decode("utf-8", "replace"))
    return (found, done[2]) if found else None


def descendants(processes: list[Process], root: int, known_dead: set[int], since: float | None = None,
                ) -> tuple[list[Process], list[Process]]:
    """一覧の中の、root（試験のプロセス）から生まれたプロセス。（確かなもの、たぶんそうなもの）。

    確か: 動いている親をたどって root に届く。
    たぶん: 親がもう動いておらず、その番号が known_dead（この実行で控えたプロセスの番号）にある。そこから先の
    子も、たぶん。プロセスの番号は使い回されるので、確かとはしない。
    since（秒）より前に作られたものは、どちらにも入れない（作られた時刻が分かるときだけ見る）。"""
    alive = {p.pid: p for p in processes}
    kinds: dict[int, str] = {root: "sure"}

    def kind_of(pid: int, seen: frozenset[int] = frozenset()) -> str:
        if pid in kinds:
            return kinds[pid]
        if pid in seen:
            return "no"
        me = alive.get(pid)
        if me is None:
            return "no"
        if since is not None and me.created is not None and me.created < since:
            result = "no"
        elif me.parent in alive or me.parent == root:
            parent = alive.get(me.parent)
            younger = (parent is None or parent.created is None or me.created is None
                       or me.created >= parent.created)
            # 親より先に作られていたら、親の番号は使い回し（別のプロセス）
            result = kind_of(me.parent, seen | {pid}) if younger else "no"
        else:
            result = "maybe" if me.parent in known_dead else "no"
        kinds[pid] = result
        return result

    sure = [p for p in processes if p.pid != root and kind_of(p.pid) == "sure"]
    maybe = [p for p in processes if p.pid != root and kind_of(p.pid) == "maybe"]
    return sure, maybe


def app_like(processes: list[Process]) -> list[Process]:
    return [p for p in processes if APP_LIKE.search(p.command)]


def running_apps() -> list[Process] | None:
    """いま動いている、アプリ本体らしいプロセス（誰のものでも。自分を除く）。調べられなければ None。"""
    listed = list_processes()
    if listed is None:
        return None
    processes, lister = listed
    return [p for p in app_like(processes) if p.pid not in (os.getpid(), lister)]


def kill_pid(pid: int) -> bool:
    """番号でプロセスを止める（Windows の os.kill は、SIGTERM でも TerminateProcess）。"""
    try:
        os.kill(pid, signal.SIGTERM if WINDOWS else signal.SIGKILL)
        return True
    except OSError:
        return False


def still_marked(token: str, grace: float) -> list[Process] | None:
    """Linux: 印のあるアプリ本体らしいプロセスのうち、grace 秒たっても動いているもの。"""
    deadline = time.monotonic() + grace
    while True:
        found = marked_processes(token)
        if found is None:
            return None
        found = app_like(found)
        if not found or time.monotonic() >= deadline:
            return found
        time.sleep(0.1)


@dataclass
class Leftovers:
    """実行の終わりに残っていた、アプリ本体らしいプロセス。"""

    checked: bool                   # 調べられたか
    killed: list[Process]           # この実行のものと確かめられたので、止めた
    reported: list[Process]         # たぶんこの実行のもの（止めていない）

    def any(self) -> bool:
        return bool(self.killed or self.reported)


def leftover_apps(token: str, known_dead: set[int], since: float, grace: float = GRACE_SECONDS,
                  linux: bool = LINUX, windows: bool = WINDOWS) -> Leftovers:
    """この実行から生まれたアプリ本体らしいプロセスで、まだ動いているものを調べ、確かなものは止める。"""
    if linux:
        found = still_marked(token, grace)
        if found is not None:
            return Leftovers(True, [p for p in found if kill_pid(p.pid)], [])
    first = list_processes(windows, linux)
    if first is None:
        return Leftovers(False, [], [])
    time.sleep(min(grace, 1.0))     # 終わりかけているものを、残りと数えない
    second = list_processes(windows, linux)
    if second is None:
        return Leftovers(False, [], [])
    before = {(p.pid, p.created) for p in first[0]}
    still = [p for p in second[0] if (p.pid, p.created) in before and p.pid not in (first[1], second[1])]
    sure, maybe = descendants(still, os.getpid(), known_dead, since)
    return Leftovers(True, [p for p in app_like(sure) if kill_pid(p.pid)], app_like(maybe))
