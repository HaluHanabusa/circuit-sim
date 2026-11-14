"""開発サイクルのスクリプト（scripts/cycle.py）が、ゲートの決まりを守らせることを確かめる。

どのテストも、決まりを1つ外すと落ちるように書く（スクリプトを1か所ずつ壊して確かめている）。
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

CYCLE = Path(__file__).resolve().parents[1] / "scripts" / "cycle.py"


def load_main():
    spec = importlib.util.spec_from_file_location("cycle_script", CYCLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


CYCLE_MAIN = load_main()

# 試験の中の git は、利用者の設定（署名、フックなど）や、呼び出し元の GIT_DIR などに左右されないようにする
TEST_ENV = {
    **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

SPEC = """---
id: U1
owner: solver-engineer
---

# U1: 試験用の作業単位
"""

# 試験用の確認スクリプト。FAIL があるか、A と B が両方あると失敗する
CHECK = "#!/bin/sh\ntest ! -f FAIL && { test ! -f A || test ! -f B; }\n"

PASS = {"verdict": "pass", "summary": "ok", "criteria": [], "issues": []}


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=TEST_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(base: Path) -> Path:
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "plan").mkdir(parents=True)
    (root / "scripts" / "check.sh").write_text(CHECK, encoding="utf-8")
    (root / "scripts" / "check.sh").chmod(0o755)
    (root / "docs" / "plan" / "U1-demo.md").write_text(SPEC, encoding="utf-8")
    (root / ".gitignore").write_text(".cycle/\n", encoding="utf-8")
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture(scope="module")
def implemented(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """実装まで済ませたリポジトリ。ヘッドレスの結果の読み方を、同じ状態から何通りも確かめるのに使い回す。"""
    root = make_repo(tmp_path_factory.mktemp("implemented"))
    implement(root)
    return root


def cycle_env(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**TEST_ENV, "CYCLE_ROOT": str(root), **(extra or {})}


def cycle(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """cycle.py のコマンドを実行して、終了コードと出力を返す。

    起動の時間を省くため、試験のプロセスの中で main() を呼ぶ（環境変数は、その間だけ差し替える）。
    コマンドラインからの起動は start_cycle を使う試験と test_command_line_entry_point が確かめる。
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.dict(os.environ, cycle_env(root, env), clear=True),
        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
    ):
        try:
            code = CYCLE_MAIN(list(args))
        except SystemExit as exc:           # argparse が引数の誤りで終わる場合
            code = exc.code
    return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())


