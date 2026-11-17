"""A0 の独立検証（verifier、9回目）: tests/app_procs.py の kill_posix_tree の直し（7e00e21。8回目の問題1）を、
os.kill の呼び出しの位置だけでなく、文の切れ目のどこで例外が入っても確かめる。

記録は docs/verification/A0-verifier-9.md。

Ctrl+C（KeyboardInterrupt）は、os.kill の中（送った後に、待っている合図を処理する）だけでなく、命令の切れ目
でも入りうる（部品の説明の「止められないもの」）。8回目の試験（test_a0_fix8_procs_browser.py の 3）は、os.kill
の呼び出しの位置ごとに例外を差し込んだ。ここでは、kill_posix_tree の構文木の、どの文の並び（本体、except、
finally、else）にも、各文の前と並びの終わりに「ここで例外を出せる」呼び出しを差し込んだ写しを作り、その全部の
位置で 1 つずつ例外を出す（命令の切れ目を、文の単位で覆う。CPython が本当に合図を処理するのは、その一部）。

本物の合図は送らない（D-078）: 写しは、os を身代わり（kill は控えて状態を変えるだけ）に、一覧を身代わりの木
（存在しえない番号 9000001 から）にした名前空間で動かす。部品のモジュールそのものは変えない。

1. 文の切れ目のどこで例外が入っても:
   - 木の外に送らない。例外を消さない（出し直す）
   - SIGKILL を送るのは、root と、一時停止を送った親の子として一覧に載ったものだけ
   - 一時停止のまま残るのは、finally の中（後始末の SIGKILL の繰り返し）で例外が入ったときだけ（部品の説明の
     既知の限界）。root の一時停止、一覧、子の一時停止のどこで入っても残らない（直す前の 8aea404 は、root の
     一時停止の直後や、子の一時停止の直後で残った）
2. os.kill の中で、送る前に例外が出たとき（os.kill は、送る前に監査の事象 os.kill を出す。Python の監査フック
   の中で Ctrl+C が KeyboardInterrupt になると、送らずに例外が出る。Python 3.13.18 で確かめた）:
   - 一時停止を送る前なら、何も残らない
   - SIGKILL を送る前なら、残るのはその 1 つまで。ほかの全部には SIGKILL を送り、例外は出し直す（直す前の
     8aea404 は、残りの全部が残った）。その 1 つが残るのは軽微（9回目の問題1。A1 へ引き継ぐ）。直って 0 に
     なっても、この試験は通る
"""
from __future__ import annotations

import ast
import errno
import inspect
import os
import signal
import textwrap

import app_procs as procs
import pytest

WINDOWS = os.name == "nt"
only_posix = pytest.mark.skipif(WINDOWS, reason="POSIX の止め方（SIGSTOP と SIGKILL）の話")

# 存在しえない番号の木: root の子が A、B。B の子が C
ROOT, A, B, C = 9_000_001, 9_000_002, 9_000_003, 9_000_004
TREE = {ROOT: [A, B], B: [C]}


class Interrupted(BaseException):
    """後始末の最中の Ctrl+C（KeyboardInterrupt）の身代わり（pytest そのものは止めない）。"""


class Probe(ast.NodeTransformer):
    """どの文の並びにも、各文の前と並びの終わりに __probe(行) を差し込む（並びの終わりは、負の行）。"""

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                wrapped = []
                for stmt in body:
                    wrapped.append(self.call(stmt.lineno))
                    wrapped.append(stmt)
                wrapped.append(self.call(-body[-1].end_lineno))
                setattr(node, field, wrapped)
        return node

    @staticmethod
    def call(line: int) -> ast.stmt:
        return ast.Expr(ast.Call(ast.Name("__probe", ast.Load()), [ast.Constant(line)], []))


def instrumented():
    """kill_posix_tree の写し（文の切れ目ごとに __probe を呼ぶ）のコードと、finally の中の行の集まり。"""
    func_obj = procs.kill_posix_tree
    tree = ast.parse(textwrap.dedent(inspect.getsource(func_obj)))
    ast.increment_lineno(tree, func_obj.__code__.co_firstlineno - 1)
    finals = [node for node in ast.walk(tree.body[0]) if isinstance(node, ast.Try) and node.finalbody]
    assert finals, "kill_posix_tree に finally が無い（止め方の形が変わった。この試験を見直す）"
    final_lines: set[int] = set()
    for node in finals:
        for stmt in node.finalbody:
            final_lines.update(range(stmt.lineno, stmt.end_lineno + 1))
    Probe().visit(tree.body[0])  # 関数の中だけ（モジュールの並びには差し込まない）
    ast.fix_missing_locations(tree)
    return compile(tree, procs.__file__, "exec"), final_lines


