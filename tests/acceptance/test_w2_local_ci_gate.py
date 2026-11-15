"""W2（手元の確認 scripts/local_ci.py と、Windows での確認のゲート）の受け入れ試験（検証担当。先頭 fee52d2）。

実測値と測り方は `docs/verification/W2-verifier.md` にある。受け入れ基準（`docs/plan/W2-local-ci.md`）の番号を
試験の名前の先頭に付けた（c1 = 基準1、…）。

期待値の作り方:
- 一時リポジトリ、確認スクリプト（本物と同じく pytest を子プロセスで起動する）、試験は、このファイルで作る。
- コミットIDとツリーIDの期待値は git（rev-parse）から取る。scripts/cycle.py の関数は使わない。
- スクリプトはコマンドラインから起動する（試験のプロセスの中で main() を呼ばない）。
- Windows での結果は、結果に書かれるOS（platform.system()）だけを Windows にして local_ci.py を起動して作る。
- 取り出した場所が残っていないことは、一時フォルダ（TMPDIR、TEMP、TMP）を試験ごとの場所に向けて確かめる。

製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import json
import locale
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CYCLE = ROOT / "scripts" / "cycle.py"
LOCAL_CI = ROOT / "scripts" / "local_ci.py"
WINDOWS = os.name == "nt"

EMPTY_GIT_CONFIG = Path(tempfile.gettempdir()) / "circuit-sim-w2-acceptance-empty.gitconfig"
EMPTY_GIT_CONFIG.write_text("", encoding="utf-8")

# local_ci.py を起動する。W2_AS_WINDOWS があれば、結果に書かれるOS（platform.system()）だけを Windows にする
LAUNCH = """\
import os, platform, runpy, sys
if os.environ.get("W2_AS_WINDOWS"):
    platform.system = lambda: "Windows"
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""

# 試験用の確認スクリプト。本物と同じく、pytest を子プロセスで起動する。
# LINT_FAILS があれば、試験が通っても失敗する（ruff の指摘の代わり）。W2_RUNS があれば、起動した回数を控える
CHECK = """\
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runs = os.environ.get("W2_RUNS")
if runs:
    with open(runs, "a", encoding="utf-8") as handle:
        handle.write("run\\n")
code = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT).returncode
if code == 0 and (ROOT / "LINT_FAILS").exists():
    code = 3
sys.exit(code)
"""

TESTS = """\
import json
import os
import time
from pathlib import Path

import product


def test_value():
    seen = os.environ.get("W2_SEEN")
    if seen:
        where = {"product": str(Path(product.__file__).resolve()), "cwd": os.getcwd()}
        Path(seen).write_text(json.dumps(where), encoding="utf-8")
    print("W2-OUTPUT-MARKER", os.environ.get("W2_SECRET"))
    assert product.VALUE == 1, "W2-FAILMSG-MARKER " + str(os.environ.get("W2_SECRET"))


def test_env():
    assert not os.environ.get("W2_FAIL"), "W2-FAILMSG-MARKER"


def test_wait():
    wait = os.environ.get("W2_WAIT")
    if wait:
        Path(wait).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(float(os.environ["W2_WAIT_SECONDS"]))
        Path(wait + ".done").write_text("done", encoding="utf-8")
"""

SPEC = """---
id: U1
owner: solver-engineer
needs_windows: true
---

# U1: 試験用の作業単位
"""


