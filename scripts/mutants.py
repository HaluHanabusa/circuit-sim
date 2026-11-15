#!/usr/bin/env python3
"""1か所ずつ壊す確認（変異）を、繰り返せる形で流す。

道具のスクリプト（scripts/cycle.py、scripts/doctor.py、scripts/local_ci.py）を1か所ずつ壊し、道具の試験を
流して、試験が落ちる（壊れたことに気づく）かを確かめる。落ちなかった壊し方（生き残り）は、試験の穴か、
壊しても動作が変わらない書き換え（同値）のどちらか。同値のものは、理由を付けて scripts/mutants.toml に書く。

  python scripts/mutants.py list [--file F]           壊し方の一覧（番号、場所、壊し方）
  python scripts/mutants.py run [--file F] [--only ID ...] [--jobs N] [--sample N]
                                                      壊し方を流して、生き残りを表示する

壊し方は2通り:
  - 機械的に作るもの: 比べ方の反転（== と !=、< と >=、in と not in など）、and と or の入れ替え、not を外す、
    条件の反転、真偽の定数の反転、比べる数を1つずらす、文を消す（呼び出し、raise、continue、break）、
    決まりの一覧（大文字の名前の、文字列の並び）から1つ外す。表示だけのもの（print の呼び出しの中、
    f 文字列、説明の文字列）は作らない。
  - scripts/mutants.toml に手で書くもの（[[mutant]]。元の文字列と、置き換える文字列）。

流し方（速さのため）:
  1. 壊す前の試験を1回流し、試験ごとに、スクリプトのどの行を通ったかを記録する（標準の sys.monitoring。
     試験が起動した Python の子プロセスも、PYTHONPATH に置いた sitecustomize で記録する）。結果は
     .cycle/mutants/ に控え、スクリプトと試験の内容が同じなら使い回す。
  2. 壊した行を通る試験だけを、短い順に流す（-x。最初に落ちたところで止める）。関数の外の代入（決まりの
     一覧など）を壊したものは、その名前を使う関数を通る試験を流す。
  3. --confirm を付けると、そこで生き残ったものを、道具の試験の全体でもう一度流す。付けなくても、生き残りを
     見逃す側には倒れない（選び方の漏れは、生き残りを多めに数えるだけ）。
各回の後で、その回が起動して残ったプロセス（親を失った偽の claude など）を止める（環境変数の印で探す。
Linux の /proc を読む。ほかのOSでは、探せないことを表示する）。

試験は、リポジトリの写し（追跡しているファイルと、無視されていない新しいファイル）の中で流す。
作業中のフォルダには触らない。本物の claude は起動しない（試験の遮断 tests/conftest.py が写しでも効く）。
終了コード: 0 = 一覧に無い生き残りが無い、1 = ある、2 = 流せなかった（壊す前の試験が落ちる、など）。
標準ライブラリだけで動く。
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST_FILE = "scripts/mutants.toml"
TARGETS = ("scripts/cycle.py", "scripts/doctor.py", "scripts/local_ci.py")
# 道具の試験。壊し方ごとに、ここから選んで流す
TOOL_TESTS = (
    "tests/test_harness.py", "tests/test_harness_os.py", "tests/test_dev_tools.py", "tests/test_local_ci.py",
    "tests/test_claude_guard.py",
    "tests/test_stop_posix.py", "tests/test_console_encoding.py", "tests/test_error_dialogs.py",
    "tests/acceptance/test_w1_recheck.py", "tests/acceptance/test_w1_windows.py",
    "tests/acceptance/test_w2_local_ci_gate.py",
)
STATE_DIR = ".cycle/mutants"
MARK_VAR = "CIRCUIT_SIM_MUTANT_RUN"     # この回が起動したプロセスの印（後始末で探す）
# 試験の入れ子を止める印（tests/conftest.py）。この道具は試験の外から写しを流すので、引き継がない
NESTED_VAR = "CIRCUIT_SIM_PYTEST_RUNNING"
# pytest の終了コード: 1 = 失敗した試験がある、2 = 集める途中で止まった（壊した道具を読み込めない、など）、
# 71 = 見張り役の claude が起動された（tests/conftest.py）。どれも、壊れたことに試験が気づいた
KILLED_CODES = {1: "failed", 2: "collection", 71: "sentinel"}
COVER_VAR = "CIRCUIT_SIM_MUTANT_COVER"  # 通った行を書く場所（壊す前の試験のときだけ）
SELECT_VAR = "CIRCUIT_SIM_MUTANT_SELECT"  # 流す試験の一覧のファイル
KILLER_VAR = "CIRCUIT_SIM_MUTANT_KILLER"  # 最初に落ちた試験の名前を書くファイル
KILLERS_FILE = "killers.json"             # 壊し方ごとに、前回それを落とした試験（次の回は、それを先に流す）
# 通った行の記録の仕方の版（記録の仕方を変えたら上げる。壊す前の試験の記録を使い回す鍵に入る）
COVER_VERSION = 1
RUN_TIMEOUT = 900                        # 1回の試験の上限（秒）。超えたら「止まった」として落ちた扱い

NEGATED_COMPARE = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
COMPARE_TEXT = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=",
    ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
}


@dataclasses.dataclass
class Mutant:
    id: str
    file: str
    line: int
    end_line: int
    kind: str
    original: str
    replacement: str
    start: int          # 置き換える範囲（ファイルの中の文字の位置）
    end: int
    module_level: bool  # 関数の外（読み込むだけで通る）
    names: tuple[str, ...] = ()     # 関数の外の代入なら、その名前（その名前を使う関数を通る試験を選ぶ）


# ------------------------------------------------------------------ 壊し方を作る

def line_offsets(text: str) -> list[int]:
    offsets, total = [0], 0
    for line in text.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)
    return offsets


class Generator(ast.NodeVisitor):
    """壊し方を、ソースの順に作る。番号は「関数ごと、壊し方の種類ごと」の通し番号（ほかの関数の変更で動かない）。"""

    def __init__(self, path: str, text: str):
        self.path, self.text = path, text
        self.offsets = line_offsets(text)
        self.scope: list[str] = []
        self.counts: dict[tuple[str, str], int] = {}
        self.mutants: list[Mutant] = []
        self.quiet = 0          # 表示だけの場所（print の中、f 文字列）の深さ
        self.assigning: tuple[str, ...] = ()    # 関数の外で代入している名前

    def pos(self, line: int, col: int) -> int:
        # col_offset は UTF-8 のバイト数で数える
        start = self.offsets[line - 1]
        row = self.text[start:self.offsets[line]]
        return start + len(row.encode("utf-8")[:col].decode("utf-8"))

    def span(self, node: ast.AST) -> tuple[int, int]:
        return self.pos(node.lineno, node.col_offset), self.pos(node.end_lineno, node.end_col_offset)

    def add(self, node: ast.AST, kind: str, replacement: str, span: tuple[int, int] | None = None) -> None:
        if self.quiet:
            return
        start, end = span or self.span(node)
        name = ".".join(self.scope) or "<module>"
        number = self.counts.get((name, kind), 0) + 1
        self.counts[(name, kind)] = number
        self.mutants.append(Mutant(
            id=f"{Path(self.path).stem}:{name}:{kind}:{number}", file=self.path, line=node.lineno,
            end_line=node.end_lineno, kind=kind, original=self.text[start:end], replacement=replacement,
            start=start, end=end, module_level=not self.scope, names=self.assigning,
        ))

    def segment(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.text[start:end]

    # 範囲
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]             # 説明の文字列は壊さない
        for item in node.decorator_list:
            self.visit(item)
        self.visit(node.args)
        for statement in body:
            self.visit(statement)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_If(self, node: ast.If) -> None:
        main = (
            isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        )
        if main:
            return                      # if __name__ == "__main__": は壊さない
        self.condition(node.test)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.condition(node.test)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.condition(node.test)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self.quiet += 1
        self.generic_visit(node)
        self.quiet -= 1

    def visit_Call(self, node: ast.Call) -> None:
        printing = isinstance(node.func, ast.Name) and node.func.id == "print"
        self.quiet += printing
        self.generic_visit(node)
        self.quiet -= printing

    def condition(self, test: ast.expr) -> None:
        """条件が、比べ方、and / or、not でなければ、反転する（それらは、ほかの壊し方が受け持つ）。"""
        if isinstance(test, ast.Compare | ast.BoolOp) or (
            isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        ):
            return
        if isinstance(test, ast.Constant):
            return
        self.add(test, "cond", f"not ({self.segment(test)})")

    # 壊し方
    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1:
            op = type(node.ops[0])
            left, right = self.segment(node.left), self.segment(node.comparators[0])
            self.add(node, "cmp", f"({left}) {COMPARE_TEXT[NEGATED_COMPARE[op]]} ({right})")
            right_node = node.comparators[0]
            if isinstance(right_node, ast.Constant) and type(right_node.value) is int:
                self.add(right_node, "num", str(right_node.value + 1))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        other = " or " if isinstance(node.op, ast.And) else " and "
        parts = [self.segment(value) for value in node.values]
        self.add(node, "bool", "(" + other.join(f"({part})" for part in parts) + ")")
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            self.add(node, "not", f"({self.segment(node.operand)})")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool):
            self.add(node, "const", str(not node.value))

    def statement(self, node: ast.stmt) -> None:
        self.add(node, "del", "pass")

    def visit_Expr(self, node: ast.Expr) -> None:
        call = node.value
        printing = (
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "print"
        )
        if isinstance(call, ast.Call) and not printing:
            self.statement(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self.statement(node)
        self.generic_visit(node)

    def visit_Continue(self, node: ast.Continue) -> None:
        self.statement(node)

    def visit_Break(self, node: ast.Break) -> None:
        self.statement(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if self.scope:
            self.assign_items(node, names)
            return
        self.assigning = tuple(names)
        self.assign_items(node, names)
        self.assigning = ()

    def assign_items(self, node: ast.Assign, names: list[str]) -> None:
        """関数の外の、大文字の名前の、文字列の並び（決まりの一覧）から、1つずつ外す。"""
        value = node.value
        if isinstance(value, ast.Call) and value.args and isinstance(value.args[0], ast.Set | ast.Tuple):
            value = value.args[0]                               # frozenset({...})
        listing = isinstance(value, ast.Tuple | ast.List | ast.Set)
        if not self.scope and names and names[0].isupper() and listing:
            items = value.elts
            strings = [item for item in items if isinstance(item, ast.Constant) and type(item.value) is str]
            if len(items) >= 2 and len(strings) == len(items):
                for index, item in enumerate(items):
                    start, _ = self.span(item)
                    # 次の要素の手前（区切りの , と空白を含む）まで、または最後なら前の , から
                    if index + 1 < len(items):
                        end = self.span(items[index + 1])[0]
                    else:
                        start, end = self.span(items[index - 1])[1], self.span(item)[1]
                    self.add(item, "elem", "", span=(start, end))
                return
        self.generic_visit(node)


def name_users(root: Path, path: str) -> dict[str, list[tuple[int, int]]]:
    """関数の外の名前ごとに、それを使う関数の行の範囲（その関数を通る試験が、壊した名前の影響を受けうる）。"""
    tree = ast.parse((root / path).read_text(encoding="utf-8"))
    users: dict[str, list[tuple[int, int]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name):
                    users.setdefault(inner.id, []).append((node.lineno, node.end_lineno))
                elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                    users.setdefault(inner.attr, []).append((node.lineno, node.end_lineno))   # cycle.NAME
    return users


def generated(root: Path, files: tuple[str, ...]) -> list[Mutant]:
    found: list[Mutant] = []
    for path in files:
        text = (root / path).read_text(encoding="utf-8")
        generator = Generator(path, text)
        generator.visit(ast.parse(text))
        found.extend(generator.mutants)
    return found


def listed(root: Path) -> tuple[list[Mutant], dict[str, str]]:
    """一覧のファイルの、手で書いた壊し方と、同値として除く壊し方（番号 → 理由）。"""
    data = tomllib.loads((root / LIST_FILE).read_text(encoding="utf-8"))
    mutants = []
    for item in data.get("mutant", []):
        text = (root / item["file"]).read_text(encoding="utf-8")
        start = text.find(item["original"])
        if start < 0 or text.count(item["original"]) != 1:
            raise SystemExit(f"{LIST_FILE}: {item['id']} の元の文字列が、{item['file']} にちょうど1つ無い")
        line = text.count("\n", 0, start) + 1
        end_line = line + item["original"].count("\n")
        mutants.append(Mutant(
            id=item["id"], file=item["file"], line=line, end_line=end_line, kind="hand",
            original=item["original"], replacement=item["replacement"], start=start,
            end=start + len(item["original"]), module_level=bool(item.get("module_level", False)),
        ))
    equivalent = {item["id"]: item["reason"] for item in data.get("equivalent", [])}
    return mutants, equivalent


def all_mutants(root: Path, files: tuple[str, ...]) -> tuple[list[Mutant], dict[str, str]]:
    hand, equivalent = listed(root)
    mutants = generated(root, files) + [m for m in hand if m.file in files]
    ids = [m.id for m in mutants]
    if len(ids) != len(set(ids)):
        raise SystemExit("壊し方の番号が重なっている")
    return mutants, equivalent


def mutated_text(root: Path, mutant: Mutant) -> str:
    text = (root / mutant.file).read_text(encoding="utf-8")
    return text[:mutant.start] + mutant.replacement + text[mutant.end:]


# ------------------------------------------------------------------ 通った行の記録

# 試験が起動する Python の子プロセスでも、通った行を記録する（PYTHONPATH に置く）
SITECUSTOMIZE = f'''\
import atexit, json, os, sys, time
_out = os.environ.get("{COVER_VAR}")
LINES = set()
if _out and hasattr(sys, "monitoring"):
    _targets = set(json.loads(os.environ["{COVER_VAR}_TARGETS"]))
    _tool = 4
    _start = time.time()

    def _line(code, line):
        if code.co_filename in _targets or os.path.realpath(code.co_filename) in _targets:
            LINES.add((os.path.realpath(code.co_filename), line))
        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(_tool, "mutants")
    sys.monitoring.register_callback(_tool, sys.monitoring.events.LINE, _line)
    sys.monitoring.set_events(_tool, sys.monitoring.events.LINE)

    def restart():
        LINES.clear()
        sys.monitoring.restart_events()

    def _save():
        path = os.path.join(_out, "%d-%d.json" % (os.getpid(), time.time_ns()))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({{"start": _start, "lines": sorted(LINES)}}, handle)

    atexit.register(_save)
'''

# pytest に読ませる差し込み。壊す前の試験では、試験ごとに通った行と時間を書く。
# 壊した後の試験では、流す試験を選び、並べ替える。
PLUGIN = f'''\
import json, os, time
import pytest

SELECT = os.environ.get("{SELECT_VAR}")
COVER = os.environ.get("{COVER_VAR}")
KILLER = os.environ.get("{KILLER_VAR}")
RECORD = []


def pytest_runtest_logreport(report):
    if KILLER and report.failed and not os.path.exists(KILLER):
        with open(KILLER, "w", encoding="utf-8") as handle:
            handle.write(report.nodeid)


def pytest_collection_modifyitems(session, config, items):
    if not SELECT:
        return
    with open(SELECT, encoding="utf-8") as handle:
        order = json.load(handle)
    rank = {{name: index for index, name in enumerate(order)}}
    chosen = [item for item in items if item.nodeid in rank]
    chosen.sort(key=lambda item: rank[item.nodeid])
    config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in rank])
    items[:] = chosen


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not COVER:
        yield
        return
    import sitecustomize
    sitecustomize.restart()
    start = time.time()
    yield
    RECORD.append({{"test": item.nodeid, "start": start, "end": time.time(),
                    "lines": sorted(sitecustomize.LINES)}})


def pytest_sessionfinish(session, exitstatus):
    if COVER:
        with open(os.path.join(COVER, "main.json"), "w", encoding="utf-8") as handle:
            json.dump(RECORD, handle)
'''


def helper_dir(base: Path) -> Path:
    folder = base / "helpers"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    (folder / "mutants_plugin.py").write_text(PLUGIN, encoding="utf-8")
    return folder


# ------------------------------------------------------------------ 写しと実行（OSの違いは、ここにまとめる）

def copy_repo(root: Path, target: Path) -> None:
    """追跡しているファイルと、無視されていない新しいファイルを写す（作業中のフォルダには触らない）。"""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    )
    for name in proc.stdout.decode("utf-8").split("\0"):
        source = root / name
        if not name or not source.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def processes_with_mark(token: str) -> list[int]:
    """印（環境変数 MARK_VAR=token）を持つプロセス。Linux の /proc を読む。読めないOSでは空。"""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return []
    needle = f"{MARK_VAR}={token}".encode()
    found = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
    return found


def can_find_leftovers() -> bool:
    return Path("/proc").is_dir()


def stop_leftovers(token: str) -> int:
    """この回が起動して、まだ残っているプロセスを止める。止めた数を返す。"""
    stopped = 0
    for _ in range(5):              # 止める間に、新しく起動されたものも拾う
        found = processes_with_mark(token)
        if not found:
            break
        for pid in found:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
                stopped += 1
        time.sleep(0.2)
    return stopped


def run_tests(
    copy: Path, helpers: Path, tests: list[str], select: list[str] | None, cover: Path | None, timeout: float,
    log: Path | None = None, killer: Path | None = None,
) -> tuple[str, float, int]:
    """写しの中で試験を流す。返すのは（結果: passed / failed / timeout、秒、後で止めた残りのプロセスの数）。

    log を渡すと、試験の出力をそこに書く（渡さなければ捨てる）。
    killer を渡すと、最初に落ちた試験の名前を書く。
    """
    token = uuid.uuid4().hex
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", COVER_VAR, SELECT_VAR, NESTED_VAR))
    }
    env.update({
        MARK_VAR: token, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(helpers), *filter(None, [env.get("PYTHONPATH")])]),
    })
    if killer is not None:
        env[KILLER_VAR] = str(killer)
    if cover is not None:
        env[COVER_VAR] = str(cover)
        env[f"{COVER_VAR}_TARGETS"] = json.dumps([str((copy / name).resolve()) for name in TARGETS])
    args = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "mutants_plugin"]
    if select is not None:
        listing = copy.parent / f"select-{token}.json"
        listing.write_text(json.dumps(select), encoding="utf-8")
        env[SELECT_VAR] = str(listing)
    started = time.monotonic()
    options = {} if os.name == "nt" else {"start_new_session": True}
    output = open(log, "wb") if log is not None else subprocess.DEVNULL   # noqa: SIM115  終わったら閉じる
    try:
        proc = subprocess.Popen(
            [*args, *tests], cwd=copy, env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, **options,
        )
    finally:
        if log is not None:
            output.close()
    try:
        code = proc.wait(timeout=timeout)
        # 0 = 通った。KILLED_CODES = 落ちた。それ以外（試験が無い、入れ子など）は、結果を得られなかった
        outcome = "passed" if code == 0 else KILLED_CODES.get(code, f"error-{code}")
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.kill()
        proc.wait()
    seconds = time.monotonic() - started
    return outcome, seconds, stop_leftovers(token)


# ------------------------------------------------------------------ 壊す前の試験

def content_key(root: Path) -> str:
    """壊す前の試験の記録を使い回してよいかの鍵（スクリプト、試験、記録の仕方が同じ）。"""
    digest = hashlib.sha256(f"{COVER_VERSION}\0{SITECUSTOMIZE}".encode())
    for name in (*TARGETS, *TOOL_TESTS, "tests/conftest.py"):
        path = root / name
        digest.update(name.encode() + b"\0" + (path.read_bytes() if path.is_file() else b"") + b"\0")
    return digest.hexdigest()[:16]


def baseline(root: Path, work: Path, helpers: Path, tests: list[str]) -> dict:
    """壊す前の試験を流し、試験ごとに通った行（ファイル → 行の一覧）と時間を記録する。"""
    key = content_key(root)
    cache = root / STATE_DIR / f"baseline-{key}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    copy = work / "baseline"
    copy_repo(root, copy)
    cover = work / "cover"
    cover.mkdir()
    print("壊す前の試験を流し、試験ごとに通った行を記録します（数分かかります）", flush=True)
    log = root / STATE_DIR / "baseline.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    outcome, seconds, _ = run_tests(copy, helpers, tests, None, cover, RUN_TIMEOUT * 2, log)
    if outcome != "passed":
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        print("\n".join(tail))
        raise SystemExit(f"壊す前の試験が通らない（{outcome}。出力は {STATE_DIR}/baseline.log）")
    records = json.loads((cover / "main.json").read_text(encoding="utf-8"))
    children = [json.loads(path.read_text(encoding="utf-8")) for path in cover.glob("*-*.json")]
    prefix = str(copy.resolve()) + os.sep
    result: dict[str, dict] = {}
    for record in records:
        lines = {tuple(item) for item in record["lines"]}
        for child in children:          # 試験の間に起動された子プロセスの行
            if record["start"] <= child["start"] <= record["end"]:
                lines |= {tuple(item) for item in child["lines"]}
        by_file: dict[str, list[int]] = {}
        for path, line in lines:
            if path.startswith(prefix):
                by_file.setdefault(path[len(prefix):].replace(os.sep, "/"), []).append(line)
        result[record["test"]] = {"seconds": record["end"] - record["start"], "lines": by_file}
    data = {"key": key, "seconds": seconds, "tests": result}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def covering(base: dict, mutant: Mutant, users: dict[str, list[tuple[int, int]]]) -> list[str]:
    """壊した行を通る試験（短い順）。

    関数の外の代入を壊したなら、その名前を使う関数を通る試験。名前が分からない（関数の外の、代入でない文）か、
    どの関数も使わないなら、すべての試験。
    """
    tests = base["tests"]
    if mutant.module_level:
        ranges = [item for name in mutant.names for item in users.get(name, [])]
        span = {line for first, last in ranges for line in range(first, last + 1)}
        if not span:
            return sorted(tests, key=lambda name: tests[name]["seconds"])
    else:
        span = set(range(mutant.line, mutant.end_line + 1))
    chosen = [name for name, info in tests.items() if span & set(info["lines"].get(mutant.file, []))]
    return sorted(chosen, key=lambda name: tests[name]["seconds"])


# ------------------------------------------------------------------ 流す

def run_one(
    root: Path, copy: Path, helpers: Path, mutant: Mutant, base: dict | None, tests: list[str],
    users: dict, confirm: bool, hint: str | None,
) -> dict:
    target = copy / mutant.file
    original = target.read_bytes()
    text = mutated_text(root, mutant)
    try:
        ast.parse(text)
    except SyntaxError:
        return {"id": mutant.id, "status": "invalid", "seconds": 0.0, "leftovers": 0}
    target.write_text(text, encoding="utf-8", newline="")
    try:
        total, leftovers, stage = 0.0, 0, "selected"
        select = covering(base, mutant, users.get(mutant.file, {})) if base is not None else None
        if select and hint in select:           # 前回これを落とした試験を、先に流す
            select = [hint, *(name for name in select if name != hint)]
        killer = copy.parent / f"killer-{uuid.uuid4().hex}.txt"
        if select == []:
            outcome = "passed"
        else:
            # 選んだ試験のあるファイルだけを集める（集める時間を省く）
            files = sorted({name.split("::")[0] for name in select}) if select is not None else tests
            outcome, seconds, left = run_tests(copy, helpers, files, select, None, RUN_TIMEOUT, None, killer)
            total, leftovers = total + seconds, leftovers + left
        if outcome == "passed" and select is not None and confirm:      # 生き残った。全体で確かめる
            stage = "all"
            outcome, seconds, left = run_tests(copy, helpers, tests, None, None, RUN_TIMEOUT, None, killer)
            total, leftovers = total + seconds, leftovers + left
    finally:
        target.write_bytes(original)
    found = killer.read_text(encoding="utf-8") if killer.is_file() else None
    status = "survived" if outcome == "passed" else "error" if outcome.startswith("error") else "killed"
    return {
        "id": mutant.id, "status": status, "how": outcome, "stage": stage, "seconds": round(total, 1),
        "leftovers": leftovers, "killer": found,
    }


def describe(mutant: Mutant) -> str:
    original = " ".join(mutant.original.split())[:70]
    replacement = " ".join(mutant.replacement.split())[:70] or "（外す）"
    return f"{mutant.id}  {mutant.file}:{mutant.line}  {original}  →  {replacement}"


def cmd_list(args: argparse.Namespace) -> int:
    mutants, equivalent = all_mutants(ROOT, tuple(args.file or TARGETS))
    for mutant in mutants:
        note = "  [同値]" if mutant.id in equivalent else ""
        print(describe(mutant) + note)
    print(f"{len(mutants)} 通り（同値として除くもの {sum(m.id in equivalent for m in mutants)}）")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else ROOT
    files = tuple(args.file or TARGETS)
    mutants, equivalent = all_mutants(root, files)
    stale = sorted(set(equivalent) - {m.id for m in all_mutants(root, TARGETS)[0]})
    if args.only:
        wanted = set(args.only)
        mutants = [m for m in mutants if m.id in wanted]
        missing = wanted - {m.id for m in mutants}
        if missing:
            print(f"知らない番号: {', '.join(sorted(missing))}")
            return 2
    elif not args.include_equivalent:
        mutants = [m for m in mutants if m.id not in equivalent]
    if args.sample:
        step = max(1, len(mutants) // args.sample)
        mutants = mutants[::step][:args.sample]
    tests = list(args.tests or [t for t in TOOL_TESTS if (root / t).is_file()])
    if not can_find_leftovers():
        print("注意: このOSでは、試験が残したプロセスを探せない（/proc が要る）。残っていたら手で止める")
    with tempfile.TemporaryDirectory(prefix="circuit-sim-mutants-") as tmp:
        work = Path(tmp)
        helpers = helper_dir(work)
        base = None if args.no_select else baseline(root, work, helpers, tests)
        copies = []
        for index in range(max(1, args.jobs)):
            copy = work / f"copy-{index}"
            copy_repo(root, copy)
            copies.append(copy)
        free = list(copies)
        results: list[dict] = []
        started = time.monotonic()

        users = {name: name_users(root, name) for name in files}
        killers_path = root / STATE_DIR / KILLERS_FILE
        killers = json.loads(killers_path.read_text(encoding="utf-8")) if killers_path.is_file() else {}

        def work_on(mutant: Mutant) -> dict:
            copy = free.pop()
            try:
                hint = killers.get(mutant.id)
                return run_one(root, copy, helpers, mutant, base, tests, users, args.confirm, hint)
            finally:
                free.append(copy)

        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            for done, result in enumerate(pool.map(work_on, mutants), 1):
                results.append(result)
                if result["status"] == "survived" or args.verbose:
                    mutant = next(m for m in mutants if m.id == result["id"])
                    print(f"[{done}/{len(mutants)}] {result['status']}: {describe(mutant)}", flush=True)
                elif done % 25 == 0:
                    print(f"[{done}/{len(mutants)}] …", flush=True)
    killers.update({r["id"]: r["killer"] for r in results if r.get("killer")})
    killers_path.parent.mkdir(parents=True, exist_ok=True)
    killers_path.write_text(json.dumps(killers, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    survived = [r for r in results if r["status"] == "survived"]
    errors = [r["id"] for r in results if r["status"] == "error"]
    unexpected = [r for r in survived if r["id"] not in equivalent]
    killed_listed = [r["id"] for r in results if r["status"] == "killed" and r["id"] in equivalent]
    summary = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": list(files),
        "count": len(results), "killed": sum(r["status"] == "killed" for r in results),
        "survived": [r["id"] for r in survived], "unexpected": [r["id"] for r in unexpected],
        "invalid": [r["id"] for r in results if r["status"] == "invalid"],
        "equivalent_but_killed": killed_listed, "errors": errors,
        "leftovers_stopped": sum(r["leftovers"] for r in results),
        "seconds": round(time.monotonic() - started, 1), "stale_equivalent": stale, "results": results,
    }
    out = root / STATE_DIR / f"result-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(
        f"{summary['count']} 通り: 落ちた {summary['killed']}、生き残り {len(survived)}"
        f"（一覧に無いもの {len(unexpected)}）、結果を得られなかった {len(errors)}、"
        f"作れなかった {len(summary['invalid'])}、"
        f"後で止めた残りのプロセス {summary['leftovers_stopped']}、{summary['seconds']} 秒"
    )
    if killed_listed:
        print(f"同値として一覧にあるが、落ちたもの（一覧から外す）: {', '.join(killed_listed)}")
    if stale:
        print(f"一覧の同値のうち、もう無い番号（一覧から外す）: {', '.join(stale)}")
    if errors:
        print(f"結果を得られなかったもの（生き残りと同じに扱う）: {', '.join(errors)}")
    print(f"記録: {out.relative_to(root).as_posix()}")
    return 1 if unexpected or errors else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="mutants.py", description="1か所ずつ壊す確認")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="壊し方の一覧")
    p.add_argument("--file", action="append", choices=TARGETS)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("run", help="壊し方を流す")
    p.add_argument("--file", action="append", choices=TARGETS)
    p.add_argument("--only", nargs="+", help="この番号の壊し方だけを流す")
    p.add_argument("--sample", type=int, help="一覧から等間隔にこの数だけ選んで流す")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--tests", nargs="+", help="流す試験（既定は道具の試験）")
    p.add_argument("--no-select", action="store_true", help="通った行で試験を選ばない（毎回、全体を流す）")
    p.add_argument("--include-equivalent", action="store_true", help="同値として一覧にあるものも流す")
    p.add_argument(
        "--confirm", action="store_true",
        help="選んだ試験で生き残ったものを、道具の試験の全体でもう一度流す（落ちれば、生き残りから外れる）",
    )
    p.add_argument("--root", help=argparse.SUPPRESS)       # 試験用（別のリポジトリを対象にする）
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
