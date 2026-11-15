"""開発の道具（scripts/check.py と scripts/doctor.py）の試験。

本物の claude は起動しない。偽の claude（tests/test_harness.py と同じもの）を PATH の先頭に置く。
scripts/check.py の本物も起動しない（その中で pytest が走り、この試験がもう一度呼ばれるため）。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from conftest import NESTED_EXIT_CODE
from test_harness import (
    PASS,
    TEST_ENV,
    claude_argv,
    fake_claude,
    headless,
    install_claude,
    make_repo,
    sh,
    was_run,
    write_text_like_windows,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SECRET-7f3a"          # 記録にも画面にも出てはいけない文字列の目印


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load("check")
DOCTOR = load("doctor")


# ------------------------------------------------------------ scripts/check.py

def run_steps(steps: list[str]) -> int:
    """終了コードの並びを、確認の手順（ruff、pytest の代わり）として実行する。"""
    commands = [[sys.executable, "-c", code] for code in steps]
    with mock.patch.object(CHECK, "STEPS", commands):
        return CHECK.main()


def test_check_runs_ruff_and_pytest_with_the_running_python():
    assert [step[:3] for step in CHECK.STEPS] == [
        [sys.executable, "-m", "ruff"], [sys.executable, "-m", "pytest"],
    ]


def test_check_passes_only_when_every_step_passes(tmp_path):
    mark = tmp_path / "second-step-ran"
    second = f"import pathlib; pathlib.Path({str(mark)!r}).touch()"
    assert run_steps(["import sys; sys.exit(0)", second]) == 0
    assert mark.exists()                    # 1つ目が通れば、2つ目も実行する
    assert run_steps([second, "import sys; sys.exit(3)"]) == 3


def test_check_stops_at_the_first_failure(tmp_path):
    mark = tmp_path / "second-step-ran"
    second = f"import pathlib; pathlib.Path({str(mark)!r}).touch()"
    assert run_steps(["import sys; sys.exit(7)", second]) == 7
    assert not mark.exists()


def test_check_runs_at_the_top_of_the_repository_wherever_it_is_started(tmp_path, monkeypatch):
    out = tmp_path / "cwd.txt"
    code = f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.getcwd(), encoding='utf-8')"
    monkeypatch.chdir(tmp_path)             # リポジトリの直下でない場所から起動する
    assert run_steps([code]) == 0
    assert Path(out.read_text(encoding="utf-8")).resolve() == ROOT


def run_copy_of_check(base: Path, files: dict[str, str]) -> tuple[int, str]:
    """check.py を別の場所に写して、コマンドラインから実行する（ruff と pytest は、そこの files を見る）。

    起動するのは、写した先でもこのリポジトリでもない、空の場所から。check.py が実行の場所を自分で決めずに
    起動した場所で動くと、ここでは何も見つからずに失敗する（このリポジトリの試験の全体を、入れ子で実行しない）。
    """
    elsewhere = base / "started-here"
    elsewhere.mkdir()
    (base / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check.py", base / "scripts" / "check.py")
    # ruff の設定は、この試験用のもの（使っていない import などを見る）
    (base / "ruff.toml").write_text('[lint]\nselect = ["F"]\n', encoding="utf-8")
    for name, text in files.items():
        (base / name).write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(base / "scripts" / "check.py")], capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=TEST_ENV, cwd=elsewhere, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


PASSING_TEST = "def test_ok():\n    assert True\n"


def test_check_script_passes_clean_code_with_passing_tests(tmp_path):
    code, output = run_copy_of_check(tmp_path, {"test_sample.py": PASSING_TEST})
    assert code == 0, output
    assert "All checks passed" in output and "1 passed" in output


def test_check_script_fails_on_a_lint_error_without_running_the_tests(tmp_path):
    files = {"unused.py": "import os\n", "test_sample.py": PASSING_TEST}
    code, output = run_copy_of_check(tmp_path, files)
    assert code != 0
    assert "F401" in output and "1 passed" not in output


def test_check_script_fails_on_a_failing_test_and_lists_skipped_tests(tmp_path):
    test = (
        "import pytest\n\n\n"
        "@pytest.mark.skip(reason='not on this OS')\ndef test_skipped():\n    pass\n\n\n"
        "def test_broken():\n    assert False\n"
    )
    code, output = run_copy_of_check(tmp_path, {"test_sample.py": test})
    assert code == 1
    assert "1 failed, 1 skipped" in output
    assert "SKIPPED [1] test_sample.py:4: not on this OS" in output


def test_tests_of_this_repository_started_from_inside_a_test_are_stopped():
    """試験の中から、このリポジトリの試験がもう一度実行されたら、すぐに止まる（tests/conftest.py）。

    止める仕組みが無いと、この起動は、1件も選ばずに終わる（終了コード 5）。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "no_test_has_this_name"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=TEST_ENV,
        timeout=300,
    )
    assert proc.returncode == NESTED_EXIT_CODE, proc.stdout + proc.stderr
    assert "入れ子" in proc.stdout + proc.stderr


# ------------------------------------------------------------ .github/workflows/ci.yml

def workflow() -> list[str]:
    """自動テストの定義の行（注釈と、行末の空白を除く）。"""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lines = [line.split("  #")[0].rstrip() for line in text.splitlines()]
    return [line for line in lines if line.strip() and not line.strip().startswith("#")]