class Run:
    """写しを 1 回流す。合図は控えて状態を変えるだけ（本当には送らない）。

    probe_at: n 番目の差し込み口で Interrupted を出す。before_send: ("stop" | "kill", n) は、n 回目の
    その合図を送る前に、os.kill の中から Interrupted を出す。gone: 一覧に載るが、合図は ESRCH の番号。"""

    def __init__(self, code, *, probe_at: int | None = None, before_send: tuple[str, int] | None = None,
                 gone: frozenset[int] = frozenset()) -> None:
        self.state = {pid: "S" for pid in (ROOT, A, B, C)}
        self.probe_at, self.before_send, self.gone = probe_at, before_send, gone
        self.probes = 0
        self.fired: int | None = None
        self.counts = {"stop": 0, "kill": 0}
        self.stop_tried: set[int] = set()
        self.listed = {ROOT}
        self.killed: list[int] = []
        self.outside: list[tuple] = []
        namespace = dict(vars(procs))
        namespace.update(os=self, children_table=self.table, __probe=self.probe)
        exec(code, namespace)
        try:
            namespace["kill_posix_tree"](ROOT)
            self.raised = None
        except Interrupted as exc:
            self.raised = str(exc)

    def probe(self, line: int) -> None:
        self.probes += 1
        if self.probes == self.probe_at:
            self.fired = abs(line)
            raise Interrupted(f"{line} 行の切れ目")

    def table(self) -> dict[int, list[int]]:
        result = {p: [c for c in cs if self.state[c] != "dead"]
                  for p, cs in TREE.items() if self.state[p] != "dead"}
        for parent, children in result.items():
            if parent in self.stop_tried:
                self.listed.update(children)
        return result

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.state:
            self.outside.append((pid, int(sig)))
            raise ProcessLookupError(errno.ESRCH, "木の外（身代わりは送らない）")
        kind = {signal.SIGSTOP: "stop", signal.SIGKILL: "kill"}.get(sig, "other")
        if kind == "stop":
            self.stop_tried.add(pid)
        if kind == "kill":
            self.killed.append(pid)
        if kind in self.counts:
            self.counts[kind] += 1
            if self.before_send == (kind, self.counts[kind]):
                raise Interrupted(f"{self.counts[kind]} 回目の {kind} を送る前")
        if pid in self.gone or self.state[pid] == "dead":
            raise ProcessLookupError(errno.ESRCH, "No such process")
        if kind == "stop":
            self.state[pid] = "T"
        elif kind == "kill":
            self.state[pid] = "dead"

    def killpg(self, pgid: int, sig: int) -> None:
        self.outside.append(("group", pgid, int(sig)))
        raise ProcessLookupError(errno.ESRCH, "まとまりには送らない（身代わり）")

    @property
    def left_stopped(self) -> list[int]:
        return [pid for pid, state in self.state.items() if state == "T"]


@only_posix
@pytest.mark.parametrize("gone", [frozenset(), frozenset({C})], ids=["all-alive", "grandchild-gone"])
def test_an_interrupt_at_any_statement_boundary_leaves_nothing_stopped_before_the_final_loop(gone):
    """文の切れ目の全部で 1 つずつ例外を出す。木の外に送らない、例外を消さない、一覧に載ったものだけに
    SIGKILL、一時停止のまま残るのは finally の中で入ったときだけ。"""
    code, final_lines = instrumented()
    plain = Run(code, gone=gone)
    assert plain.raised is None and not plain.left_stopped and not plain.outside, vars(plain)
    total = plain.probes
    assert total >= 30, f"差し込み口が少ない（{total}）。写しの作り方が効いていない"
    problems, in_final = [], 0
    for n in range(1, total + 1):
        run = Run(code, probe_at=n, gone=gone)
        assert run.fired is not None, f"{n} 番目の差し込み口に届かなかった（流れ方が、例外の無い実行と違う）"
        where = f"{run.fired} 行の切れ目（{n} 番目）"
        if run.outside:
            problems.append((where, "木の外に送った", run.outside))
        if run.raised is None:
            problems.append((where, "例外が消えた"))
        if set(run.killed) - run.listed:
            problems.append((where, "一覧に載っていない番号に SIGKILL", sorted(set(run.killed) - run.listed)))
        if run.fired in final_lines:
            in_final += 1
        elif run.left_stopped:
            problems.append((where, "一時停止のまま残った", run.left_stopped))
    assert in_final > 0, "finally の中の差し込み口が無い（写しの作り方を見直す）"
    assert not problems, problems


@only_posix
@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_an_interrupt_before_a_sigstop_is_sent_leaves_nothing_stopped(n):
    """os.kill の中で、n 回目の一時停止を送る前に例外が出ても、何も残らない。例外は出し直す。"""
    code, _ = instrumented()
    run = Run(code, before_send=("stop", n))
    assert run.raised == f"{n} 回目の stop を送る前", vars(run)
    assert not run.left_stopped and not run.outside, vars(run)


@only_posix
@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_an_interrupt_before_a_sigkill_is_sent_leaves_at_most_that_one_stopped(n):
    """os.kill の中で、n 回目の SIGKILL を送る前に例外が出ても、残るのはその 1 つまで。ほかの全部には
    SIGKILL を送り、例外は出し直す（その 1 つが残るのは軽微。9回目の問題1）。"""
    code, _ = instrumented()
    plain = Run(code)
    order = plain.killed  # 例外の無いときの SIGKILL の順（子から先）
    assert len(order) == 4, vars(plain)
    run = Run(code, before_send=("kill", n))
    assert run.raised == f"{n} 回目の kill を送る前", vars(run)
    assert set(run.left_stopped) <= {order[n - 1]}, (
        f"送る前の例外で、{order[n - 1]} のほかにも一時停止のまま残った: {run.left_stopped}", vars(run))
    assert set(run.killed) >= set(order) - {order[n - 1]}, ("ほかのものに SIGKILL を送っていない", vars(run))
    assert not run.outside, vars(run)
