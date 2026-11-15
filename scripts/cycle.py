#!/usr/bin/env python3
"""開発サイクルのゲートを、スクリプトで確実に運ぶ。

判断はエージェント（AI）が行い、手順と合否の扱いはこのスクリプト（決定的なコード）が行う。
ゲートの結果は、そのとき見ていた内容（gitのツリー）と一緒に記録する。
検証の後で中身が変わっていたら、統合を拒む。

  scripts/cycle.py start <ID> [--reset]          作業ツリーとブランチを用意する
                                                 （--reset はゲートの記録を消してやり直す）
  scripts/cycle.py run <ID> <gate> [--dry-run]   担当エージェントをヘッドレスで起動し、結果を記録する
  scripts/cycle.py snapshot <ID>                 いまの内容のツリーIDを表示する
  scripts/cycle.py record <ID> <gate> --verdict pass|fail --by <名前> --tree <ID> [--report <ファイル>]
                                                 セッション内で動かしたエージェントの結果を記録する
                                                 （--tree を省けるのは implement だけ）
  scripts/cycle.py check <ID>                    作業ツリーで scripts/check.py を実行して記録する
  scripts/cycle.py record-ci <ID> --result <ファイル>
                                                 Windows での手元の確認（scripts/local_ci.py）の結果を取り込む
  scripts/cycle.py status [<ID>]                 ゲートの状況を表示する
  scripts/cycle.py merge <ID>                    必要なゲートがすべて合格のときだけ main に取り込む

ゲート: implement（実装）、check（自動確認）、review（レビュー）、verify（独立検証）、ux（UX確認）、
        windows（Windows での確認。仕様の見出しに needs_windows: true と書いた作業単位だけ）
標準ライブラリだけで動く。Windows、macOS、Linux のどれでも同じ決まりを強制する
（Windows では `python scripts/cycle.py …` の形で実行する。OSで違う箇所は「OSの違い」の節にまとめてある）。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import IO, TypeVar

T = TypeVar("T")

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

AGENT_GATES = ("implement", "review", "verify", "ux")
ALL_GATES = ("implement", "check", "review", "verify", "ux")
DEFAULT_GATES = ("implement", "check", "review", "verify")
GATE_AGENT = {"review": "code-reviewer", "verify": "verifier", "ux": "ux-designer"}
# 確認の対象（ツリーID）を、確認の前に控えておく必要があるゲート
TREE_REQUIRED_GATES = ("review", "verify", "ux")

# レビューや検証の後で足してよい場所（検証担当の受け入れ試験と記録）と、そこに足してよいファイルの名前。
# 許すのは、ゲートの時点で無かったファイルの追加だけ。それ以外の変更があれば、そのゲートは古いものとして扱う。
# 名前を絞るのは、試験の集め方を変えるファイル（conftest.py、pytest.ini など）を後から足させないため
# （conftest.py ひとつで、受け入れ試験を全部外せる）。
AFTER_GATE_FILES = (("tests/acceptance/", "test_*.py"), ("docs/verification/", "*.md"))

# 統合先のブランチ。ゲートの記録に、その時点の main との分かれ目（git merge-base）を控える
MAIN_REF = "refs/heads/main"

# 役割の定義の場所（ヘッドレスで起動する役割は、ここに定義が要る）
AGENTS_DIR = ".claude/agents"

HEADER_KEYS = ("id", "owner", "needs_ux", "gates", "needs_windows")
# 見出しのうち、true か false だけで書くもの
HEADER_FLAGS = ("needs_ux", "needs_windows")

# Windows での確認のゲート。見出しの needs_windows: true で必要になり、記録は record-ci だけが作る
# （gates: には書けない。record と run でも記録できない）。
WINDOWS_GATE = "windows"
WINDOWS_SYSTEM = "Windows"          # 結果の os.system（platform.system() の値）

# 手元の確認（scripts/local_ci.py）の結果のファイルの形
LOCAL_CI_SCRIPT = "scripts/local_ci.py"
CI_SCHEMA = 1
CI_KIND = "local-ci"
CI_VERDICTS = ("pass", "fail", "error")
CI_COUNTS = ("total", "passed", "failed", "errors", "skipped")

# git の置き場所を外から差し替える環境変数。持ち込むと、対象でないリポジトリや索引を見てしまう。
GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)

# 内容（ツリーID、確認のための取り出し）を扱う git では、利用者とシステムの設定のうち、内容の読み方を
# 変えるものを、ここで決めた値にする。同じ内容なら、誰が、どの環境（設定）で求めても、同じツリーIDになる
# （決まり9）。
# 読むのは、リポジトリ自身の設定（.git/config。実行できる印やリンクを表せるか、など）と、
# リポジトリの中の .gitattributes、.gitignore、.git/info/ の指定だけ。
#   core.autocrlf: 改行の変換（Git for Windows は、システムの設定で true にする）
#   core.safecrlf: 改行の変換の警告で、内容を読むのを止める
#   attr.tree: 属性を、作業ツリーの .gitattributes ではなく、指定したツリーから読む（git 2.43 から。
#     空の値は、どのツリーも指さないので、git は読み方を変えない。それより前の git は、この名前を読まない）
#   ほかに、リポジトリの外の属性（core.attributesFile、システムの属性 GIT_ATTR_NOSYSTEM、GIT_ATTR_SOURCE）、
#   利用者の無視の指定（core.excludesFile）、フック（core.hooksPath）は、pinned_git_options が空に向ける。
# 署名、認証、safe.directory、長いパスの扱いなど、内容の読み方を変えない設定は、利用者のものを使う。
PINNED_GIT_SETTINGS = (("core.autocrlf", "false"), ("core.safecrlf", "false"), ("attr.tree", ""))
PINNED_GIT_DROP = ("GIT_ATTR_SOURCE",)

# 確認（pytest）の、試験の選び方を変えうる環境変数の名前の始まり（check_env）
PYTEST_VARS_PREFIX = "PYTEST_"

GATE_TOOLS = {
    "implement": "Read,Edit,Write,Bash,Grep,Glob",
    "review": "Read,Bash,Grep,Glob",
    "verify": "Read,Edit,Write,Bash,Grep,Glob",
    "ux": "Read,Edit,Write,Bash,Grep,Glob",
}
# レビューは、起動の側でも編集の道具を禁じる（役割の定義の disallowedTools: だけに頼らない）。
# 編集を自動で受け入れる設定（acceptEdits）にもしない（ヘッドレスでは、許していない操作は断られる）。
GATE_DISALLOWED = {"review": "Edit,Write,NotebookEdit,MultiEdit"}
GATE_PERMISSION = {"review": "default"}
DEFAULT_PERMISSION = "acceptEdits"

ADD_ONLY = (
    "既にあるファイルは書き換えたり消したりせず、新しいファイルとして足してください"
    "（書き換えると、レビューと検証がやり直しになります）。"
)

GATE_PROMPT = {
    "implement": (
        "仕様 {spec} を、この作業ツリーの中で実装してください。"
        "受け入れ基準をすべて自分で実測し、python scripts/check.py を通してください。"
        "コミットはしないでください。"
    ),
    "review": (
        "仕様 {spec} に対するこの作業ツリーの変更（git diff main...HEAD と"
        "未コミットの変更）をレビューしてください。"
        "コードは変更しないでください。重大な指摘が1つでもあれば verdict は fail にしてください。"
    ),
    "verify": (
        "仕様 {spec} の受け入れ基準を、実装者とは別の立場で1つずつ実測してください。"
        "製品のコードは変更しないでください。"
        "受け入れ試験は tests/acceptance/ に、記録は docs/verification/ に置いてください。"
        + ADD_ONLY +
        "測れなかった基準が1つでもあれば verdict は fail にしてください。"
    ),
    "ux": (
        "仕様 {spec} で実装された画面を実際に動かし、"
        "画面仕様と docs/design/app-design.md の原則に照らして判定してください。"
        "コードは変更しないでください。記録は docs/verification/ に置いてください。"
        + ADD_ONLY
    ),
}

SEVERITIES = ("critical", "major", "minor")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "summary": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "measured": {"type": "string"},
                    "pass": {"type": "boolean"},
                },
                "required": ["name", "measured", "pass"],
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "description": {"type": "string"},
                },
                "required": ["severity", "description"],
            },
        },
    },
    "required": ["verdict", "summary", "criteria", "issues"],
}

NOT_ADOPTED = "実行中に同じゲートの別の記録が入ったので、この合格は採用しない。やり直す"
HEADER_DRIFT = (
    "仕様の見出し（owner、needs_ux、needs_windows、gates）が start の後で変わっている。start をやり直す"
    "（必要なゲートが増えただけなら、記録は残る）"
)


class CycleError(Exception):
    pass


class CommandNotFound(CycleError):
    """起動するコマンドが、使ってよい場所に見つからない。"""


# ------------------------------------------------------------------ OSの違い
# Windows と、それ以外（Linux / macOS）で違う箇所は、この節にまとめる。ゲートの決まりは、どのOSでも同じ。

# 終了の合図。SIGHUP は Windows に無く、SIGBREAK（Ctrl-Break）は Windows にだけある。
STOP_SIGNAL_NAMES = ("SIGTERM", "SIGHUP", "SIGBREAK")

# 自動確認のスクリプト。作業ツリーの直下から見た場所（git の中の名前と同じく、区切りは /）
CHECK_SCRIPT = "scripts/check.py"

# Windows の起動用のファイル（claude.cmd など）は cmd.exe を通る。そこで別の意味になる文字は渡せない。
# % ^ & | < > は cmd.exe の記号。! は遅延展開が有効な起動ファイルで変数の印になる。
# ( ) は、起動ファイルが引数をかっこの中（if や for のブロック）に置いていると、ブロックの区切りに読まれる。
BATCH_SUFFIXES = (".cmd", ".bat")
BATCH_UNSAFE = "%^&|<>!()\r\n"

# Windows で、コマンドの名前に付けて探す拡張子（環境変数 PATHEXT が無いときの既定）
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

# Windows では、他のプロセスが開いているファイルの置き換えと、置き換えの最中のファイルの読み取りが、
# 一時的に拒まれることがある。この秒数までは、待ってやり直す。
BUSY_WAIT_SECONDS = 5.0 if WINDOWS else 0.0
POLL_SECONDS = 0.05

# Windows の、OSのエラー画面（人が OK を押すまで、起動した側を止める）を出さない設定（SetErrorMode の印）。
# SEM_FAILCRITICALERRORS: 起動できない実行ファイル（壊れた claude.exe で出る「サポートされていない 16 ビット
# アプリケーション」など）や、見つからない DLL の画面。SEM_NOGPFAULTERRORBOX: 異常終了の画面。
# SEM_NOOPENFILEERRORBOX: ファイルを開けない画面。
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000
QUIET_ERROR_MODE = SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX


def quiet_error_dialogs() -> None:
    """Windows で、OSのエラー画面を出させずに、エラーとして返させる（人が見ていない実行が止まらないように）。

    このプロセスに設定する。起動する子プロセス（git、claude、確認の python と pytest）にも引き継がれる
    （Popen に CREATE_DEFAULT_ERROR_MODE を付けると引き継がれないので、どこにも付けない）。もとの印は残す。
    scripts/cycle.py、scripts/local_ci.py、scripts/doctor.py の main が、最初に呼ぶ。
    """
    if not WINDOWS:
        return
    import ctypes  # Windows でだけ読む（ctypes の無い Python の作りでも、ほかのOSで動くように）

    kernel32 = ctypes.WinDLL("kernel32")        # 印は 16 ビットに収まるので、ctypes の既定の型（int）で渡せる
    kernel32.SetErrorMode(kernel32.GetErrorMode() | QUIET_ERROR_MODE)


def lock_handle(handle: IO[str]) -> None:
    """開いたファイルにロックを掛ける。他の実行が持っていれば、外れるまで待つ。"""
    if WINDOWS:
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except PermissionError:     # 他の実行が持っている
                time.sleep(POLL_SECONDS)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX)


def unlock_handle(handle: IO[str]) -> None:
    if WINDOWS:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


def stop_signals() -> list[signal.Signals]:
    """このOSにある終了の合図。"""
    return [getattr(signal, name) for name in STOP_SIGNAL_NAMES if hasattr(signal, name)]


def command_file_names(name: str) -> list[str]:
    """その名前のコマンドとして起動できるファイルの名前。Windows では拡張子が付く（claude.cmd など）。

    拡張子の無い同名のファイル（npm が一緒に置く、シェル用のもの）は、Windows では起動できないので含めない。
    """
    if not WINDOWS:
        return [name]
    extensions = os.environ.get("PATHEXT") or DEFAULT_PATHEXT
    return [name + extension for extension in extensions.split(os.pathsep) if extension]


def real_path(path: str | Path) -> Path:
    """リンクや .. をたどった先の場所（比べるための形。たどれない所があっても、例外にしない）。"""
    return Path(os.path.realpath(path))


def is_inside(path: Path, tree: Path) -> bool:
    return path == tree or tree in path.parents


def find_command(name: str, avoid: Iterable[Path] = ()) -> str | None:
    """コマンドの実体の場所（絶対パス）。見つからなければ None。PATH の項目を、書かれた順に探す。

    次の場所にあるものは採らない。確認を受けていないファイルが、その名前のコマンドとして動かないようにする
    （作業ツリーに置かれた claude.cmd が、レビューや検証の合格を返す、など）。
    - 実行した場所（カレント）。Windows の端末と shutil.which は、PATH より先にカレントを探す。
    - PATH の項目のうち、空のものと相対パスのもの（実行した場所によって、指す先が変わる）。
    - avoid に渡した場所の中（作業ツリーと本体。無視の対象のファイルは、ゲートが見る内容に入らない）。
    """
    here = real_path(os.getcwd())
    trees = [real_path(tree) for tree in avoid]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        directory = real_path(entry)
        if directory == here or any(is_inside(directory, tree) for tree in trees):
            continue
        for file_name in command_file_names(name):
            candidate = os.path.join(entry, file_name)
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                continue
            # 置き場所が使ってよい場所でも、リンクの先（ファイルの実体）が避ける場所の中なら採らない
            target = real_path(candidate)
            if target.parent == here or any(is_inside(target, tree) for tree in trees):
                continue
            return candidate
    return None


def launchable(cmd: list[str], avoid: Iterable[Path] = ()) -> list[str]:
    """先頭のコマンドを実体の場所に直した、起動できるコマンド。

    見つからなければ CommandNotFound、起動用のファイルに渡せない引数があれば CycleError。
    """
    found = find_command(cmd[0], avoid)
    if found is None:
        raise CommandNotFound(
            f"{cmd[0]} コマンドが見つからない（実行した場所、作業ツリー、本体の中にあるものは使わない）。"
            "Claude Code の CLI を入れて PATH を通すか、"
            "セッション内でエージェントを動かして record で記録する"
        )
    if found.lower().endswith(BATCH_SUFFIXES):
        unsafe = sorted({ch for arg in cmd[1:] for ch in arg if ch in BATCH_UNSAFE})
        if unsafe:
            raise CycleError(
                f"{Path(found).name} は cmd.exe を通るので、引数に {unsafe} を含められない"
                "（指示や仕様のファイル名から外すか、.exe の形の CLI を入れる）"
            )
    return [found, *cmd[1:]]


# Windows の cmd.exe と CreateProcess は、名前だけで書かれたコマンドを、PATH より先に、実行した場所
# （カレント）で探す（npm 版の claude.cmd が中で呼ぶ node、名前で起動する git など）。
# この環境変数があると、探さない。
# 起動する子プロセスにも引き継ぐ。ほかのOSでは、読む仕組みが無いので何も変わらない。
NO_CURRENT_DIRECTORY_SEARCH = "NoDefaultCurrentDirectoryInExePath"


def no_current_directory_search() -> None:
    """名前だけのコマンドを、実行した場所で探させない（このプロセスと、起動する子プロセス）。main が呼ぶ。"""
    os.environ[NO_CURRENT_DIRECTORY_SEARCH] = "1"


def system_command(name: str) -> str:
    """Windows に付いてくるコマンド（taskkill）の場所。名前だけで起動しない"""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", f"{name}.exe")


def shell_line(cwd: Path, cmd: list[str]) -> str:
    """人が端末に貼って実行できる形の1行（Windows では、コマンド プロンプト用の書き方）。"""
    if WINDOWS:
        return f"cd /d {subprocess.list2cmdline([str(cwd)])} && {subprocess.list2cmdline(cmd)}"
    return f"cd {shlex.quote(str(cwd))} && {shlex.join(cmd)}"


def group_options() -> dict:
    """起動するプロセスを、その先のプロセスごと止められるようにする Popen の指定（kill_tree と組で使う）。

    Linux / macOS では、新しいセッション（プロセスのまとまり）で起動する。端末の Ctrl-C はこのスクリプトだけに
    届き、こちらがまとまりごと止める。Windows では指定しない（taskkill /T が、その先のプロセスをたどる）。
    """
    return {} if WINDOWS else {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """起動したプロセスを、その先のプロセスごと止める（group_options で起動したもの）。

    Windows では、起動用のファイル（claude.cmd）の先の本体や、確認の先の pytest まで、taskkill /T でたどる。
    Linux / macOS では、プロセスのまとまりごと止める（起動したプロセスが先に終わっていても、残りを止める）。
    """
    if WINDOWS:
        subprocess.run([system_command("taskkill"), "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError):       # まとまりが、もう無い（どれも終わっている）
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()


def run_captured(
    cmd: list[str], cwd: Path, timeout: float | None, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """コマンドを実行して、出力を UTF-8 の文字列で受け取る。

    呼び出し元の標準入力は渡さない（人が見ていない実行で、入力を待って止まり続けないように）。
    時間切れなら、起動したプロセスを、その先のプロセスごと止めてから subprocess.TimeoutExpired を送出する。

    止めた後は、出力の終わりを待たない。起動したプロセスが残した子プロセスが出力の口を持ったままだと、
    その子が終わるまで戻れなくなる（打ち切りは、人が見ていない実行の最後の歯止め）。
    打ち切った実行の出力は使わないので、読み残してよい。

    終了の合図（SIGTERM、SIGHUP。端末を閉じたときなど）も例外に変えて、同じく止める。起動したプロセスは
    別のまとまりにいるので、合図は届かない。こちらだけが終わると、起動したプロセスが動き続ける。
    """
    with termination_as_exception(), subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, **group_options(),
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:           # 時間切れ、中断（Ctrl-C、終了の合図）
            kill_tree(proc)
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def python_env() -> dict[str, str]:
    """Python のスクリプトを起動するときの環境変数。出力を UTF-8 で受け取れるようにする。"""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def check_env(base: dict[str, str]) -> dict[str, str]:
    """確認（scripts/check.py → pytest）を起動するときの環境変数。base から、pytest の環境変数を除いたもの。

    PYTEST_ で始まる変数（PYTEST_ADDOPTS の -k、-m、--deselect、--ignore、--lf、PYTEST_PLUGINS の差し込み、
    PYTEST_DISABLE_PLUGIN_AUTOLOAD など）は、実行する試験を選び直せる。呼び出し元から引き継ぐと、
    一部の試験だけを実行した結果が、合格として記録される（Windows の os.environ の名前は、もとから大文字）。
    """
    return {key: value for key, value in base.items() if not key.startswith(PYTEST_VARS_PREFIX)}


def retry_while_busy(action: Callable[[], T]) -> T:
    """ファイルの操作を行う。他のプロセスが使っている間の一時的な拒否は、少し待ってやり直す。"""
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SECONDS)


def make_output_safe() -> None:
    """画面に出せない文字があっても落ちないよう、置き換えて出す（UTF-8 でない端末への備え）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


