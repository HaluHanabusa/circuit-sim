"""1か所ずつ壊す確認の仕組み（scripts/mutants.py）の試験。

- 壊し方の作り方（機械的に作る種類、表示だけの場所を除くこと、要素2つのタプルは (x,) の形のまま）。
- 流し方: 試験が落ちれば「落ちた」、通れば「生き残り」、同値として一覧にあれば数えない。別枠（そのOSでだけ
  落とせる）は、ほかのOSでは流さずに別に数える。
- 結果を得られない実行を、落ちたと数えない: 壊す前の試験が通らない（途中で 0 で終わる場合を含む）、
  遮断を組めない（終了コード 72）、入れ子（70）、作れない壊し方（読めない形）。
- 壊す場所: 開始時の内容（写し）に当てる。流している間に作業中のフォルダが変わっても、ずれない。
- 後始末: 試験が残したプロセス（親を失った偽の claude など）を止める（25番）。
- 代表: このリポジトリの決まりに関わる壊し方のいくつかを、近い試験で流し、落ちることを確かめる
  （全件は手で流す。docs/design/harness.md の5章）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import SAFE_PATH_VAR
from test_harness import TEST_ENV, install_command, sh

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = ROOT / "scripts" / "mutants.py"


def load():
    spec = importlib.util.spec_from_file_location("mutants_script", MUTANTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module             # dataclasses が、読み込んだ場所を引く
    spec.loader.exec_module(module)
    return module


M = load()

SAMPLE = '''\
"""説明。"""
NAMES = ("A", "B")
MORE = ("C", "D", "E")
PAIR = ("F", "G",)


def judge(x, items):
    """説明の文字列は壊さない。"""
    print(f"x == {x == 1}")
    if x == 1 and items:
        return True
    if not items:
        raise ValueError("空")
    while x:
        x -= 1
        continue
    record(x)
    return x if x else False
'''


def test_mutants_are_made_where_decisions_are_and_not_in_text_for_the_screen(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "cycle.py").write_text(SAMPLE, encoding="utf-8")
    made = {m.id: (m.original, m.replacement) for m in M.generated(tmp_path, ("scripts/cycle.py",))}
    assert made == {
        # 要素2つのタプルから1つ外すと、残る1つを (x,) の形にする（("B") は文字列で、別の壊し方になる）
        "cycle:<module>:elem:1": ('"A", "B"', '"B",'),
        "cycle:<module>:elem:2": (', "B"', ","),
        "cycle:<module>:elem:3": ('"C", ', ""),
        "cycle:<module>:elem:4": ('"D", ', ""),
        "cycle:<module>:elem:5": (', "E"', ""),
        "cycle:<module>:elem:6": ('"F", "G"', '"G"'),       # 後ろの , は、もとからある
        "cycle:<module>:elem:7": (', "G"', ""),
        "cycle:judge:bool:1": ("x == 1 and items", "((x == 1) or (items))"),
        "cycle:judge:cmp:1": ("x == 1", "(x) != (1)"),
        "cycle:judge:num:1": ("1", "2"),
        "cycle:judge:const:1": ("True", "False"),
        "cycle:judge:const:2": ("False", "True"),
        "cycle:judge:not:1": ("not items", "(items)"),
        "cycle:judge:del:1": ('raise ValueError("空")', "pass"),
        "cycle:judge:cond:1": ("x", "not (x)"),
        "cycle:judge:cond:2": ("x", "not (x)"),
        "cycle:judge:del:2": ("continue", "pass"),
        "cycle:judge:del:3": ("record(x)", "pass"),
    }
    lists = {}
    for mutant in M.generated(tmp_path, ("scripts/cycle.py",)):
        text = M.mutated_text(tmp_path, mutant)
        compile(text, "x", "exec")                     # どれも読める形
        if mutant.kind == "elem":
            space: dict = {}
            exec(text, space)
            lists[mutant.id] = (space["NAMES"], space["MORE"], space["PAIR"])
    assert lists["cycle:<module>:elem:1"][0] == ("B",) and lists["cycle:<module>:elem:2"][0] == ("A",)
    assert lists["cycle:<module>:elem:4"][1] == ("C", "E")
    assert lists["cycle:<module>:elem:6"][2] == ("G",) and lists["cycle:<module>:elem:7"][2] == ("F",)


def test_mutant_is_not_applied_where_the_text_has_moved(tmp_path):
    """壊す場所の中身が、壊し方を作ったときと違えば、ずれた場所を壊さない（None）。"""
    (tmp_path / "scripts").mkdir()
    target = tmp_path / "scripts" / "cycle.py"
    target.write_text(SAMPLE, encoding="utf-8")
    mutant = next(m for m in M.generated(tmp_path, ("scripts/cycle.py",)) if m.id == "cycle:judge:cmp:1")
    target.write_text("# 足した行\n" + SAMPLE, encoding="utf-8")
    assert M.mutated_text(tmp_path, mutant) is None


FAKE_TESTS = '''\
import importlib.util, os, pathlib, time

import pytest

here = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("c", here / "scripts" / "cycle.py")
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


def test_edit_elsewhere():
    """流している間に、作業中のフォルダ（EDIT_DURING_RUN）が変わる。この試験が見た cycle.py を控える。"""
    target = os.environ.get("EDIT_DURING_RUN")
    if target and not os.path.exists(target + ".done"):
        path = pathlib.Path(target)
        path.write_text("# 足した行\\n" * 3 + path.read_text(encoding="utf-8"), encoding="utf-8")
        pathlib.Path(target + ".done").touch()
    seen = os.environ.get("SEEN_DIR")
    if seen:
        text = (here / "scripts" / "cycle.py").read_text(encoding="utf-8")
        (pathlib.Path(seen) / f"{time.time_ns()}.py").write_text(text, encoding="utf-8")


def test_judge():
    assert not os.environ.get("BASELINE_BREAK")
    assert cycle.judge(1) == "positive"


def test_spawn():
    marker = os.environ.get("SPAWN_MARKER")
    if marker:
        cycle.spawn(marker)
        deadline = time.monotonic() + 10
        while not pathlib.Path(marker).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def test_session_cannot_start():
    """壊れたときに、試験を始められない終了コード（遮断を組めない 72、入れ子 70 の模擬）で止まる。"""
    code = os.environ.get("STOP_CODE_WHEN_BROKEN")
    if code and cycle.judge(1) != "positive":
        pytest.exit("試験を始めない（模擬）", returncode=int(code))


def test_ends_early():
    if os.environ.get("EXIT_EARLY"):
        os._exit(0)


def test_sleeps():
    time.sleep(float(os.environ.get("SLEEP", "0")))


def test_runs_a_copy():
    """道具を別の場所に写して、子の Python で流す（scripts/check.py を写して流す試験の形）。"""
    if os.environ.get("RUN_COPY"):
        import shutil, subprocess, sys, tempfile
        target = pathlib.Path(tempfile.mkdtemp()) / "cycle.py"
        shutil.copy(here / "scripts" / "cycle.py", target)
        code = (
            "import importlib.util, sys; spec = importlib.util.spec_from_file_location('c', sys.argv[1]); "
            "c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c); "
            "sys.exit(0 if c.judge(1) == 'positive' else 1)"
        )
        assert subprocess.run([sys.executable, "-c", code, str(target)]).returncode == 0
'''


def fake_repo(base: Path, listing: str = "") -> Path:
    """壊す対象（scripts/cycle.py）と、その試験だけを持つリポジトリ。"""
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts" / "cycle.py").write_text(textwrap.dedent('''\
        import subprocess, sys


        def judge(x):
            if x > 0:
                return "positive"
            return "other"


        def spawn(marker):
            """親を失っても残るプロセスを起動する（偽の claude が残る場合の模擬）。"""
            code = f"import pathlib, time; pathlib.Path({marker!r}).write_text('alive'); time.sleep(600)"
            subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        '''), encoding="utf-8")
    for name in ("doctor.py", "local_ci.py", "check.py"):
        (root / "scripts" / name).write_text("", encoding="utf-8")
    (root / "scripts" / "mutants.toml").write_text(listing, encoding="utf-8")
    (root / "tests" / "test_fake.py").write_text(FAKE_TESTS, encoding="utf-8")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


def run(
    root: Path, *args: str, env: dict | None = None, select: bool = False,
) -> tuple[subprocess.CompletedProcess, dict]:
    options = [] if select else ["--no-select"]
    proc = subprocess.run(
        [sys.executable, str(MUTANTS), "run", "--root", str(root), "--file", "scripts/cycle.py",
         "--jobs", "1", *options, *args],
        capture_output=True, text=True, encoding="utf-8", env={**TEST_ENV, **(env or {})}, timeout=600,
    )
    results = sorted((root / ".cycle" / "mutants").glob("result-*.json"))
    assert results, proc.stdout + proc.stderr
    summary = json.loads(results[-1].read_text(encoding="utf-8"))
    for path in results:
        path.unlink()
    return proc, summary


@pytest.mark.parametrize("select", [False, True], ids=["全体を流す", "通った行で選ぶ"])
def test_killed_and_surviving_mutants_are_told_apart(tmp_path, select):
    root = fake_repo(tmp_path)
    wanted = ("cycle:judge:cmp:1", "cycle:spawn:del:1")
    proc, summary = run(root, "--only", *wanted, "--tests", "tests/test_fake.py", select=select)
    statuses = {item["id"]: item["status"] for item in summary["results"]}
    # judge の比べ方を反転すると試験が落ちる。spawn の起動を消しても、印の無い試験は気づかない
    assert statuses == {"cycle:judge:cmp:1": "killed", "cycle:spawn:del:1": "survived"}
    assert proc.returncode == 1 and summary["unexpected"] == ["cycle:spawn:del:1"]
    assert summary["verdict"] == "fail"
    assert "survived: cycle:spawn:del:1" in proc.stdout
    assert (root / "scripts" / "cycle.py").read_text(encoding="utf-8").count("subprocess.Popen") == 1
    assert summary["source"]["commit"] == sh(root, "git", "rev-parse", "HEAD")


def test_surviving_mutant_listed_as_equivalent_with_a_reason_is_not_counted(tmp_path):
    listed = '[[equivalent]]\nid = "cycle:spawn:del:1"\nreason = "試験用"\n'
    root = fake_repo(tmp_path, listed)
    proc, summary = run(root, "--only", "cycle:spawn:del:1", "--tests", "tests/test_fake.py")
    assert proc.returncode == 0 and summary["survived"] == ["cycle:spawn:del:1"]
    assert summary["unexpected"] == [] and summary["verdict"] == "pass"
    proc, summary = run(root, "--tests", "tests/test_fake.py")      # 一覧の同値は、既定では流さない
    assert "cycle:spawn:del:1" not in {item["id"] for item in summary["results"]}
    assert summary["equivalent"] == ["cycle:spawn:del:1"]


@pytest.mark.parametrize("platform", ["windows", "posix"])
def test_platform_only_mutant_is_counted_apart_where_it_cannot_be_killed(tmp_path, platform):
    """別枠（そのOSでだけ通る分岐）は、ほかのOSでは流さずに別に数える。そのOSでは流し、生き残れば数える。"""
    listed = (
        f'[[platform_only]]\nid = "cycle:spawn:del:1"\nplatform = "{platform}"\nreason = "試験用"\n'
        '[[equivalent]]\nid = "cycle:spawn:const:1"\nreason = "試験用"\n'
    )
    root = fake_repo(tmp_path, listed)
    proc, summary = run(root, "--tests", "tests/test_fake.py")
    ran = "cycle:spawn:del:1" in {item["id"] for item in summary["results"]}
    here = platform == M.THIS_PLATFORM
    assert ran is here
    assert summary["platform_only_not_run"] == ([] if here else ["cycle:spawn:del:1"])
    assert summary["unexpected"] == (["cycle:spawn:del:1"] if here else [])
    assert proc.returncode == (1 if here else 0)
    assert f"別枠（このOSでは落とせない分岐）{0 if here else 1}" in proc.stdout


@pytest.mark.parametrize("listing, message", [
    ('[[platform_only]]\nid = "x"\nplatform = "dos"\nreason = "r"\n', "platform"),
    ('[[platform_only]]\nid = "x"\nplatform = "windows"\n', "reason"),
    (
        '[[equivalent]]\nid = "x"\nreason = "r"\n[[platform_only]]\nid = "x"\nplatform = "windows"\n'
        'reason = "r"\n',
        "同値と別枠の両方",
    ),
])
def test_list_with_an_unknown_platform_or_without_a_reason_is_refused(tmp_path, listing, message):
    root = fake_repo(tmp_path, listing)
    proc, summary = run(root, "--tests", "tests/test_fake.py")
    assert proc.returncode == 2 and summary["verdict"] == "error" and message in summary["reason"]


@pytest.mark.parametrize("code", [70, 72])
def test_run_that_could_not_start_the_tests_is_not_counted_as_killed(tmp_path, code):
    """試験を始められなかった実行（72 = 遮断を組めない・見張り役を起動できない、70 = 入れ子）は、
    落ちたと数えない。71（見張り役が起動された）とは分けて数える（tests/conftest.py）。"""
    root = fake_repo(tmp_path)
    proc, summary = run(
        root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py::test_session_cannot_start",
        env={"STOP_CODE_WHEN_BROKEN": str(code)},
    )
    assert summary["errors"] == ["cycle:judge:cmp:1"] and summary["killed"] == 0
    assert summary["results"][0]["how"] == f"error-{code}"
    assert proc.returncode == 1 and summary["verdict"] == "fail"


def test_run_that_started_the_sentinel_is_counted_as_killed(tmp_path):
    """71（見張り役の claude が起動された）は、壊れたことに試験が気づいた（落ちた）と数える。"""
    root = fake_repo(tmp_path)
    proc, summary = run(
        root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py::test_session_cannot_start",
        env={"STOP_CODE_WHEN_BROKEN": "71"},
    )
    assert [(item["status"], item["how"]) for item in summary["results"]] == [("killed", "sentinel")]
    assert proc.returncode == 0 and summary["verdict"] == "pass"


@pytest.mark.parametrize("select", [False, True], ids=["全体を流す", "通った行で選ぶ"])
@pytest.mark.parametrize(
    "env", [{"BASELINE_BREAK": "1"}, {"EXIT_EARLY": "1"}], ids=["落ちる", "途中で 0 で終わる"],
)
def test_baseline_that_does_not_pass_gives_no_result(tmp_path, select, env):
    """壊す前の試験が通らなければ（落ちる、途中で終了コード 0 で終わる）、何も壊さず「結果を得られなかった」。
    壊す前に落ちる試験は、どの壊し方も「落ちた」に見せる（遮断を組めない一時フォルダなど）。"""
    root = fake_repo(tmp_path)
    proc, summary = run(root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py", env=env,
                        select=select)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert summary["verdict"] == "error" and "壊す前の試験が通らない" in summary["reason"]
    assert "results" not in summary and "killed" not in summary


def test_baseline_is_run_every_time(tmp_path):
    """壊す前の試験は、記録を使い回さず、毎回流す（試験が読むものや環境が変わっても、通ることを確かめる）。"""
    root = fake_repo(tmp_path)
    proc, _ = run(root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py", select=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc, summary = run(root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py", select=True,
                        env={"BASELINE_BREAK": "1"})
    assert proc.returncode == 2 and summary["verdict"] == "error"


def test_mutant_that_cannot_be_read_is_counted_in_the_exit_code(tmp_path):
    listed = (
        '[[mutant]]\nid = "hand:broken"\nfile = "scripts/cycle.py"\n'
        'original = "return \\"other\\""\nreplacement = "return ("\n'
    )
    root = fake_repo(tmp_path, listed)
    proc, summary = run(root, "--only", "hand:broken", "--tests", "tests/test_fake.py")
    assert summary["invalid"] == ["hand:broken"] and summary["verdict"] == "fail"
    assert proc.returncode == 1 and "作れなかったもの" in proc.stdout


def test_mutant_is_applied_to_the_content_at_the_start_even_if_the_working_folder_changes(tmp_path):
    """壊し方は、開始時の内容の写しに当てる。流している間に作業中のフォルダが変わっても（ここでは、
    壊す前の試験が、作業中のフォルダの cycle.py の先頭に行を足す）、壊す場所はずれず、作業中のフォルダにも
    触らない。"""
    root = fake_repo(tmp_path)
    original = (root / "scripts" / "cycle.py").read_text(encoding="utf-8")
    seen = tmp_path / "seen"
    seen.mkdir()
    env = {"EDIT_DURING_RUN": str(root / "scripts" / "cycle.py"), "SEEN_DIR": str(seen)}
    tests = ("tests/test_fake.py::test_edit_elsewhere", "tests/test_fake.py::test_judge")
    proc, summary = run(root, "--only", "cycle:judge:cmp:1", "--tests", *tests, env=env)
    assert [item["status"] for item in summary["results"]] == ["killed"], proc.stdout + proc.stderr
    texts = [path.read_text(encoding="utf-8") for path in sorted(seen.iterdir())]
    assert texts == [original, original.replace("if x > 0:", "if (x) <= (0):")]   # 壊す前、壊した後
    assert (root / "scripts" / "cycle.py").read_text(encoding="utf-8") == "# 足した行\n" * 3 + original


def test_tests_that_run_a_copy_of_the_tool_are_chosen_for_its_lines(tmp_path):
    """試験が道具を別の場所に写して流しても、名前と中身が同じなら、その道具の行を通った試験として選ぶ
    （選ばないと、その試験が落とす壊し方を、生き残りに数える。全件の2回目で、check.py の1件がそうだった）。"""
    root = fake_repo(tmp_path)
    proc, summary = run(
        root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py::test_runs_a_copy",
        env={"RUN_COPY": "1"}, select=True,
    )
    assert [(item["status"], item["stage"]) for item in summary["results"]] == [("killed", "selected")], (
        proc.stdout + proc.stderr
    )


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="残ったプロセスを /proc で探す（Linux）")
def test_processes_left_by_the_tests_are_stopped_after_each_run(tmp_path):
    root = fake_repo(tmp_path)
    marker = tmp_path / "spawned"
    proc, summary = run(
        root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py::test_spawn",
        env={"SPAWN_MARKER": str(marker)},
    )
    assert marker.exists()                      # 試験は、親を失っても残るプロセスを起動した
    assert summary["leftovers_stopped"] >= 1
    time.sleep(0.3)
    assert str(marker) not in "\n".join(command_lines())       # 止められて、もう無い


def command_lines() -> list[str]:
    lines = []
    for entry in Path("/proc").iterdir():
        try:
            lines.append((entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"))
        except OSError:
            continue
    return lines


def test_git_inside_the_target_is_not_used_to_copy_it(tmp_path):
    """写しを作る git は、ハーネスと同じ探し方の絶対パス（対象のリポジトリの中の git を動かさない）。"""
    root = fake_repo(tmp_path)
    planted = root / "tools" / "bin"
    planted.mkdir(parents=True)
    marker = tmp_path / "planted-git-ran"
    install_command(planted, "git", f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n")
    calls = []
    real = M.subprocess.run

    def noted(cmd, *args, **kwargs):
        calls.append(cmd)
        return real(cmd, *args, **kwargs)

    env = {"PATH": f"{planted}{os.pathsep}{os.environ[SAFE_PATH_VAR]}"}
    with mock.patch.dict(os.environ, env), mock.patch.object(M.subprocess, "run", noted):
        M.copy_repo(root, tmp_path / "copy")
        expected = M.HARNESS.find_command("git", (root,))
    assert not marker.exists() and (tmp_path / "copy" / "tests" / "test_fake.py").is_file()
    assert Path(calls[0][0]).is_absolute() and calls[0][0] == expected
    assert Path(expected).parent != planted


def test_tests_that_run_too_long_are_stopped_with_everything_they_started(tmp_path):
    """時間切れの試験は、ハーネスと同じ止め方（kill_tree。Windows では System32 の taskkill /T）で止める。"""
    root = fake_repo(tmp_path)
    helpers = M.helper_dir(tmp_path / "work")
    stopped = []
    real = M.HARNESS.kill_tree

    def noted(proc):
        stopped.append(proc.pid)
        real(proc)

    with mock.patch.dict(os.environ, {"SLEEP": "60"}), mock.patch.object(M.HARNESS, "kill_tree", noted):
        outcome, seconds, _ = M.run_tests(root, helpers, ["tests/test_fake.py::test_sleeps"], None, None, 3)
    assert outcome == "timeout" and len(stopped) == 1 and seconds < 30


REPRESENTATIVE = {
    "hand:lock-shared": "tests/test_harness_os.py::test_lock_is_exclusive_and_waits_even_for_a_shared_holder",
    "cycle:git_paths:del:1": (
        "tests/test_harness.py::test_failure_of_git_listing_files_is_an_error_not_an_empty_list"
    ),
}


@pytest.mark.skipif(os.name == "nt", reason="共有のロックは Linux / macOS にだけある")
@pytest.mark.parametrize("mutant", REPRESENTATIVE)
def test_representative_mutants_of_the_harness_are_killed(mutant):
    """決まりに関わる壊し方の代表（H1 からの生き残り）を、近い試験で流すと落ちる。全件は手で流す。"""
    proc = subprocess.run(
        [sys.executable, str(MUTANTS), "run", "--only", mutant, "--no-select", "--jobs", "1",
         "--tests", REPRESENTATIVE[mutant]],
        capture_output=True, text=True, encoding="utf-8", env=TEST_ENV, timeout=600, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 通り: 落ちた 1、生き残り 0" in proc.stdout
