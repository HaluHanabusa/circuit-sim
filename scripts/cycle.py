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
  scripts/cycle.py status [<ID>]                 ゲートの状況を表示する
  scripts/cycle.py merge <ID>                    必要なゲートがすべて合格のときだけ main に取り込む

ゲート: implement（実装）、check（自動確認）、review（レビュー）、verify（独立検証）、ux（UX確認）
標準ライブラリだけで動く。Windows、macOS、Linux のどれでも同じ決まりを強制する
（Windows では `python scripts/cycle.py …` の形で実行する。OSで違う箇所は「OSの違い」の節にまとめてある）。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
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

# レビューや検証の後で足してよい場所（検証担当の受け入れ試験と記録）。
# 許すのは、ゲートの時点で無かったファイルの追加だけ。それ以外の変更があれば、そのゲートは古いものとして扱う。
AFTER_GATE_PATHS = ("tests/acceptance/", "docs/verification/")

HEADER_KEYS = ("id", "owner", "needs_ux", "gates")

# git の置き場所を外から差し替える環境変数。持ち込むと、対象でないリポジトリや索引を見てしまう。
GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)

GATE_TOOLS = {
    "implement": "Read,Edit,Write,Bash,Grep,Glob",
    "review": "Read,Bash,Grep,Glob",
    "verify": "Read,Edit,Write,Bash,Grep,Glob",
    "ux": "Read,Edit,Write,Bash,Grep,Glob",
}

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
HEADER_DRIFT = "仕様の見出し（owner、needs_ux、gates）が start の後で変わっている。start をやり直す"


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
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
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


def shell_line(cwd: Path, cmd: list[str]) -> str:
    """人が端末に貼って実行できる形の1行（Windows では、コマンド プロンプト用の書き方）。"""
    if WINDOWS:
        return f"cd /d {subprocess.list2cmdline([str(cwd)])} && {subprocess.list2cmdline(cmd)}"
    return f"cd {shlex.quote(str(cwd))} && {shlex.join(cmd)}"


def kill_tree(proc: subprocess.Popen) -> None:
    """起動したプロセスを止める。Windows では、起動用のファイル（claude.cmd）の先のプロセスも一緒に止める。"""
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.kill()


def run_captured(cmd: list[str], cwd: Path, timeout: float | None) -> subprocess.CompletedProcess:
    """コマンドを実行して、出力を UTF-8 の文字列で受け取る。

    呼び出し元の標準入力は渡さない（人が見ていない実行で、入力を待って止まり続けないように）。
    時間切れなら、起動したプロセスを止めてから subprocess.TimeoutExpired を送出する。

    止めた後は、出力の終わりを待たない。起動したプロセスが残した子プロセスが出力の口を持ったままだと、
    その子が終わるまで戻れなくなる（打ち切りは、人が見ていない実行の最後の歯止め）。
    打ち切った実行の出力は使わないので、読み残してよい。
    """
    with subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
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