# ------------------------------------------------------------------ 共通

def repo_root() -> Path:
    env = os.environ.get("CYCLE_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[1]


def git_env() -> dict[str, str]:
    """git を起動するときの環境変数。置き場所を差し替える変数（GIT_DIR など）は、持ち込まない。"""
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION_VARS}


def pinned_git_options(folder: Path) -> list[str]:
    """内容を扱う git に渡す設定（-c の並び。PINNED_GIT_SETTINGS の説明）。

    リポジトリの外の属性、利用者の無視の指定、フックは、folder の中の、作らない名前に向ける（git は、無い
    ファイルを空のファイルとして、無いフォルダをフックの無いフォルダとして扱う）。folder は、確かめる内容の
    外の一時フォルダ（ツリーIDを求める間、取り出す間だけある。ほかの誰も書かない）。
    """
    nothing = (folder / "pinned-nothing").as_posix()
    settings = (
        *PINNED_GIT_SETTINGS, ("core.attributesFile", nothing), ("core.excludesFile", nothing),
        ("core.hooksPath", nothing),
    )
    return [option for key, value in settings for option in ("-c", f"{key}={value}")]


def git_program() -> str:
    """起動する git の場所。claude と同じ探し方（find_command）で、実行した場所と本体の中のものは使わない。"""
    found = find_command("git", (repo_root(),))
    if found is None:
        raise CycleError(
            "git が見つからない（実行した場所と本体の中にあるものは使わない）。git を入れて PATH を通す"
        )
    return found