def test_ci_result_is_decided_by_the_check_itself():
    """自動テストの合否は、scripts/check.py の終了コードで直接決まる。

    環境の確認スクリプト（doctor.py）の終了コードだけに懸けない。doctor.py は別のステップで、確認を重ねない。
    """
    steps = [line.strip() for line in workflow()]
    check, environment = "run: python scripts/check.py", "run: python scripts/doctor.py --no-check"
    assert check in steps and environment in steps
    assert steps.index(check) < steps.index(environment)
    # 失敗を見逃す指定（失敗しても続ける、いつでも実行する）を、どのステップにも付けない
    assert not [line for line in steps if "continue-on-error" in line or line.startswith("if: always")]


def test_ci_can_only_read_the_repository():
    """自動テストに渡る権限は、リポジトリの読み取りだけ（定義の全体で1か所、最上位に書く）。"""
    lines = workflow()
    assert [line for line in lines if line.strip().startswith("permissions:")] == ["permissions:"]
    start = lines.index("permissions:") + 1
    block = []
    for line in lines[start:]:
        if not line.startswith(" "):
            break
        block.append(line.strip())
    assert block == ["contents: read"]


# ------------------------------------------------------------ scripts/doctor.py

@pytest.fixture
def root(tmp_path: Path) -> Path:
    """確認スクリプトだけを持つ、試験用のリポジトリの場所。CHECK_EXIT があれば、その終了コードで終わる。"""
    base = tmp_path / "root"
    (base / "scripts").mkdir(parents=True)
    script = "import os, sys\nprint('check output')\nsys.exit(int(os.environ.get('CHECK_EXIT', '0')))\n"
    (base / "scripts" / "check.py").write_text(script, encoding="utf-8")
    return base


def started_here(root: Path) -> Path:
    """doctor.py を起動する場所（試験用のリポジトリの外の、空の場所）。"""
    elsewhere = root.parent / "started-here"
    elsewhere.mkdir(exist_ok=True)
    return elsewhere


def doctor(root: Path, *args: str, env: dict[str, str]) -> tuple[int, dict, str]:
    """doctor.py をコマンドラインから実行する。返すのは（終了コード、結果のJSON、画面の出力）。

    起動するのは、このリポジトリでない空の場所から（doctor.py が確認を起動する場所を間違えても、
    このリポジトリの本物の scripts/check.py を実行しない）。
    --headless で役割を指定しなければ、code-reviewer だけを確かめる（役割ごとの確認は
    test_headless_probe_runs_for_every_role_the_gates_use が確かめる）。
    """
    if "--headless" in args and "--role" not in args:
        args = (*args, "--role", "code-reviewer")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), *args], capture_output=True,
        text=True, encoding="utf-8", env={**TEST_ENV, "CYCLE_ROOT": str(root), **env},
        cwd=started_here(root), timeout=300,
    )
    path = root / ".cycle" / "doctor-result.json"
    assert path.is_file(), proc.stdout + proc.stderr
    return proc.returncode, json.loads(path.read_text(encoding="utf-8")), proc.stdout + proc.stderr


def probe(result: dict, index: int = 0) -> dict:
    """ヘッドレスの確認のうち、1つの役割の結果。"""
    return result["headless"]["roles"][index]


def good_output(**top: object) -> dict:
    """ハーネスの前提どおりの出力。"""
    return headless(PASS, session="0f8fad5b-d9cb-469f-a165-70867728950e", **{
        "type": "result", "subtype": "success", "is_error": False, "result": "ok",
        "total_cost_usd": 0.0125, "num_turns": 1, "duration_ms": 900, **top,
    })