def start_cycle(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """cycle.py をコマンドラインから起動し、終わるのを待たずに返す（並行する実行、ロック、中断の確認用）。"""
    return subprocess.Popen(
        [sys.executable, str(CYCLE), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=cycle_env(root, env), preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )


def state(root: Path) -> dict:
    return json.loads((root / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))


def snap(root: Path) -> str:
    result = cycle(root, "snapshot", "U1")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def record(root: Path, gate: str, by: str, verdict: str = "pass", tree: str | None = None) -> None:
    args = ["record", "U1", gate, "--verdict", verdict, "--by", by]
    result = cycle(root, *args, *(["--tree", tree] if tree else []))
    assert result.returncode == 0, result.stderr


def implement(root: Path, content: str = "x = 2\n") -> Path:
    """start して、実装（製品コードの変更）を済ませた状態にする。"""
    assert cycle(root, "start", "U1").returncode == 0
    wt = root.parent / "wt" / "U1"
    (wt / "product.py").write_text(content, encoding="utf-8")
    record(root, "implement", "impl-1")
    return wt


def pass_all(root: Path, verifier: str = "ver-1") -> None:
    """いまの内容を対象に、レビューと検証の合格を記録し、check を通す。"""
    tree = snap(root)
    record(root, "review", "rev-1", tree=tree)
    record(root, "verify", verifier, tree=tree)
    assert cycle(root, "check", "U1").returncode == 0


def commit(wt: Path, message: str = "work") -> None:
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "-m", message)


def set_header(root: Path, extra: str) -> None:
    """本体側の仕様の見出しに行を足して、コミットする。"""
    text = SPEC.replace("owner: solver-engineer\n", "owner: solver-engineer\n" + extra)
    (root / "docs" / "plan" / "U1-demo.md").write_text(text, encoding="utf-8")
    commit(root, "仕様の見出し")


def fake_claude(tmp_path: Path, output: object, script: str = "", exit_code: int = 0) -> dict[str, str]:
    """偽の claude を PATH の先頭に置く環境変数を返す。script を実行してから output を出力する。"""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    (fake / "out.json").write_text(text, encoding="utf-8")
    body = f"#!/bin/sh\n{script}\ncat '{fake}/out.json'\nexit {exit_code}\n"
    (fake / "claude").write_text(body, encoding="utf-8")
    (fake / "claude").chmod(0o755)
    return {"PATH": f"{fake}:{os.environ['PATH']}"}


def headless(structured: object = PASS, session: str = "s1", **top: object) -> dict:
    """ヘッドレス実行の出力（--output-format json）の形。"""
    return {"session_id": session, "structured_output": structured, **top}


def wait_for(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} が現れない"
        time.sleep(0.02)


def main_log(root: Path) -> list[str]:
    return sh(root, "git", "log", "--format=%s", "main").splitlines()


def test_command_line_entry_point(repo):
    env = cycle_env(repo)
    proc = subprocess.run([sys.executable, CYCLE, "status"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and "進行中の作業単位は無い" in proc.stdout
    proc = subprocess.run([sys.executable, CYCLE, "status", "U1"], capture_output=True, text=True, env=env)
    assert proc.returncode == 2 and "エラー: U1 はまだ始まっていない" in proc.stderr


# ------------------------------------------------------------ 決まり1: 必要なゲート

def test_merge_succeeds_when_all_gates_pass(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    assert "統合: できる" in cycle(repo, "status", "U1").stdout
    result = cycle(repo, "merge", "U1", env={"CYCLE_COMMIT_TRAILERS": "Reviewed-by: someone"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 2\n"
    message = sh(repo, "git", "log", "-1", "--format=%B")
    assert "review: pass（rev-1）" in message
    assert sh(wt, "git", "rev-parse", "HEAD") in message
    assert message.endswith("\n\nReviewed-by: someone")
    assert (repo / state(repo)["gates"]["check"]["report"]).is_file()
    assert not (repo / ".cycle" / "merge-pending.json").exists()
    assert "U1（docs/plan/U1-demo.md、担当 solver-engineer）  統合済み" in cycle(repo, "status", "U1").stdout


def test_merge_refused_when_a_gate_is_missing(repo):
    assert cycle(repo, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    (wt / "product.py").write_text("x = 2\n", encoding="utf-8")
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    for gate in ("implement", "check", "review", "verify"):         # 既定で必要な4つ
        assert f"{gate}: まだ記録が無い" in result.stdout
    assert "ux" not in result.stdout
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.parametrize("gate", ["implement", "review", "verify"])
def test_failed_verdict_blocks_merge(repo, gate):
    wt = implement(repo)
    pass_all(repo)
    record(repo, gate, "someone", verdict="fail", tree=snap(repo))
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert f"{gate}: fail" in result.stdout
    assert main_log(repo) == ["init"]


def test_needs_ux_adds_the_ux_gate(repo):
    set_header(repo, "needs_ux: true\n")
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "ux: まだ記録が無い" in result.stdout
    record(repo, "ux", "ux-1", tree=snap(repo))
    assert cycle(repo, "merge", "U1").returncode == 0


@pytest.mark.parametrize("value", ["yes", '"true"', "true  # 画面あり", "1", ""])
def test_unknown_needs_ux_value_is_rejected(repo, value):
    set_header(repo, f"needs_ux: {value}\n")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "needs_ux は true か false だけ" in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_gates_header_sets_the_required_gates(repo):
    set_header(repo, "gates: implement, check\n")
    wt = implement(repo)
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr


def test_gates_header_can_require_ux(repo):
    set_header(repo, "gates: implement, ux\n")
    wt = implement(repo)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "ux: まだ記録が無い" in result.stdout
    assert "check" not in result.stdout


@pytest.mark.parametrize("extra, message", [
    ("needs_ux: true\ngates: implement, check\n", "needs_ux と gates が食い違っている"),
    ("needs_ux: false\ngates: implement, check, ux\n", "needs_ux と gates が食い違っている"),
    ("gates: implement, chek\n", "知らないゲート"),
    ("gates:\n", "知らないゲート"),
    ("need_ux: true\n", "見出しに読めない行がある"),
    ("needs_ux true\n", "見出しに読めない行がある"),
    ("needs_ux: true\nneeds_ux: false\n", "needs_ux が2回ある"),
])
def test_contradicting_or_unreadable_header_is_rejected(repo, extra, message):
    set_header(repo, extra)
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert message in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_spec_without_header_is_rejected(repo):
    (repo / "docs" / "plan" / "U1-demo.md").write_text("# 見出しなし\n", encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "見出し" in result.stderr


def test_spec_for_another_id_is_rejected(repo):
    (repo / "docs" / "plan" / "U1-demo.md").write_text(SPEC.replace("id: U1", "id: U2"), encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "見出しの id（U2）がファイル名と合わない" in result.stderr


def test_start_refuses_to_change_gates_once_records_exist(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    set_header(repo, "gates: implement\n")      # 本体側の見出しを書き換えて、必要なゲートを減らす
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "start --reset" in result.stderr
    assert "review     pass" in cycle(repo, "status", "U1").stdout
    # start をやり直さないまま統合することもできない
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "start の後で変わっている" in result.stdout

    # 記録を明示的に消せば、やり直せる。消したことは履歴に残る
    before = state(repo)["history"]
    assert cycle(repo, "start", "U1", "--reset").returncode == 0
    after = state(repo)
    assert after["gates"] == {}
    assert after["history"][:-1] == before
    assert after["history"][-1]["event"] == "reset"
    assert after["history"][-1]["cleared"] == ["check", "implement", "review", "verify"]
    status = cycle(repo, "status", "U1").stdout
    assert "implement  未実施" in status and "review" not in status
    assert "ゲートの記録を消した回数: 1" in status
    record(repo, "implement", "impl-1")
    assert cycle(repo, "merge", "U1").returncode == 0
    assert "ゲートの記録を消した回数: 1" in sh(repo, "git", "log", "-1", "--format=%B")


def test_start_refuses_to_change_owner_once_records_exist(repo):
    implement(repo)
    text = SPEC.replace("owner: solver-engineer", "owner: ui-engineer")
    (repo / "docs" / "plan" / "U1-demo.md").write_text(text, encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "start --reset" in result.stderr
    assert state(repo)["owner"] == "solver-engineer"


def test_merge_refused_when_the_spec_header_cannot_be_read(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    sh(repo, "git", "rm", "-q", "docs/plan/U1-demo.md")         # 本体側で仕様が無くなっている
    commit(repo, "仕様を消す")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "仕様の見出しを読めない" in result.stdout
    assert main_log(repo) == ["仕様を消す", "init"]


def test_start_follows_the_header_until_gates_are_recorded(repo):
    assert cycle(repo, "start", "U1").returncode == 0
    set_header(repo, "needs_ux: true\n")
    assert cycle(repo, "start", "U1").returncode == 0
    assert "ux         未実施" in cycle(repo, "status", "U1").stdout


def test_restart_keeps_gate_records_and_history(repo):
    wt = implement(repo)
    pass_all(repo)
    before = state(repo)
    assert cycle(repo, "start", "U1").returncode == 0
    after = state(repo)
    assert after["gates"] == before["gates"] and len(after["gates"]) == 4
    assert after["history"] == before["history"] and len(after["history"]) == 4
    commit(wt)
    assert cycle(repo, "merge", "U1").returncode == 0


def test_merged_unit_cannot_be_restarted_or_merged_again(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    assert cycle(repo, "merge", "U1").returncode == 0
    head = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "統合済み" in result.stderr
    assert state(repo)["merged_commit"] == head
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "既に統合済み" in result.stdout
    assert sh(repo, "git", "rev-parse", "HEAD") == head


# ------------------------------------------- 決まり2・3: 確認の後の変更と、その例外

def test_merge_refused_when_product_changes_after_review(repo):
    wt = implement(repo)
    pass_all(repo)
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")     # 検証の後でこっそり変更
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: 確認の後で変わったファイルがある（product.py）。やり直す" in result.stdout
    assert "verify: 確認の後で変わったファイルがある（product.py）。やり直す" in result.stdout


def test_verifier_may_add_acceptance_tests_after_review(repo):
    wt = implement(repo)
    tree = snap(repo)
    record(repo, "review", "rev-1", tree=tree)
    (wt / "tests" / "acceptance").mkdir(parents=True)
    (wt / "tests" / "acceptance" / "test_u1.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (wt / "docs" / "verification").mkdir(parents=True)
    (wt / "docs" / "verification" / "U1.md").write_text("記録\n", encoding="utf-8")
    record(repo, "verify", "ver-1", tree=tree)
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "tests" / "acceptance" / "test_u1.py").exists()


def test_japanese_file_name_in_exception_path_is_allowed(repo):
    wt = implement(repo)
    tree = snap(repo)
    (wt / "docs" / "verification").mkdir(parents=True)
    (wt / "docs" / "verification" / "検証記録.md").write_text("記録\n", encoding="utf-8")
    record(repo, "review", "rev-1", tree=tree)
    record(repo, "verify", "ver-1", tree=tree)
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "docs" / "verification" / "検証記録.md").exists()


def test_exception_is_limited_to_acceptance_tests_and_verification_records(repo):
    wt = implement(repo)
    pass_all(repo)
    outside = [
        "docs/other.md", "docs/verification.md", "tests/acceptance_more/test_x.py", "tests/test_other.py",
        "製品.py",
    ]
    for path in [*outside, "tests/acceptance/test_new.py", "docs/verification/U1.md"]:
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text("\n", encoding="utf-8")            # 確認の後で足す
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    # 例外の場所に足した2つは挙がらない。それ以外は、すべて挙がる
    assert f"review: 確認の後で変わったファイルがある（{', '.join(outside)}）。やり直す" in result.stdout
    assert f"verify: 確認の後で変わったファイルがある（{', '.join(outside)}）。やり直す" in result.stdout


@pytest.mark.parametrize("change, reported", [
    ("rewrite", "tests/acceptance/test_old.py"),
    ("delete", "tests/acceptance/test_old.py"),
    ("rename", "tests/acceptance/test_old.py"),
    ("move_product_in", "product.py"),
])
def test_exception_paths_allow_only_files_that_did_not_exist_at_the_gate(repo, change, reported):
    wt = implement(repo)
    old = wt / "tests" / "acceptance" / "test_old.py"
    old.parent.mkdir(parents=True)
    old.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    pass_all(repo)
    if change == "rewrite":                                       # 既にある受け入れ試験を骨抜きにする
        old.write_text("def test_old():\n    pass\n", encoding="utf-8")
    elif change == "delete":
        old.unlink()
    elif change == "rename":
        old.rename(old.with_name("test_renamed.py"))
    else:                                                         # 製品ファイルを例外の場所へ移す
        (wt / "product.py").rename(old.with_name("product.py"))
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert f"review: 確認の後で変わったファイルがある（{reported}）。やり直す" in result.stdout
    assert f"verify: 確認の後で変わったファイルがある（{reported}）。やり直す" in result.stdout
    assert main_log(repo) == ["init"]


def test_record_requires_tree_for_confirmation_gates(repo):
    implement(repo)
    for gate in ("review", "verify", "ux"):
        result = cycle(repo, "record", "U1", gate, "--verdict", "pass", "--by", "someone")
        assert result.returncode == 2
        assert "--tree が必要" in result.stderr
    assert list(state(repo)["gates"]) == ["implement"]


def test_record_resolves_tree_at_the_time_of_recording(repo):
    wt = implement(repo)
    commit(wt)
    record(repo, "review", "rev-1", tree="HEAD")
    record(repo, "verify", "ver-1", tree="U1")
    reviewed = sh(wt, "git", "rev-parse", "HEAD^{tree}")
    assert state(repo)["gates"]["review"]["tree"] == reviewed
    assert state(repo)["gates"]["verify"]["tree"] == reviewed
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")     # 記録の後で変更（HEAD も進む）
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: 確認の後で変わったファイルがある（product.py）。やり直す" in result.stdout
    assert "verify: 確認の後で変わったファイルがある（product.py）。やり直す" in result.stdout


@pytest.mark.parametrize("value", ["nonsense", "--all", "0" * 40])
def test_record_rejects_a_tree_that_cannot_be_resolved(repo, value):
    implement(repo)
    result = cycle(repo, "record", "U1", "review", "--verdict", "pass", "--by", "rev-1", f"--tree={value}")
    assert result.returncode == 2
    assert "ツリーとして読めない" in result.stderr
    assert "review" not in state(repo)["gates"]


@pytest.mark.parametrize("tree, message", [
    ("HEAD", "review: 記録の対象がツリーIDでない（HEAD）"),     # 名前のままの記録は、決して古くならない
    ("1" * 40, "review: 記録の対象（1111111111）を読めない"),   # 無くなったツリーとは比べられない
])
def test_gate_whose_recorded_tree_cannot_be_compared_is_not_valid(repo, tree, message):
    """以前の版が残した記録や、手で直した記録を、合格として扱わない。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    data = state(repo)
    data["gates"]["review"]["tree"] = tree
    (repo / ".cycle" / "state" / "U1.json").write_text(json.dumps(data), encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert message in result.stdout
    assert main_log(repo) == ["init"]


# ------------------------------------------------------------ 決まり4: check

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


def test_check_that_changes_the_content_is_not_a_pass(repo):
    wt = implement(repo)
    (wt / "scripts" / "check.sh").write_text("#!/bin/sh\necho 'y = 1' >> product.py\n", encoding="utf-8")
    result = cycle(repo, "check", "U1")
    assert result.returncode == 1
    assert "check: error" in result.stdout
    assert state(repo)["gates"]["check"]["verdict"] == "error"


def test_check_that_cannot_be_started_is_an_error_not_a_crash(repo):
    wt = implement(repo)
    (wt / "scripts" / "check.sh").chmod(0o644)
    result = cycle(repo, "check", "U1")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert state(repo)["gates"]["check"]["verdict"] == "error"


def test_force_added_ignored_file_is_part_of_the_checked_content(repo):
    wt = implement(repo)
    (wt / ".gitignore").write_text(".cycle/\n*.csv\n", encoding="utf-8")
    (wt / "ref.csv").write_text("1\n", encoding="utf-8")
    sh(wt, "git", "add", "-f", "ref.csv")
    pass_all(repo)
    (wt / "ref.csv").write_text("2\n", encoding="utf-8")            # 確認の後で、無視の対象のファイルを変える
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "check: 確認した後で内容が変わっている" in result.stdout
    assert "review: 確認の後で変わったファイルがある（ref.csv）。やり直す" in result.stdout
    pass_all(repo)
    assert cycle(repo, "merge", "U1").returncode == 0
    assert (repo / "ref.csv").read_text(encoding="utf-8") == "2\n"


# ------------------------------------------------------------ 決まり5: 実装と検証は別の担当

@pytest.mark.parametrize("verifier", ["impl-1", " impl-1 "])
def test_implementer_cannot_verify_own_work(repo, verifier):
    wt = implement(repo)
    pass_all(repo, verifier=verifier)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "実装した本人が検証している" in result.stdout


def test_implementer_is_remembered_after_implement_is_recorded_again(repo):
    wt = implement(repo)                            # impl-1 が実装
    record(repo, "implement", "someone-else")       # 別の名前で記録し直す
    pass_all(repo, verifier="impl-1")
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "実装した本人が検証している" in result.stdout


def test_headless_runs_without_session_id_count_as_the_same_person(repo, tmp_path):
    wt = implement(repo)
    env = fake_claude(tmp_path, {"structured_output": PASS})        # session_id が無い
    assert cycle(repo, "run", "U1", "implement", env=env).returncode == 0
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0
    assert cycle(repo, "run", "U1", "verify", env=env).returncode == 0
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "実装した本人が検証している" in result.stdout


def test_record_requires_a_name(repo):
    implement(repo)
    result = cycle(repo, "record", "U1", "implement", "--verdict", "pass", "--by", " ")
    assert result.returncode == 2
    assert "--by が空" in result.stderr


# ------------------------------------------------------------ 決まり6: ヘッドレスの結果

def test_dry_run_shows_headless_command_for_the_right_agent(repo):
    implement(repo)
    out = cycle(repo, "run", "U1", "implement", "--dry-run").stdout
    assert "claude -p" in out and "--agent solver-engineer" in out
    assert "--output-format json" in out and "--json-schema" in out
    assert "--agent code-reviewer" in cycle(repo, "run", "U1", "review", "--dry-run").stdout
    assert "--agent verifier" in cycle(repo, "run", "U1", "verify", "--dry-run").stdout


def test_headless_run_without_structured_result_is_not_a_pass(repo, tmp_path):
    implement(repo)
    env = fake_claude(tmp_path, {"result": "done", "session_id": "s1"})
    result = cycle(repo, "run", "U1", "review", env=env)
    assert result.returncode == 1
    assert "review: error" in result.stdout

    # 構造化された合格を返す場合は記録される
    env = fake_claude(tmp_path, headless(session="s2"))
    result = cycle(repo, "run", "U1", "review", env=env)
    assert result.returncode == 0
    assert "review     pass  s2" in cycle(repo, "status", "U1").stdout


def criterion(passed: object = None, **item: object) -> dict:
    """基準を1つ持つ「合格」の結果。passed を渡したときだけ、基準に pass を入れる。"""
    if passed is not None:
        item["pass"] = passed
    return {**PASS, "criteria": [item]}


def issue(**item: object) -> dict:
    """指摘を1つ持つ「合格」の結果。"""
    return {**PASS, "issues": [item]}


NOT_A_PASS = {
    "is_error が真": (headless(is_error=True), 0, "error"),
    "subtype が success でない": (headless(is_error=False, subtype="error_max_turns"), 0, "error"),
    "終了コードが 0 でない": (headless(), 1, "error"),
    "JSON でない": ("done", 0, "error"),
    "JSON だがオブジェクトでない": ([headless()], 0, "error"),
    "structured_output が文字列": (headless("pass"), 0, "error"),
    "verdict が無い": (headless({k: v for k, v in PASS.items() if k != "verdict"}), 0, "error"),
    "summary が無い": (headless({k: v for k, v in PASS.items() if k != "summary"}), 0, "error"),
    "criteria が無い": (headless({k: v for k, v in PASS.items() if k != "criteria"}), 0, "error"),
    "issues が無い": (headless({k: v for k, v in PASS.items() if k != "issues"}), 0, "error"),
    "verdict が pass でも fail でもない": (headless({**PASS, "verdict": "ok"}), 0, "error"),
    "summary が文字列でない": (headless({**PASS, "summary": 1}), 0, "error"),
    "criteria が一覧でない": (headless({**PASS, "criteria": {}}), 0, "error"),
    "issues が一覧でない": (headless({**PASS, "issues": {}}), 0, "error"),
    "criteria の項目がオブジェクトでない": (headless({**PASS, "criteria": ["ok"]}), 0, "error"),
    "criteria に pass が無い": (headless(criterion(name="c1", measured="1")), 0, "error"),
    "criteria の pass が文字列": (headless(criterion(name="c1", measured="1", passed="false")), 0, "error"),
    "criteria に measured が無い": (headless(criterion(name="c1", passed=True)), 0, "error"),
    "issues の項目がオブジェクトでない": (headless({**PASS, "issues": ["critical"]}), 0, "error"),
    "issues の severity が知らない値": (headless(issue(severity="blocker", description="x")), 0, "error"),
    "issues に description が無い": (headless(issue(severity="minor")), 0, "error"),
    "pass なのに critical の指摘": (headless(issue(severity="critical", description="x")), 0, "fail"),
    "pass なのに満たしていない基準": (headless(criterion(name="c1", measured="1", passed=False)), 0, "fail"),
}


@pytest.mark.parametrize("case", NOT_A_PASS)
def test_headless_result_with_gaps_or_contradictions_is_not_a_pass(implemented, tmp_path, case):
    output, exit_code, expected = NOT_A_PASS[case]
    env = fake_claude(tmp_path, output, exit_code=exit_code)
    result = cycle(implemented, "run", "U1", "review", env=env)
    assert result.returncode == 1
    assert f"review: {expected}" in result.stdout
    assert state(implemented)["gates"]["review"]["verdict"] == expected


def test_headless_pass_with_minor_findings_is_a_pass(repo, tmp_path):
    implement(repo)
    structured = {
        "verdict": "pass", "summary": "ok",
        "criteria": [{"name": "c1", "measured": "1", "pass": True}],
        "issues": [{"severity": "major", "description": "x"}, {"severity": "minor", "description": "y"}],
    }
    env = fake_claude(tmp_path, headless(structured, is_error=False, subtype="success"))
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0
    assert state(repo)["gates"]["review"]["verdict"] == "pass"


def test_confirmation_run_records_the_content_seen_before_the_run(repo, tmp_path):
    wt = implement(repo)
    # レビューの実行中に、製品のコードが変わる
    env = fake_claude(tmp_path, headless(), script="echo 'x = 7' > product.py")
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0
    record(repo, "verify", "ver-1", tree=snap(repo))
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: 確認の後で変わったファイルがある（product.py）。やり直す" in result.stdout
    assert "verify:" not in result.stdout


def test_headless_run_that_times_out_is_not_a_pass(repo, tmp_path):
    implement(repo)
    env = fake_claude(tmp_path, headless(), script="exec sleep 20")
    result = cycle(repo, "run", "U1", "review", "--timeout", "1", env=env)
    assert result.returncode == 1
    assert "review: error" in result.stdout
    assert state(repo)["gates"]["review"]["verdict"] == "error"


def test_missing_claude_is_reported_clearly(repo, tmp_path):
    implement(repo)
    (tmp_path / "empty").mkdir()
    result = cycle(repo, "run", "U1", "review", env={"PATH": str(tmp_path / "empty")})
    assert result.returncode == 2
    assert "claude コマンドが見つからない" in result.stderr
    assert "Traceback" not in result.stderr
    assert "review" not in state(repo)["gates"]


# ------------------------------------------------------------ 記録を失わない（並行実行）

def blocking_claude(tmp_path: Path, session: str) -> tuple[dict[str, str], Path, Path]:
    """起動したら started を置き、go が置かれるまで待ってから合格を返す、偽の claude。"""
    started, go = tmp_path / "started", tmp_path / "go"
    script = f"touch '{started}'\nwhile [ ! -f '{go}' ]; do sleep 0.02; done"
    return fake_claude(tmp_path, headless(session=session), script=script), started, go


def test_parallel_run_does_not_lose_or_overwrite_other_records(repo, tmp_path):
    implement(repo)
    tree = snap(repo)
    record(repo, "review", "rev-1", tree=tree)
    env, started, go = blocking_claude(tmp_path, "ver-s")
    proc = start_cycle(repo, "run", "U1", "verify", env=env)
    wait_for(started)
    record(repo, "review", "rev-2", verdict="fail", tree=tree)      # verify の実行中に、review が不合格になる
    go.touch()
    assert proc.wait(timeout=30) == 0
    gates = state(repo)["gates"]
    assert gates["verify"]["verdict"] == "pass" and gates["verify"]["by"] == "ver-s"
    assert gates["review"]["verdict"] == "fail" and gates["review"]["by"] == "rev-2"


def test_pass_from_a_run_overtaken_by_another_record_is_not_adopted(repo, tmp_path):
    implement(repo)
    env, started, go = blocking_claude(tmp_path, "rev-s")
    proc = start_cycle(repo, "run", "U1", "review", env=env)
    wait_for(started)
    record(repo, "review", "rev-2", verdict="fail", tree=snap(repo))    # 先に始めた実行より後に入った不合格
    go.touch()
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 1
    assert "この合格は採用しない" in out
    data = state(repo)
    assert data["gates"]["review"]["verdict"] == "fail" and data["gates"]["review"]["by"] == "rev-2"
    # 採用しなかった結果も履歴には残る
    assert [(h["by"], h["adopted"]) for h in data["history"] if h.get("gate") == "review"] == [
        ("rev-2", True), ("rev-s", False),
    ]


def test_pass_from_a_run_that_started_before_a_reset_is_not_adopted(repo, tmp_path):
    implement(repo)
    env, started, go = blocking_claude(tmp_path, "rev-s")
    proc = start_cycle(repo, "run", "U1", "review", env=env)
    wait_for(started)
    assert cycle(repo, "start", "U1", "--reset").returncode == 0    # 実行中に、記録を消してやり直すことにした
    go.touch()
    assert proc.wait(timeout=30) == 1
    data = state(repo)
    assert data["gates"] == {}
    assert [h.get("adopted") for h in data["history"]] == [True, None, False]


def test_pass_from_a_check_overtaken_by_another_check_is_not_adopted(repo, tmp_path):
    wt = implement(repo)
    # WAIT があれば合図を待ってから合格し、BROKEN があれば失敗する確認スクリプト
    script = (
        '#!/bin/sh\nif [ -n "$WAIT" ]; then : > "$WAIT.started"; '
        'while [ ! -f "$WAIT.go" ]; do sleep 0.02; done; fi\ntest -z "$BROKEN"\n'
    )
    (wt / "scripts" / "check.sh").write_text(script, encoding="utf-8")
    wait = tmp_path / "wait"
    proc = start_cycle(repo, "check", "U1", env={"WAIT": str(wait)})
    wait_for(Path(f"{wait}.started"))
    assert cycle(repo, "check", "U1", env={"BROKEN": "1"}).returncode == 1  # 先に始めた check の実行中
    Path(f"{wait}.go").touch()
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 1
    assert "check: pass" in out and "この合格は採用しない" in out
    assert state(repo)["gates"]["check"]["verdict"] == "fail"


@pytest.mark.parametrize("command, lock", [
    (["record", "U1", "implement", "--verdict", "fail", "--by", "impl-2"], "state/U1.lock"),
    (["start", "U1"], "state/U1.lock"),
    (["merge", "U1"], "state/U1.lock"),
    (["merge", "U1"], "merge.lock"),
])
def test_writing_commands_wait_for_the_lock(repo, command, lock):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before = state(repo)
    with open(repo / ".cycle" / lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)                      # 別の実行がロックを持っている
        proc = start_cycle(repo, *command)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
        assert state(repo) == before
        assert main_log(repo) == ["init"]
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert proc.wait(timeout=30) == 0
    assert state(repo) != before


# ------------------------------------------------------------ 見るだけの操作は索引を変えない

def test_reading_and_recording_leave_the_index_alone(repo, tmp_path):
    wt = implement(repo)                                    # product.py は、ステージしていない変更
    (wt / "staged.py").write_text("s = 1\n", encoding="utf-8")
    sh(wt, "git", "add", "staged.py")                       # 選んでステージしたファイル
    (wt / "untracked.py").write_text("u = 1\n", encoding="utf-8")
    before = sh(wt, "git", "status", "--porcelain")
    assert "?? untracked.py" in before and "A  staged.py" in before
    assert cycle(repo, "status", "U1").returncode == 0
    tree = snap(repo)
    record(repo, "implement", "impl-1")
    record(repo, "review", "rev-1", tree=tree)
    assert cycle(repo, "check", "U1").returncode == 0
    assert cycle(repo, "run", "U1", "verify", env=fake_claude(tmp_path, headless())).returncode == 0
    assert cycle(repo, "merge", "U1").returncode == 1
    assert sh(wt, "git", "status", "--porcelain") == before
    # 索引は変えないが、内容にはステージしていないファイルも含めている
    assert sh(wt, "git", "ls-tree", "-r", "--name-only", tree).splitlines() == [
        ".gitignore", "docs/plan/U1-demo.md", "product.py", "scripts/check.sh", "staged.py", "untracked.py",
    ]


def test_git_location_variables_of_the_caller_are_ignored(repo, tmp_path):
    """git のフックの中などから呼ばれても、対象の作業ツリーと、その本物でない索引を見る。"""
    implement(repo)
    other = tmp_path / "other"
    other.mkdir()
    sh(other, "git", "init", "-q")
    index = other / "index"
    env = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other), "GIT_INDEX_FILE": str(index)}
    assert cycle(repo, "snapshot", "U1", env=env).stdout.strip() == snap(repo)
    assert not index.exists()


def test_snapshot_during_conflict_does_not_mark_it_resolved(repo):
    wt = implement(repo)
    commit(wt)
    (repo / "product.py").write_text("x = 9\n", encoding="utf-8")
    commit(repo, "main が進む")
    assert subprocess.run(["git", "merge", "main"], cwd=wt, capture_output=True, env=TEST_ENV).returncode != 0
    unmerged = sh(wt, "git", "ls-files", "-u")
    assert "product.py" in unmerged
    snap(repo)
    assert cycle(repo, "status", "U1").returncode == 0
    assert sh(wt, "git", "ls-files", "-u") == unmerged


# ------------------------------------------------------------ merge の前提と、取り込む対象

def test_merge_requires_committed_worktree(repo):
    implement(repo)
    pass_all(repo)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "作業ツリーに未コミットの変更がある" in result.stdout
    assert main_log(repo) == ["init"]


def test_merge_sees_changes_hidden_from_git_status(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    sh(wt, "git", "update-index", "--assume-unchanged", "product.py")
    (wt / "product.py").write_text("x = 4\n", encoding="utf-8")
    assert sh(wt, "git", "status", "--porcelain") == ""         # git status には出ない変更
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "check: 確認した後で内容が変わっている" in result.stdout
    assert "作業ツリーに未コミットの変更がある" in result.stdout


@pytest.mark.parametrize("dirt, message", [
    ("other_branch", "本体のチェックアウトが main ではない"),
    ("untracked", "本体（main）に未コミットの変更がある"),
    ("staged_only", "本体（main）に未コミットの変更がある"),
    ("hidden", "本体（main）に未コミットの変更がある"),
])
def test_merge_requires_clean_main_checkout(repo, dirt, message):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    if dirt == "other_branch":
        sh(repo, "git", "checkout", "-q", "-b", "side")
    elif dirt == "untracked":
        (repo / "note.txt").write_text("memo\n", encoding="utf-8")
    elif dirt == "staged_only":                             # 索引だけが HEAD と違う
        (repo / "product.py").write_text("x = 5\n", encoding="utf-8")
        sh(repo, "git", "add", "product.py")
        (repo / "product.py").write_text("x = 1\n", encoding="utf-8")
    else:                                                   # git status には出ない変更
        sh(repo, "git", "update-index", "--assume-unchanged", "product.py")
        (repo / "product.py").write_text("x = 5\n", encoding="utf-8")
        assert sh(repo, "git", "status", "--porcelain") == ""
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert message in result.stdout
    assert main_log(repo) == ["init"]
    assert sh(repo, "git", "log", "--format=%s", "HEAD") == "init"
    assert "merged_at" not in state(repo)


def test_merge_takes_the_checked_commit_not_the_branch_recorded_at_start(repo):
    assert cycle(repo, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    (wt / "product.py").write_text("x = 666\n", encoding="utf-8")   # 誰も確認していない内容
    commit(wt, "unreviewed")
    sh(wt, "git", "checkout", "-q", "-b", "other", "main")          # 作業ツリーを別のブランチに切り替える
    (wt / "product.py").write_text("x = 2\n", encoding="utf-8")
    record(repo, "implement", "impl-1")
    pass_all(repo)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 2\n"
    assert sh(repo, "git", "rev-parse", "HEAD^2") == sh(wt, "git", "rev-parse", "other")
    assert "unreviewed" not in main_log(repo)


def test_start_refuses_a_worktree_with_detached_head(repo):
    wt = repo.parent / "wt" / "U1"
    sh(repo, "git", "worktree", "add", "-q", "--detach", str(wt))
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "ブランチから切り離されている" in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_merge_refuses_a_worktree_with_detached_head(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    sh(wt, "git", "checkout", "-q", "--detach")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "ブランチから切り離されている" in result.stdout
    assert "まだできない" in cycle(repo, "status", "U1").stdout
    assert main_log(repo) == ["init"]


def test_start_refuses_a_directory_below_the_worktree_top(repo):
    """下の階層を作業ツリーとして渡すと、そこに置いた別の scripts/check.sh が使われてしまう。"""
    wt = repo.parent / "wt" / "U1"
    sh(repo, "git", "worktree", "add", "-q", str(wt), "-b", "U1")
    (wt / "sub").mkdir()
    result = cycle(repo, "start", "U1", "--worktree", str(wt / "sub"))
    assert result.returncode == 2
    assert "最上位" in result.stderr


def test_merge_fails_when_there_is_nothing_to_merge(repo):
    assert cycle(repo, "start", "U1").returncode == 0           # 変更の無いブランチ
    record(repo, "implement", "impl-1")
    pass_all(repo)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "取り込むものが無い" in result.stdout
    assert "merged_at" not in state(repo)
    assert main_log(repo) == ["init"]


# ------------------------------------------------------------ 決まり7: 取り込んだ後の確認と取り消し

def assert_main_untouched(root: Path, before: str) -> None:
    assert sh(root, "git", "rev-parse", "HEAD") == before
    assert sh(root, "git", "status", "--porcelain") == ""
    assert not (root / ".git" / "MERGE_HEAD").exists()
    assert not (root / ".cycle" / "merge-pending.json").exists()
    assert "merged_at" not in state(root)
    status = cycle(root, "status", "U1").stdout
    assert "統合済み" not in status and "注意" not in status


def test_merge_is_undone_when_check_fails_on_main(repo):
    wt = implement(repo)
    (wt / "B").write_text("", encoding="utf-8")
    pass_all(repo)
    commit(wt)
    # ブランチを切った後で main が進んでいる。それぞれは check を通るが、合わせると通らない
    (repo / "A").write_text("", encoding="utf-8")
    commit(repo, "main が進む")
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "取り込みを取り消した" in result.stdout
    assert not (repo / "B").exists()
    assert_main_untouched(repo, before)


def test_merge_conflict_leaves_main_as_it_was(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    (repo / "product.py").write_text("x = 9\n", encoding="utf-8")   # main 側でも同じ行が変わっている
    commit(repo, "main が進む")
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "取り込めなかったので、本体を取り込み前" in result.stdout
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 9\n"
    assert_main_untouched(repo, before)


def test_merge_is_undone_when_check_cannot_be_started_on_main(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    (repo / "scripts" / "check.sh").chmod(0o644)                # main 側で実行ビットが落ちている
    commit(repo, "実行ビットを落とす")
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "取り込みを取り消した" in result.stdout
    assert_main_untouched(repo, before)


def merge_stopped_during_check(root: Path, tmp_path: Path, stop: signal.Signals) -> str:
    """取り込んだ後の check が走っている最中に、merge を止める。取り込み前の main のコミットを返す。"""
    wt = implement(root)
    # 環境変数 HANG があるときだけ、印を置いて止まる確認スクリプト
    script = '#!/bin/sh\nif [ -n "$HANG" ]; then : > "$HANG"; exec sleep 30; fi\n'
    (wt / "scripts" / "check.sh").write_text(script, encoding="utf-8")
    pass_all(root)
    commit(wt)
    before = sh(root, "git", "rev-parse", "HEAD")
    marker = tmp_path / "check-started"
    proc = start_cycle(root, "merge", "U1", env={"HANG": str(marker)})
    wait_for(marker)                                            # 取り込んだ後の check が走っている
    assert sh(root, "git", "rev-parse", "HEAD^1") == before     # 取り込みコミットが、もう main にある
    proc.send_signal(stop)
    assert proc.wait(timeout=30) != 0
    return before


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_merge_is_undone_when_interrupted_during_check_on_main(repo, tmp_path, stop):
    before = merge_stopped_during_check(repo, tmp_path, stop)
    assert_main_untouched(repo, before)


def test_merge_killed_during_check_is_undone_by_the_next_merge(repo, tmp_path):
    before = merge_stopped_during_check(repo, tmp_path, signal.SIGKILL)
    # 後始末なしで終わったので、確認の済んでいない取り込みが main に残っている
    assert sh(repo, "git", "rev-parse", "HEAD^1") == before
    assert "注意: 前回の統合が途中で終わっている" in cycle(repo, "status").stdout
    result = cycle(repo, "merge", "U1")
    assert "前回の U1 の統合が途中で終わっていたので、本体を取り込み前" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr    # 戻してから、改めて取り込んで確認する
    assert sh(repo, "git", "rev-parse", "HEAD^1") == before
    assert sh(repo, "git", "rev-parse", "HEAD") == state(repo)["merged_commit"]
    assert not (repo / ".cycle" / "merge-pending.json").exists()


@pytest.mark.parametrize("after", ["commit_on_top", "edit", "other_branch"])
def test_recovery_leaves_main_alone_when_it_changed_after_the_kill(repo, tmp_path, after):
    merge_stopped_during_check(repo, tmp_path, signal.SIGKILL)
    if after == "commit_on_top":
        (repo / "note.txt").write_text("memo\n", encoding="utf-8")
        commit(repo, "その後の変更")
    elif after == "edit":
        (repo / "product.py").write_text("x = 5\n", encoding="utf-8")
    else:
        sh(repo, "git", "checkout", "-q", "-b", "side")
    refs = sh(repo, "git", "for-each-ref")
    files = sh(repo, "git", "status", "--porcelain")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "前回の U1 の統合が途中で終わっているが" in result.stdout
    assert sh(repo, "git", "for-each-ref") == refs                 # 人が確かめるまで、何も動かさない
    assert sh(repo, "git", "status", "--porcelain") == files
    assert (repo / ".cycle" / "merge-pending.json").exists()


def test_recovery_cleans_up_a_merge_left_half_done(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    (repo / "product.py").write_text("x = 9\n", encoding="utf-8")   # main 側でも同じ行が変わっている
    commit(repo, "main が進む")
    before, head = sh(repo, "git", "rev-parse", "HEAD"), sh(wt, "git", "rev-parse", "HEAD")
    # 取り込みの最中（競合で止まったところ）で強制終了された状態を作る
    assert subprocess.run(["git", "merge", head], cwd=repo, capture_output=True, env=TEST_ENV).returncode != 0
    pending = {"id": "U1", "before": before, "head": head}
    (repo / ".cycle" / "merge-pending.json").write_text(json.dumps(pending), encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert "前回の U1 の統合が途中で終わっていたので、本体を取り込み前" in result.stdout
    assert result.returncode == 1                               # 改めて取り込むと、やはり競合する
    assert_main_untouched(repo, before)


def test_recovery_does_not_undo_a_merge_that_was_completed(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before = sh(repo, "git", "rev-parse", "HEAD")
    assert cycle(repo, "merge", "U1").returncode == 0
    merged = sh(repo, "git", "rev-parse", "HEAD")
    # 「統合済み」を記録した直後、控えを消す前に強制終了された状態を作る
    pending = {"id": "U1", "before": before, "head": sh(wt, "git", "rev-parse", "HEAD")}
    (repo / ".cycle" / "merge-pending.json").write_text(json.dumps(pending), encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1 and "既に統合済み" in result.stdout
    assert sh(repo, "git", "rev-parse", "HEAD") == merged
    assert not (repo / ".cycle" / "merge-pending.json").exists()


def test_unreadable_merge_record_blocks_merge(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    (repo / ".cycle" / "merge-pending.json").write_text("{}", encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "前回の統合の控えを読めない" in result.stdout
    assert main_log(repo) == ["init"]
