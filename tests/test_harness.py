"""開発サイクルのスクリプト（scripts/cycle.py）が、ゲートの決まりを守らせることを確かめる。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

CYCLE = Path(__file__).resolve().parents[1] / "scripts" / "cycle.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

SPEC = """---
id: U1
owner: solver-engineer
---

# U1: 試験用の作業単位
"""


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "plan").mkdir(parents=True)
    # FAIL というファイルがあると失敗する、試験用の確認スクリプト
    (root / "scripts" / "check.sh").write_text("#!/bin/sh\ntest ! -f FAIL\n", encoding="utf-8")
    (root / "scripts" / "check.sh").chmod(0o755)
    (root / "docs" / "plan" / "U1-demo.md").write_text(SPEC, encoding="utf-8")
    (root / ".gitignore").write_text(".cycle/\n", encoding="utf-8")
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


def cycle(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV, "CYCLE_ROOT": str(root)}
    return subprocess.run([sys.executable, str(CYCLE), *args], capture_output=True, text=True, env=env)


def implement(root: Path, content: str = "x = 2\n") -> Path:
    """start して、実装（製品コードの変更）を済ませた状態にする。"""
    assert cycle(root, "start", "U1").returncode == 0
    wt = root.parent / "wt" / "U1"
    (wt / "product.py").write_text(content, encoding="utf-8")
    assert cycle(root, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-1").returncode == 0
    return wt


def pass_all(root: Path, verifier: str = "ver-1") -> None:
    assert cycle(root, "record", "U1", "review", "--verdict", "pass", "--by", "rev-1").returncode == 0
    assert cycle(root, "record", "U1", "verify", "--verdict", "pass", "--by", verifier).returncode == 0
    assert cycle(root, "check", "U1").returncode == 0


def commit(wt: Path) -> None:
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "-m", "work")


def test_merge_succeeds_when_all_gates_pass(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 2\n"
    assert "review: pass" in sh(repo, "git", "log", "-1", "--format=%B")


def test_merge_refused_when_a_gate_is_missing(repo):
    wt = implement(repo)
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: まだ記録が無い" in result.stdout
    assert "verify: まだ記録が無い" in result.stdout
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 1\n"


def test_merge_refused_when_product_changes_after_review(repo):
    wt = implement(repo)
    pass_all(repo)
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")     # 検証の後でこっそり変更
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: 確認の後で変わったファイルがある（product.py）" in result.stdout
    assert "verify: 確認の後で変わったファイルがある（product.py）" in result.stdout


def test_verifier_may_add_acceptance_tests_after_review(repo):
    wt = implement(repo)
    assert cycle(repo, "record", "U1", "review", "--verdict", "pass", "--by", "rev-1").returncode == 0
    snapshot = cycle(repo, "snapshot", "U1").stdout.strip()
    (wt / "tests" / "acceptance").mkdir(parents=True)
    (wt / "tests" / "acceptance" / "test_u1.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (wt / "docs" / "verification").mkdir(parents=True)
    (wt / "docs" / "verification" / "U1.md").write_text("記録\n", encoding="utf-8")
    assert cycle(repo, "record", "U1", "verify", "--verdict", "pass", "--by", "ver-1",
                 "--tree", snapshot).returncode == 0
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "tests" / "acceptance" / "test_u1.py").exists()


def test_merge_refused_when_check_is_stale_or_failed(repo):
    wt = implement(repo)
    pass_all(repo)
    (wt / "tests" / "acceptance").mkdir(parents=True)
    (wt / "tests" / "acceptance" / "test_new.py").write_text("\n", encoding="utf-8")   # check の後で追加
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "check: 確認した後で内容が変わっている" in result.stdout

    (wt / "FAIL").write_text("", encoding="utf-8")
    assert cycle(repo, "check", "U1").returncode == 1
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "check: fail" in result.stdout


def test_implementer_cannot_verify_own_work(repo):
    wt = implement(repo)
    pass_all(repo, verifier="impl-1")
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "実装した本人が検証している" in result.stdout


def test_failed_verdict_blocks_merge(repo):
    wt = implement(repo)
    pass_all(repo)
    assert cycle(repo, "record", "U1", "verify", "--verdict", "fail", "--by", "ver-1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "verify: fail" in result.stdout


def test_merge_requires_committed_worktree(repo):
    implement(repo)
    pass_all(repo)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "未コミットの変更がある" in result.stdout


def test_dry_run_shows_headless_command_for_the_right_agent(repo):
    implement(repo)
    out = cycle(repo, "run", "U1", "implement", "--dry-run").stdout
    assert "claude -p" in out and "--agent solver-engineer" in out
    assert "--output-format json" in out and "--json-schema" in out
    assert "--agent code-reviewer" in cycle(repo, "run", "U1", "review", "--dry-run").stdout
    assert "--agent verifier" in cycle(repo, "run", "U1", "verify", "--dry-run").stdout


def test_headless_run_without_structured_result_is_not_a_pass(repo, tmp_path):
    implement(repo)
    # 偽の claude: 構造化された結果を返さない
    fake = tmp_path / "bin"
    fake.mkdir()
    script = '#!/bin/sh\necho \'{"result": "done", "session_id": "s1"}\'\n'
    (fake / "claude").write_text(script, encoding="utf-8")
    (fake / "claude").chmod(0o755)
    env = {**os.environ, **GIT_ENV, "CYCLE_ROOT": str(repo), "PATH": f"{fake}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(CYCLE), "run", "U1", "review"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 1
    assert "review: error" in result.stdout

    # 構造化された合格を返す場合は記録される
    (fake / "claude").write_text(
        '#!/bin/sh\necho \'{"session_id": "s2", "structured_output": '
        '{"verdict": "pass", "summary": "ok", "criteria": [], "issues": []}}\'\n', encoding="utf-8")
    result = subprocess.run([sys.executable, str(CYCLE), "run", "U1", "review"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0
    assert "review     pass  s2" in cycle(repo, "status", "U1").stdout


def test_spec_without_header_is_rejected(repo):
    (repo / "docs" / "plan" / "U1-demo.md").write_text("# 見出しなし\n", encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "見出し" in result.stderr