def test_doctor_reports_the_environment(root, tmp_path):
    code, result, screen = doctor(root, env=fake_claude(tmp_path, good_output()))
    assert code == 0, screen
    assert result["ok"] is True and result["problems"] == []
    assert set(result) >= {"os", "python", "encoding", "git", "claude", "content", "check", "headless"}
    assert result["os"]["system"] and result["os"]["release"]
    assert result["python"]["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert result["python"]["supported"] is True
    assert result["git"]["found"] and result["git"]["runs"]
    assert result["git"]["version"].startswith("git version ")
    assert result["claude"]["found"] and result["claude"]["runs"]
    assert result["claude"]["version"] == "9.9.9 (fake)"
    assert result["claude"]["file"].lower() == ("claude.cmd" if os.name == "nt" else "claude")
    assert result["claude"]["api_key_env_set"] is False
    check = result["check"]
    assert check["ran"] and check["passed"] and check["returncode"] == 0 and check["seconds"] >= 0
    assert "check output" in screen                         # 確認の出力は、そのまま画面に流れる
    # --headless を付けない限り、claude には版を聞くだけ
    assert result["headless"] == {"ran": False, "reason": "not_requested"}
    assert claude_argv(tmp_path) == ["--version"]
    assert "結果: 問題なし" in screen and "9.9.9 (fake)" in screen


def test_failing_check_is_a_problem(root, tmp_path):
    env = {**fake_claude(tmp_path, good_output()), "CHECK_EXIT": "3"}
    code, result, screen = doctor(root, env=env)
    assert code == 1
    assert result["check"]["passed"] is False and result["check"]["returncode"] == 3
    assert result["problems"] == ["check"] and result["ok"] is False
    assert "不合格（終了コード 3）" in screen and "結果: 問題あり（check）" in screen


def test_check_can_be_left_out(root, tmp_path):
    env = {**fake_claude(tmp_path, good_output()), "CHECK_EXIT": "3"}
    code, result, screen = doctor(root, "--no-check", env=env)
    assert code == 0
    assert result["check"] == {"ran": False, "reason": "skipped"}
    assert "check output" not in screen


def test_missing_check_script_is_a_problem(root, tmp_path):
    (root / "scripts" / "check.py").unlink()
    code, result, _ = doctor(root, env=fake_claude(tmp_path, good_output()))
    assert code == 1
    assert result["check"] == {"ran": False, "reason": "missing"} and result["problems"] == ["check"]


def test_check_run_by_doctor_does_not_get_the_callers_pytest_settings(root, tmp_path):
    """呼び出し元の PYTEST_ で始まる変数（試験を選び直せる）は、確認に渡さない（cycle.py の check と同じ）"""
    seen = tmp_path / "seen.json"
    script = (
        "import json, os, pathlib\n"
        "names = sorted(name for name in os.environ if name.startswith('PYTEST_'))\n"
        f"pathlib.Path({str(seen)!r}).write_text(json.dumps(names), encoding='utf-8')\n"
    )
    (root / "scripts" / "check.py").write_text(script, encoding="utf-8")
    env = {**fake_claude(tmp_path, good_output()), "PYTEST_ADDOPTS": "-k nothing", "PYTEST_PLUGINS": "x"}
    code, result, screen = doctor(root, env=env)
    assert code == 0 and result["check"]["passed"] is True, screen
    assert json.loads(seen.read_text(encoding="utf-8")) == []


def in_process(root: Path, *args: str, find=None) -> tuple[int, dict]:
    """doctor.py を試験のプロセスの中で実行する（コマンドの見つけ方などを差し替える試験用）。"""
    patches = [mock.patch.dict(os.environ, {**TEST_ENV, "CYCLE_ROOT": str(root)}, clear=True)]
    if find is not None:
        patches.append(mock.patch.object(DOCTOR.cycle, "find_command", find))
    for patch in patches:
        patch.start()
    try:
        code = DOCTOR.main(list(args))
    finally:
        for patch in reversed(patches):
            patch.stop()
    return code, json.loads((root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8"))


def without(*missing: str):
    """指定したコマンドだけが見つからない、コマンドの探し方。それ以外は git だけを本物で探す。"""
    real = DOCTOR.cycle.find_command

    def find(name: str, avoid: tuple = ()) -> str | None:
        return None if name in missing or name != "git" else real(name, avoid)
    return find


def test_doctor_turns_the_error_dialogs_off_first(root):
    """doctor.py は、git や claude を探して起動する前に、Windows のエラー画面を出さない設定にする。"""
    order = []
    real = without("claude")

    def noted_find(name: str, avoid: tuple = ()) -> str | None:
        order.append(name)
        return real(name, avoid)

    with mock.patch.object(DOCTOR.cycle, "quiet_error_dialogs", lambda: order.append("quiet")):
        code, _ = in_process(root, "--no-check", find=noted_find)
    assert code == 0
    assert order[0] == "quiet" and "git" in order


def test_missing_claude_is_reported_but_is_not_a_problem(root, capsys):
    code, result = in_process(root, "--no-check", find=without("claude"))
    assert code == 0
    assert result["claude"] == {"found": False, "api_key_env_set": False}
    assert result["problems"] == []
    assert "claude      : 見つからない" in capsys.readouterr().out
    # ヘッドレスの確認を頼まれたのに実行できなければ、問題として返す
    code, result = in_process(root, "--no-check", "--headless", find=without("claude"))
    assert code == 1
    assert result["headless"] == {"ran": False, "reason": "claude_not_found"}
    assert result["problems"] == ["headless"]


def test_missing_git_is_a_problem(root, capsys):
    code, result = in_process(root, "--no-check", find=without("claude", "git"))
    assert code == 1
    assert result["git"] == {"found": False} and result["problems"] == ["git"]
    assert "git         : 見つからない" in capsys.readouterr().out


def test_old_python_is_a_problem(root, capsys):
    with mock.patch.object(DOCTOR, "MIN_PYTHON", (99, 0)):
        code, result = in_process(root, "--no-check", find=without("claude"))
    assert code == 1
    assert result["python"]["supported"] is False and result["problems"] == ["python"]
    assert "99.0 以上が必要" in capsys.readouterr().out


UNKNOWN_CONTENT = {"commit": None, "tree": None, "branch": None, "dirty": None}


def test_doctor_records_which_content_it_checked(tmp_path, capsys):
    """結果に、確かめた内容（コミットID、ツリーID、ブランチ、未コミットの変更の有無）が入る。

    ツリーIDは、ハーネス（scripts/cycle.py）と同じ求め方。
    """
    repo = make_repo(tmp_path)
    commit, tree = sh(repo, "git", "rev-parse", "HEAD"), sh(repo, "git", "rev-parse", "HEAD^{tree}")
    code, result = in_process(repo, "--no-check", find=without("claude"))
    assert code == 0
    assert result["content"] == {"commit": commit, "tree": tree, "branch": "main", "dirty": False}
    assert f"内容        : コミット {commit[:10]}、ツリー {tree[:10]}（main）" in capsys.readouterr().out

    # 未コミットの変更と新しいファイルを含む内容。ブランチから切り離されていれば、ブランチは無い
    (repo / "product.py").write_text("x = 5\n", encoding="utf-8")
    (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
    sh(repo, "git", "checkout", "-q", "--detach")
    code, result = in_process(repo, "--no-check", find=without("claude"))
    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
        changed = DOCTOR.cycle.tree_id(repo)
    assert changed != tree
    assert result["content"] == {"commit": commit, "tree": changed, "branch": None, "dirty": True}
    shown = f"コミット {commit[:10]}、ツリー {changed[:10]}（ブランチから切り離されている、未コミットの"
    assert shown in capsys.readouterr().out


def test_content_is_unknown_outside_a_repository_with_a_commit(root, tmp_path, capsys):
    """git の作業ツリーの最上位でなければ、内容は「分からない」。上の階層のリポジトリの内容を、書かない。"""
    code, result = in_process(root, "--no-check", find=without("claude"))
    assert code == 0 and result["content"] == UNKNOWN_CONTENT
    assert "内容        : 分からない" in capsys.readouterr().out

    below = make_repo(tmp_path) / "below"           # リポジトリの中の、下の階層
    below.mkdir()
    code, result = in_process(below, "--no-check", find=without("claude"))
    assert code == 0 and result["content"] == UNKNOWN_CONTENT

    empty = tmp_path / "empty"                      # コミットがまだ無いリポジトリ
    empty.mkdir()
    sh(empty, "git", "init", "-q", "-b", "main")
    code, result = in_process(empty, "--no-check", find=without("claude"))
    assert code == 0 and result["content"] == UNKNOWN_CONTENT

    # git を起動できなくても、止まらない（git が無いことは、別の項目が問題として知らせる）
    with mock.patch.object(DOCTOR.cycle, "run_git", side_effect=FileNotFoundError("git")):
        code, result = in_process(root, "--no-check", find=without("claude"))
    assert code == 0 and result["content"] == UNKNOWN_CONTENT


def test_version_text_that_does_not_look_like_a_version_is_not_recorded(root):
    proc = subprocess.CompletedProcess([], 0, f"token={SECRET}; rm -rf\n", "")
    with mock.patch.object(DOCTOR.cycle, "run_captured", return_value=proc):
        code, result = in_process(root, "--no-check", find=lambda name, avoid=(): f"/somewhere/{name}")
    assert result["git"]["runs"] is True and result["git"]["version"] is None
    assert SECRET not in json.dumps(result)


# ------------------------------------------------------------ doctor.py --headless

def test_headless_probe_runs_once_with_the_same_command_as_the_harness(root, tmp_path):
    env = fake_claude(tmp_path, good_output())
    code, result, screen = doctor(root, "--no-check", "--headless", "--budget", "0.5", env=env)
    assert code == 0, screen
    cycle = DOCTOR.cycle
    argv = claude_argv(tmp_path)
    assert argv == cycle.claude_command(DOCTOR.PROBE_PROMPT, "code-reviewer", 0.5, "review")[1:]
    # ハーネスの run と、オプションの並びも、結果の形の指定も同じ
    harness = cycle.agent_command({"owner": "x", "spec": "docs/plan/U1-demo.md"}, "review", 0.5)[1:]
    assert argv[0::2] == harness[0::2]
    same = dict(zip(harness[0::2], harness[1::2], strict=True))
    used = dict(zip(argv[0::2], argv[1::2], strict=True))
    # 指示のほかは、道具の一覧、許可の扱い、禁じる道具まで、ハーネスの review と同じ
    assert {**used, "-p": ""} == {**same, "-p": ""}
    head = probe(result)
    assert (head["role"], head["gate"], head["restricts_tools"]) == ("code-reviewer", "review", False)
    assert head["ran"] is True and head["timed_out"] is False and head["unexpected"] == []
    assert head["failure"] is None
    assert head["options"] == argv[0::2]
    assert head["launcher"].lower() == ("claude.cmd" if os.name == "nt" else "claude")
    assert head["returncode"] == 0 and head["harness_verdict"] == "pass"
    assert head["stdout"]["form"] == "json_object"
    assert head["fields"]["is_error"] == {"present": True, "kind": "boolean", "value": False}
    assert head["fields"]["subtype"] == {"present": True, "kind": "string", "value": "success"}
    assert head["fields"]["type"] == {"present": True, "kind": "string", "value": "result"}
    assert head["fields"]["session_id"] == {"present": True, "kind": "string", "empty": False}
    assert head["fields"]["result"] == {"present": True, "kind": "string"}
    assert head["fields"]["total_cost_usd"] == {"present": True, "kind": "number", "value": 0.0125}
    assert head["fields"]["structured_output"] == {
        "present": True, "kind": "object", "keys": ["criteria", "issues", "summary", "verdict"],
        "other_keys": 0, "problem": "", "verdict": "pass", "criteria": 1, "issues": 0,
    }
    assert head["keys"] == sorted(good_output()) and head["other_keys"] == 0
    assert head["result_as_json"] is None           # structured_output があるので、result の中身は調べない
    # 実行の前に、何をするかを表示する
    assert "claude -p を、役割ごとに1回ずつ、1 回実行します" in screen and "利用枠を少し使います" in screen
    assert "ハーネスの前提どおりの形で返ってきた" in screen


def test_probe_prompt_and_gate_prompts_can_pass_through_a_batch_launcher():
    """Windows の claude.cmd は cmd.exe を通る。そこで崩れる文字を、指示に入れていないこと。"""
    cycle = DOCTOR.cycle
    state = {"owner": "solver-engineer", "spec": "docs/plan/U1-demo.md"}
    commands = [cycle.agent_command(state, gate, 5.0) for gate in cycle.AGENT_GATES]
    commands += [cycle.claude_command(DOCTOR.PROBE_PROMPT, "x", 1.0, gate) for gate in cycle.AGENT_GATES]
    with mock.patch.object(cycle, "find_command", return_value="C:/npm/claude.cmd"):
        for cmd in commands:
            assert cycle.launchable(cmd)[1:] == cmd[1:]


SHAPES = {
    "session_id が無い": (
        {k: v for k, v in good_output().items() if k != "session_id"}, 0, "", ["session_id"],
    ),
    "session_id が空": (good_output(session_id=""), 0, "", ["session_id"]),
    "is_error が無い": ({k: v for k, v in good_output().items() if k != "is_error"}, 0, "", ["is_error"]),
    "is_error が真": (good_output(is_error=True), 0, "", ["is_error", "harness_verdict"]),
    "is_error が文字列": (good_output(is_error="false"), 0, "", ["is_error", "harness_verdict"]),
    "subtype が無い": ({k: v for k, v in good_output().items() if k != "subtype"}, 0, "", ["subtype"]),
    "subtype が success でない": (
        good_output(subtype="error_max_turns"), 0, "", ["subtype", "harness_verdict"],
    ),
    "structured_output が無い": (
        {k: v for k, v in good_output().items() if k != "structured_output"}, 0, "",
        ["structured_output", "harness_verdict"],
    ),
    "structured_output の項目が足りない": (
        good_output(structured_output={"verdict": "pass"}), 0, "", ["structured_output", "harness_verdict"],
    ),
    "verdict が fail": (
        good_output(structured_output={**PASS, "verdict": "fail"}), 0, "", ["harness_verdict"],
    ),
    "終了コードが 0 でない": (good_output(), 2, "", ["returncode", "harness_verdict"]),
    "JSON の一覧": (
        [{"type": "system"}, good_output()], 0, "", ["stdout_form", "harness_verdict"],
    ),
    "1行ずつの JSON": (
        '{"type": "system"}\n' + json.dumps(good_output()) + "\n", 0, "", ["stdout_form", "harness_verdict"],
    ),
    "JSON でない": (
        "done", 0, "",
        ["stdout_form", "is_error", "subtype", "structured_output", "session_id", "harness_verdict"],
    ),
    "何も出ない": (
        "", 1, "error: unknown option '--json-schema'\n",
        ["returncode", "stdout_form", "is_error", "subtype", "structured_output", "session_id",
         "harness_verdict"],
    ),
}


@pytest.mark.parametrize("case", SHAPES)
def test_headless_output_that_differs_from_what_the_harness_expects_is_a_problem(root, tmp_path, case):
    output, exit_code, stderr, unexpected = SHAPES[case]
    env = fake_claude(tmp_path, output, exit_code=exit_code, stderr=stderr)
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    assert code == 1
    assert result["problems"] == ["headless"]
    assert probe(result)["unexpected"] == unexpected
    assert f"ハーネスの前提と違う点がある: {'、'.join(unexpected)}" in screen


def test_headless_shape_details_are_recorded(root, tmp_path):
    # JSON の一覧で返ってきた場合も、結果のオブジェクト（type が result）の形を記録する
    failed = good_output(subtype="error_max_turns")
    output = [{"type": "system", "session_id": "x"}, failed, {"type": "other"}]
    code, result, screen = doctor(root, "--no-check", "--headless", env=fake_claude(tmp_path, output))
    head = probe(result)
    assert head["stdout"]["form"] == "json_array" and head["harness_verdict"] == "error"
    assert head["fields"]["subtype"]["value"] == "error_max_turns"
    assert head["fields"]["num_turns"] == {"present": True, "kind": "number", "value": 1}
    assert "出力の形 json_array、ハーネスの判定 error" in screen
    assert "あった項目: type、subtype、is_error、session_id、result、structured_output" in screen

    # 知らないと言われたオプションは、こちらが渡した名前に限って記録する
    stderr = "error: unknown option '--json-schema'\n(Did you mean --json?)\n"
    env = fake_claude(tmp_path, "", exit_code=1, stderr=stderr)
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    head = probe(result)
    assert head["stdout"] == {"form": "empty", "characters": 0}
    assert head["messages"] == {
        "characters": len(stderr), "lines": 2, "rejected_options": ["--json-schema"], "mentions_login": False,
    }
    assert head["fields"] == {} and head["keys"] == []
    assert "CLI が知らないと言ったオプション: --json-schema" in screen

    # サインインを求める文面（出力の側に出た場合も）
    env = fake_claude(tmp_path, "Invalid API key · Please run /login", exit_code=1)
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    head = probe(result)
    assert head["stdout"]["form"] == "not_json"
    assert head["messages"]["mentions_login"] is True and head["messages"]["rejected_options"] == []
    assert "サインインや認証に触れている" in screen


def test_headless_probe_that_does_not_finish_is_a_problem(root, tmp_path):
    env = fake_claude(tmp_path, good_output(), script="time.sleep(20)")
    code, result, screen = doctor(root, "--no-check", "--headless", "--timeout", "1", env=env)
    assert code == 1
    head = probe(result)
    assert head["ran"] is True and head["timed_out"] is True and head["unexpected"] == ["timed_out"]
    assert "時間内に終わらなかった" in screen


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_headless_probe_is_not_run_on_api_billing_unless_allowed(root, tmp_path, variable):
    env = {**fake_claude(tmp_path, good_output()), variable: f"sk-{SECRET}"}
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    assert code == 1
    assert result["headless"] == {"ran": False, "reason": "api_key_set"}
    assert result["claude"]["api_key_env_set"] is True
    assert claude_argv(tmp_path) == ["--version"]                   # 版を聞いただけで、実行していない
    assert f"環境変数 {variable} が設定されていて" in screen and "APIの料金" in screen
    assert SECRET not in screen and SECRET not in json.dumps(result)

    code, result, screen = doctor(root, "--no-check", "--headless", "--allow-api-billing", env=env)
    assert code == 0, screen
    assert result["headless"]["ran"] is True and claude_argv(tmp_path)[0] == "-p"
    assert SECRET not in screen and SECRET not in json.dumps(result)


def test_response_text_and_credentials_are_not_written_anywhere(root, tmp_path):
    """記録と画面に出るのは、形と真偽だけ。応答の本文、セッションのID、エラーの文面、環境変数の中身は出ない。"""
    structured = {
        "verdict": "fail", "summary": f"summary {SECRET}",
        "criteria": [{"name": f"criterion {SECRET}", "measured": f"measured {SECRET}", "pass": False}],
        "issues": [{"severity": "critical", "description": f"issue {SECRET}"}],
        f"key {SECRET}": 1,
    }
    output = {
        "type": f"type {SECRET}", "subtype": f"sk-{SECRET}", "is_error": f"error {SECRET}",
        "session_id": f"session-{SECRET}", "result": f"result {SECRET}", "structured_output": structured,
        "total_cost_usd": f"cost {SECRET}", "num_turns": [SECRET], "duration_ms": {SECRET: SECRET},
        "api_key": f"sk-{SECRET}", f"key {SECRET}": 1, f"{SECRET}": 1, "usage": {"note": SECRET},
    }
    env = {
        **fake_claude(tmp_path, output, exit_code=1, stderr=f"token {SECRET}\nunknown option --{SECRET}\n"),
        "ANTHROPIC_API_KEY": f"sk-{SECRET}", "SOME_OTHER_VARIABLE": SECRET,
    }
    code, result, screen = doctor(root, "--headless", "--allow-api-billing", env=env)
    assert code == 1 and result["headless"]["ran"] is True
    written = (root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8")
    assert SECRET not in written and SECRET not in screen
    assert SECRET.lower() not in written.lower() and SECRET.lower() not in screen.lower()
    # 形は記録されている
    fields = probe(result)["fields"]
    assert fields["subtype"] == {"present": True, "kind": "string"}
    assert fields["is_error"] == {"present": True, "kind": "string"}
    assert fields["total_cost_usd"] == {"present": True, "kind": "string"}
    assert fields["structured_output"]["keys"] == ["criteria", "issues", "summary", "verdict"]
    assert fields["structured_output"]["other_keys"] == 1
    # 項目の名前は、決めてある一覧のものだけ。api_key と、目印の入った2つは、個数だけ
    assert probe(result)["keys"] == sorted(key for key in output if key in (*DOCTOR.FIELDS, "usage"))
    assert probe(result)["other_keys"] == 3
    # 結果のファイルのほかに、何も書き出していない
    assert [path.name for path in (root / ".cycle").iterdir()] == ["doctor-result.json"]


# 項目の名前の位置に入る、秘密のような文字列（名前らしい形、英小文字だけ、記号つき）
SECRET_NAMES = ("sk_ant_SECRETTOKEN123", "hunterpassword", "Bearer abc.def")


def test_item_names_outside_the_known_list_are_only_counted(root, tmp_path):
    """項目の名前は、あらかじめ決めた一覧にあるものだけを残す。それ以外は、形がどうであれ個数だけ。"""
    extra = {name: 1 for name in SECRET_NAMES}
    output = good_output(structured_output={**PASS, **extra}, usage=extra, **extra)
    code, result, screen = doctor(root, "--no-check", "--headless", env=fake_claude(tmp_path, output))
    assert code == 0, screen                    # 知らない項目が増えているだけなら、問題にしない
    written = (root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8")
    for name in SECRET_NAMES:
        assert name.lower() not in written.lower() and name.lower() not in screen.lower()
    head = probe(result)
    assert head["keys"] == sorted([*good_output(), "usage"]) and head["other_keys"] == 3
    structured = head["fields"]["structured_output"]
    assert structured["keys"] == ["criteria", "issues", "summary", "verdict"]
    assert structured["other_keys"] == 3


def test_names_that_may_be_recorded_are_a_fixed_list():
    """名前を残してよい項目は、ハーネスが読む項目と、CLI の出力にある決まった名前だけ。"""
    assert DOCTOR.KNOWN_KEYS == {
        "type", "subtype", "is_error", "session_id", "result", "structured_output", "total_cost_usd",
        "num_turns", "duration_ms", "duration_api_ms", "usage", "modelUsage", "permission_denials", "uuid",
        "errors", "stop_reason", "terminal_reason",
    }
    assert DOCTOR.KNOWN_RESULT_KEYS == {"verdict", "summary", "criteria", "issues"}


FITTING = json.dumps({**PASS, "summary": f"summary {SECRET}"})

# structured_output が無い（オブジェクトでない）出力の、result の中身と、記録される形、画面の文面
RESULT_TEXTS = {
    "決まった形の JSON": (
        {"result": FITTING}, {"readable": True, "kind": "object", "fits_schema": True},
        "JSON として読める（決まった形に合う）",
    ),
    "形の違う JSON": (
        {"result": json.dumps({"verdict": "pass", "note": SECRET})},
        {"readable": True, "kind": "object", "fits_schema": False},
        "JSON として読める（決まった形には合わない）",
    ),
    "JSON の一覧": (
        {"result": json.dumps([SECRET])}, {"readable": True, "kind": "array", "fits_schema": False},
        "JSON として読める（決まった形には合わない）",
    ),
    "JSON でない文": ({"result": f"done {SECRET}"}, {"readable": False}, "JSON として読めない"),
    "structured_output が null": (
        {"result": FITTING, "structured_output": None},
        {"readable": True, "kind": "object", "fits_schema": True}, "JSON として読める（決まった形に合う）",
    ),
    "structured_output が文字列": (
        {"result": FITTING, "structured_output": FITTING},
        {"readable": True, "kind": "object", "fits_schema": True}, "JSON として読める（決まった形に合う）",
    ),
    "result が文字列でない": ({"result": {"verdict": "pass"}}, None, None),
    "result が無い": ({}, None, None),
}


@pytest.mark.parametrize("case", RESULT_TEXTS)
def test_result_text_is_checked_for_json_when_structured_output_is_missing(root, tmp_path, case):
    """structured_output が無いとき、result の文字列が JSON として読めるかを記録する（中身は記録しない）。

    本物の CLI が結果を result の側に返すなら、ハーネスの読み方を直す。その判断の材料。
    """
    top, expected, shown = RESULT_TEXTS[case]
    output = {k: v for k, v in good_output().items() if k not in ("structured_output", "result")} | top
    code, result, screen = doctor(root, "--no-check", "--headless", env=fake_claude(tmp_path, output))
    assert code == 1 and "structured_output" in probe(result)["unexpected"]
    assert probe(result)["result_as_json"] == expected
    if shown is None:
        assert "result の文字列は" not in screen
    else:
        assert f"structured_output が無い。result の文字列は {shown}" in screen
    assert SECRET not in screen and SECRET not in json.dumps(result)


def test_doctor_does_not_run_a_claude_placed_inside_the_repository(root, tmp_path):
    """本体の中に置かれた claude は、PATH に書かれていても使わない（版を聞くための起動もしない）。"""
    planted = root / "tools"
    install_claude(planted, good_output())
    # PATH には、置いた場所だけを書く（使わなかった先で、本物の claude に届かないように）
    code, result, _ = doctor(root, "--no-check", "--headless", env={"PATH": str(planted)})
    assert code == 1
    assert result["claude"] == {"found": False, "api_key_env_set": False}
    assert result["headless"] == {"ran": False, "reason": "claude_not_found"}
    assert not was_run(planted)


def test_result_file_ends_lines_with_lf_where_text_files_get_crlf(root):
    """結果のファイルの改行は、テキストの書き込みが改行を CRLF に変えるOS（Windows）でも LF。"""
    with write_text_like_windows():
        code, _ = in_process(root, "--no-check", find=without("claude"))
    data = (root / ".cycle" / "doctor-result.json").read_bytes()
    assert code == 0 and b"\r" not in data and data.endswith(b"}\n") and data.count(b"\n") > 10


@pytest.mark.parametrize("encoding", ["ascii", "cp932"])
def test_doctor_does_not_crash_on_a_terminal_that_cannot_show_the_text(root, tmp_path, encoding):
    env = {**fake_claude(tmp_path, good_output()), "PYTHONIOENCODING": encoding}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--headless"], capture_output=True,
        env={**TEST_ENV, "CYCLE_ROOT": str(root), **env}, cwd=started_here(root), timeout=300,
    )
    assert proc.returncode == 0, proc.stderr.decode(encoding, errors="replace")
    assert b"Traceback" not in proc.stderr
    result = json.loads((root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8"))
    assert result["encoding"]["stdout"] == encoding and result["ok"] is True


# ------------------------------------------------------------ 役割ごとの確認、失敗の種類（H2）

def define_roles(root: Path, *names: str, restricted: str = "") -> None:
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for name in names:
        tools = "tools: Read, Grep\n" if name == restricted else "disallowedTools: Edit\n"
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n{tools}---\n本文\n", encoding="utf-8")


def test_headless_probe_runs_for_every_role_the_gates_use(root, tmp_path):
    """ゲートで使う役割（確認系の担当と、役割の定義にある実装の担当）ごとに、そのゲートの組み立てで1回ずつ。"""
    define_roles(root, "code-reviewer", "verifier", "ux-designer", "solver-engineer", "tooling-engineer",
                 restricted="ux-designer")
    script = "(here / 'calls.txt').open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')"
    env = fake_claude(tmp_path, good_output(), script=script)
    code, result, screen = doctor(root, "--no-check", "--headless", "--role", "verifier", env=env)
    assert code == 0 and [item["role"] for item in result["headless"]["roles"]] == ["verifier"]
    (tmp_path / "bin" / "calls.txt").unlink()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--no-check", "--headless"],
        capture_output=True,
        text=True, encoding="utf-8", env={**TEST_ENV, "CYCLE_ROOT": str(root), **env}, cwd=started_here(root),
    )
    result = json.loads((root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8"))
    roles = [(item["role"], item["gate"]) for item in result["headless"]["roles"]]
    assert roles == [
        ("code-reviewer", "review"), ("verifier", "verify"), ("ux-designer", "ux"),
        ("solver-engineer", "implement"), ("tooling-engineer", "implement"),
    ]
    lines = (tmp_path / "bin" / "calls.txt").read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines]
    cycle = DOCTOR.cycle
    assert calls == [cycle.claude_command(DOCTOR.PROBE_PROMPT, role, 1.0, gate)[1:] for role, gate in roles]
    # 道具の一覧（tools:）のある役割は、実行の前に知らせ、記録する
    restricted = [item["restricts_tools"] for item in result["headless"]["roles"]]
    assert restricted == [False, False, True, False, False]
    assert "役割 ux-designer の定義に、使える道具の一覧（tools:）がある" in proc.stdout
    assert proc.returncode == 0 and result["headless"]["unexpected"] == []


def test_each_role_is_judged_by_the_gate_it_is_used_for(root, tmp_path):
    """独立検証の担当だけは、基準ゼロ件の合格を、ハーネスと同じく合格にしない。"""
    output = good_output(structured_output={**PASS, "criteria": []})
    env = fake_claude(tmp_path, output)
    roles = ("--role", "verifier", "--role", "code-reviewer")
    code, result, screen = doctor(root, "--no-check", "--headless", *roles, env=env)
    assert code == 1
    assert result["headless"]["unexpected"] == ["verifier: harness_verdict"]
    assert [item["harness_verdict"] for item in result["headless"]["roles"]] == ["pass", "fail"]


def test_unknown_role_is_not_run(root, tmp_path):
    env = fake_claude(tmp_path, good_output())
    code, result, screen = doctor(root, "--no-check", "--headless", "--role", "no-such-role", env=env)
    assert code == 1 and result["headless"] == {"ran": False, "reason": "unknown_role"}
    assert claude_argv(tmp_path) == ["--version"]           # 版を聞いただけで、-p では起動していない


FAILURES = {
    "サインインしていない": ("Not logged in · Please run /login", "not_logged_in"),
    "利用枠の上限": ("Claude AI usage limit reached|1763200000", "usage_limit"),
    "モデルを使えない": (
        "There's an issue with the selected model (x). It may not exist or you may not have access to it.",
        "model_unavailable",
    ),
    "上限額": ("Reached maximum budget ($1)", "budget_exceeded"),
    "分類できない": ("something odd happened", "other"),
}


@pytest.mark.parametrize("case", FAILURES)
def test_kind_of_failure_is_recorded_without_the_text(root, tmp_path, case):
    """失敗したとき、原因の種類を、文面を記録せずに分類して出す（利用者にもう一度打ってもらわずに済むように）。"""
    text, expected = FAILURES[case]
    output = good_output(is_error=True, subtype="success", result=f"{text} {SECRET}", stop_reason="end_turn")
    del output["structured_output"]
    code, result, screen = doctor(root, "--no-check", "--headless", env=fake_claude(tmp_path, output))
    assert code == 1
    head = probe(result)
    assert head["failure"] == expected
    assert head["fields"]["stop_reason"] == {"present": True, "kind": "string", "value": "end_turn"}
    assert DOCTOR.FAILURE_TEXT[expected] in screen
    assert SECRET not in json.dumps(result) and SECRET not in screen


def test_failure_on_the_error_output_is_classified_too(root, tmp_path):
    env = fake_claude(tmp_path, "", exit_code=1, stderr=f"Error: Invalid API key {SECRET}\n")
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    assert code == 1 and probe(result)["failure"] == "not_logged_in"
    assert SECRET not in json.dumps(result) and SECRET not in screen


@pytest.mark.parametrize("args, ran", [((), False), (("--check",), True), (("--no-check", "--check"), False)])
def test_headless_leaves_the_check_out_unless_asked(root, tmp_path, args, ran):
    """--headless のときは、確認（数分かかる）を省くのが既定。--check で流す。"""
    code, result, screen = doctor(root, "--headless", *args, env=fake_claude(tmp_path, good_output()))
    assert result["check"]["ran"] is ran
    assert code == 0
