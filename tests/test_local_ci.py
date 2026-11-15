"""手元の確認（scripts/local_ci.py）の試験。

このリポジトリの本物の確認（scripts/check.py → pytest）は起動しない。一時リポジトリに、振る舞いを指定できる
試験用の確認スクリプトを置く。本物の ruff と pytest を通す試験は、一時リポジトリの小さな試験だけを対象にする。
どの試験も、決まりを1つ外すと落ちるように書く。
"""
import codecs
import contextlib
import importlib.util
import io
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath
from unittest import mock

import pytest
from test_harness import (
    TEST_ENV,
    WINDOWS,
    commit,
    cycle,
    default_stop_signals,
    implement,
    install_hook,
    make_repo,
    only_posix,
    only_windows,
    set_header,
    sh,
    state,
    users_git_config,
    wait_for,
    write_text_like_windows,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_ci.py"
SECRET = "SECRET-7f3a"          # 結果のファイルにも、local_ci 自身の表示にも、出てはいけない文字列の目印


def load_local_ci():
    spec = importlib.util.spec_from_file_location("local_ci_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOCAL_CI = load_local_ci()
HARNESS = LOCAL_CI.cycle        # local_ci.py が使っている、ハーネス（scripts/cycle.py）のモジュール

# 試験用の確認スクリプト。リポジトリの plan.json（コミットされた内容）に書かれたとおりに振る舞う:
#   junit: 試験ごとの結果として書く XML（無ければ書かない）、write: 実行中に書くファイル（名前と中身）、
#   sleep: 止まる秒数、exit: 終了コード（環境変数 CHECK_EXIT があれば、そちら）、
#   child: 確認の先で動くプロセス（pytest の代わり）を起動し、その秒数の後に NOTES.child の印を置かせる、
#   import_product: 別の場所から起動した Python が、product をどこから読むかを控える
# 環境変数 NOTES のファイルに、実行のたびに、実行した場所や受け取ったものを控える。
# 最後まで進んだら、NOTES に .finished を付けた名前のファイルを置く。
PLANNED_CHECK = """\
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

plan = json.loads(pathlib.Path("plan.json").read_text(encoding="utf-8"))
if plan.get("child"):
    later = "import pathlib, sys, time; time.sleep(float(sys.argv[1])); pathlib.Path(sys.argv[2]).touch()"
    subprocess.Popen([sys.executable, "-c", later, str(plan["child"]), os.environ["NOTES"] + ".child"])
options = shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
junit = [option.split("=", 1)[1] for option in options if option.startswith("--junitxml=")]
note = {
    "cwd": os.getcwd(), "files": sorted(os.listdir(".")), "options": options, "junit": junit,
    "stdin": sys.stdin.readline() if os.environ.get("READ_STDIN") else None,
    "guard": os.environ.get("CIRCUIT_SIM_PYTEST_RUNNING"),
    "pytest_settings": sorted(name for name in os.environ if name.startswith("PYTEST_")),
    "pythonpath": os.environ.get("PYTHONPATH", "").split(os.pathsep),
}
if plan.get("import_product"):
    # 場所は JSON の ASCII で受け渡す。起動した Python の出力の文字コード（PYTHONIOENCODING）と、ここで読む
    # 既定の文字コード（日本語 Windows では cp932）が違っても、日本語の場所が化けない
    where = "import json, product; print(json.dumps(product.__file__))"
    elsewhere = os.path.dirname(os.environ["NOTES"])
    found = subprocess.run(
        [sys.executable, "-B", "-c", where], cwd=elsewhere, capture_output=True, encoding="ascii",
    )
    note["product"] = json.loads(found.stdout)
notes = pathlib.Path(os.environ["NOTES"])
runs = json.loads(notes.read_text(encoding="utf-8")) if notes.exists() else []
notes.write_text(json.dumps(runs + [note]), encoding="utf-8")
print("check output " + os.environ.get("SOME_VARIABLE", ""))
if plan.get("junit") is not None and junit:      # ハーネスの check からは、書く先を渡されない
    pathlib.Path(junit[-1]).write_text(plan["junit"], encoding="utf-8")     # pytest と同じく、後のほう
for name, text in plan.get("write", {}).items():
    pathlib.Path(name).write_text(text, encoding="utf-8")
time.sleep(plan.get("sleep", 0))
pathlib.Path(os.environ["NOTES"] + ".finished").touch()
sys.exit(int(os.environ.get("CHECK_EXIT", plan.get("exit", 0))))
"""


def junit(*cases: tuple[str, str]) -> str:
    """試験ごとの結果（JUnit の形）。cases は（名前、結果）。結果は passed、failure、error、skipped。"""
    items = []
    for name, outcome in cases:
        inner = "" if outcome == "passed" else f'<{outcome} message="{SECRET}">{SECRET}</{outcome}>'
        where = 'classname="tests.test_sample" file="tests/test_sample.py"'
        items.append(f'<testcase {where} name="{name}">{inner}</testcase>')
    return f'<testsuites><testsuite name="pytest">{"".join(items)}</testsuite></testsuites>'


PASSING = junit(("test_a", "passed"), ("test_b", "passed"), ("test_c", "skipped"))


def set_plan(root: Path, **plan: object) -> str:
    """確認スクリプトの振る舞いを書いて、コミットする。コミットIDを返す。"""
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    commit(root, "plan")
    return sh(root, "git", "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """試験用の確認スクリプトを持つリポジトリ。コミットされた内容は、確認を通る。"""
    root = make_repo(tmp_path)
    (root / "scripts" / "check.py").write_text(PLANNED_CHECK, encoding="utf-8")
    (root / ".gitignore").write_text(".cycle/\n.venv/\n", encoding="utf-8")
    set_plan(root, junit=PASSING)
    return root


def work_dir(root: Path) -> Path:
    """local_ci.py が作業用の場所を作る先。名前に空白と日本語を入れて、場所の受け渡しも確かめる。"""
    path = root.parent / "作業 用"
    path.mkdir(exist_ok=True)
    return path


def ci_env(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """local_ci.py を動かすときの環境変数。

    この試験の全体が、外側の local_ci.py の下で走っていることがある（POのPCでの確認が、その形）。
    外側が pytest に渡したオプション（試験ごとの結果を書く先）は、引き継がない。
    """
    work = str(work_dir(root))
    base = {key: value for key, value in TEST_ENV.items() if key != "PYTEST_ADDOPTS"}
    return {
        **base, "CYCLE_ROOT": str(root), "TMPDIR": work, "TEMP": work, "TMP": work,
        "NOTES": str(root.parent / "notes.json"), **(extra or {}),
    }


def local_ci(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """local_ci.py を、試験のプロセスの中で実行する（環境変数と一時フォルダの場所は、その間だけ差し替える）。

    確認スクリプトの出力は、local_ci.py が受け取らずに画面へ流すので、ここで返す出力には入らない。
    """
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.dict(os.environ, ci_env(root, env), clear=True),
        mock.patch.object(tempfile, "tempdir", None),       # 一時フォルダの場所を、環境変数から決め直させる
        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
    ):
        try:
            code = LOCAL_CI.main(list(args))
        except SystemExit as exc:           # argparse が引数の誤りで終わる場合
            code = exc.code
    return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())


def start_local_ci(root: Path, *args: str, env: dict[str, str] | None = None, **options) -> subprocess.Popen:
    """local_ci.py をコマンドラインから起動する。起動するのは、リポジトリの外の空の場所から。"""
    elsewhere = root.parent / "started-here"
    elsewhere.mkdir(exist_ok=True)
    if WINDOWS:     # Ctrl-Break の合図を、このプロセスだけに送れるようにする
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:           # 呼び出し元が終了の合図を無視していても（nohup の SIGHUP など）、既定の扱いに戻す
        options.setdefault("preexec_fn", default_stop_signals)
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=ci_env(root, env), cwd=elsewhere, **options,
    )


def run_local_ci(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    proc = start_local_ci(root, *args, env=env)
    out, err = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def result_files(root: Path) -> list[Path]:
    """結果のファイル（古い順）。.cycle/ci/ にあるものは、すべて数える。"""
    folder = root / ".cycle" / "ci"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def results(root: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in result_files(root)]


def notes(root: Path) -> list[dict]:
    """試験用の確認スクリプトが、実行のたびに控えたもの。"""
    path = root.parent / "notes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def worktrees(root: Path) -> list[str]:
    lines = sh(root, "git", "worktree", "list", "--porcelain").splitlines()
    return [line for line in lines if line.startswith("worktree ")]


def assert_cleaned_up(root: Path) -> None:
    """作業用の場所が残っていない。git の作業ツリーの登録も、本体の1つだけに戻っている。"""
    assert list(work_dir(root).iterdir()) == []
    assert len(worktrees(root)) == 1


def head(root: Path) -> tuple[str, str]:
    return sh(root, "git", "rev-parse", "HEAD"), sh(root, "git", "rev-parse", "HEAD^{tree}")


# ------------------------------------------------------------ 取り出して実行

def test_checks_the_commit_in_a_clean_place_whatever_the_working_folder_holds(repo):
    commit_id, tree = head(repo)
    # 作業中のフォルダ: 未コミットの変更（失敗する内容、壊れた確認スクリプト）、新しいファイル、無視の対象
    (repo / "plan.json").write_text(json.dumps({"exit": 1}), encoding="utf-8")
    (repo / "scripts" / "check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("v\n", encoding="utf-8")
    before = sh(repo, "git", "status", "--porcelain")

    result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    [written] = results(repo)
    assert (written["commit"], written["tree"]) == (commit_id, tree)
    assert (written["mode"], written["ref"], written["branch"], written["dirty"]) == (
        "checkout", "main", "main", False,
    )
    assert (written["verdict"], written["reason"]) == ("pass", "ok")
    # 確認は、作業用の場所（リポジトリの外）で走った。そこにあるのは、コミットされた内容だけ
    [note] = notes(repo)
    place = Path(note["cwd"])
    assert place.name == "checkout" and place.parent.parent == HARNESS.real_path(work_dir(repo))
    assert place.parent.name.startswith("circuit-sim-ci-")
    assert note["files"] == [".git", ".gitignore", "docs", "plan.json", "product.py", "scripts"]
    # 作業中のフォルダには触っていない。作業用の場所は片付いている
    assert sh(repo, "git", "status", "--porcelain") == before
    assert (repo / ".venv" / "marker").exists() and (repo / "untracked.txt").exists()
    assert_cleaned_up(repo)
    assert "結果: 合格（確認が通った）" in result.stdout
    assert f"main のコミット {commit_id[:10]}、ツリー {tree[:10]}" in result.stdout


def test_check_gets_the_report_file_outside_the_checked_content_and_no_input(repo):
    """試験ごとの結果を書かせる先は、確かめる内容の外。呼び出し元の入力は渡さない。試験の入れ子の印は消さない。"""
    if not WINDOWS:
        # 作業用の場所への道に、リンクを挟む（Windows の一時フォルダの、短い名前の代わり）。
        # 場所は、たどった先で扱う。git に渡す場所と、pytest に渡す場所が、同じ書き方になる
        (repo.parent / "実際の場所").mkdir()
        (repo.parent / "作業 用").symlink_to(repo.parent / "実際の場所", target_is_directory=True)
    env = {"CIRCUIT_SIM_PYTEST_RUNNING": "1", "READ_STDIN": "1"}
    proc = start_local_ci(repo, "--no-fetch", "--ref", "main", env=env, stdin=subprocess.PIPE)
    out, err = proc.communicate("input of the caller\n", timeout=120)
    assert proc.returncode == 0, out + err
    [note] = notes(repo)
    place = Path(note["cwd"])
    report = (place.parent / "junit.xml").as_posix()                    # 取り出した内容の、外
    assert note["options"] == [f"--junitxml={report}", "-o", "junit_family=xunit1"]
    assert note["stdin"] == ""
    assert note["guard"] == "1"
    assert "check output" in out                                         # 確認の出力は、そのまま画面に流れる
    assert_cleaned_up(repo)


CALLERS_PYTEST_SETTINGS = {
    "試験を名前で選ぶ（-k）": {"PYTEST_ADDOPTS": "-k test_skipped"},
    "落ちる試験を外す（--deselect。外さなければ合格になる）": {
        "PYTEST_ADDOPTS": "--deselect tests/test_sample.py::TestGroup"
        " --deselect 'tests/test_sample.py::test_param[a b]'"
        " --deselect tests/test_sample.py::test_with_fixture",
    },
    "ファイルを外す（--ignore）、前に失敗した試験だけ（--lf）、印で選ぶ（-m）": {
        "PYTEST_ADDOPTS": "--ignore=tests/test_sample.py --lf -m slow",
    },
    "試験を選び直す差し込み（PYTEST_PLUGINS）": {"PYTEST_PLUGINS": "chooser"},
}

# PYTEST_PLUGINS で読み込ませる差し込み。スキップの試験だけを残す
CHOOSER = """\
def pytest_collection_modifyitems(config, items):
    items[:] = [item for item in items if item.name == "test_skipped"]
"""


@pytest.mark.parametrize("case", CALLERS_PYTEST_SETTINGS)
def test_callers_pytest_settings_do_not_choose_the_tests(tmp_path, case):
    """呼び出し元の pytest の環境変数（PYTEST_ADDOPTS、PYTEST_PLUGINS など）は、確認に渡さない。

    渡すと、全体では落ちるコミットで、一部の試験だけを実行した結果が合格になり、取り込まれ、使い回される。
    本物の pytest で確かめる。渡すオプションは、試験ごとの結果を書かせるものだけ。
    """
    root = make_repo(tmp_path)
    shutil.copy(ROOT / "scripts" / "check.py", root / "scripts" / "check.py")
    (root / "tests").mkdir()
    (root / "tests" / "test_sample.py").write_text(SAMPLE_TESTS, encoding="utf-8")
    ignored = ".cycle/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n"
    (root / ".gitignore").write_text(ignored, encoding="utf-8")
    commit(root, "tests")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "chooser.py").write_text(CHOOSER, encoding="utf-8")
    env = {"BREAK": "1", "PYTHONPATH": str(plugins), **CALLERS_PYTEST_SETTINGS[case]}
    failed = run_local_ci(root, "--no-fetch", "--ref", "main", env=env)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    [written] = results(root)
    assert (written["verdict"], written["reason"]) == ("fail", "tests_failed")
    assert written["tests"] == {"total": 5, "passed": 1, "failed": 2, "errors": 1, "skipped": 1}
    # 同じ設定のままでも、素の実行でも、使い回すのは、この不合格
    assert run_local_ci(root, "--no-fetch", "--ref", "main", env={"BREAK": "1"}).returncode == 1
    assert len(results(root)) == 1


@pytest.mark.parametrize("output_encoding", ["utf-8", "cp932"])
def test_python_started_elsewhere_reads_the_checked_content_first(repo, output_encoding):
    """pip install -e . の環境では、作業フォルダが、どこから起動した Python にも読み込まれる。

    試験が別の場所から起動した Python も、確かめる場所の版を先に読む（Python が読み込む場所の先頭に置く）。
    ここでは、作業フォルダを PYTHONPATH に入れて、入れた状態の代わりにする。
    作業用の場所には、日本語と空白が入る（work_dir）。起動した Python の出力の文字コードが、試験用の確認
    スクリプトの既定の文字コードと違っても、場所が化けずに届くこと（POのPC: 出力は UTF-8、既定は cp932。
    Linux では、出力を cp932 にして、同じ食い違いを逆向きに作る）。
    """
    set_plan(repo, junit=PASSING, import_product=True)
    (repo / "product.py").write_text("x = 99\n", encoding="utf-8")       # 作業フォルダの、未コミットの変更
    env = {"PYTHONPATH": str(repo), "PYTHONIOENCODING": output_encoding}
    result = local_ci(repo, "--no-fetch", "--ref", "main", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    [note] = notes(repo)
    place = Path(note["cwd"])
    assert Path(note["product"]).resolve() == (place / "product.py").resolve()
    assert [Path(entry) for entry in note["pythonpath"]] == [place, repo]   # 呼び出し元の値は、後に続く


def test_local_ci_turns_the_error_dialogs_off_first(repo):
    """local_ci.py は、git や確認を起動する前に、Windows のエラー画面を出さない設定にする。"""
    order = []
    run_git = HARNESS.run_git

    def noted_git(*args, **kwargs):
        order.append("git")
        return run_git(*args, **kwargs)

    with (
        mock.patch.object(HARNESS, "quiet_error_dialogs", lambda: order.append("quiet")),
        mock.patch.object(HARNESS, "run_git", noted_git),
    ):
        assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    assert order[0] == "quiet" and "git" in order


def test_report_file_option_survives_the_way_pytest_splits_it_on_windows():
    """pytest は、環境変数のオプションを、どのOSでもシェルの決まりで分ける。Windows のパスが崩れないこと。"""
    junit_file = PureWindowsPath(r"C:\Users\名前 x\AppData\Local\Temp\circuit-sim-ci-1\junit.xml")
    assert shlex.split(LOCAL_CI.junit_options(junit_file)) == [
        "--junitxml=C:/Users/名前 x/AppData/Local/Temp/circuit-sim-ci-1/junit.xml",
        "-o", "junit_family=xunit1",
    ]


@pytest.fixture
def with_origin(repo: Path) -> tuple[Path, Path]:
    """リモート（origin）を持つリポジトリと、origin に push できる別の作業場所。"""
    origin = repo.parent / "origin.git"
    sh(repo.parent, "git", "clone", "-q", "--bare", str(repo), str(origin))
    sh(repo, "git", "remote", "add", "origin", str(origin))
    sh(repo, "git", "fetch", "-q", "origin")
    other = repo.parent / "other"
    sh(repo.parent, "git", "clone", "-q", str(origin), str(other))
    return repo, other


@pytest.mark.parametrize("env", [{}, {"GIT_NAMESPACE": "elsewhere"}], ids=["そのまま", "GIT_NAMESPACE"])
def test_default_is_the_latest_main_of_the_remote(with_origin, env):
    """呼び出し元の GIT_NAMESPACE は、取得に持ち込まない（持ち込むと、手元のリモートからの取得が、別の名前の
    範囲の参照だけを見て、何も取得せずに 0 で終わる。古い main を「最新」として確かめてしまう）。"""
    repo, other = with_origin
    old, _ = head(repo)
    # リモートの main が、確認の失敗する内容に進む
    new = set_plan(other, junit=junit(("test_a", "failure")), exit=1)
    sh(other, "git", "push", "-q", "origin", "main")

    result = local_ci(repo, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    [written] = results(repo)
    assert written["commit"] == new != old
    assert (written["ref"], written["branch"], written["verdict"]) == ("origin/main", "origin/main", "fail")
    assert sh(repo, "git", "rev-parse", "HEAD") == old                  # 作業中のフォルダは、進めていない
    assert_cleaned_up(repo)


def test_without_fetching_the_commit_at_hand_is_checked(with_origin):
    repo, other = with_origin
    old, _ = head(repo)
    set_plan(other, exit=1)
    sh(other, "git", "push", "-q", "origin", "main")
    result = local_ci(repo, "--no-fetch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert results(repo)[0]["commit"] == old


def test_nothing_is_checked_when_the_remote_cannot_be_fetched(repo):
    """取得できなければ、古い内容を確かめずに止まる。git の出力（リモートのURL）は表示しない。"""
    sh(repo, "git", "remote", "add", "origin", str(repo.parent / SECRET / "missing.git"))
    sh(repo, "git", "update-ref", "refs/remotes/origin/main", "HEAD")       # 前に取得した内容は、手元にある
    result = local_ci(repo)
    assert result.returncode == 2
    assert "最新を取得できなかった（終了コード " in result.stderr and "--no-fetch" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    assert results(repo) == [] and notes(repo) == []
    assert_cleaned_up(repo)


def test_fetch_cannot_wait_for_input_or_run_forever(repo):
    """取得は、端末での入力を求めず、時間で打ち切る。呼び出し元の GIT_DIR などは持ち込まない。"""
    calls = []

    def never_ends(cmd, cwd, timeout, env=None):
        calls.append((cmd, cwd, timeout, env))
        raise subprocess.TimeoutExpired(cmd, timeout)

    with mock.patch.object(HARNESS, "run_captured", never_ends):
        result = local_ci(repo, env={"GIT_DIR": str(repo.parent / "elsewhere")})
    assert result.returncode == 2
    assert "最新を取得できなかった（時間内に終わらなかった）" in result.stderr
    [(cmd, cwd, timeout, env)] = calls
    # git は、名前ではなく、探した場所（絶対パス）で起動する（実行した場所の git を使わない）
    assert Path(cmd[0]).name in ("git", "git.exe") and Path(cmd[0]).is_absolute()
    assert cmd[1] == "-C" and cmd[3:] == ["fetch", "--quiet", "origin"]
    assert Path(cmd[2]).resolve() == repo.resolve() == Path(cwd).resolve()
    assert timeout == 300
    assert env["GIT_TERMINAL_PROMPT"] == "0" and "GIT_DIR" not in env and env["CYCLE_ROOT"] == str(repo)
    assert results(repo) == [] and notes(repo) == []


@pytest.mark.parametrize("ref", ["no-such-branch", "--all", "0" * 40, "HEAD^{tree}"])
def test_name_that_is_not_a_commit_is_refused(repo, ref):
    result = local_ci(repo, "--no-fetch", f"--ref={ref}")
    assert result.returncode == 2
    assert "このリポジトリのコミットとして読めない" in result.stderr
    assert results(repo) == [] and notes(repo) == []
    assert_cleaned_up(repo)


def test_commit_can_be_named_by_branch_tag_or_id(repo):
    first, _ = head(repo)
    sh(repo, "git", "tag", "v1")
    sh(repo, "git", "branch", "side")
    second = set_plan(repo, junit=PASSING, note="second")
    for ref, commit_id, branch in (
        ("side", first, "side"), ("v1", first, None), (first, first, None), ("HEAD", second, "main"),
    ):
        result = local_ci(repo, "--no-fetch", "--force", "--ref", ref)
        assert result.returncode == 0, result.stdout + result.stderr
        written = results(repo)[-1]
        assert (written["ref"], written["commit"], written["branch"]) == (ref, commit_id, branch)


def test_checkout_that_differs_from_the_commit_is_not_checked(repo):
    """取り出した内容がコミットと違えば（改行の変換など）、確認を実行せず、合格にもしない。"""
    real = HARNESS.git

    def add_then_alter(cwd, *args, **kwargs):
        output = real(cwd, *args, **kwargs)
        if args[:2] == ("worktree", "add"):
            (Path(args[3]) / "product.py").write_text("x = 99\n", encoding="utf-8")
        return output

    with mock.patch.object(HARNESS, "git", add_then_alter):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 1
    [written] = results(repo)
    assert (written["verdict"], written["reason"]) == ("error", "content_mismatch")
    assert written["changed"] == ["product.py"] and written["check"]["returncode"] is None
    assert notes(repo) == []                                # 確認は実行していない
    assert "結果を得られなかった" in result.stdout and "違い  : product.py" in result.stdout
    assert_cleaned_up(repo)


# ------------------------------------------------------------ 記録の中身

def test_result_file_says_what_was_checked_where_and_how_it_went(repo):
    commit_id, tree = head(repo)
    with write_text_like_windows():
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    [path] = result_files(repo)
    system = platform.system()
    assert re.fullmatch(rf"\d{{8}}T\d{{12}}Z-{system.lower()}-{commit_id[:10]}\.json", path.name)
    data = path.read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n") and data.count(b"\n") > 20
    written = json.loads(data)
    assert list(written) == [
        "schema", "kind", "at", "mode", "ref", "commit", "tree", "branch", "dirty", "os", "python",
        "encoding", "check", "tests", "failed", "changed", "verdict", "reason",
    ]
    assert (written["schema"], written["kind"]) == (1, "local-ci")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", written["at"])
    assert written["os"]["system"] == system and written["os"]["release"] == platform.release()
    assert written["python"]["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert set(written["encoding"]) == {"stdout", "locale", "filesystem", "utf8_mode"}
    check = written["check"]
    assert (check["script"], check["returncode"], check["timed_out"]) == ("scripts/check.py", 0, False)
    assert isinstance(check["seconds"], float) and check["seconds"] >= 0
    assert written["tests"] == {"total": 3, "passed": 2, "failed": 0, "errors": 0, "skipped": 1}
    assert written["failed"] == [] and written["changed"] == []
    assert HARNESS.ci_result_problem(written) == ""         # ハーネスが取り込める形
    assert "試験  : 通過 2、失敗 0、エラー 0、スキップ 1" in result.stdout
    assert f"記録  : .cycle/ci/{path.name}" in result.stdout


SAMPLE_TESTS = """\
import os

import pytest


class TestGroup:
    def test_in_class(self):
        assert os.environ.get("BREAK") is None, "SECRET-7f3a in the message"


@pytest.mark.parametrize("value", ["a b", "c"])
def test_param(value):
    print("SECRET-7f3a printed")
    assert os.environ.get("BREAK") is None or value == "c"


@pytest.mark.skip(reason="not on this OS")
def test_skipped():
    pass


@pytest.fixture
def broken():
    if os.environ.get("BREAK"):
        raise RuntimeError("SECRET-7f3a in the fixture")


def test_with_fixture(broken):
    pass
"""


def test_counts_and_failed_test_names_come_from_real_pytest_and_output_is_not_recorded(tmp_path):
    """本物の確認スクリプト（ruff と pytest）を、一時リポジトリの小さな試験に対して通す。

    結果に入るのは、件数と、失敗した試験の名前（ノードID）だけ。失敗の文面、試験の出力、環境変数の中身は入らない。
    """
    root = make_repo(tmp_path)
    shutil.copy(ROOT / "scripts" / "check.py", root / "scripts" / "check.py")
    (root / "tests").mkdir()
    (root / "tests" / "test_sample.py").write_text(SAMPLE_TESTS, encoding="utf-8")
    ignored = ".cycle/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n"
    (root / ".gitignore").write_text(ignored, encoding="utf-8")
    commit(root, "tests")

    passed = run_local_ci(root, "--no-fetch", "--ref", "main")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    [written] = results(root)
    assert (written["verdict"], written["reason"]) == ("pass", "ok")
    assert written["tests"] == {"total": 5, "passed": 4, "failed": 0, "errors": 0, "skipped": 1}
    assert "4 passed, 1 skipped" in passed.stdout               # pytest の出力は、画面に流れる

    env = {"BREAK": "1", "SOME_VARIABLE": SECRET, "ANTHROPIC_API_KEY": f"sk-{SECRET}"}
    failed = run_local_ci(root, "--no-fetch", "--ref", "main", "--force", env=env)
    assert failed.returncode == 1
    written = results(root)[-1]
    assert (written["verdict"], written["reason"]) == ("fail", "tests_failed")
    assert written["check"]["returncode"] == 1
    assert written["tests"] == {"total": 5, "passed": 1, "failed": 2, "errors": 1, "skipped": 1}
    assert written["failed"] == [
        "tests/test_sample.py::TestGroup::test_in_class",
        "tests/test_sample.py::test_param[a b]",
        "tests/test_sample.py::test_with_fixture",
    ]
    assert SECRET in failed.stdout                              # 画面には、pytest の出力として出ている
    for path in result_files(root):
        assert SECRET not in path.read_text(encoding="utf-8")
    # local_ci.py 自身の表示（結果の要約）にも入らない
    summary = failed.stdout[failed.stdout.index("結果: 不合格（失敗した試験がある）"):]
    assert SECRET not in summary and "失敗  : tests/test_sample.py::test_param[a b]" in summary
    # 結果のファイルのほかに、何も残していない
    assert [path.name for path in (root / ".cycle").iterdir()] == ["ci"] and len(result_files(root)) == 2
    assert_cleaned_up(root)


# ------------------------------------------------------------ 合否

NOT_A_PASS = {
    "試験が失敗した": (
        {"exit": 1, "junit": junit(("test_a", "passed"), ("test_b", "failure"))}, "fail", "tests_failed",
    ),
    "試験の結果が無いまま失敗した（ruff の指摘など）": ({"exit": 1}, "fail", "check_failed"),
    "試験は通ったと書かれているが、終了コードが 0 でない": (
        {"exit": 3, "junit": PASSING}, "fail", "check_failed",
    ),
    "終了コードは 0 だが、試験の結果が無い": ({"exit": 0}, "error", "no_test_results"),
    "終了コードは 0 だが、試験の結果を読めない": (
        {"exit": 0, "junit": "<testsuites><broken"}, "error", "no_test_results",
    ),
    "終了コードは 0 だが、失敗した試験がある": (
        {"exit": 0, "junit": junit(("test_a", "passed"), ("test_b", "failure"))},
        "error", "test_results_disagree",
    ),
    "終了コードは 0 だが、エラーの試験がある": (
        {"exit": 0, "junit": junit(("test_a", "passed"), ("test_b", "error"))},
        "error", "test_results_disagree",
    ),
    "終了コードは 0 だが、試験が1件も無い": ({"exit": 0, "junit": junit()}, "error", "test_results_disagree"),
    "終了コードは 0 だが、通過した試験が無い": (
        {"exit": 0, "junit": junit(("test_a", "skipped"))}, "error", "test_results_disagree",
    ),
    "確認が、新しいファイルを残した": (
        {"exit": 0, "junit": PASSING, "write": {"leftover.txt": "x\n"}}, "error", "content_changed",
    ),
    "確認が、ファイルを書き換えた": (
        {"exit": 0, "junit": PASSING, "write": {"product.py": "x = 99\n"}}, "error", "content_changed",
    ),
}


@pytest.mark.parametrize("case", NOT_A_PASS)
def test_run_with_a_failure_a_gap_or_a_contradiction_is_not_a_pass(repo, case):
    plan, verdict, reason = NOT_A_PASS[case]
    set_plan(repo, **plan)
    result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 1, result.stdout + result.stderr
    [written] = results(repo)
    assert (written["verdict"], written["reason"]) == (verdict, reason)
    assert written["check"]["returncode"] == plan["exit"]
    assert written["changed"] == sorted(plan.get("write", {}))
    assert HARNESS.ci_result_problem(written) == ""
    assert len(notes(repo)) == 1
    assert "結果: 合格" not in result.stdout
    assert SECRET not in json.dumps(written) and SECRET not in result.stdout
    assert_cleaned_up(repo)


def test_names_of_changed_files_are_limited_in_the_result(repo):
    set_plan(repo, junit=PASSING, write={f"left-{number:02}.txt": "x\n" for number in range(25)})
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 1
    [written] = results(repo)
    assert written["reason"] == "content_changed"
    assert written["changed"] == [f"left-{number:02}.txt" for number in range(20)]


def test_failed_tests_are_counted_and_named_from_the_report(repo):
    cases = (("test_a", "passed"), ("test_b", "failure"), ("test_c", "error"), ("test_d", "skipped"))
    set_plan(repo, exit=1, junit=junit(*cases))
    result = local_ci(repo, "--no-fetch", "--ref", "main")
    [written] = results(repo)
    assert written["tests"] == {"total": 4, "passed": 1, "failed": 1, "errors": 1, "skipped": 1}
    assert written["failed"] == ["tests/test_sample.py::test_b", "tests/test_sample.py::test_c"]
    assert "  失敗  : tests/test_sample.py::test_b\n  失敗  : tests/test_sample.py::test_c\n" in result.stdout


NAMES = {
    "関数": (
        {"file": "tests/test_a.py", "classname": "tests.test_a", "name": "test_x"}, "tests/test_a.py::test_x",
    ),
    "クラスの中": (
        {"file": "tests/test_a.py", "classname": "tests.test_a.TestOne.TestTwo", "name": "test_x[1]"},
        "tests/test_a.py::TestOne::TestTwo::test_x[1]",
    ),
    "Windows の区切り": (
        {"file": "tests\\sub\\test_a.py", "classname": "tests.sub.test_a.TestOne", "name": "test_x"},
        "tests/sub/test_a.py::TestOne::test_x",
    ),
    "名前の似た別のモジュール": (
        {"file": "tests/test_a.py", "classname": "tests.test_ab", "name": "test_x"},
        "tests/test_a.py::test_x",
    ),
    "ファイル名が無い（試験を集める段階のエラー）": (
        {"classname": "", "name": "tests.test_broken"}, "tests.test_broken",
    ),
    "ファイル名が無く、クラスの名前がある": (
        {"classname": "tests.test_a", "name": "test_x"}, "tests.test_a::test_x",
    ),
}


@pytest.mark.parametrize("case", NAMES)
def test_test_name_is_built_from_the_report_items(case):
    attributes, expected = NAMES[case]
    element = LOCAL_CI.ElementTree.Element("testcase", attributes)
    assert LOCAL_CI.node_id(element) == expected


def test_check_that_does_not_finish_is_stopped_and_is_not_a_pass(repo):
    """時間切れでは、確認スクリプトと、その先のプロセス（pytest の代わり）を止める。"""
    set_plan(repo, junit=PASSING, sleep=30, child=2)
    started = time.monotonic()
    result = local_ci(repo, "--no-fetch", "--ref", "main", "--timeout", "1")
    assert time.monotonic() - started < 20
    time.sleep(3)
    assert not (repo.parent / "notes.json.child").exists(), "時間切れの後も、確認の先のプロセスが動いていた"
    assert result.returncode == 1
    [written] = results(repo)
    assert (written["verdict"], written["reason"]) == ("error", "timeout")
    assert written["check"]["timed_out"] is True and written["check"]["returncode"] is None
    assert written["tests"] is None                 # 途中まで書かれた結果は、読まない
    assert "時間内に終わらなかった" in result.stdout
    assert_cleaned_up(repo)


def test_result_that_cannot_be_written_is_not_a_pass(repo):
    def refused(source, target):
        raise PermissionError(13, "書けない")

    with mock.patch.object(HARNESS, "BUSY_WAIT_SECONDS", 0.2), mock.patch.object(os, "replace", refused):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 2
    assert "結果を" in result.stderr and "に書けない" in result.stderr
    assert result_files(repo) == []
    assert_cleaned_up(repo)


def test_git_that_cannot_be_started_is_an_error_not_a_crash(repo):
    with mock.patch.object(HARNESS, "run_git", side_effect=FileNotFoundError("git")):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 2 and result.stderr.startswith("エラー: ")
    assert results(repo) == [] and notes(repo) == []


def test_result_write_waits_while_another_process_has_the_file_open(repo):
    """Windows では、他のプロセスが開いている間は置き換えが拒まれる。待ってやり直す。"""
    replace, calls = os.replace, []

    def busy_twice(source, target):
        calls.append(target)
        if len(calls) <= 2:
            raise PermissionError(13, "他のプロセスが使っている")
        replace(source, target)

    with mock.patch.object(HARNESS, "BUSY_WAIT_SECONDS", 5.0), mock.patch.object(os, "replace", busy_twice):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0 and len(calls) == 3
    assert [path.suffix for path in result_files(repo)] == [".json"]


# ------------------------------------------------------------ 既にある結果

def test_existing_result_for_the_same_content_is_shown_without_running_again(repo):
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    again = local_ci(repo, "--no-fetch", "--ref", "main")
    assert again.returncode == 0
    assert len(results(repo)) == 1 and len(notes(repo)) == 1            # 実行も、記録も、増えていない
    assert "既にあるので、実行しなかった" in again.stdout and "結果: 合格" in again.stdout
    assert f"記録  : .cycle/ci/{result_files(repo)[0].name}" in again.stdout

    forced = local_ci(repo, "--no-fetch", "--ref", "main", "--force")
    assert forced.returncode == 0 and "既にある" not in forced.stdout
    assert len(results(repo)) == 2 and len(notes(repo)) == 2


def test_latest_result_for_the_same_content_decides(repo):
    """同じ内容の結果が複数あれば、最も新しいものを示す。不合格の結果を示したときの終了コードは、0 でない。"""
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    assert local_ci(repo, "--no-fetch", "--ref", "main", "--force", env={"CHECK_EXIT": "1"}).returncode == 1
    again = local_ci(repo, "--no-fetch", "--ref", "main")
    assert again.returncode == 1 and "既にあるので、実行しなかった" in again.stdout
    assert len(results(repo)) == 2 and len(notes(repo)) == 2


def test_result_of_another_commit_is_not_reused(repo):
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    set_plan(repo, exit=1)                          # 次のコミットは、確認が失敗する
    result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 1 and "既にある" not in result.stdout
    assert [written["verdict"] for written in results(repo)] == ["pass", "fail"]


def test_result_of_a_run_without_a_result_is_not_reused(repo):
    set_plan(repo, exit=0)                          # 試験ごとの結果が無い（error）
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 1
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 1
    assert [written["verdict"] for written in results(repo)] == ["error", "error"]
    assert len(notes(repo)) == 2


def altered(**changes: object):
    """結果の項目を差し替える書き換え。os、python、encoding は、中の項目だけを差し替える。"""
    def change(written: dict) -> str:
        for key, value in changes.items():
            written[key] = {**written[key], **value} if key in ("os", "python", "encoding") else value
        return json.dumps(written)
    return change


def other_encoding(name: str | None) -> str:
    """name とは別の文字コード（どのPCで実行しても、記録された値と必ず違う）。

    決め打ちにすると、その値のPC（POのPCの既定の cp932 など）では違いにならず、使い回される。
    """
    def same(candidate: str) -> bool:
        try:
            return codecs.lookup(candidate).name == codecs.lookup(name or "").name
        except LookupError:
            return candidate == name
    return next(candidate for candidate in ("cp932", "utf-8", "iso8859-15") if not same(candidate))


def altered_encoding(field: str):
    """文字コードの項目（field）を、記録された値と違うものにする書き換え。"""
    def change(written: dict) -> str:
        recorded = written["encoding"][field]
        value = not recorded if field == "utf8_mode" else other_encoding(recorded)
        assert value != recorded                    # 試験の前提: 違いになっている
        written["encoding"] = {**written["encoding"], field: value}
        return json.dumps(written)
    return change


def test_other_encoding_always_differs():
    names = ("cp932", "CP932", "ms932", "shift_jis", "utf-8", "UTF-8", "utf8", "ascii", None, "x-unknown")
    for name in names:
        assert other_encoding(name) != name
        if name not in (None, "x-unknown"):
            assert codecs.lookup(other_encoding(name)).name != codecs.lookup(name).name


NOT_THE_SAME_CHECK = {
    "別のOSの結果": altered(os={"system": "SomeOtherOS"}),
    "別の Python の結果": altered(python={"version": "3.0.0"}),
    "別のツリーの結果": altered(tree="1" * 40),
    "別のコミットの結果": altered(commit="1" * 40),
    "取り出さずに実行した結果": altered(mode="current"),
    "既定の文字コードが違う結果": altered_encoding("locale"),
    "UTF-8 モード（PYTHONUTF8）が違う結果": altered_encoding("utf8_mode"),
    "画面の文字コードが違う結果": altered_encoding("stdout"),
    "ファイル名の文字コードが違う結果": altered_encoding("filesystem"),
    "文字コードの記録が空の結果": lambda written: json.dumps({**written, "encoding": None}),
    "文字コードの項目が無い結果": lambda written: json.dumps(
        {key: value for key, value in written.items() if key != "encoding"},
    ),
    "合格と書かれているが、中身が合格と矛盾する": altered(tests=None),
    "JSON でない": lambda written: "{",
    "UTF-8 でない": lambda written: b"\x80\x81",
}


@pytest.mark.parametrize("case", NOT_THE_SAME_CHECK)
def test_result_that_is_not_for_the_same_check_is_not_reused(repo, case):
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    [path] = result_files(repo)
    data = NOT_THE_SAME_CHECK[case](json.loads(path.read_text(encoding="utf-8")))
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0 and "既にある" not in result.stdout
    assert len(result_files(repo)) == 2 and len(notes(repo)) == 2


# ------------------------------------------------------------ いまの作業フォルダで実行（--current）

def test_current_checks_the_working_folder_as_it_is(repo):
    commit_id, tree = head(repo)
    result = local_ci(repo, "--current")
    assert result.returncode == 0, result.stdout + result.stderr
    [written] = results(repo)
    assert (written["mode"], written["ref"], written["branch"]) == ("current", None, "main")
    assert (written["commit"], written["tree"], written["dirty"]) == (commit_id, tree, False)
    assert Path(notes(repo)[0]["cwd"]).resolve() == repo.resolve()
    assert "いまの作業フォルダの内容。コミット" in result.stdout and "未コミット" not in result.stdout
    # 取り出して確かめた結果とは、別の確認として扱う
    assert local_ci(repo, "--no-fetch", "--ref", "main").returncode == 0
    assert len(results(repo)) == 2 and len(notes(repo)) == 2

    # 未コミットの変更があれば、それを含む内容で確かめる（同じコミットの、前の結果は使い回さない）
    (repo / "plan.json").write_text(json.dumps({"exit": 1}), encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    result = local_ci(repo, "--current")
    assert result.returncode == 1
    written = results(repo)[-1]
    assert (written["commit"], written["dirty"], written["verdict"]) == (commit_id, True, "fail")
    assert written["tree"] == HARNESS.tree_id(repo) != tree
    assert "未コミットの変更あり" in result.stdout
    assert len(worktrees(repo)) == 1 and list(work_dir(repo).iterdir()) == []


def test_current_records_the_tree_the_harness_finds_whatever_git_settings_it_reads(repo, tmp_path):
    """Windows の実機で落ちた形を、どのOSでも再現する（W2 の確認。原因は docs/design/harness.md の5章）。

    試験の中の local_ci.py は、利用者とシステムの git の設定から切り離して動く（TEST_ENV）。試験のプロセス
    自身の呼び出しは、利用者の端末の設定（Git for Windows のシステムの設定 core.autocrlf=true）を読む。
    新しいファイルを CRLF で書いた（Windows の write_text）とき、その設定を読む側だけが LF に直していた。
    """
    with write_text_like_windows():
        (repo / "plan.json").write_text(json.dumps({"exit": 1}), encoding="utf-8")
        (repo / "new.txt").write_text("n\n", encoding="utf-8")
    result = local_ci(repo, "--current")
    assert result.returncode == 1, result.stdout + result.stderr
    written = results(repo)[-1]
    users = users_git_config(tmp_path, "[core]\n\tautocrlf = true\n\tfileMode = false\n")
    with mock.patch.dict(os.environ, {**TEST_ENV, **users}, clear=True):
        assert written["tree"] == HARNESS.tree_id(repo)
    assert sh(repo, "git", "rev-parse", f"{written['tree']}:new.txt") == sh(
        repo, "git", "hash-object", "--no-filters", "new.txt",
    )


def test_checkout_is_the_commit_whatever_git_settings_and_hooks_the_working_folder_has(repo, tmp_path):
    """取り出す git も、ツリーIDと同じ決めた設定で動く。改行を変換せず、作業フォルダのフックを動かさない。

    利用者の core.autocrlf=true（Git for Windows の既定）で取り出すと、属性の無いファイルの改行が CRLF に
    なり、確かめる内容がコミットと食い違う。post-checkout などのフックは、取り出した場所で動き、内容を
    変えうる。
    """
    marker = tmp_path / "hook-ran"
    for hook in ("post-checkout", "reference-transaction"):
        install_hook(repo, hook, marker)
    commit_id, tree = head(repo)
    users = users_git_config(tmp_path, "[core]\n\tautocrlf = true\n")
    result = local_ci(repo, "--no-fetch", "--ref", "main", env=users)
    assert result.returncode == 0, result.stdout + result.stderr
    [written] = results(repo)
    assert (written["commit"], written["tree"], written["verdict"]) == (commit_id, tree, "pass")
    assert not marker.exists()
    assert_cleaned_up(repo)


def test_current_result_is_not_taken_as_the_windows_gate(tmp_path):
    """作業フォルダで確かめた結果（--current）は、ツリーが同じでも取り込まない。

    コミットに無い、無視の対象のファイル（*.csv など）に助けられた合格でも、ツリーは同じで、
    未コミットの変更なしになる。
    """
    root = make_repo(tmp_path)
    (root / "scripts" / "check.py").write_text(PLANNED_CHECK, encoding="utf-8")
    (root / "plan.json").write_text(json.dumps({"junit": PASSING}), encoding="utf-8")
    (root / ".gitignore").write_text(".cycle/\n*.csv\n", encoding="utf-8")
    commit(root, "plan")
    set_header(root, "needs_windows: true\n")
    wt = implement(root)
    commit(wt)
    (wt / "data.csv").write_text("1\n", encoding="utf-8")         # 無視の対象。コミットには無い
    with as_windows():
        assert local_ci(wt, "--current").returncode == 0
    [written] = results(wt)
    unit_tree = cycle(root, "snapshot", "U1").stdout.strip()
    assert (written["mode"], written["dirty"], written["tree"]) == ("current", False, unit_tree)
    path = result_files(wt)[0]
    refused = cycle(root, "record-ci", "U1", "--result", str(path))
    assert refused.returncode == 2
    assert "コミットを取り出して確かめた結果ではない（--current の結果は取り込まない）" in refused.stderr
    assert "windows" not in state(root)["gates"]


def test_current_on_a_detached_head_has_no_branch(repo):
    sh(repo, "git", "checkout", "-q", "--detach")
    assert local_ci(repo, "--current").returncode == 0
    assert results(repo)[0]["branch"] is None


def test_current_needs_a_repository_with_a_commit(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text(PLANNED_CHECK, encoding="utf-8")
    result = local_ci(root, "--current")
    assert result.returncode == 2 and "gitの作業ツリー（の最上位）ではない" in result.stderr
    assert results(root) == [] and notes(root) == []


def test_current_and_a_named_commit_cannot_be_combined(repo):
    result = local_ci(repo, "--current", "--ref", "main")
    assert result.returncode == 2 and results(repo) == [] and notes(repo) == []


# ------------------------------------------------------------ 後始末

def test_checkout_is_removed_when_the_run_is_interrupted_from_the_keyboard(repo):
    """Ctrl-C（KeyboardInterrupt）。他のプロセスへ SIGINT を送れないOSでも確かめるため、試験の中で起こす。"""
    def interrupted(cmd, cwd, env, timeout):
        assert len(worktrees(repo)) == 2 and (cwd / "plan.json").is_file()      # 取り出した後
        raise KeyboardInterrupt

    with mock.patch.object(LOCAL_CI, "run_streaming", interrupted), pytest.raises(KeyboardInterrupt):
        local_ci(repo, "--no-fetch", "--ref", "main")
    assert results(repo) == []
    assert_cleaned_up(repo)


def test_checkout_is_removed_when_it_cannot_be_made(repo):
    real = HARNESS.git

    def add_fails_half_way(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            real(cwd, *args, **kwargs)
            raise HARNESS.CycleError("git worktree add が失敗")
        return real(cwd, *args, **kwargs)

    with mock.patch.object(HARNESS, "git", add_fails_half_way):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 2 and "git worktree add が失敗" in result.stderr
    assert results(repo) == [] and notes(repo) == []
    assert_cleaned_up(repo)


@pytest.mark.parametrize("stop", [
    pytest.param("SIGTERM", marks=only_posix("Windows の SIGTERM は強制終了で、後始末の機会が無い")),
    pytest.param("SIGHUP", marks=only_posix("Windows には SIGHUP が無い")),
    pytest.param("CTRL_BREAK_EVENT", marks=only_windows("Ctrl-Break の合図は Windows にだけある")),
])
def test_checkout_is_removed_and_the_check_is_stopped_when_the_run_is_told_to_end(repo, stop):
    set_plan(repo, junit=PASSING, sleep=3, child=2)
    proc = start_local_ci(repo, "--no-fetch", "--ref", "main")
    wait_for(repo.parent / "notes.json")                    # 確認が走っている
    proc.send_signal(getattr(signal, stop))
    out, err = proc.communicate(timeout=30)
    assert proc.returncode != 0, out + err
    assert results(repo) == []
    assert_cleaned_up(repo)
    time.sleep(4)
    assert not (repo.parent / "notes.json.finished").exists()       # 確認スクリプトは、止められている
    assert not (repo.parent / "notes.json.child").exists()          # その先のプロセスも


def test_registration_is_removed_even_when_git_first_refuses_to_remove_the_checkout(repo):
    """Windows では、中のファイルが使われていると、git は作業ツリーを消せない。消してから、登録を外し直す。"""
    real, refused = HARNESS.run_git, []

    def refuse_once(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove") and not refused:
            refused.append(args)
            return subprocess.CompletedProcess(args, 1, "", "Permission denied")
        return real(cwd, *args, **kwargs)

    with mock.patch.object(HARNESS, "run_git", refuse_once):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0 and len(refused) == 1
    assert_cleaned_up(repo)


def test_place_that_cannot_be_removed_is_reported_and_does_not_change_the_result(repo):
    rmtree = shutil.rmtree

    def refused(path, *args, **kwargs):
        if Path(path).name.startswith("circuit-sim-ci-"):
            raise PermissionError(13, "使われている")
        rmtree(path, *args, **kwargs)                       # ほかの一時フォルダ（ツリーIDの計算用）は、消せる

    with mock.patch.object(HARNESS, "BUSY_WAIT_SECONDS", 0.2), mock.patch.object(shutil, "rmtree", refused):
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0
    [left] = work_dir(repo).iterdir()
    assert f"注意: 作業用の場所を片付けられなかった。消してよい: {HARNESS.real_path(left)}" in result.stderr
    assert results(repo)[0]["verdict"] == "pass"
    assert len(worktrees(repo)) == 1                        # 取り出した内容と、git の登録は、消えている


def test_place_is_removed_after_waiting_while_a_file_in_it_is_in_use(repo):
    rmtree, calls = shutil.rmtree, []

    def busy_twice(path, *args, **kwargs):
        if Path(path).name.startswith("circuit-sim-ci-"):
            calls.append(path)
            if len(calls) <= 2:
                raise PermissionError(13, "使われている")
        rmtree(path, *args, **kwargs)

    busy = mock.patch.object(shutil, "rmtree", busy_twice)
    with mock.patch.object(HARNESS, "BUSY_WAIT_SECONDS", 5.0), busy:
        result = local_ci(repo, "--no-fetch", "--ref", "main")
    assert result.returncode == 0 and len(calls) == 3 and "注意" not in result.stderr
    assert_cleaned_up(repo)


# ------------------------------------------------------------ 画面

@pytest.mark.parametrize("encoding", ["ascii", "cp932"])
def test_local_ci_does_not_crash_on_a_terminal_that_cannot_show_the_text(repo, encoding):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--no-fetch", "--ref", "main"], capture_output=True,
        env=ci_env(repo, {"PYTHONIOENCODING": encoding}), cwd=work_dir(repo).parent, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode(encoding, errors="replace")
    assert b"Traceback" not in proc.stderr
    if encoding == "cp932":
        assert "結果: 合格（確認が通った）" in proc.stdout.decode(encoding)
    # エラーの表示（標準エラー）も、同じ置き換えで出る
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--no-fetch", "--ref", "µ"], capture_output=True,
        env=ci_env(repo, {"PYTHONIOENCODING": encoding}), cwd=work_dir(repo).parent, timeout=120,
    )
    assert proc.returncode == 2 and b"Traceback" not in proc.stderr and b"\\x" not in proc.stderr


# ------------------------------------------------------------ ハーネスとのつなぎ

def as_windows() -> contextlib.AbstractContextManager:
    """結果に書かれるOSを、Windows にする（Windows の実物が無い場所で、取り込みまでを通して確かめるため）。"""
    return mock.patch.object(platform, "system", return_value="Windows")


def test_result_of_the_unit_branch_can_be_recorded_as_the_windows_gate(tmp_path):
    """作業単位のブランチを local_ci.py で確かめた結果を、ハーネスが取り込み、統合の条件が満たされる。

    local_ci.py が記録するツリーIDと、ハーネスが作業ツリーから求めるツリーIDは、同じ求め方
    （実行できる印の付いたファイルがあっても、コミットのツリーと一致する）。
    """
    root = make_repo(tmp_path)
    (root / "scripts" / "check.py").write_text(PLANNED_CHECK, encoding="utf-8")
    (root / "scripts" / "check.py").chmod(0o755)
    (root / "plan.json").write_text(json.dumps({"junit": PASSING}), encoding="utf-8")
    sh(root, "git", "add", "-A")
    sh(root, "git", "update-index", "--chmod=+x", "scripts/check.py")  # 実行できる印を表せないOSでも付ける
    sh(root, "git", "commit", "-q", "-m", "plan")
    assert sh(root, "git", "ls-files", "-s", "scripts/check.py").startswith("100755 ")
    set_header(root, "needs_windows: true\n")
    wt = implement(root)
    commit(wt)
    # 試験用の確認スクリプトが控えを書く先。外側の pytest のオプションは、引き継がない
    env = {"NOTES": str(tmp_path / "notes.json"), "PYTEST_ADDOPTS": ""}
    tree = cycle(root, "snapshot", "U1").stdout.strip()
    assert tree == sh(wt, "git", "rev-parse", "HEAD^{tree}")
    for gate, by in (("review", "rev-1"), ("verify", "ver-1")):
        recorded = cycle(root, "record", "U1", gate, "--verdict", "pass", "--by", by, "--tree", tree)
        assert recorded.returncode == 0, recorded.stderr
    assert cycle(root, "check", "U1", env=env).returncode == 0
    refused = cycle(root, "merge", "U1", env=env)
    assert refused.returncode == 1 and "windows: まだ記録が無い" in refused.stdout

    here = local_ci(root, "--no-fetch", "--ref", "U1")
    assert here.returncode == 0, here.stdout + here.stderr
    path = result_files(root)[-1]
    recorded = cycle(root, "record-ci", "U1", "--result", str(path))
    if not WINDOWS:
        # このOSでの結果は取り込めない。結果に書かれるOSだけを Windows にして、取り込みまでを通す
        assert recorded.returncode == 2 and "Windows での結果ではない（" in recorded.stderr
        runs = len(notes(root))
        with as_windows():
            there = local_ci(root, "--no-fetch", "--ref", "U1")
        assert there.returncode == 0 and len(notes(root)) == runs + 1      # 別のOSの結果を、使い回していない
        path = result_files(root)[-1]
        assert "-windows-" in path.name
        recorded = cycle(root, "record-ci", "U1", "--result", str(path))
    assert recorded.returncode == 0, recorded.stderr
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tree"] == tree and written["commit"] == sh(wt, "git", "rev-parse", "HEAD")
    assert state(root)["gates"]["windows"]["tree"] == tree
    merged = cycle(root, "merge", "U1", env=env)
    assert merged.returncode == 0, merged.stdout + merged.stderr