def base_env(tmp: Path, root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """git は利用者の設定に左右させない。一時フォルダは試験ごと。外側の pytest のオプションは渡さない。"""
    drop = ("PYTEST_ADDOPTS", "W2_AS_WINDOWS", "W2_FAIL", "W2_WAIT", "W2_SEEN", "W2_RUNS", "W2_SECRET")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in drop}
    env.update({
        "GIT_AUTHOR_NAME": "w2", "GIT_AUTHOR_EMAIL": "w2@example.invalid",
        "GIT_COMMITTER_NAME": "w2", "GIT_COMMITTER_EMAIL": "w2@example.invalid",
        "GIT_CONFIG_GLOBAL": str(EMPTY_GIT_CONFIG), "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONIOENCODING": "utf-8", "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "CYCLE_ROOT": str(root),
    })
    env.update(extra or {})
    return env


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        env=base_env(cwd, cwd),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def commit_all(cwd: Path, message: str = "work") -> str:
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """local_ci.py が取り出す場所を作る、一時フォルダ（ここが空なら、取り出した場所は残っていない）。"""
    place = tmp_path / "tmp"
    place.mkdir()
    return place


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs" / "plan").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text(CHECK, encoding="utf-8")
    (root / "scripts" / "check.py").chmod(0o755)
    (root / "tests" / "test_product.py").write_text(TESTS, encoding="utf-8")
    (root / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "docs" / "plan" / "U1-demo.md").write_text(SPEC, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n', encoding="utf-8",
    )
    ignored = ".cycle/\n.venv/\n__pycache__/\n*.pyc\ntests/test_ign_*.py\n*.csv\n"
    (root / ".gitignore").write_text(ignored, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "update-index", "--chmod=+x", "scripts/check.py")    # 本物と同じく、実行できる印を付ける
    git(root, "commit", "-q", "-m", "init")
    return root


def local_ci(
    root: Path, tmp: Path, *args: str, env: dict[str, str] | None = None, windows: bool = False,
) -> subprocess.CompletedProcess:
    extra = {**(env or {}), **({"W2_AS_WINDOWS": "1"} if windows else {})}
    return subprocess.run(
        [sys.executable, "-c", LAUNCH, str(LOCAL_CI), *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=base_env(tmp, root, extra), timeout=180,
    )


def start_local_ci(root: Path, tmp: Path, *args: str, env: dict[str, str]) -> subprocess.Popen:
    """終わるのを待たずに返す（POSIX だけで使う）。呼び出し元が SIGINT を無視していても、既定の扱いに戻す。"""
    return subprocess.Popen(
        [sys.executable, "-c", LAUNCH, str(LOCAL_CI), *args], cwd=root, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=base_env(tmp, root, env),
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )


def cycle(root: Path, tmp: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CYCLE), *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=base_env(tmp, root), timeout=180,
    )


def results(root: Path) -> list[tuple[Path, dict]]:
    found = sorted((root / ".cycle" / "ci").glob("*.json"))
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in found]


def leftovers(tmp: Path) -> list[str]:
    return sorted(p.name for p in tmp.iterdir() if p.name.startswith("circuit-sim-ci-"))


def worktrees(root: Path) -> list[str]:
    listed = git(root, "worktree", "list", "--porcelain").splitlines()
    return [line for line in listed if line.startswith("worktree ")]


