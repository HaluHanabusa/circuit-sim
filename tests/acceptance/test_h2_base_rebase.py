"""H2 基準1（ブランチの土台の付け替え）の受け入れ試験（検証担当。ツリー 53e9779f）。

実測値と測り方は `docs/verification/H2-verifier.md` にある。

確かめること: ゲートを通した内容を、main に入った他の成果を消す形で取り込めないこと。
遅れているだけのブランチは、今までどおり取り込めること。

期待値の作り方:
- 一時リポジトリ、仕様、確認スクリプト（何もせず通る）は、このファイルで作る。
  scripts/cycle.py の関数は使わない。
- 「他の成果が消えた」は、取り込んだ後の main のツリーに、他の作業単位が足したファイルが残っているかで見る
  （git ls-tree）。拒んだかどうかは、merge の終了コードで見る。
- スクリプトはコマンドラインから起動する（試験のプロセスの中で main() を呼ばない）。
- PATH は CIRCUIT_SIM_SAFE_PATH（claude の無い PATH。CLAUDE.md の決まり11）。

製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CYCLE = ROOT / "scripts" / "cycle.py"

EMPTY_GIT_CONFIG = Path(tempfile.gettempdir()) / "circuit-sim-h2-acceptance-empty.gitconfig"
EMPTY_GIT_CONFIG.write_text("", encoding="utf-8")

SPEC = "---\nid: U1\nowner: tooling-engineer\nneeds_ux: false\n{extra}---\n# U1\n"
CHECK = "import sys\nsys.exit(0)\n"
OTHER = "other.txt"     # 他の作業単位が main に足すファイル


def env() -> dict[str, str]:
    base = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {
        **base, "PATH": os.environ.get("CIRCUIT_SIM_SAFE_PATH", os.defpath),
        "GIT_AUTHOR_NAME": "h2", "GIT_AUTHOR_EMAIL": "h2@example.invalid",
        "GIT_COMMITTER_NAME": "h2", "GIT_COMMITTER_EMAIL": "h2@example.invalid",
        "GIT_CONFIG_GLOBAL": str(EMPTY_GIT_CONFIG), "GIT_CONFIG_NOSYSTEM": "1", "PYTHONIOENCODING": "utf-8",
    }


def sh(cwd: Path, *args: str, extra: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env={**env(), **(extra or {})},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def cycle(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CYCLE), *args], capture_output=True, text=True, encoding="utf-8",
        env={**env(), "CYCLE_ROOT": str(root)}, timeout=300,
    )


def ok(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


class Unit:
    """main と、作業単位 U1 の作業ツリー（main から start したもの）。"""

    def __init__(self, base: Path, spec_extra: str = ""):
        self.root = base / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "docs" / "plan").mkdir(parents=True)
        (self.root / "scripts" / "check.py").write_text(CHECK, encoding="utf-8")
        spec = self.root / "docs" / "plan" / "U1-demo.md"
        spec.write_text(SPEC.format(extra=spec_extra), encoding="utf-8")
        (self.root / ".gitignore").write_text(".cycle/\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "M0")

    def start(self) -> Path:
        ok(cycle(self.root, "start", "U1"))
        self.wt = self.root.parent / "wt" / "U1"
        (self.wt / "product.py").write_text("x = 2\n", encoding="utf-8")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-q", "-m", "U1 の実装")
        ok(cycle(self.root, "record", "U1", "implement", "--verdict", "pass", "--by", "impl"))
        return self.wt

    def land_on_main(self, name: str = OTHER) -> str:
        """他の作業単位の成果が main に入る（main が先に進む）。"""
        (self.root / name).write_text("他の作業単位の成果\n", encoding="utf-8")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", f"main に {name}")
        return sh(self.root, "git", "rev-parse", "HEAD")

    def snapshot(self) -> str:
        return ok(cycle(self.root, "snapshot", "U1"))

    def record_checks(self, tree: str) -> None:
        for gate, by in (("review", "rev"), ("verify", "ver")):
            ok(cycle(self.root, "record", "U1", gate, "--verdict", "pass", "--by", by, "--tree", tree))
        ok(cycle(self.root, "check", "U1"))

    def pass_gates(self) -> None:
        self.record_checks(self.snapshot())

    def merge(self) -> tuple[int, str, list[str]]:
        result = cycle(self.root, "merge", "U1")
        files = sh(self.root, "git", "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        return result.returncode, result.stdout + result.stderr, files


@pytest.fixture
def unit(tmp_path: Path) -> Unit:
    made = Unit(tmp_path)
    made.start()
    return made


def assert_refused_and_main_kept(unit: Unit) -> None:
    code, output, files = unit.merge()
    assert code != 0, f"土台を付け替えた内容が取り込まれた:\n{output}"
    assert OTHER in files


# ------------------------------------------------------------------ 再レビューの再現手順（基準1）

@pytest.mark.parametrize("rebase", [
    ["reset", "--soft", "main"],
    ["merge", "-q", "-s", "ours", "main", "-m", "main を中身なしで取り込む"],
], ids=["reset-soft-main", "merge-s-ours-main"])
def test_c1_rebase_after_the_gates_is_refused(unit, rebase):
    unit.land_on_main()
    unit.pass_gates()
    sh(unit.wt, "git", *rebase)
    if rebase[0] == "reset":
        sh(unit.wt, "git", "commit", "-q", "-m", "付け替え")
    assert sh(unit.wt, "git", "rev-parse", "HEAD^{tree}") == unit.snapshot()     # ツリーは同じ
    assert_refused_and_main_kept(unit)


def test_c1_rerunning_only_check_after_the_rebase_is_still_refused(unit):
    unit.land_on_main()
    unit.pass_gates()
    sh(unit.wt, "git", "reset", "--soft", "main")
    sh(unit.wt, "git", "commit", "-q", "-m", "付け替え")
    ok(cycle(unit.root, "check", "U1"))         # check だけをやり直す（review と verify は古いまま）
    assert_refused_and_main_kept(unit)


def test_c1_branch_that_is_only_behind_main_is_merged(unit):
    unit.pass_gates()
    unit.land_on_main()                         # ゲートの後に main が進んだだけ
    code, output, files = unit.merge()
    assert code == 0, output
    assert OTHER in files and "product.py" in files


def test_c1_branch_behind_by_several_commits_is_merged(unit):
    unit.land_on_main("first.txt")
    unit.pass_gates()
    unit.land_on_main()
    unit.land_on_main("third.txt")
    code, output, files = unit.merge()
    assert code == 0, output
    assert {OTHER, "first.txt", "third.txt", "product.py"} <= set(files)


# ------------------------------------------------------------------ 検証担当が考えた別の手口

def test_c1_rebase_between_snapshot_and_record_is_refused(unit):
    """確認を始める前に snapshot で控え、確認の途中で土台を付け替え、確認の後で record --tree で記録する。

    確認した内容（snapshot のツリー）は、付け替える前の土台の上で見たもの。
    統合すると main の他の成果が消える。
    """
    unit.land_on_main()
    tree = unit.snapshot()
    sh(unit.wt, "git", "reset", "--soft", "main")
    sh(unit.wt, "git", "commit", "-q", "-m", "確認の途中で付け替え")
    unit.record_checks(tree)
    assert_refused_and_main_kept(unit)


def test_c1_second_merge_base_added_by_merging_an_older_unit_without_its_content_is_refused(tmp_path):
    """main に入った別の作業単位 A の先端（main の先端ではない）を -s ours で取り込み、分かれ目を2つにする。

    A のコミットが、U1 の分かれ目（M1）より古い日時だと、git merge-base は M1 を返し、控えと一致する。
    実際の取り込みは2つの分かれ目から作った仮の土台で行うので、A の成果が main から消える。
    """
    unit = Unit(tmp_path)
    sh(unit.root, "git", "branch", "A")             # A は M0 から
    unit.land_on_main()                             # M1
    unit.start()                                    # U1 は M1 から
    a_tree = unit.root.parent / "wt" / "A"
    sh(unit.root, "git", "worktree", "add", "-q", str(a_tree), "A")
    (a_tree / "a_unit.txt").write_text("A の成果\n", encoding="utf-8")
    sh(a_tree, "git", "add", "-A")
    old = {"GIT_AUTHOR_DATE": "2000-01-01T00:00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00"}
    sh(a_tree, "git", "commit", "-q", "-m", "A1", extra=old)
    a1 = sh(a_tree, "git", "rev-parse", "HEAD")
    unit.pass_gates()
    sh(unit.root, "git", "merge", "-q", "--no-ff", "A", "-m", "A を統合")
    sh(unit.wt, "git", "merge", "-q", "-s", "ours", a1, "-m", "A1 を中身なしで取り込む")
    assert len(sh(unit.wt, "git", "merge-base", "--all", "main", "HEAD").split()) == 2
    code, output, files = unit.merge()
    assert code != 0, f"分かれ目を2つにした内容が取り込まれた:\n{output}"
    assert "a_unit.txt" in files


def test_c1_windows_result_from_before_the_rebase_is_stale(tmp_path):
    """Windows での結果（record-ci）は、結果のコミットの分かれ目で控える。結果の後に付け替えたら古い。"""
    unit = Unit(tmp_path, "needs_windows: true\n")
    unit.start()
    unit.land_on_main()
    commit = sh(unit.wt, "git", "rev-parse", "HEAD")
    tree = sh(unit.wt, "git", "rev-parse", "HEAD^{tree}")
    sh(unit.wt, "git", "reset", "--soft", "main")
    sh(unit.wt, "git", "commit", "-q", "-m", "付け替え")
    unit.pass_gates()       # 付け替えの後に、ほかのゲートは記録し直す
    result = tmp_path / "ci.json"
    result.write_text(json.dumps({
        "schema": 1, "kind": "local-ci", "at": "2026-11-15T00:00:00Z", "mode": "checkout", "ref": "origin/U1",
        "commit": commit, "tree": tree, "branch": None, "dirty": False,
        "os": {"system": "Windows", "release": "11", "machine": "AMD64"}, "python": {"version": "3.13.0"},
        "encoding": {},
        "check": {"script": "scripts/check.py", "returncode": 0, "seconds": 1, "timed_out": False},
        "tests": {"total": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        "failed": [], "changed": [],
        "verdict": "pass", "reason": "ok",
    }), encoding="utf-8")
    ok(cycle(unit.root, "record-ci", "U1", "--result", str(result)))
    code, output, _ = unit.merge()
    assert code != 0, output
    assert "windows" in output
