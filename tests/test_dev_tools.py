"""開発の道具（scripts/check.py と scripts/doctor.py）の試験。

本物の claude は起動しない。偽の claude（tests/test_harness.py と同じもの）を PATH の先頭に置く。
scripts/check.py の本物も起動しない（その中で pytest が走り、この試験がもう一度呼ばれるため）。
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from test_harness import PASS, TEST_ENV, claude_argv, fake_claude, headless

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


def test_check_runs_at_the_top_of_the_repository(tmp_path):
    out = tmp_path / "cwd.txt"
    code = f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.getcwd(), encoding='utf-8')"
    assert run_steps([code]) == 0
    assert Path(out.read_text(encoding="utf-8")).resolve() == ROOT


# ------------------------------------------------------------ scripts/doctor.py

@pytest.fixture
def root(tmp_path: Path) -> Path:
    """確認スクリプトだけを持つ、試験用のリポジトリの場所。CHECK_EXIT があれば、その終了コードで終わる。"""
    base = tmp_path / "root"
    (base / "scripts").mkdir(parents=True)
    script = "import os, sys\nprint('check output')\nsys.exit(int(os.environ.get('CHECK_EXIT', '0')))\n"
    (base / "scripts" / "check.py").write_text(script, encoding="utf-8")
    return base


def doctor(root: Path, *args: str, env: dict[str, str]) -> tuple[int, dict, str]:
    """doctor.py をコマンドラインから実行する。返すのは（終了コード、結果のJSON、画面の出力）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), *args], capture_output=True,
        text=True, encoding="utf-8", env={**TEST_ENV, "CYCLE_ROOT": str(root), **env},
    )
    path = root / ".cycle" / "doctor-result.json"
    assert path.is_file(), proc.stdout + proc.stderr
    return proc.returncode, json.loads(path.read_text(encoding="utf-8")), proc.stdout + proc.stderr


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
    assert set(result) >= {"os", "python", "encoding", "git", "claude", "check", "headless"}
    assert result["os"]["system"] and result["os"]["release"]
    assert result["python"]["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert result["python"]["supported"] is True
    assert result["git"]["found"] and result["git"]["runs"]
    assert result["git"]["version"].startswith("git version ")
    assert result["claude"]["found"] and result["claude"]["runs"]
    assert result["claude"]["version"] == "9.9.9 (fake)"
    assert result["claude"]["file"] == ("claude.cmd" if os.name == "nt" else "claude")
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
    def find(name: str) -> str | None:
        return None if name in missing or name != "git" else DOCTOR.cycle.shutil.which(name)
    return find


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


def test_version_text_that_does_not_look_like_a_version_is_not_recorded(root):
    proc = subprocess.CompletedProcess([], 0, f"token={SECRET}; rm -rf\n", "")
    with mock.patch.object(DOCTOR.cycle, "run_captured", return_value=proc):
        code, result = in_process(root, "--no-check", find=lambda name: f"/somewhere/{name}")
    assert result["git"]["runs"] is True and result["git"]["version"] is None
    assert SECRET not in json.dumps(result)


# ------------------------------------------------------------ doctor.py --headless

def test_headless_probe_runs_once_with_the_same_command_as_the_harness(root, tmp_path):
    env = fake_claude(tmp_path, good_output())
    code, result, screen = doctor(root, "--no-check", "--headless", "--budget", "0.5", env=env)
    assert code == 0, screen
    cycle = DOCTOR.cycle
    argv = claude_argv(tmp_path)
    assert argv == cycle.claude_command(DOCTOR.PROBE_PROMPT, "code-reviewer", 0.5, "Read")[1:]
    # ハーネスの run と、オプションの並びも、結果の形の指定も同じ
    harness = cycle.agent_command({"owner": "x", "spec": "docs/plan/U1-demo.md"}, "review", 0.5)[1:]
    assert argv[0::2] == harness[0::2]
    same = dict(zip(harness[0::2], harness[1::2], strict=True))
    used = dict(zip(argv[0::2], argv[1::2], strict=True))
    for option in ("--agent", "--output-format", "--json-schema", "--permission-mode", "--max-budget-usd"):
        assert used[option] == same[option]
    head = result["headless"]
    assert head["ran"] is True and head["timed_out"] is False and head["unexpected"] == []
    assert head["options"] == argv[0::2]
    assert head["launcher"] == ("claude.cmd" if os.name == "nt" else "claude")
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
        "problem": "", "verdict": "pass", "criteria": 0, "issues": 0,
    }
    assert "is_error" in head["keys"] and "structured_output" in head["keys"]
    # 実行の前に、何をするかを表示する
    assert "claude -p を1回だけ実行します" in screen and "利用枠を少し使います" in screen
    assert "ハーネスの前提どおりの形で返ってきた" in screen