def run_git(
    cwd: Path, *args: str, index: Path | None = None, stdin: str | None = None,
) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION_VARS}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def git(cwd: Path, *args: str, index: Path | None = None, stdin: str | None = None) -> str:
    proc = run_git(cwd, *args, index=index, stdin=stdin)
    if proc.returncode != 0:
        raise CycleError(f"git {' '.join(args)} が失敗: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def git_paths(cwd: Path, *args: str, index: Path | None = None) -> list[str]:
    """-z を付けた git の出力（NUL 区切りのファイル名）を一覧にする。"""
    proc = run_git(cwd, *args, index=index)
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

    書けるのは id、owner、needs_ux、gates だけ。読めない行、知らない値、食い違いはエラーにする
    （書き間違いが「ゲートは不要」と読まれないように）。
    返すのは id、owner、needs_ux（真偽）、gates_required（見出しに gates があればその一覧、無ければ空）。
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

    header = {"id": raw["id"], "owner": raw["owner"], "needs_ux": False, "gates_required": []}
    if "needs_ux" in raw:
        if raw["needs_ux"].lower() not in ("true", "false"):
            raise CycleError(
                f"{spec.name}: needs_ux は true か false だけで書く（書かれている値: {raw['needs_ux']}）"
            )
        header["needs_ux"] = raw["needs_ux"].lower() == "true"
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
    """必要なゲート。source は見出し（read_header の結果）か、状態（start が保存したもの）。"""
    wanted = list(source.get("gates_required") or DEFAULT_GATES)
    if source.get("needs_ux"):
        wanted.append("ux")
    return [g for g in ALL_GATES if g in wanted]


def header_changed(header: dict, state: dict) -> bool:
    """見出しのうちゲートに関わる内容（担当、必要なゲート）が、状態と違うか。"""
    return header["owner"] != state.get("owner") or required_gates(header) != required_gates(state)


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
    """終了の合図（stop_signals）を例外に変えて、後始末（取り込みの取り消し）が走るようにする。"""
    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        saved = {sig: signal.signal(sig, stop) for sig in stop_signals()}
    except ValueError:          # メインのスレッド以外からは設定できない
        saved = {}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def tree_id(worktree: Path) -> str:
    """作業ツリーのいまの内容（未コミットと新規ファイルを含む）を表すツリーID。

    一時的な索引で計算するので、作業ツリーの索引（選んでステージした状態、競合の解決の途中）は変えない。
    空の索引から組み直すので、索引に付けた「変更を見ない」印にも左右されない。
    """
    with tempfile.TemporaryDirectory(prefix="cycle-index-") as tmp:
        index = Path(tmp) / "index"
        git(worktree, "add", "-A", index=index)
        # 無視の対象でも、追跡しているファイル（git add -f で足したもの）は内容に含める
        taken = set(git_paths(worktree, "ls-files", "-z", index=index))
        forced = [
            path for path in git_paths(worktree, "ls-files", "-z")
            if path not in taken and ((worktree / path).is_file() or (worktree / path).is_symlink())
        ]
        if forced:
            names = "\0".join(forced) + "\0"
            git(worktree, "update-index", "--add", "-z", "--stdin", index=index, stdin=names)
        return git(worktree, "write-tree", index=index)


def is_tree_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None


def resolve_tree(worktree: Path, value: str) -> str:
    """--tree の値を、記録する時点でツリーIDに直す（HEAD やブランチ名のまま記録しない）。"""
    tree = run_git(worktree, "rev-parse", "--verify", "--quiet", f"{value}^{{tree}}").stdout.strip()
    if not is_tree_id(tree):        # 解決できなければ、何も出力されない
        raise CycleError(f"--tree の値（{value}）をツリーとして読めない。snapshot が表示したIDを渡す")
    return tree


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


def record_count(state: dict, gate: str) -> int:
    """このゲートの記録と、記録の消去が、これまでに何回あったか。"""
    return sum(1 for h in state.get("history", []) if h.get("gate") == gate or h.get("event") == "reset")


def add_record(
    root: Path, uid: str, gate: str, verdict: str, tree: str, by: str, source: str,
    report: str = "", seen: int | None = None,
) -> bool:
    """ゲートの結果を記録する。ロックを取り、状態を読み直してから書く（並行する実行の記録を失わない）。

    seen は、実行を始めたときの record_count。実行中に同じゲートの別の記録が入っていたら、
    この結果が合格でも採用しない（先に始めた実行の合格で、後から入った結果を上書きしない）。
    採用したら True を返す。採用しなかった結果も履歴には残す。
    """
    entry = {"verdict": verdict, "tree": tree, "by": by, "source": source, "report": report, "at": now()}
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

    返すのは（判定、確認した内容のツリーID、出力）。

    合格にするのは、終了コードが 0 で、実行の前後で内容が変わっていないときだけ。
    """
    before = tree_id(cwd)
    if not (cwd / CHECK_SCRIPT).is_file():
        return "error", before, f"{CHECK_SCRIPT} が無いので、確認を起動できない"
    try:
        proc = subprocess.run(
            [sys.executable, CHECK_SCRIPT], cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=python_env(),
        )
    except OSError as exc:
        return "error", before, f"{CHECK_SCRIPT} を起動できない: {exc}"
    output = proc.stdout + proc.stderr
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
        if old.get("gates") and not args.reset and header_changed(header, old):
            raise CycleError(
                f"{args.id}: ゲートの記録があるのに、見出し（owner、needs_ux、gates）が変わっている。"
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
        state = {
            "id": args.id,
            "spec": spec.relative_to(root).as_posix(),
            "owner": header["owner"],
            "needs_ux": header["needs_ux"],
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


def claude_command(prompt: str, role: str, budget: float, tools: str) -> list[str]:
    """ヘッドレス実行のコマンド。結果は VERDICT_SCHEMA の形で受け取る。"""
    return [
        "claude", "-p", prompt,
        "--agent", role,
        "--output-format", "json",
        "--json-schema", json.dumps(VERDICT_SCHEMA, ensure_ascii=False),
        "--permission-mode", "acceptEdits",
        "--max-budget-usd", str(budget),
        "--allowedTools", tools,
    ]


def agent_command(state: dict, gate: str, budget: float) -> list[str]:
    role = state["owner"] if gate == "implement" else GATE_AGENT[gate]
    return claude_command(GATE_PROMPT[gate].format(spec=state["spec"]), role, budget, GATE_TOOLS[gate])


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


def headless_verdict(returncode: int, stdout: str) -> tuple[str, str, dict]:
    """ヘッドレス実行の結果から（判定、要約、出力の中身）を決める。

    合格にするのは、実行が正常に終わり、構造化された結果に欠けも矛盾も無いときだけ。
    結果を得られなかった場合は error、結果の中身が合格と矛盾する場合は fail にする。
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
    return result["verdict"], result["summary"], data


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    cmd = agent_command(state, args.gate, args.budget)
    if args.dry_run:
        print(shell_line(worktree, cmd))
        return 0
    cmd = launchable(cmd, (root, worktree))

    seen = record_count(state, args.gate)
    before = tree_id(worktree)
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
        verdict, summary, data = headless_verdict(proc.returncode, proc.stdout)

    # 実装は「作った結果」を、確認系は「見た対象」（実行前の内容）を記録する
    tree = tree_id(worktree) if args.gate == "implement" else before
    by = str(data["session_id"]) if data.get("session_id") else "headless-unknown"
    report = log.relative_to(root).as_posix()
    adopted = add_record(root, args.id, args.gate, verdict, tree, by, "headless", report, seen)
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
    add_record(root, args.id, args.gate, args.verdict, tree, by, "session", args.report or "")
    print(f"{args.id} {args.gate}: {args.verdict} を記録（対象 {tree[:10]}、担当 {by}）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    seen = record_count(state, "check")
    verdict, tree, output = run_check(worktree)
    log = runs_dir(root) / f"{args.id}-check-{stamp()}.log"
    log.write_text(output, encoding="utf-8")
    report = log.relative_to(root).as_posix()
    adopted = add_record(root, args.id, "check", verdict, tree, CHECK_SCRIPT, "script", report, seen)
    tail = output.strip().splitlines()[-1:] or [""]
    print(f"{args.id} check: {verdict}（{tail[0]}）")
    if not adopted:
        print(NOT_ADOPTED)
    return 0 if verdict == "pass" and adopted else 1


def changed_after_gate(worktree: Path, gate_tree: str, current: str) -> list[str]:
    """ゲートの後で変わったファイルのうち、許されないもの。

    許すのは、例外の場所（AFTER_GATE_PATHS）への、ゲートの時点で無かったファイルの追加だけ。
    名前の変更は「削除と追加」に分けて見る。ファイル名は -z で受け取り、引用符つきの表記にさせない。
    """
    proc = run_git(worktree, "diff", "--no-renames", "--name-status", "-z", gate_tree, current)
    fields = proc.stdout.split("\0")
    # 出力は「種類 NUL ファイル名 NUL」の繰り返し。その形でなければ、変更なしとは見なさない
    if proc.returncode != 0 or fields.pop() != "" or len(fields) % 2:
        raise CycleError(f"git diff の結果を読めない: {proc.stderr.strip()}")
    return [
        path for status, path in zip(fields[0::2], fields[1::2], strict=True)
        if not (status == "A" and path.startswith(AFTER_GATE_PATHS))
    ]


def gate_problems(root: Path, state: dict, worktree: Path, current: str) -> list[str]:
    """統合を妨げる理由の一覧。空なら、内容 current（ツリーID）を統合してよい。"""
    problems = []
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


def unit_problems(root: Path, state: dict) -> tuple[list[str], str]:
    """統合を妨げる理由（作業ツリーとゲート）と、作業ツリーのいまの内容のツリーID。"""
    worktree = Path(state["worktree"])
    try:
        worktree_branch(worktree)
        current = tree_id(worktree)
    except CycleError as exc:
        return [str(exc)], ""
    return gate_problems(root, state, worktree, current), current


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
            problems, _ = unit_problems(root, state)
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
    worktree = Path(state["worktree"])
    # 取り込むのは、作業ツリーの HEAD のコミット。そのツリーが、いまの内容（＝ゲートを判定した内容）と
    # 同じであることを確かめる。状態に控えたブランチ名は使わない。
    problems, current = unit_problems(root, state)
    head = ""
    if current:
        head = git(worktree, "rev-parse", "HEAD")
        if git(worktree, "rev-parse", f"{head}^{{tree}}") != current:
            problems.append("作業ツリーに未コミットの変更がある。コミットしてから統合する")
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