def run_git(
    cwd: Path, *args: str, index: Path | None = None, stdin: str | None = None, pinned: Path | None = None,
) -> subprocess.CompletedProcess:
    """git を実行する。pinned を渡すと、内容の読み方を変える設定を決めた値にする（pinned_git_options）。"""
    env = git_env()
    options = []
    if pinned is not None:
        options = pinned_git_options(pinned)
        env = {key: value for key, value in env.items() if key not in PINNED_GIT_DROP}
        env["GIT_ATTR_NOSYSTEM"] = "1"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        [git_program(), *options, "-C", str(cwd), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def git(
    cwd: Path, *args: str, index: Path | None = None, stdin: str | None = None, pinned: Path | None = None,
) -> str:
    proc = run_git(cwd, *args, index=index, stdin=stdin, pinned=pinned)
    if proc.returncode != 0:
        raise CycleError(f"git {' '.join(args)} が失敗: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def git_paths(cwd: Path, *args: str, index: Path | None = None, pinned: Path | None = None) -> list[str]:
    """-z を付けた git の出力（NUL 区切りのファイル名）を一覧にする。"""
    proc = run_git(cwd, *args, index=index, pinned=pinned)
    if proc.returncode != 0:
        raise CycleError(f"git {' '.join(args)} が失敗: {proc.stderr.strip()}")
    return [path for path in proc.stdout.split("\0") if path]


def git_ok(cwd: Path, *args: str) -> bool:
    return run_git(cwd, *args).returncode == 0


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    """記録ファイルの名前に使う時刻。同じ秒の実行が重ならないよう、マイクロ秒まで入れる。"""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")


def find_spec(root: Path, uid: str) -> Path:
    plan = root / "docs" / "plan"
    found = sorted(plan.glob(f"{uid}-*.md")) + sorted(plan.glob(f"{uid}.md"))
    if len(found) != 1:
        raise CycleError(f"docs/plan/ に {uid} の仕様が1つだけ必要（見つかった数: {len(found)}）")
    return found[0]


def read_header(spec: Path) -> dict:
    """仕様ファイル先頭の `---` で囲まれた部分を読む。

    書けるのは id、owner、needs_ux、needs_windows、gates だけ。読めない行、知らない値、食い違いは
    エラーにする（書き間違いが「ゲートは不要」と読まれないように）。
    返すのは id、owner、needs_ux と needs_windows（真偽）、gates_required（見出しに gates があれば
    その一覧、無ければ空）。
    """
    lines = spec.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise CycleError(f"{spec.name}: 先頭に --- で囲んだ見出し（id、owner）が必要")
    raw: dict[str, str] = {}
    for line in lines[1:]:
        text = line.strip()
        if text == "---":
            break
        if not text:
            continue
        key, colon, value = text.partition(":")
        key = key.strip()
        if not colon or key not in HEADER_KEYS:
            raise CycleError(
                f"{spec.name}: 見出しに読めない行がある（{text}）。"
                f"書けるのは {'、'.join(HEADER_KEYS)} を「名前: 値」の形で"
            )
        if key in raw:
            raise CycleError(f"{spec.name}: 見出しに {key} が2回ある")
        raw[key] = value.strip()
    else:
        raise CycleError(f"{spec.name}: 見出しが --- で閉じられていない")
    for key in ("id", "owner"):
        if not raw.get(key):
            raise CycleError(f"{spec.name}: 見出しに {key} が無い")

    header = {"id": raw["id"], "owner": raw["owner"], "gates_required": []}
    for flag in HEADER_FLAGS:
        value = raw.get(flag, "false").lower()
        if value not in ("true", "false"):
            raise CycleError(
                f"{spec.name}: {flag} は true か false だけで書く（書かれている値: {raw[flag]}）"
            )
        header[flag] = value == "true"
    if "gates" in raw:
        gates = [g.strip() for g in raw["gates"].split(",")]
        unknown = [g for g in gates if g not in ALL_GATES]
        if unknown:
            raise CycleError(f"{spec.name}: 知らないゲート {unknown}")
        if "needs_ux" in raw and header["needs_ux"] != ("ux" in gates):
            raise CycleError(
                f"{spec.name}: needs_ux と gates が食い違っている"
                "（needs_ux: true なら gates に ux を入れる。false なら入れない）"
            )
        header["gates_required"] = [g for g in ALL_GATES if g in gates]
    return header


def required_gates(source: dict) -> list[str]:
    """必要なゲート。source は見出し（read_header の結果）か、状態（start が保存したもの）。

    needs_windows: true なら、見出しに gates: があっても windows を必ず加える（gates: には windows を
    書けないので、食い違いにはならない。gates: で必要なゲートを絞っても、Windows での確認は外れない）。
    """
    wanted = list(source.get("gates_required") or DEFAULT_GATES)
    if source.get("needs_ux"):
        wanted.append("ux")
    gates = [g for g in ALL_GATES if g in wanted]
    if source.get("needs_windows"):
        gates.append(WINDOWS_GATE)
    return gates


def header_changed(header: dict, state: dict) -> bool:
    """見出しのうちゲートに関わる内容（担当、必要なゲート）が、状態と違うか。"""
    return header["owner"] != state.get("owner") or required_gates(header) != required_gates(state)


def header_loosened(header: dict, state: dict) -> bool:
    """見出しの変更が、担当を変えるか、必要なゲートを減らすか（記録を消してやり直す必要がある変更）。

    必要なゲートが増えるだけの変更（needs_ux: true を足す、など）は、記録を残したまま start で取り込める
    （増えたゲートは、まだ記録が無いので、統合はそのゲートを待つ）。
    """
    removed = set(required_gates(state)) - set(required_gates(header))
    return header["owner"] != state.get("owner") or bool(removed)


def state_file(root: Path, uid: str) -> Path:
    return root / ".cycle" / "state" / f"{uid}.json"


def load_state(root: Path, uid: str) -> dict:
    path = state_file(root, uid)
    if not path.exists():
        raise CycleError(f"{uid} はまだ始まっていない。先に start を実行する")
    try:
        state = json.loads(retry_while_busy(lambda: path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise CycleError(f"{path} を読めない: {exc}") from exc
    if not isinstance(state, dict):
        raise CycleError(f"{path} の形が正しくない")
    return state


def save_state(root: Path, state: dict) -> None:
    """状態を書く。途中まで書いたファイルを他の実行が読まないよう、別名で書いてから置き換える。"""
    path = state_file(root, state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    partial.write_text(text, encoding="utf-8", newline="\n")
    try:
        retry_while_busy(lambda: os.replace(partial, path))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise CycleError(f"{path} を書けない: {exc}") from exc


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """同じファイルのロックを持つ他の実行が終わるまで待つ。プロセスが落ちれば、ロックは OS が外す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        lock_handle(handle)
        try:
            yield
        finally:
            unlock_handle(handle)


def unit_lock(root: Path, uid: str) -> Path:
    return root / ".cycle" / "state" / f"{uid}.lock"


def merge_lock(root: Path) -> Path:
    return root / ".cycle" / "merge.lock"


def pending_file(root: Path) -> Path:
    """取り込みを始めてから、取り込んだ後の確認が済む（か、取り消す）までの間だけある控え。"""
    return root / ".cycle" / "merge-pending.json"


@contextlib.contextmanager
def termination_as_exception() -> Iterator[None]:
    """終了の合図（stop_signals）を例外に変えて、後始末（取り込みの取り消し）が走るようにする。

    合図を受けたら、例外を送出する前に、終了の合図をすべて無視に切り替え、この枠を出るときに元に戻す。
    端末を閉じると、前面のジョブに SIGHUP が続けて2回届く。2回目で後始末（起動した claude を止める、
    取り込みを取り消す）が途中で切られると、claude が動き続け、こちらはその終わりを待ち続ける
    （W2 の3回目のレビュー）。枠が入れ子なら、外側の枠を出るまで無視を続ける（外側の後始末も切らせない）。
    """
    signals = stop_signals()
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    stop.raises_on_stop = True      # 入れ子の内側が、外側の枠の扱いだと見分ける印
    saved = {}
    try:
        for sig in signals:
            # 無視に設定されていた合図（nohup の SIGHUP など）は、差し替えない（無視のまま続ける）
            if signal.getsignal(sig) is not signal.SIG_IGN:
                saved[sig] = signal.signal(sig, stop)
    except ValueError:          # メインのスレッド以外からは設定できない
        pass
    try:
        yield
    finally:
        for sig, handler in saved.items():
            outer_stop = getattr(handler, "raises_on_stop", False)
            signal.signal(sig, signal.SIG_IGN if stopping and outer_stop else handler)


def tree_id(worktree: Path) -> str:
    """作業ツリーのいまの内容（未コミットと新規ファイルを含む）を表すツリーID。

    一時的な索引で計算するので、作業ツリーの索引（選んでステージした状態、競合の解決の途中）は変えない。
    索引は HEAD のコミットから組み直すので、索引に付けた「変更を見ない」印にも左右されない。

    空の索引からではなく HEAD から始めるのは、同じ内容なら、どのOSでも同じIDにするため。
    実行できる印とリンクを表せないファイルシステム（Windows の既定）では、git は、索引に無いファイルを
    「実行できない普通のファイル」として足す。空の索引から始めると、実行できる印の付いたファイルを含む内容で、
    コミットのツリーとも、Linux で求めたIDとも食い違う。索引にあるファイルは、その印を引き継ぐ。

    利用者とシステムの git の設定のうち、内容の読み方を変えるもの（改行の変換、リポジトリの外の属性と
    無視の指定、フック）は、決めた値にする（PINNED_GIT_SETTINGS）。同じ内容なら、どの環境で求めても同じIDになる。
    """
    with tempfile.TemporaryDirectory(prefix="cycle-index-") as tmp:
        index, pinned = Path(tmp) / "index", Path(tmp)
        # コミットがまだ無いリポジトリでは失敗する。その場合は、空の索引から始める
        run_git(worktree, "read-tree", "HEAD", index=index, pinned=pinned)
        git(worktree, "add", "-A", index=index, pinned=pinned)
        # 無視の対象でも、追跡しているファイル（git add -f で足したもの）は内容に含める
        taken = set(git_paths(worktree, "ls-files", "-z", index=index, pinned=pinned))
        forced = [
            path for path in git_paths(worktree, "ls-files", "-z", pinned=pinned)
            if path not in taken and ((worktree / path).is_file() or (worktree / path).is_symlink())
        ]
        if forced:
            names = "\0".join(forced) + "\0"
            git(worktree, "update-index", "--add", "-z", "--stdin", index=index, stdin=names, pinned=pinned)
        return git(worktree, "write-tree", index=index, pinned=pinned)


def is_tree_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None


def resolve_tree(worktree: Path, value: str) -> str:
    """--tree の値を、記録する時点でツリーIDに直す（HEAD やブランチ名のまま記録しない）。"""
    tree = run_git(worktree, "rev-parse", "--verify", "--quiet", f"{value}^{{tree}}").stdout.strip()
    if not is_tree_id(tree):        # 解決できなければ、何も出力されない
        raise CycleError(f"--tree の値（{value}）をツリーとして読めない。snapshot が表示したIDを渡す")
    return tree


def main_base(worktree: Path, commit: str = "HEAD") -> str | None:
    """commit と main との分かれ目（git merge-base）。求められなければ None。

    ゲートの記録に控え、統合のときに比べる。ツリーIDが同じでも、分かれ目が変わっていれば
    （`git reset --soft main && git commit`、`git merge -s ours main` で土台を付け替えた）、
    取り込みは main に入った他の成果を消すので、ゲートは古いものとして扱う。
    """
    proc = run_git(worktree, "merge-base", MAIN_REF, commit)
    base = proc.stdout.strip()
    return base if proc.returncode == 0 and is_tree_id(base) else None


def worktree_branch(worktree: Path) -> str:
    """作業ツリーがブランチの上にあることを確かめて、ブランチ名を返す。"""
    if not worktree.is_dir():
        raise CycleError(f"作業ツリー {worktree} が無い")
    top = run_git(worktree, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != worktree.resolve():
        raise CycleError(f"{worktree} は gitの作業ツリー（の最上位）ではない")
    head = run_git(worktree, "symbolic-ref", "-q", "--short", "HEAD")
    if head.returncode != 0:
        raise CycleError(
            f"作業ツリー {worktree} の HEAD がブランチから切り離されている。ブランチに切り替えてからやり直す"
        )
    return head.stdout.strip()


def content_info(root: Path) -> dict:
    """その場所のいまの内容が、どれかを表す情報: コミットID、ツリーID、ブランチ、未コミットの変更の有無。

    ツリーIDは tree_id と同じ求め方（未コミットの変更と新しいファイルを含む）。
    gitの作業ツリーの最上位でない、コミットがまだ無い、git を起動できない、のどれかなら、すべて None。
    """
    unknown = {"commit": None, "tree": None, "branch": None, "dirty": None}
    try:
        top = run_git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
            return unknown
        commit = git(root, "rev-parse", "--verify", "HEAD^{commit}")     # コミットがまだ無ければ、失敗する
        tree = tree_id(root)
        dirty = tree != git(root, "rev-parse", f"{commit}^{{tree}}")
    except (OSError, CycleError):
        return unknown
    branch = run_git(root, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    return {"commit": commit, "tree": tree, "branch": branch or None, "dirty": dirty}


def is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def ci_result_problem(data: object) -> str:
    """手元の確認（scripts/local_ci.py）の結果が、決まった形でなければ、その理由を返す。正しければ空文字。

    合格（verdict: pass）と書かれた結果は、中身が合格と矛盾しないことも確かめる:
    確認の終了コードが 0、試験の件数があり、通過が1件以上、失敗とエラーが 0、失敗した試験の名前が無い。
    """
    if not isinstance(data, dict):
        return "JSON のオブジェクトでない"
    if data.get("schema") != CI_SCHEMA or data.get("kind") != CI_KIND:
        return f"{LOCAL_CI_SCRIPT} の結果ではない（schema、kind）"
    if not is_tree_id(data.get("commit")) or not is_tree_id(data.get("tree")):
        return "commit か tree が、git のIDの形でない"
    system, python, check = data.get("os"), data.get("python"), data.get("check")
    if not isinstance(system, dict) or not isinstance(system.get("system"), str):
        return "os.system が無い"
    if not isinstance(python, dict) or not isinstance(python.get("version"), str):
        return "python.version が無い"
    if data.get("verdict") not in CI_VERDICTS:
        return "verdict が pass、fail、error のどれでもない"
    if not isinstance(check, dict):
        return "check が無い"
    tests, failed = data.get("tests"), data.get("failed")
    if tests is not None and not (isinstance(tests, dict) and all(is_count(tests.get(k)) for k in CI_COUNTS)):
        return "tests の件数が、0 以上の整数でない"
    if not isinstance(failed, list) or not all(isinstance(name, str) for name in failed):
        return "failed が、名前の一覧でない"
    if data["verdict"] == "pass":
        if type(check.get("returncode")) is not int or check["returncode"] != 0:
            return "verdict は pass だが、確認の終了コードが 0 でない"
        if tests is None:
            return "verdict は pass だが、試験の件数が無い"
        if tests["passed"] < 1 or tests["failed"] or tests["errors"] or failed:
            return "verdict は pass だが、通過した試験が無いか、失敗した試験がある"
    return ""


def record_count(state: dict, gate: str) -> int:
    """このゲートの記録と、記録の消去が、これまでに何回あったか。"""
    return sum(1 for h in state.get("history", []) if h.get("gate") == gate or h.get("event") == "reset")


def add_record(
    root: Path, uid: str, gate: str, verdict: str, tree: str, base: str | None, by: str, source: str,
    report: str = "", seen: int | None = None, extra: dict | None = None,
) -> bool:
    """ゲートの結果を記録する。ロックを取り、状態を読み直してから書く（並行する実行の記録を失わない）。

    base は、確かめた時点の main との分かれ目（main_base）。
    seen は、実行を始めたときの record_count。実行中に同じゲートの別の記録が入っていたら、
    この結果が合格でも採用しない（先に始めた実行の合格で、後から入った結果を上書きしない）。
    採用したら True を返す。採用しなかった結果も履歴には残す。extra は、記録に付け足す項目。
    """
    entry = {
        "verdict": verdict, "tree": tree, "base": base, "by": by, "source": source, "report": report,
        "at": now(),
    }
    entry.update(extra or {})
    with locked(unit_lock(root, uid)):
        state = load_state(root, uid)
        adopted = verdict != "pass" or seen is None or record_count(state, gate) == seen
        if adopted:
            state.setdefault("gates", {})[gate] = entry
        state.setdefault("history", []).append({"gate": gate, **entry, "adopted": adopted})
        save_state(root, state)
    return adopted


def runs_dir(root: Path) -> Path:
    path = root / ".cycle" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_check(cwd: Path) -> tuple[str, str, str]:
    """scripts/check.py を、このスクリプトと同じ Python で実行する。

    返すのは（判定、確認した内容のツリーID、出力）。出力は、標準エラー、標準出力の順に並べる（最後の行が、
    標準出力の最後の行＝ pytest の件数になる。標準エラーの最後は、試験の遮断の表示のことがある）。

    合格にするのは、終了コードが 0 で、実行の前後で内容が変わっていないときだけ。
    呼び出し元の pytest の環境変数（PYTEST_ADDOPTS など）は渡さない（check_env。試験を選び直させない）。
    """
    before = tree_id(cwd)
    if not (cwd / CHECK_SCRIPT).is_file():
        return "error", before, f"{CHECK_SCRIPT} が無いので、確認を起動できない"
    try:
        proc = subprocess.run(
            [sys.executable, CHECK_SCRIPT], cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=check_env(python_env()),
        )
    except OSError as exc:
        return "error", before, f"{CHECK_SCRIPT} を起動できない: {exc}"
    output = proc.stderr + proc.stdout
    if proc.returncode != 0:
        return "fail", before, output
    if tree_id(cwd) != before:
        return "error", before, output + "\ncheck の実行中に内容が変わった。どの内容を確認したか決められない"
    return "pass", before, output


# ---------------------------------------------------------------- コマンド

def cmd_start(args: argparse.Namespace) -> int:
    root = repo_root()
    spec = find_spec(root, args.id)
    header = read_header(spec)
    if header["id"] != args.id:
        raise CycleError(f"{spec.name}: 見出しの id（{header['id']}）がファイル名と合わない")
    worktree = Path(args.worktree).resolve() if args.worktree else root.parent / "wt" / args.id
    with locked(unit_lock(root, args.id)):
        old = load_state(root, args.id) if state_file(root, args.id).exists() else {}
        if old.get("merged_at"):
            raise CycleError(f"{args.id} は統合済み。start はやり直せない")
        if old.get("gates") and not args.reset and header_loosened(header, old):
            raise CycleError(
                f"{args.id}: ゲートの記録があるのに、見出しで担当が変わったか、必要なゲートが減っている。"
                "やり直すなら start --reset で記録を消す（消したことは履歴に残る）"
            )
        if worktree.exists():
            branch = worktree_branch(worktree)
        else:
            branch = args.branch or args.id
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "add", str(worktree), "-b", branch)
        gates = dict(old.get("gates", {}))
        history = list(old.get("history", []))
        if args.reset and gates:
            history.append({
                "event": "reset", "at": now(), "cleared": sorted(gates),
                "gates_before": required_gates(old), "gates_after": required_gates(header),
            })
            gates = {}
        elif old and header_changed(header, old):     # 必要なゲートが増えただけ。記録は残す
            history.append({
                "event": "gates_added", "at": now(),
                "gates_before": required_gates(old), "gates_after": required_gates(header),
            })
        state = {
            "id": args.id,
            "spec": spec.relative_to(root).as_posix(),
            "owner": header["owner"],
            "needs_ux": header["needs_ux"],
            "needs_windows": header["needs_windows"],
            "gates_required": header["gates_required"],
            "worktree": str(worktree),
            "branch": branch,
            "started_at": now(),
            "gates": gates,
            "history": history,
        }
        save_state(root, state)
    print(f"{args.id}: 作業ツリー {worktree}（ブランチ {branch}）、必要なゲート {required_gates(state)}")
    return 0


def claude_command(prompt: str, role: str, budget: float, gate: str, tools: str = "") -> list[str]:
    """ヘッドレス実行のコマンド。道具と許可の扱いは、ゲート（gate）で決まる。結果は VERDICT_SCHEMA の形。

    tools を渡すと、許す道具の一覧（--allowedTools）だけを差し替える（環境の確認は、読む道具だけにする）。
    """
    cmd = [
        "claude", "-p", prompt,
        "--agent", role,
        "--output-format", "json",
        "--json-schema", json.dumps(VERDICT_SCHEMA, ensure_ascii=False),
        "--permission-mode", GATE_PERMISSION.get(gate, DEFAULT_PERMISSION),
        "--max-budget-usd", str(budget),
        "--allowedTools", tools or GATE_TOOLS[gate],
    ]
    if gate in GATE_DISALLOWED:
        cmd += ["--disallowedTools", GATE_DISALLOWED[gate]]
    return cmd


def agent_command(state: dict, gate: str, budget: float) -> list[str]:
    role = state["owner"] if gate == "implement" else GATE_AGENT[gate]
    return claude_command(GATE_PROMPT[gate].format(spec=state["spec"]), role, budget, gate)


def schema_problem(result: object) -> str:
    """構造化された結果が VERDICT_SCHEMA の形でなければ、その理由を返す。正しければ空文字。"""
    if not isinstance(result, dict):
        return "structured_output が無い"
    missing = [key for key in VERDICT_SCHEMA["required"] if key not in result]
    if missing:
        return f"必須の項目が無い: {'、'.join(missing)}"
    if result["verdict"] not in ("pass", "fail"):
        return "verdict が pass でも fail でもない"
    if not isinstance(result["summary"], str):
        return "summary が文字列でない"
    if not isinstance(result["criteria"], list) or not isinstance(result["issues"], list):
        return "criteria か issues が一覧でない"
    for item in result["criteria"]:
        if not isinstance(item, dict) or not isinstance(item.get("pass"), bool):
            return "criteria に pass（真偽）の無い項目がある"
        if not isinstance(item.get("name"), str) or not isinstance(item.get("measured"), str):
            return "criteria に name か measured の無い項目がある"
    for item in result["issues"]:
        if not isinstance(item, dict) or item.get("severity") not in SEVERITIES:
            return "issues に severity が critical / major / minor でない項目がある"
        if not isinstance(item.get("description"), str):
            return "issues に description の無い項目がある"
    return ""


def headless_verdict(returncode: int, stdout: str, gate: str = "") -> tuple[str, str, dict]:
    """ヘッドレス実行の結果から（判定、要約、出力の中身）を決める。gate は、実行したゲート。

    合格にするのは、実行が正常に終わり、構造化された結果に欠けも矛盾も無いときだけ。
    結果を得られなかった場合は error、結果の中身が合格と矛盾する場合は fail にする。
    独立検証（verify）の合格は、確かめた基準が1つ以上あるときだけ（基準ゼロ件の合格は、何も測っていない）。
    """
    if returncode != 0:
        return "error", f"claude が終了コード {returncode} で終わった", {}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "error", "出力を JSON として読めなかった", {}
    if not isinstance(data, dict):
        return "error", "出力が JSON のオブジェクトでない", {}
    if data.get("is_error"):
        return "error", "実行がエラーで終わった（is_error）", data
    if data.get("subtype", "success") != "success":
        return "error", f"実行が正常に終わらなかった（subtype: {data['subtype']}）", data
    result = data.get("structured_output")
    problem = schema_problem(result)
    if problem:
        return "error", f"構造化された結果が得られなかった（{problem}）", data
    if result["verdict"] == "pass":
        if any(item["severity"] == "critical" for item in result["issues"]):
            return "fail", "verdict は pass だが、critical の指摘がある", data
        unmet = [item["name"] for item in result["criteria"] if not item["pass"]]
        if unmet:
            return "fail", f"verdict は pass だが、満たしていない基準がある（{'、'.join(unmet[:5])}）", data
        if gate == "verify" and not result["criteria"]:
            return "fail", "verdict は pass だが、確かめた基準（criteria）が1つも無い", data
    return result["verdict"], result["summary"], data


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    cmd = agent_command(state, args.gate, args.budget)
    if args.dry_run:
        print(shell_line(worktree, cmd))
        return 0
    # 実装の担当（見出しの owner）は、役割の定義があるものだけをヘッドレスで起動する（無い名前で起動すると、
    # CLI は役割の指示なしで動く）。確認系のゲートの担当（GATE_AGENT）は、決まった名前
    owner = state["owner"]
    if args.gate == "implement" and not (worktree / AGENTS_DIR / f"{owner}.md").is_file():
        raise CycleError(
            f"担当 {owner} の役割の定義（{AGENTS_DIR}/{owner}.md）が作業ツリーに無いので、"
            "ヘッドレスで起動できない。仕様の見出しの owner には、定義のある役割の名前を書く"
            "（セッション内で動かした結果は record で記録できる）"
        )
    cmd = launchable(cmd, (root, worktree))

    seen = record_count(state, args.gate)
    before, base = tree_id(worktree), main_base(worktree)
    log = runs_dir(root) / f"{args.id}-{args.gate}-{stamp()}.json"
    try:
        proc = run_captured(cmd, worktree, args.timeout)
    except subprocess.TimeoutExpired:
        log.write_text("timeout", encoding="utf-8")
        verdict, summary, data = "error", f"{args.timeout} 秒で終わらなかったので打ち切った", {}
    except OSError as exc:
        raise CycleError(f"claude を起動できない: {exc}") from exc
    else:
        log.write_text(proc.stdout or proc.stderr, encoding="utf-8")
        verdict, summary, data = headless_verdict(proc.returncode, proc.stdout, args.gate)

    # 実装は「作った結果」を、確認系は「見た対象」（実行前の内容）を記録する
    tree = tree_id(worktree) if args.gate == "implement" else before
    by = str(data["session_id"]) if data.get("session_id") else "headless-unknown"
    report = log.relative_to(root).as_posix()
    adopted = add_record(root, args.id, args.gate, verdict, tree, base, by, "headless", report, seen)
    cost = data.get("total_cost_usd")
    note = f" 費用の概算 ${cost}" if cost else ""
    print(f"{args.id} {args.gate}: {verdict}（{summary}）記録 {log.name}{note}")
    if not adopted:
        print(NOT_ADOPTED)
    return 0 if verdict == "pass" and adopted else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    state = load_state(repo_root(), args.id)
    print(tree_id(Path(state["worktree"])))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    by = args.by.strip()
    if not by:
        raise CycleError("--by が空。担当の名前を渡す")
    if args.tree:
        tree = resolve_tree(worktree, args.tree)
    elif args.gate in TREE_REQUIRED_GATES:
        raise CycleError(
            f"{args.gate} の記録には --tree が必要。確認を始める前に snapshot で控えたツリーIDを渡す"
        )
    else:
        tree = tree_id(worktree)
    base = main_base(worktree)
    add_record(root, args.id, args.gate, args.verdict, tree, base, by, "session", args.report or "")
    print(f"{args.id} {args.gate}: {args.verdict} を記録（対象 {tree[:10]}、担当 {by}）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    seen = record_count(state, "check")
    base = main_base(worktree)
    verdict, tree, output = run_check(worktree)
    log = runs_dir(root) / f"{args.id}-check-{stamp()}.log"
    log.write_text(output, encoding="utf-8")
    report = log.relative_to(root).as_posix()
    adopted = add_record(root, args.id, "check", verdict, tree, base, CHECK_SCRIPT, "script", report, seen)
    tail = output.strip().splitlines()[-1:] or [""]         # 標準出力の最後の行（run_check）
    print(f"{args.id} check: {verdict}（{tail[0]}）")
    if not adopted:
        print(NOT_ADOPTED)
    return 0 if verdict == "pass" and adopted else 1


def cmd_record_ci(args: argparse.Namespace) -> int:
    """Windows での手元の確認（scripts/local_ci.py）の結果を、ゲート windows の記録として取り込む。

    取り込むのは、形が正しく、Windows で、コミットを取り出して（mode: checkout、未コミットの変更なし、
    内容の食い違いなし）実行された、作業ツリーのいまの内容に対する結果だけ。
    それ以外は、記録を変えずにエラーで終わる。結果が不合格なら、不合格として記録する。
    内容の突き合わせは、ほかの確認系のゲートと同じ決まり（結果の後で増えてよいのは、例外の場所への
    新しいファイルだけ）。
    """
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    source = Path(args.result)
    try:
        data = source.read_bytes()
        result = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as exc:        # 読めない、UTF-8 でない、JSON でない
        raise CycleError(f"結果のファイル {source} を読めない: {exc}") from exc
    refused = f"結果のファイル {source.name} は取り込めない: "
    problem = ci_result_problem(result)
    if problem:
        raise CycleError(refused + problem)
    system = result["os"]["system"]
    if system != WINDOWS_SYSTEM:
        raise CycleError(refused + f"Windows での結果ではない（{system[:40]}）")
    # 取り込むのは、コミットを取り出して、その内容のまま確かめた結果だけ。作業フォルダで確かめた結果
    # （--current）は、無視の対象のファイル（コミットに無い）に助けられた合格でも、ツリーが同じになるので
    # 取り込まない
    if result.get("mode") != "checkout":
        raise CycleError(
            refused + "コミットを取り出して確かめた結果ではない（--current の結果は取り込まない）。"
            "Windows のPCで python scripts\\local_ci.py --ref origin/<ブランチ> を実行した結果を渡す"
        )
    if result.get("dirty") is not False:
        raise CycleError(refused + "未コミットの変更を含む内容に対する結果（dirty）")
    if result.get("changed") != []:
        raise CycleError(refused + "確かめた内容が、対象のコミットと食い違っていた結果（changed）")
    tree = result["tree"]
    current = tree_id(worktree)
    try:
        differing = changed_after_gate(worktree, tree, current)
    except CycleError as exc:
        raise CycleError(
            refused + f"結果のツリー（{tree[:10]}）が、このリポジトリに無い"
            "（別の内容に対する結果。確かめたコミットを取得してから、やり直す）"
        ) from exc
    if differing:
        raise CycleError(
            refused + f"結果のツリー（{tree[:10]}）が、作業ツリーのいまの内容（{current[:10]}）と"
            f"一致しない（違うファイル: {', '.join(differing[:5])}）"
        )
    copy = runs_dir(root) / f"{args.id}-{WINDOWS_GATE}-{stamp()}.json"
    copy.write_bytes(data)                      # 取り込んだ結果の写し（中身は変えない）
    extra = {"os": system, "commit": result["commit"]}
    # 分かれ目は、確かめたコミットのもの（取り込む時点の HEAD では、結果の後の付け替えを見逃す）
    base = main_base(worktree, result["commit"])
    add_record(
        root, args.id, WINDOWS_GATE, result["verdict"], tree, base, LOCAL_CI_SCRIPT, "ci",
        copy.relative_to(root).as_posix(), extra=extra,
    )
    print(f"{args.id} {WINDOWS_GATE}: {result['verdict']} を記録（対象 {tree[:10]}、{system}）")
    return 0 if result["verdict"] == "pass" else 1


def changed_after_gate(worktree: Path, gate_tree: str, current: str) -> list[str]:
    """ゲートの後で変わったファイルのうち、許されないもの。

    許すのは、例外の場所（AFTER_GATE_FILES）への、決まった名前の、ゲートの時点で無かったファイルの追加だけ。
    名前の変更は「削除と追加」に分けて見る。ファイル名は -z で受け取り、引用符つきの表記にさせない。
    """
    proc = run_git(worktree, "diff", "--no-renames", "--name-status", "-z", gate_tree, current)
    fields = proc.stdout.split("\0")
    # 出力は「種類 NUL ファイル名 NUL」の繰り返し。その形でなければ、変更なしとは見なさない
    if proc.returncode != 0 or fields.pop() != "" or len(fields) % 2:
        raise CycleError(f"git diff の結果を読めない: {proc.stderr.strip()}")
    return [
        path for status, path in zip(fields[0::2], fields[1::2], strict=True)
        if not (status == "A" and may_add_after_gate(path))
    ]


def may_add_after_gate(path: str) -> bool:
    """ゲートの後で足してよいファイルか（AFTER_GATE_FILES）。名前は、場所の中のどの階層でも同じ決まり。"""
    name = path.rpartition("/")[2]
    return any(
        path.startswith(place) and fnmatch.fnmatchcase(name, pattern) for place, pattern in AFTER_GATE_FILES
    )


def gate_problems(root: Path, state: dict, worktree: Path, current: str) -> list[str]:
    """統合を妨げる理由の一覧。空なら、内容 current（ツリーID）を統合してよい。"""
    problems = []
    base = main_base(worktree)
    try:
        if header_changed(read_header(root / state["spec"]), state):
            problems.append(HEADER_DRIFT)
    except (OSError, CycleError) as exc:
        problems.append(f"仕様の見出しを読めない（{exc}）")
    gates = state.get("gates", {})
    for gate in required_gates(state):
        entry = gates.get(gate)
        if entry is None:
            problems.append(f"{gate}: まだ記録が無い")
            continue
        if entry.get("verdict") != "pass":
            problems.append(f"{gate}: {entry.get('verdict')}")
            continue
        if gate == "implement":
            continue
        if not is_tree_id(entry.get("tree")):
            problems.append(f"{gate}: 記録の対象がツリーIDでない（{entry.get('tree')}）。やり直す")
            continue
        if entry.get("base") is None or entry["base"] != base:
            problems.append(
                f"{gate}: 確認の後で、main との分かれ目（土台）が変わっている。"
                "土台を付け替えた内容を取り込むと、"
                "main に入った他の成果が消える。main を取り込み直して（git merge main）からやり直す"
            )
            continue
        if gate == "check":
            if entry["tree"] != current:
                problems.append("check: 確認した後で内容が変わっている。check をやり直す")
            continue
        try:
            stale = changed_after_gate(worktree, entry["tree"], current)
        except CycleError:
            problems.append(f"{gate}: 記録の対象（{entry['tree'][:10]}）を読めない。やり直す")
            continue
        if stale:
            problems.append(f"{gate}: 確認の後で変わったファイルがある（{', '.join(stale[:5])}）。やり直す")
    # 実装した担当は、履歴の全体から集める（implement を別の名前で記録し直しても消えない）
    implementers = {h.get("by") for h in state.get("history", []) if h.get("gate") == "implement"}
    if gates.get("verify") and gates["verify"].get("by") in implementers:
        problems.append("verify: 実装した本人が検証している。別の担当でやり直す")
    return problems


def uncommitted_problem(worktree: Path, committed: str, current: str) -> str:
    """作業ツリーの内容（current）が、HEAD のコミットのツリー（committed）と違うときの、理由の文。

    違うファイルの名前を示す。利用者の git の設定（core.excludesFile など）だけで無視しているファイルは、
    git status には出ないが、ツリーIDは利用者の無視の指定を読まないので、内容に数える（決まり9）。
    それらは、リポジトリの無視の指定に足すよう案内する。
    """
    names = git_paths(worktree, "diff", "--no-renames", "--name-only", "-z", committed, current)
    # 利用者の設定のままの git で、無視の対象と出るもの（追跡しているファイルは出ない）
    proc = run_git(worktree, "check-ignore", "-z", "--stdin", stdin="".join(f"{name}\0" for name in names))
    ignored = [name for name in proc.stdout.split("\0") if name in names]
    others = [name for name in names if name not in ignored]

    def listed(items: list[str]) -> str:
        more = f" ほか {len(items) - 5} 件" if len(items) > 5 else ""
        return f"（{', '.join(items[:5])}{more}）" if items else ""

    parts = []
    if others or not ignored:
        parts.append(f"作業ツリーに未コミットの変更がある{listed(others)}。コミットしてから統合する")
    if ignored:
        parts.append(
            f"作業ツリーに、利用者の git の設定（core.excludesFile など）だけで無視しているファイルがある"
            f"{listed(ignored)}。ハーネスは利用者の無視の指定を読まないので、内容に数える。"
            "リポジトリの .gitignore か .git/info/exclude に足す（または消す）"
        )
    return "。".join(parts)


def unit_problems(root: Path, state: dict) -> tuple[list[str], str, str]:
    """統合を妨げる理由（作業ツリーとゲート）、作業ツリーのいまの内容のツリーID、HEAD のコミット。

    status と merge は、同じこの判定を使う（status が「できる」と言うのに、merge が拒む、をなくす）。
    """
    worktree = Path(state["worktree"])
    try:
        worktree_branch(worktree)
        current = tree_id(worktree)
        head = git(worktree, "rev-parse", "HEAD")
        committed = git(worktree, "rev-parse", f"{head}^{{tree}}")
    except CycleError as exc:
        return [str(exc)], "", ""
    problems = gate_problems(root, state, worktree, current)
    # 取り込むのは HEAD のコミット。そのツリーが、いまの内容（＝ゲートを判定した内容）と同じであること
    if committed != current:
        problems.append(uncommitted_problem(worktree, committed, current))
    return problems, current, head


def reset_count(state: dict) -> int:
    return sum(1 for h in state.get("history", []) if h.get("event") == "reset")


def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    ids = [args.id] if args.id else sorted(p.stem for p in (root / ".cycle" / "state").glob("*.json"))
    if not ids:
        print("進行中の作業単位は無い")
    for uid in ids:
        state = load_state(root, uid)
        done = "  統合済み" if state.get("merged_at") else ""
        print(f"{uid}（{state['spec']}、担当 {state['owner']}）{done}")
        for gate in required_gates(state):
            entry = state.get("gates", {}).get(gate)
            text = f"{entry['verdict']}  {entry['by']}  {entry['at']}" if entry else "未実施"
            print(f"  {gate:<10} {text}")
        if reset_count(state):
            print(f"  ゲートの記録を消した回数: {reset_count(state)}")
        if not state.get("merged_at"):
            problems, _, _ = unit_problems(root, state)
            print("  統合: " + ("できる" if not problems else "まだできない"))
            for p in problems:
                print(f"    - {p}")
    if pending_file(root).exists():
        print("注意: 前回の統合が途中で終わっている。次の merge が、本体（main）を取り込み前に戻す")
    return 0


def restore(root: Path, commit: str) -> None:
    """本体を取り込み前のコミットに戻す。取り込み途中の印（MERGE_HEAD）と競合の印も消える。"""
    git(root, "reset", "--hard", commit)


def on_main(root: Path) -> bool:
    return run_git(root, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip() == "main"


def recover_merge(root: Path) -> str:
    """前回の merge が、取り込んだ後の確認を終えずに止まっていたら（強制終了など）、本体を取り込み前に戻す。

    戻したか、戻すものが無ければ空文字を返す。
    本体がその後で変わっていて、人が確かめる必要があれば、その理由を返す（本体には触らない）。
    """
    path = pending_file(root)
    if not path.exists():
        return ""
    manual = f"本体（main）の状態を確かめ、必要なら取り込み前に戻してから、{path} を消す"
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
        uid, before, head = pending["id"], pending["before"], pending["head"]
        finished = bool(load_state(root, uid).get("merged_at"))
    except (ValueError, KeyError, TypeError, CycleError):
        return f"前回の統合の控えを読めない。{manual}"
    manual += f"（取り込み前のコミット {before}、取り込もうとしたコミット {head}）"
    if not finished:
        if not on_main(root):
            return f"前回の {uid} の統合が途中で終わっているが、本体が main にいない。{manual}"
        now_head, *parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()
        merge_head = run_git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD").stdout.strip()
        in_progress = now_head == before and merge_head == head
        committed = parents == [before, head] and not git(root, "status", "--porcelain")
        if in_progress or committed:
            restore(root, before)
            print(f"前回の {uid} の統合が途中で終わっていたので、本体を取り込み前（{before[:10]}）に戻した")
        elif now_head != before or merge_head:
            return f"前回の {uid} の統合が途中で終わっているが、本体がその後で変わっている。{manual}"
    path.unlink(missing_ok=True)
    return ""


def cmd_merge(args: argparse.Namespace) -> int:
    root = repo_root()
    # 統合は1つずつ。取り消し（reset）が、並行する別の統合を巻き込まないようにする。
    # 作業単位のロックも最後まで持ち、判定から取り込みまでの間に記録が変わらないようにする。
    with termination_as_exception(), locked(merge_lock(root)), locked(unit_lock(root, args.id)):
        problem = recover_merge(root)
        if problem:
            print(f"{args.id}: 統合できない\n  - {problem}")
            return 1
        return merge_unit(root, args.id)


def merge_unit(root: Path, uid: str) -> int:
    state = load_state(root, uid)
    if state.get("merged_at"):
        print(f"{uid}: 既に統合済み（{str(state.get('merged_commit', ''))[:10]}）")
        return 1
    # 取り込むのは、作業ツリーの HEAD のコミット（unit_problems が、そのツリーと、いまの内容＝ゲートを判定した
    # 内容が同じであることを確かめる）。状態に控えたブランチ名は使わない。
    problems, current, head = unit_problems(root, state)
    if not on_main(root):
        problems.append("本体のチェックアウトが main ではない")
    elif git(root, "status", "--porcelain") or tree_id(root) != git(root, "rev-parse", "HEAD^{tree}"):
        problems.append("本体（main）に未コミットの変更がある")
    if problems:
        print(f"{uid}: 統合できない")
        for p in problems:
            print(f"  - {p}")
        return 1
    before = git(root, "rev-parse", "HEAD")
    if git_ok(root, "merge-base", "--is-ancestor", head, before):
        print(f"{uid}: 取り込むものが無い（作業ツリーの HEAD {head[:10]} は、既に main に入っている）")
        return 1

    gates = state["gates"]
    lines = [f"{g}: {gates[g]['verdict']}（{gates[g]['by']}）" for g in required_gates(state)]
    if reset_count(state):
        lines.append(f"ゲートの記録を消した回数: {reset_count(state)}")
    message = f"{uid} を統合\n\n仕様: {state['spec']}\n対象: {head}（ツリー {current}）\n" + "\n".join(lines)
    trailers = os.environ.get("CYCLE_COMMIT_TRAILERS", "").strip()
    if trailers:
        message += "\n\n" + trailers
    # ここで強制終了されても、次の merge が取り込み前に戻せるよう、控えを置く
    pending = pending_file(root)
    pending.write_text(json.dumps({"id": uid, "before": before, "head": head}), encoding="utf-8")
    try:
        git(root, "merge", "--no-ff", head, "-m", message)
        verdict, _, output = run_check(root)
        (runs_dir(root) / f"{uid}-merge-check-{stamp()}.log").write_text(output, encoding="utf-8")
    except BaseException as exc:
        # 競合、中断（Ctrl-C、終了の合図）、記録を書けないなど。どの場合も本体を元に戻す。
        restore(root, before)
        pending.unlink(missing_ok=True)
        if not isinstance(exc, CycleError):
            raise
        print(f"{uid}: 取り込めなかったので、本体を取り込み前（{before[:10]}）に戻した")
        print(str(exc)[-600:])
        return 1
    if verdict != "pass":
        restore(root, before)
        pending.unlink(missing_ok=True)
        print(f"{uid}: main に取り込んだ状態で check が通らなかった（{verdict}）ので、取り込みを取り消した")
        print(output.strip()[-600:])
        return 1
    state["merged_at"] = now()
    state["merged_commit"] = git(root, "rev-parse", "HEAD")
    state["merged_head"] = head
    save_state(root, state)
    # 控えは「統合済み」を記録してから消す（逆の順だと、間で強制終了されたとき、済んだ統合を取り消してしまう）
    pending.unlink(missing_ok=True)
    print(f"{uid}: main に取り込んだ（{state['merged_commit'][:10]}）。push はまだ")
    return 0


def main(argv: list[str] | None = None) -> int:
    quiet_error_dialogs()
    no_current_directory_search()
    parser = argparse.ArgumentParser(prog="cycle.py", description="開発サイクルのゲートを運ぶ")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="作業ツリーとブランチを用意する")
    p.add_argument("id")
    p.add_argument("--worktree", help="既にある作業ツリーを使う")
    p.add_argument("--branch")
    p.add_argument("--reset", action="store_true", help="ゲートの記録を消してやり直す（履歴には残る）")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("run", help="担当エージェントをヘッドレスで起動する")
    p.add_argument("id")
    p.add_argument("gate", choices=AGENT_GATES)
    p.add_argument("--dry-run", action="store_true", help="実行せず、コマンドだけ表示する")
    p.add_argument("--budget", type=float, default=5.0, help="1回の上限額（米ドル）")
    p.add_argument("--timeout", type=int, default=3600, help="打ち切りまでの秒数")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("snapshot", help="いまの内容のツリーIDを表示する")
    p.add_argument("id")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("record", help="セッション内で動かした結果を記録する")
    p.add_argument("id")
    p.add_argument("gate", choices=AGENT_GATES)
    p.add_argument("--verdict", required=True, choices=["pass", "fail"])
    p.add_argument("--by", required=True, help="担当の名前（実装と検証で別であること）")
    p.add_argument("--tree", help="確認の対象にしたツリーID（snapshot の出力）。省けるのは implement だけ")
    p.add_argument("--report", help="報告を保存したファイル")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("check", help="scripts/check.py を実行して記録する")
    p.add_argument("id")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("record-ci", help="Windows での手元の確認（scripts/local_ci.py）の結果を取り込む")
    p.add_argument("id")
    p.add_argument("--result", required=True, help="結果のファイル（.cycle/ci/ の下の JSON）")
    p.set_defaults(func=cmd_record_ci)

    p = sub.add_parser("status", help="ゲートの状況")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("merge", help="必要なゲートがすべて合格のときだけ main に取り込む")
    p.add_argument("id")
    p.set_defaults(func=cmd_merge)

    make_output_safe()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CycleError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