def test_probe_prompt_and_gate_prompts_can_pass_through_a_batch_launcher():
    """Windows の claude.cmd は cmd.exe を通る。そこで崩れる文字を、指示に入れていないこと。"""
    cycle = DOCTOR.cycle
    state = {"owner": "solver-engineer", "spec": "docs/plan/U1-demo.md"}
    commands = [cycle.agent_command(state, gate, 5.0) for gate in cycle.AGENT_GATES]
    commands.append(cycle.claude_command(DOCTOR.PROBE_PROMPT, DOCTOR.PROBE_ROLE, 1.0, DOCTOR.PROBE_TOOLS))
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
    assert result["headless"]["unexpected"] == unexpected
    assert f"ハーネスの前提と違う点がある: {'、'.join(unexpected)}" in screen


def test_headless_shape_details_are_recorded(root, tmp_path):
    # JSON の一覧で返ってきた場合も、結果のオブジェクト（type が result）の形を記録する
    failed = good_output(subtype="error_max_turns")
    output = [{"type": "system", "session_id": "x"}, failed, {"type": "other"}]
    code, result, screen = doctor(root, "--no-check", "--headless", env=fake_claude(tmp_path, output))
    head = result["headless"]
    assert head["stdout"]["form"] == "json_array" and head["harness_verdict"] == "error"
    assert head["fields"]["subtype"]["value"] == "error_max_turns"
    assert head["fields"]["num_turns"] == {"present": True, "kind": "number", "value": 1}
    assert "出力の形 json_array、ハーネスの判定 error" in screen
    assert "あった項目: type、subtype、is_error、session_id、result、structured_output" in screen

    # 知らないと言われたオプションは、こちらが渡した名前に限って記録する
    stderr = "error: unknown option '--json-schema'\n(Did you mean --json?)\n"
    env = fake_claude(tmp_path, "", exit_code=1, stderr=stderr)
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    head = result["headless"]
    assert head["stdout"] == {"form": "empty", "characters": 0}
    assert head["messages"] == {
        "characters": len(stderr), "lines": 2, "rejected_options": ["--json-schema"], "mentions_login": False,
    }
    assert head["fields"] == {} and head["keys"] == []
    assert "CLI が知らないと言ったオプション: --json-schema" in screen

    # サインインを求める文面（出力の側に出た場合も）
    env = fake_claude(tmp_path, "Invalid API key · Please run /login", exit_code=1)
    code, result, screen = doctor(root, "--no-check", "--headless", env=env)
    head = result["headless"]
    assert head["stdout"]["form"] == "not_json"
    assert head["messages"]["mentions_login"] is True and head["messages"]["rejected_options"] == []
    assert "サインインや認証に触れている" in screen


def test_headless_probe_that_does_not_finish_is_a_problem(root, tmp_path):
    env = fake_claude(tmp_path, good_output(), script="time.sleep(20)")
    code, result, screen = doctor(root, "--no-check", "--headless", "--timeout", "1", env=env)
    assert code == 1
    head = result["headless"]
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
    fields = result["headless"]["fields"]
    assert fields["subtype"] == {"present": True, "kind": "string"}
    assert fields["is_error"] == {"present": True, "kind": "string"}
    assert fields["total_cost_usd"] == {"present": True, "kind": "string"}
    assert fields["structured_output"]["keys"] == ["criteria", "issues", "summary", "verdict"]
    assert result["headless"]["keys"] == sorted(
        key for key in output if SECRET not in key
    )
    # 結果のファイルのほかに、何も書き出していない
    assert [path.name for path in (root / ".cycle").iterdir()] == ["doctor-result.json"]


@pytest.mark.parametrize("encoding", ["ascii", "cp932"])
def test_doctor_does_not_crash_on_a_terminal_that_cannot_show_the_text(root, tmp_path, encoding):
    env = {**fake_claude(tmp_path, good_output()), "PYTHONIOENCODING": encoding}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--headless"], capture_output=True,
        env={**TEST_ENV, "CYCLE_ROOT": str(root), **env},
    )
    assert proc.returncode == 0, proc.stderr.decode(encoding, errors="replace")
    assert b"Traceback" not in proc.stderr
    result = json.loads((root / ".cycle" / "doctor-result.json").read_text(encoding="utf-8"))
    assert result["encoding"]["stdout"] == encoding and result["ok"] is True