def run_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def strings(value: object) -> Iterator[str]:
    """結果のファイルに入っている、すべての文字列（項目の名前と値）。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def wait_for(path: Path, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} が現れない"
        time.sleep(0.05)


# ================================================================ 基準1: 取り出して実行

def dirty_the_folder(root: Path) -> None:
    """作業フォルダに、未コミットの変更、壊れたファイル、落ちる試験、.venv、無視されたファイル、索引の変更を置く。"""
    (root / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "tests" / "test_broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "tests" / "test_untracked.py").write_text("def test_u():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_ign_x.py").write_text("def test_i():\n    assert False\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "product.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "data.csv").write_text("1\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(root, "add", "staged.txt")
    (root / "scripts" / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")


def test_c1_the_named_commit_is_checked_whatever_the_working_folder_holds(repo, tmp, tmp_path):
    main = git(repo, "rev-parse", "main")
    expected = (main, git(repo, "rev-parse", "main^{tree}"))
    dirty_the_folder(repo)
    status = git(repo, "status", "--porcelain=v1", "--ignored", "--untracked-files=all")
    index = git(repo, "ls-files", "--stage")           # 索引の中身（選んでステージした状態）
    seen = tmp_path / "seen.json"

    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main", env={"W2_SEEN": str(seen)})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    [(_, data)] = results(repo)
    assert (data["commit"], data["tree"], data["mode"], data["dirty"]) == (*expected, "checkout", False)
    assert data["tests"]["passed"] == 3 and data["tests"]["failed"] == 0      # 作業フォルダの試験は入らない
    where = json.loads(seen.read_text(encoding="utf-8"))
    checked = Path(where["product"])
    assert Path(tmp).resolve() in checked.parents and repo.resolve() not in checked.parents
    # 作業フォルダには触らない（増えるのは結果の .cycle/ だけ）
    after = git(repo, "status", "--porcelain=v1", "--ignored", "--untracked-files=all")
    assert [line for line in after.splitlines() if ".cycle/" not in line] == status.splitlines()
    assert git(repo, "ls-files", "--stage") == index
    assert leftovers(tmp) == [] and len(worktrees(repo)) == 1


def test_c1_a_commit_other_than_head_is_checked_by_its_own_ids(repo, tmp):
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "product.py").write_text("VALUE = 1\nOTHER = 1\n", encoding="utf-8")
    other = commit_all(repo, "other")
    git(repo, "checkout", "-q", "main")
    dirty_the_folder(repo)

    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "other")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    [(_, data)] = results(repo)
    assert (data["commit"], data["tree"]) == (other, git(repo, "rev-parse", "other^{tree}"))
    assert data["branch"] == "other"
    assert leftovers(tmp) == [] and len(worktrees(repo)) == 1


def test_c1_nothing_is_left_after_a_failed_check(repo, tmp):
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main", env={"W2_FAIL": "1"})
    assert proc.returncode == 1
    assert leftovers(tmp) == [] and len(worktrees(repo)) == 1


def test_c1_nothing_is_left_after_a_timeout(repo, tmp, tmp_path):
    wait = tmp_path / "wait"
    env = {"W2_WAIT": str(wait), "W2_WAIT_SECONDS": "6"}
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main", "--timeout", "3", env=env)
    assert proc.returncode == 1
    [(_, data)] = results(repo)
    assert (data["verdict"], data["reason"], data["check"]["timed_out"]) == ("error", "timeout", True)
    assert leftovers(tmp) == [] and len(worktrees(repo)) == 1
    time.sleep(5)                                   # 止められなかった試験が終わった後も、何も作り直されない
    assert leftovers(tmp) == []


@pytest.mark.skipif(WINDOWS, reason="SIGINT と SIGTERM を送る試験（Windows の Ctrl-Break は実装側の試験）")
@pytest.mark.parametrize("stop", ["SIGINT", "SIGTERM"])
def test_c1_nothing_is_left_after_an_interruption(repo, tmp, tmp_path, stop):
    wait = tmp_path / "wait"
    proc = start_local_ci(
        repo, tmp, "--no-fetch", "--ref", "main", env={"W2_WAIT": str(wait), "W2_WAIT_SECONDS": "4"},
    )
    wait_for(wait)
    proc.send_signal(getattr(signal, stop))
    assert proc.wait(timeout=30) != 0
    assert results(repo) == []                      # 中断した実行は、結果を書かない（合格にならない）
    assert leftovers(tmp) == [] and len(worktrees(repo)) == 1
    time.sleep(5)                                   # 止められなかった試験が終わった後も、何も作り直されない
    assert leftovers(tmp) == []


@pytest.mark.skipif(WINDOWS, reason="Windows では taskkill /T で、確認の先のプロセスまで止める")
@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(軽微): Linux / macOS では、時間切れで止めるのは scripts/check.py だけで、"
    "その先の pytest は "
    "動き続ける（取り出した場所を消した後も、試験を最後まで実行する）。docs/verification/W2-verifier.md"
))
def test_c1_the_tests_do_not_keep_running_after_a_timeout(repo, tmp, tmp_path):
    wait = tmp_path / "wait"
    proc = local_ci(
        repo, tmp, "--no-fetch", "--ref", "main", "--timeout", "2",
        env={"W2_WAIT": str(wait), "W2_WAIT_SECONDS": "3"},
    )
    assert proc.returncode == 1
    time.sleep(4)
    assert not Path(str(wait) + ".done").exists(), "時間切れの後も、試験が最後まで動いた"


# ================================================================ 基準2: 記録の中身

def test_c2_the_result_has_every_item_of_the_scope(repo, tmp):
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    [(path, data)] = results(repo)
    assert re.fullmatch(r"[0-9a-f]{40}", data["commit"]) and re.fullmatch(r"[0-9a-f]{40}", data["tree"])
    assert data["branch"] == "main" and data["dirty"] is False
    assert data["os"]["system"] == platform.system()
    assert data["python"]["version"] == platform.python_version()
    assert data["encoding"]["locale"] == locale.getencoding()
    counts = {k: data["tests"][k] for k in ("passed", "failed", "skipped")}
    assert counts == {"passed": 3, "failed": 0, "skipped": 0}
    assert isinstance(data["check"]["seconds"], (int, float)) and data["check"]["seconds"] >= 0
    assert (data["verdict"], data["failed"]) == ("pass", [])
    assert path.read_bytes().count(b"\r") == 0                  # 改行は LF


def test_c2_uncommitted_changes_are_marked_when_the_folder_itself_is_checked(repo, tmp):
    (repo / "notes.txt").write_text("new\n", encoding="utf-8")
    proc = local_ci(repo, tmp, "--current")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    [(_, data)] = results(repo)
    assert (data["mode"], data["dirty"], data["branch"]) == ("current", True, "main")
    assert data["tree"] != git(repo, "rev-parse", "HEAD^{tree}")


def test_c2_no_credentials_environment_or_test_output_in_the_result(repo, tmp, tmp_path):
    origin = tmp_path / "origin-W2CRED-MARKER.git"           # リモートの場所（URL）に、目印を入れる
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    env = {
        "W2_SECRET": "W2ENV-MARKER", "W2_FAIL": "1",
        "GITHUB_TOKEN": "ghp_W2TOKEN-MARKER", "ANTHROPIC_API_KEY": "sk-ant-W2KEY-MARKER",
    }
    proc = local_ci(repo, tmp, env=env)                     # 既定（リモートから取得して origin/main）
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "W2-FAILMSG-MARKER" in proc.stdout               # 失敗の文面は画面に流れる（記録には入らない）
    [(path, data)] = results(repo)
    text = path.read_text(encoding="utf-8")
    for marker in ("MARKER", "ghp_", "sk-ant-"):
        assert marker not in text
    values = list(strings(data))
    assert platform.node() not in values                    # PCの名前
    assert not [v for v in values if str(tmp_path) in v or str(Path.home()) in v]   # フォルダの場所
    assert data["failed"] == ["tests/test_product.py::test_env"]


NAMES = """\
import pytest


def test_fails():
    assert False


class TestGroup:
    @pytest.mark.parametrize("value", ["a", "b"])
    def test_param(self, value):
        assert value == "a"


@pytest.fixture
def broken():
    raise RuntimeError("W2-FIXTURE-MARKER")


def test_fixture(broken):
    pass


@pytest.mark.skip(reason="W2-SKIP-MARKER")
def test_skipped():
    pass
"""


def test_c2_names_of_the_failed_tests_are_in_the_result(repo, tmp):
    (repo / "tests" / "test_names.py").write_text(NAMES, encoding="utf-8")
    (repo / "tests" / "test_broken_module.py").write_text("def broken(:\n", encoding="utf-8")
    commit_all(repo, "落ちる試験")
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main")
    assert proc.returncode == 1
    [(path, data)] = results(repo)
    assert "MARKER" not in path.read_text(encoding="utf-8")
    assert data["verdict"] == "fail"
    assert any("test_broken_module" in name for name in data["failed"]), data["failed"]


def test_c2_names_of_failed_and_erroring_tests_when_modules_import(repo, tmp):
    (repo / "tests" / "test_names.py").write_text(NAMES, encoding="utf-8")
    commit_all(repo, "落ちる試験")
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main")
    assert proc.returncode == 1
    [(_, data)] = results(repo)
    assert sorted(data["failed"]) == [
        "tests/test_names.py::TestGroup::test_param[b]",
        "tests/test_names.py::test_fails",
        "tests/test_names.py::test_fixture",
    ]
    assert (data["tests"]["failed"], data["tests"]["errors"], data["tests"]["skipped"]) == (2, 1, 1)


# ================================================================ 基準3: 合否と、偽りの合格

@pytest.mark.parametrize("how, reason", [("test", "tests_failed"), ("lint", "check_failed")])
def test_c3_a_failed_check_ends_with_a_nonzero_code(repo, tmp, how, reason):
    if how == "test":
        (repo / "product.py").write_text("VALUE = 5\n", encoding="utf-8")
    else:
        (repo / "LINT_FAILS").write_text("", encoding="utf-8")
    commit_all(repo)
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main")
    assert proc.returncode == 1
    [(_, data)] = results(repo)
    assert (data["verdict"], data["reason"]) == ("fail", reason)


def test_c3_a_check_that_runs_no_tests_is_not_a_pass(repo, tmp):
    (repo / "scripts" / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    commit_all(repo, "試験を実行しない確認")
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main")
    assert proc.returncode == 1
    [(_, data)] = results(repo)
    assert data["verdict"] == "error"


def test_c3_a_result_of_another_commit_is_not_reused(repo, tmp, tmp_path):
    runs = tmp_path / "runs.txt"
    assert local_ci(repo, tmp, "--no-fetch", "--ref", "main", env={"W2_RUNS": str(runs)}).returncode == 0
    (repo / "product.py").write_text("VALUE = 5\n", encoding="utf-8")
    commit_all(repo, "落ちる")
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", "main", env={"W2_RUNS": str(runs)})
    assert proc.returncode == 1 and run_count(runs) == 2


def test_c3_a_result_of_the_folder_and_of_a_checkout_are_not_reused_for_each_other(repo, tmp, tmp_path):
    runs = tmp_path / "runs.txt"
    env = {"W2_RUNS": str(runs)}
    assert local_ci(repo, tmp, "--current", env=env).returncode == 0
    assert local_ci(repo, tmp, "--no-fetch", "--ref", "main", env=env).returncode == 0
    assert run_count(runs) == 2                             # 同じツリーでも、作業フォルダの結果は使い回さない
    assert local_ci(repo, tmp, "--no-fetch", "--ref", "main", env=env).returncode == 0
    assert local_ci(repo, tmp, "--current", env=env).returncode == 0
    assert run_count(runs) == 2                             # それぞれ、同じやり方の結果は使い回す


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(重大): 呼び出し元の PYTEST_ADDOPTS（-k、--deselect など）を引き継ぐので、一部の試験だけを"
    "実行した結果が合格として記録され、record-ci が取り込み、後の素の実行でも使い回される。"
    "docs/verification/W2-verifier.md"
))
def test_c3_a_run_with_only_some_tests_selected_does_not_make_a_pass(repo, tmp):
    assert cycle(repo, tmp, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    (wt / "product.py").write_text("VALUE = 5\n", encoding="utf-8")       # 確認の全体では落ちる内容
    head = commit_all(wt, "落ちる")
    selected = local_ci(
        repo, tmp, "--no-fetch", "--ref", head, env={"PYTEST_ADDOPTS": "-k test_env"}, windows=True,
    )
    for path, data in results(repo):
        if data["verdict"] == "pass":
            assert cycle(repo, tmp, "record-ci", "U1", "--result", str(path)).returncode != 0
    windows = json.loads((repo / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))
    assert windows.get("gates", {}).get("windows", {}).get("verdict") != "pass"
    assert selected.returncode in (0, 1, 2)
    plain = local_ci(repo, tmp, "--no-fetch", "--ref", head, windows=True)     # 利用者のオプション無し
    assert plain.returncode != 0


# ================================================================ 基準4: ハーネスとのつなぎ

def ready_unit(repo: Path, tmp: Path) -> tuple[Path, str]:
    """needs_windows: true の作業単位を、Windows のゲート以外は済ませて、コミットした状態にする。"""
    assert cycle(repo, tmp, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    (wt / "product.py").write_text("VALUE = 1\nEXTRA = 2\n", encoding="utf-8")
    recorded = cycle(repo, tmp, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-1")
    assert recorded.returncode == 0, recorded.stderr
    head = commit_all(wt, "実装")
    tree = cycle(repo, tmp, "snapshot", "U1").stdout.strip()
    assert tree == git(wt, "rev-parse", "HEAD^{tree}")
    for gate, by in (("review", "rev-1"), ("verify", "ver-1")):
        recorded = cycle(repo, tmp, "record", "U1", gate, "--verdict", "pass", "--by", by, "--tree", tree)
        assert recorded.returncode == 0, recorded.stderr
    checked = cycle(repo, tmp, "check", "U1")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    return wt, head


def windows_result(repo: Path, tmp: Path, ref: str, env: dict[str, str] | None = None) -> Path:
    proc = local_ci(repo, tmp, "--no-fetch", "--force", "--ref", ref, env=env, windows=True)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    path, data = results(repo)[-1]
    assert data["os"]["system"] == "Windows"
    return path


def merge(repo: Path, tmp: Path) -> subprocess.CompletedProcess:
    return cycle(repo, tmp, "merge", "U1")


def test_c4_without_a_windows_result_the_unit_cannot_be_merged(repo, tmp):
    ready_unit(repo, tmp)
    before = git(repo, "rev-parse", "main")
    refused = merge(repo, tmp)
    assert refused.returncode == 1 and "windows: まだ記録が無い" in refused.stdout
    assert git(repo, "rev-parse", "main") == before


def test_c4_local_ci_then_record_ci_then_merge(repo, tmp):
    wt, head = ready_unit(repo, tmp)
    path = windows_result(repo, tmp, head)
    recorded = cycle(repo, tmp, "record-ci", "U1", "--result", str(path))
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    merged = merge(repo, tmp)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert git(repo, "rev-parse", "main^2") == head
    assert git(repo, "rev-parse", "main^{tree}") == git(wt, "rev-parse", "HEAD^{tree}")


def test_c4_a_result_from_another_os_is_refused(repo, tmp):
    _, head = ready_unit(repo, tmp)
    proc = local_ci(repo, tmp, "--no-fetch", "--ref", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    [(path, data)] = results(repo)
    assert data["os"]["system"] != "Windows" or WINDOWS
    if WINDOWS:
        pytest.skip("この試験の OS が Windows なので、別のOSの結果を作れない")
    refused = cycle(repo, tmp, "record-ci", "U1", "--result", str(path))
    assert refused.returncode != 0 and "Windows での結果ではない" in refused.stderr
    assert merge(repo, tmp).returncode == 1


def test_c4_a_result_for_another_tree_is_refused(repo, tmp):
    _, head = ready_unit(repo, tmp)
    path = windows_result(repo, tmp, "main")                # 作業単位の前の内容（main）に対する結果
    refused = cycle(repo, tmp, "record-ci", "U1", "--result", str(path))
    assert refused.returncode != 0 and "一致しない" in refused.stderr
    state = json.loads((repo / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))
    assert "windows" not in state.get("gates", {})
    assert merge(repo, tmp).returncode == 1


def test_c4_a_windows_result_goes_stale_when_the_content_changes(repo, tmp):
    wt, head = ready_unit(repo, tmp)
    path = windows_result(repo, tmp, head)
    assert cycle(repo, tmp, "record-ci", "U1", "--result", str(path)).returncode == 0
    (wt / "product.py").write_text("VALUE = 1\nEXTRA = 3\n", encoding="utf-8")
    commit_all(wt, "結果の後の変更")
    assert cycle(repo, tmp, "check", "U1").returncode == 0
    refused = merge(repo, tmp)
    assert refused.returncode == 1 and "windows: 確認の後で変わったファイルがある" in refused.stdout


def test_c4_new_acceptance_files_after_the_windows_result_are_allowed(repo, tmp):
    wt, head = ready_unit(repo, tmp)
    path = windows_result(repo, tmp, head)
    assert cycle(repo, tmp, "record-ci", "U1", "--result", str(path)).returncode == 0
    (wt / "tests" / "acceptance").mkdir()
    (wt / "tests" / "acceptance" / "test_added.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    commit_all(wt, "検証担当の受け入れ試験")
    assert cycle(repo, tmp, "check", "U1").returncode == 0
    merged = merge(repo, tmp)
    assert merged.returncode == 0, merged.stdout + merged.stderr


def test_c4_a_failing_windows_result_blocks_and_a_later_pass_unblocks(repo, tmp):
    _, head = ready_unit(repo, tmp)
    failing = windows_result(repo, tmp, head, env={"W2_FAIL": "1"})
    assert json.loads(failing.read_text(encoding="utf-8"))["verdict"] == "fail"
    assert cycle(repo, tmp, "record-ci", "U1", "--result", str(failing)).returncode == 1
    refused = merge(repo, tmp)
    assert refused.returncode == 1 and "windows: fail" in refused.stdout
    passing = windows_result(repo, tmp, head)
    assert cycle(repo, tmp, "record-ci", "U1", "--result", str(passing)).returncode == 0
    merged = merge(repo, tmp)
    assert merged.returncode == 0, merged.stdout + merged.stderr


@pytest.mark.parametrize("gate_command", [
    ("record", "U1", "windows", "--verdict", "pass", "--by", "someone", "--tree", "HEAD"),
    ("run", "U1", "windows", "--dry-run"),
])
def test_c4_the_windows_gate_can_be_written_only_by_record_ci(repo, tmp, gate_command):
    ready_unit(repo, tmp)
    assert cycle(repo, tmp, *gate_command).returncode != 0
    state = json.loads((repo / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))
    assert "windows" not in state.get("gates", {})
    assert merge(repo, tmp).returncode == 1


def test_c4_needs_windows_is_required_also_when_the_gates_are_listed(repo, tmp):
    """見出しに gates: があっても、needs_windows: true なら windows のゲートが要る（壊す確認の生き残り）。"""
    spec = SPEC.replace("needs_windows: true\n", "needs_windows: true\ngates: implement, check\n")
    (repo / "docs" / "plan" / "U1-demo.md").write_text(spec, encoding="utf-8")
    commit_all(repo, "見出しに gates を書く")
    assert cycle(repo, tmp, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    (wt / "product.py").write_text("VALUE = 1\nEXTRA = 2\n", encoding="utf-8")
    recorded = cycle(repo, tmp, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-1")
    assert recorded.returncode == 0, recorded.stderr
    head = commit_all(wt, "実装")
    assert cycle(repo, tmp, "check", "U1").returncode == 0
    refused = merge(repo, tmp)
    assert refused.returncode == 1 and "windows: まだ記録が無い" in refused.stdout
    path = windows_result(repo, tmp, head)
    assert cycle(repo, tmp, "record-ci", "U1", "--result", str(path)).returncode == 0
    merged = merge(repo, tmp)
    assert merged.returncode == 0, merged.stdout + merged.stderr


# ================================================================ 基準6: ツリーIDの求め方（決まり9）

def test_c6_tree_id_matches_the_commit_where_the_executable_mark_is_not_trusted(repo, tmp):
    """core.fileMode=false（Windows の既定）で、実行できる印がディスクに無くても、ツリーIDはコミットと同じ。

    印を信用しないことで、中身の変更まで見逃すようにはなっていない（変えれば、IDが変わる）。
    """
    assert cycle(repo, tmp, "start", "U1").returncode == 0
    wt = repo.parent / "wt" / "U1"
    git(repo, "config", "core.fileMode", "false")
    (wt / "scripts" / "check.py").chmod(0o644)           # Windows で取り出したときと同じく、印が無い
    snapshot = cycle(repo, tmp, "snapshot", "U1")
    assert snapshot.returncode == 0, snapshot.stderr
    assert snapshot.stdout.strip() == git(wt, "rev-parse", "HEAD^{tree}")
    (wt / "scripts" / "check.py").write_text(CHECK + "# 変更\n", encoding="utf-8")
    changed = cycle(repo, tmp, "snapshot", "U1").stdout.strip()
    assert changed != git(wt, "rev-parse", "HEAD^{tree}")
