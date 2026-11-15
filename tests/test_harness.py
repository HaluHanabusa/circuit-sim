"""開発サイクルのスクリプト（scripts/cycle.py）が、ゲートの決まりを守らせることを確かめる。

どのテストも、決まりを1つ外すと落ちるように書く（スクリプトを1か所ずつ壊して確かめている）。
Windows、macOS、Linux のどれでも同じ試験を流す。偽の確認スクリプトと偽の claude は Python で書く。
OSに無い機能そのものを試す場合だけ、理由を付けてスキップする。
"""
import codecs
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from conftest import SAFE_PATH_VAR

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

CYCLE = Path(__file__).resolve().parents[1] / "scripts" / "cycle.py"


def load_cycle():
    spec = importlib.util.spec_from_file_location("cycle_script", CYCLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CYCLE_MODULE = load_cycle()
CYCLE_MAIN = CYCLE_MODULE.main

# 利用者の git の設定の代わりに読ませる、空のファイル
EMPTY_GIT_CONFIG = Path(tempfile.gettempdir()) / "circuit-sim-test-empty.gitconfig"
EMPTY_GIT_CONFIG.write_text("", encoding="utf-8")

# 試験の中の git は、利用者の設定（署名、フック、改行の変換など）や、呼び出し元の GIT_DIR などに
# 左右されないようにする。起動した Python の出力は、端末の文字コードに依らず UTF-8 で受け取る。
# 実行した場所でコマンドを探させない印（NoDefaultCurrentDirectoryInExePath）は、呼び出し元から引き継がない
# （スクリプトが自分で置くことを確かめる試験が、呼び出し元の印で通ってしまわないように）
TEST_ENV = {
    **{
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key.lower() != "nodefaultcurrentdirectoryinexepath"
    },
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": str(EMPTY_GIT_CONFIG), "GIT_CONFIG_NOSYSTEM": "1",
    "PYTHONIOENCODING": "utf-8",
}



def only_posix(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(WINDOWS, reason=reason)


def only_windows(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not WINDOWS, reason=reason)


SPEC = """---
id: U1
owner: solver-engineer
---

# U1: 試験用の作業単位
"""

# 試験用の確認スクリプト（scripts/check.py）。FAIL があるか、A と B が両方あると失敗する
CHECK = """\
import pathlib
import sys

have = {name for name in ("FAIL", "A", "B") if pathlib.Path(name).exists()}
sys.exit(1 if "FAIL" in have or {"A", "B"} <= have else 0)
"""

# 環境変数 HANG があるときだけ、印を置いて止まる確認スクリプト（印に .stop を付けたファイルが置かれるまで）
HANGING_CHECK = """\
import os
import pathlib
import time

hang = os.environ.get("HANG")
if hang:
    pathlib.Path(hang).touch()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not pathlib.Path(hang + ".stop").exists():
        time.sleep(0.02)
"""

# 環境変数 WAIT があれば合図を待ってから合格し、BROKEN があれば失敗する確認スクリプト
WAITING_CHECK = """\
import os
import pathlib
import sys
import time

wait = os.environ.get("WAIT")
if wait:
    pathlib.Path(wait + ".started").touch()
    while not pathlib.Path(wait + ".go").exists():
        time.sleep(0.02)
sys.exit(1 if os.environ.get("BROKEN") else 0)
"""

# 偽の claude。受け取った引数を控え、script を実行してから、用意された出力を返す
FAKE_CLAUDE = """\
import json
import pathlib
import sys
import time

here = pathlib.Path({here!r})
(here / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
if "--version" in sys.argv:
    print("9.9.9 (fake)")
    sys.exit(0)
{script}
sys.stdout.buffer.write((here / "out.json").read_bytes())
sys.stderr.buffer.write((here / "err.txt").read_bytes())
sys.exit({exit_code})
"""

# 合格の結果（独立検証の合格には、確かめた基準が1つ以上要る）
PASS = {
    "verdict": "pass", "summary": "ok", "criteria": [{"name": "基準1", "measured": "1", "pass": True}],
    "issues": [],
}


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=TEST_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(base: Path) -> Path:
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "plan").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text(CHECK, encoding="utf-8")
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


def start_cycle(
    root: Path, *args: str, env: dict[str, str] | None = None, stdin: int | None = None,
) -> subprocess.Popen:
    """cycle.py をコマンドラインから起動し、終わるのを待たずに返す（並行する実行、ロック、中断の確認用）。"""
    if WINDOWS:     # Ctrl-Break の合図を、このプロセスだけに送れるようにする
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:           # 呼び出し元が SIGINT を無視していても、既定の扱い（KeyboardInterrupt）に戻す
        options = {"preexec_fn": lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)}
    return subprocess.Popen(
        [sys.executable, str(CYCLE), *args], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=cycle_env(root, env), **options,
    )


def run_cycle(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """cycle.py をコマンドラインから起動して、終わるまで待つ。"""
    proc = start_cycle(root, *args, env=env)
    out, err = proc.communicate(timeout=60)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


@contextlib.contextmanager
def held_lock(path: Path, shared: bool = False) -> Iterator[None]:
    """別の実行がロックを持っている状態を作る（スクリプトの実装を通さず、OSの仕組みを直接使う）。

    shared なら、共有のロック（読む側のロック）を持つ。スクリプトが排他のロックを取っていれば、それでも待つ。
    """
    with open(path, "a", encoding="utf-8") as handle:
        if WINDOWS:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


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


def install_command(directory: Path, name: str, source: str) -> None:
    """Python で書いたコマンドを、その名前で起動できる形で directory に置く。

    Windows では、npm で入れた CLI と同じ形（名前.cmd が実体を呼ぶ）にする。
    それ以外では、実行できるファイルにする。
    """
    if WINDOWS:
        script = directory / f"{name}-script.py"
        script.write_text(source, encoding="utf-8")
        # cmd.exe が読むファイルなので、cmd.exe の文字コードで書く
        (directory / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="oem")
    else:
        command = directory / name
        command.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        command.chmod(0o755)


def install_claude(
    directory: Path, output: object, script: str = "", exit_code: int = 0, stderr: str = "",
) -> None:
    """偽の claude を directory に置く。script（Python）を実行してから output を出力する。"""
    directory.mkdir(parents=True, exist_ok=True)
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    (directory / "out.json").write_text(text, encoding="utf-8")
    (directory / "err.txt").write_text(stderr, encoding="utf-8")
    source = FAKE_CLAUDE.format(here=str(directory), script=script, exit_code=exit_code)
    install_command(directory, "claude", source)


def fake_claude(
    tmp_path: Path, output: object, script: str = "", exit_code: int = 0, stderr: str = "",
) -> dict[str, str]:
    """偽の claude を PATH の先頭に置く環境変数を返す。script（Python）を実行してから output を出力する。

    後ろに続くのは、試験のセッションの始めに claude のある場所を取り除き、末尾に見張り役を置いた PATH
    （tests/conftest.py）。偽が飛ばされたり起動に失敗したりしても、本物には届かず、見張り役が全体を失敗にする。
    """
    fake = tmp_path / "bin"
    install_claude(fake, output, script, exit_code, stderr)
    return {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}


CLAUDE_FILES = ("claude", "claude.exe", "claude.cmd", "claude.bat")


def was_run(directory: Path) -> bool:
    """directory に置いた偽の claude が、一度でも起動されたか。"""
    return (directory / "argv.json").exists()


def claude_argv(tmp_path: Path) -> list[str]:
    """偽の claude が最後に受け取った引数。"""
    return json.loads((tmp_path / "bin" / "argv.json").read_text(encoding="utf-8"))


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
    proc = run_cycle(repo, "status")
    assert proc.returncode == 0 and "進行中の作業単位は無い" in proc.stdout
    proc = run_cycle(repo, "status", "U1")
    assert proc.returncode == 2 and "エラー: U1 はまだ始まっていない" in proc.stderr


@pytest.mark.parametrize("encoding", ["ascii", "cp932", "cp1252"])
def test_output_does_not_crash_on_a_terminal_that_cannot_show_the_text(repo, encoding):
    """UTF-8 でない端末（日本語 Windows の cp932 など）でも、表せない文字を置き換えて最後まで動く。"""
    wt = implement(repo)
    (wt / "µ.py").write_text("\n", encoding="utf-8")          # cp932 に無い文字の名前のファイル
    pass_all(repo)
    (wt / "µ.py").write_text("x = 1\n", encoding="utf-8")
    env = {"PYTHONIOENCODING": encoding}
    proc = subprocess.run(
        [sys.executable, str(CYCLE), "status", "U1"], capture_output=True, env=cycle_env(repo, env),
    )
    out = proc.stdout.decode(encoding)
    assert proc.returncode == 0 and proc.stderr == b"", proc.stderr.decode(encoding, errors="replace")
    assert "review: " in out and ".py" in out
    if encoding == "cp932":         # 日本語はそのまま出て、表せない µ だけが置き換わる
        assert "review: 確認の後で変わったファイルがある（?.py）。やり直す" in out
    # エラーの表示（標準エラー）も、同じ置き換えで出る
    proc = subprocess.run(
        [sys.executable, str(CYCLE), "status", "µ"], capture_output=True, env=cycle_env(repo, env),
    )
    assert proc.returncode == 2
    assert b"Traceback" not in proc.stderr and b"\\x" not in proc.stderr and b"\\u" not in proc.stderr


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
    # 記録に残すパスは、どのOSでも / 区切り
    report = state(repo)["gates"]["check"]["report"]
    assert report.startswith(".cycle/runs/U1-check-") and (repo / report).is_file()
    assert state(repo)["spec"] == "docs/plan/U1-demo.md"
    # どの経路の記録かも残る
    assert {gate: entry["source"] for gate, entry in state(repo)["gates"].items()} == {
        "implement": "session", "review": "session", "verify": "session", "check": "script",
    }
    assert not (repo / ".cycle" / "merge-pending.json").exists()
    assert "U1（docs/plan/U1-demo.md、担当 solver-engineer）  統合済み" in cycle(repo, "status", "U1").stdout
    # 取り込んだ後の確認の出力も、記録に残る
    assert len(list((repo / ".cycle" / "runs").glob("U1-merge-check-*.log"))) == 1


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


def test_gate_that_did_not_pass_gives_only_that_reason(repo):
    """合格でないゲートは、その理由（fail）だけを挙げ、古さの理由を重ねない（どれをやり直すかが読める）。"""
    wt = implement(repo)
    tree = snap(repo)
    record(repo, "review", "rev-1", tree=tree)
    record(repo, "verify", "ver-1", verdict="fail", tree=tree)
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")       # 記録の後で、製品のコードが変わる
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    lines = [line for line in cycle(repo, "merge", "U1").stdout.splitlines() if line.startswith("  - ")]
    assert [line for line in lines if line.startswith("  - verify:")] == ["  - verify: fail"]
    assert "  - review: 確認の後で変わったファイルがある（product.py）。やり直す" in lines


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


@pytest.mark.parametrize("text, message", [
    ("# 見出しなし\n", "先頭に --- で囲んだ見出し"),
    ("# U1\nid: U1\nowner: solver-engineer\n---\n", "先頭に --- で囲んだ見出し"),    # 1行目が --- でない
    ("---\nid: U1\nowner: solver-engineer\n", "見出しが --- で閉じられていない"),
    ("---\nowner: solver-engineer\n---\n", "見出しに id が無い"),
    ("---\nid: U1\n---\n", "見出しに owner が無い"),
    ("---\nid: U1\nowner:\n---\n", "見出しに owner が無い"),
])
def test_spec_without_a_complete_header_is_rejected(repo, text, message):
    (repo / "docs" / "plan" / "U1-demo.md").write_text(text, encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert message in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_blank_lines_in_the_header_are_allowed(repo):
    """見出しの中の空行は、読み飛ばす（読めない行として拒まない）。"""
    text = "---\nid: U1\n\nowner: solver-engineer\n   \nneeds_ux: true\n---\n# U1\n"
    (repo / "docs" / "plan" / "U1-demo.md").write_text(text, encoding="utf-8")
    commit(repo, "見出しに空行")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 0, result.stderr
    assert state(repo)["owner"] == "solver-engineer" and "ux" in CYCLE_MODULE.required_gates(state(repo))


@pytest.mark.parametrize("count", [0, 2])
def test_unit_needs_exactly_one_spec(repo, count):
    spec = repo / "docs" / "plan" / "U1-demo.md"
    if count == 0:
        spec.unlink()
    else:
        spec.with_name("U1-other.md").write_text(SPEC, encoding="utf-8")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert f"U1 の仕様が1つだけ必要（見つかった数: {count}）" in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_state_file_of_another_shape_is_rejected(repo):
    implement(repo)
    (repo / ".cycle" / "state" / "U1.json").write_text("[]", encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 2
    assert "U1.json の形が正しくない" in result.stderr
    assert main_log(repo) == ["init"]


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

def test_implement_record_is_not_compared_with_the_content(repo):
    """実装の記録は、作った結果を残すだけ。その後で内容が変わっても、確認系のゲートを取り直せば統合できる。"""
    wt = implement(repo)
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")       # 実装の記録の後で、手直しする
    pass_all(repo)
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout


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


def test_exception_paths_take_only_test_files_and_records(repo):
    """例外の場所に後から足せるのは、受け入れ試験（test_*.py）と記録（*.md）だけ。

    conftest.py ひとつで、受け入れ試験を全部外せる（試験の集め方を変える）。
    """
    wt = implement(repo)
    pass_all(repo)
    refused = [
        "docs/verification/notes.txt", "docs/verification/run.py", "tests/acceptance/__init__.py",
        "tests/acceptance/conftest.py", "tests/acceptance/data.json", "tests/acceptance/pytest.ini",
        "tests/acceptance/sub/conftest.py", "tests/acceptance/test_x.py.bak",
    ]
    allowed = [
        "docs/verification/sub/U1-2.md", "tests/acceptance/sub/test_more.py", "tests/acceptance/test_u1.py",
    ]
    for path in [*refused, *allowed]:
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text("\n", encoding="utf-8")
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "review: 確認の後で変わったファイルがある（docs/verification/notes.txt" in result.stdout
    assert CYCLE_MODULE.changed_after_gate(wt, state(repo)["gates"]["review"]["tree"], snap(repo)) == refused


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
        (wt / "product.py").rename(old.with_name("test_product.py"))
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
    snap(repo)                              # 確認を始める前に控えた内容（HEAD と同じ）
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
    ("main", "review: 記録の対象がツリーIDでない（main）"),
    (None, "review: 記録の対象がツリーIDでない（None）"),
    ("1" * 40, "review: 記録の対象（1111111111）を読めない"),   # 無くなったツリーとは比べられない
])
def test_gate_whose_recorded_tree_cannot_be_compared_is_not_valid(repo, tree, message):
    """以前の版が残した記録や、手で直した記録を、合格として扱わない。理由は、その1つだけを挙げる。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    data = state(repo)
    data["gates"]["review"]["tree"] = tree
    (repo / ".cycle" / "state" / "U1.json").write_text(json.dumps(data), encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert [line for line in result.stdout.splitlines() if line.startswith("  - review:")] == [
        f"  - {message}。やり直す",
    ]
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
    # 製品のコードが変わっても、check の理由は1つだけ（ほかの確認系のゲートの比べ方を重ねない）
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")
    commit(wt)
    lines = cycle(repo, "merge", "U1").stdout.splitlines()
    assert [line for line in lines if line.startswith("  - check:")] == [
        "  - check: 確認した後で内容が変わっている。check をやり直す",
    ]

    (wt / "FAIL").write_text("", encoding="utf-8")
    assert cycle(repo, "check", "U1").returncode == 1
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "check: fail" in result.stdout


def test_check_that_changes_the_content_is_not_a_pass(repo):
    wt = implement(repo)
    script = "with open('product.py', 'a', encoding='utf-8') as out:\n    out.write('y = 1')\n"
    (wt / "scripts" / "check.py").write_text(script, encoding="utf-8")
    result = cycle(repo, "check", "U1")
    assert result.returncode == 1
    assert "check: error" in result.stdout
    assert state(repo)["gates"]["check"]["verdict"] == "error"


def test_check_that_cannot_be_started_is_an_error_not_a_crash(repo):
    wt = implement(repo)
    (wt / "scripts" / "check.py").unlink()
    result = cycle(repo, "check", "U1")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "check: error（scripts/check.py が無いので、確認を起動できない）" in result.stdout
    assert state(repo)["gates"]["check"]["verdict"] == "error"


# 試験の選び方を変えうる、pytest の環境変数が渡ってきたら、失敗する確認スクリプト
REFUSES_PYTEST_SETTINGS = """\
import os
import sys

names = sorted(name for name in os.environ if name.startswith("PYTEST_"))
print("pytest settings:", names)
sys.exit(1 if names else 0)
"""


def test_check_does_not_hand_the_callers_pytest_settings_to_the_tests(repo):
    """-k や --deselect、差し込み（PYTEST_PLUGINS）で一部の試験だけを実行した結果を、合格として記録しない。

    取り込んだ後の確認（merge）も、同じ関数を通る。
    """
    wt = implement(repo)
    (wt / "scripts" / "check.py").write_text(REFUSES_PYTEST_SETTINGS, encoding="utf-8")
    env = {"PYTEST_ADDOPTS": "-k nothing", "PYTEST_PLUGINS": "chooser", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = cycle(repo, "check", "U1", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (repo / state(repo)["gates"]["check"]["report"]).read_text(encoding="utf-8")
    assert "pytest settings: []" in log


def test_check_line_shows_the_last_line_of_the_standard_output(repo):
    """1行の表示は、標準出力の最後の行（件数）。標準エラーの最後の行（試験の遮断の表示）ではない。"""
    wt = implement(repo)
    (wt / "scripts" / "check.py").write_text(
        "import sys\nprint('12 passed in 3.0s')\nprint('  PATH から外した場所: なし', file=sys.stderr)\n",
        encoding="utf-8",
    )
    result = cycle(repo, "check", "U1")
    assert result.returncode == 0
    assert "U1 check: pass（12 passed in 3.0s）" in result.stdout


def test_check_runs_with_the_python_that_runs_the_harness(repo):
    wt = implement(repo)
    script = "import sys\nprint('python:' + sys.executable)\n"
    (wt / "scripts" / "check.py").write_text(script, encoding="utf-8")
    assert cycle(repo, "check", "U1").returncode == 0
    log = (repo / state(repo)["gates"]["check"]["report"]).read_text(encoding="utf-8")
    assert log.strip() == "python:" + sys.executable


def test_check_output_is_recorded_as_utf8_whatever_the_terminal_encoding_is(repo):
    wt = implement(repo)
    (wt / "scripts" / "check.py").write_text("print('確認した µ')\n", encoding="utf-8")
    result = cycle(repo, "check", "U1", env={"PYTHONIOENCODING": "cp932"})
    assert result.returncode == 0, result.stdout
    assert "check: pass（確認した µ）" in result.stdout
    assert (repo / state(repo)["gates"]["check"]["report"]).read_text(encoding="utf-8") == "確認した µ\n"
    assert state(repo)["gates"]["check"]["by"] == "scripts/check.py"


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


def define_role(wt: Path, role: str = "solver-engineer") -> None:
    """作業ツリーに役割の定義を置く（ヘッドレスの implement は、担当の役割の定義が要る）。"""
    (wt / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (wt / ".claude" / "agents" / f"{role}.md").write_text(f"---\nname: {role}\n---\n", encoding="utf-8")


def test_implement_is_not_started_for_an_owner_without_a_role_definition(repo, tmp_path):
    """見出しの owner に、役割の定義が無い名前（ハーネスの修正担当など）は、ヘッドレスでは起動しない。"""
    wt = implement(repo)
    before = state(repo)
    env = fake_claude(tmp_path, headless())
    result = cycle(repo, "run", "U1", "implement", env=env)
    assert result.returncode == 2 and ".claude/agents/solver-engineer.md" in result.stderr
    assert state(repo) == before and not was_run(tmp_path / "bin")
    define_role(wt)
    assert cycle(repo, "run", "U1", "implement", env=env).returncode == 0
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0      # 確認系の担当は、決まった名前


def test_headless_runs_without_session_id_count_as_the_same_person(repo, tmp_path):
    wt = implement(repo)
    define_role(wt)
    env = fake_claude(tmp_path, {"structured_output": PASS})        # session_id が無い
    assert cycle(repo, "run", "U1", "implement", env=env).returncode == 0
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0
    assert cycle(repo, "run", "U1", "verify", env=env).returncode == 0
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "実装した本人が検証している" in result.stdout


def test_record_keeps_the_report_it_names(repo):
    """セッション内の記録は、渡された報告のファイルの名前を残す（渡さなければ空）。"""
    implement(repo)
    tree = snap(repo)
    args = ["record", "U1", "review", "--verdict", "pass", "--by", "rev-1", "--tree", tree]
    assert cycle(repo, *args, "--report", "docs/verification/U1-review.md").returncode == 0
    assert state(repo)["gates"]["review"]["report"] == "docs/verification/U1-review.md"
    assert cycle(repo, *args).returncode == 0
    assert state(repo)["gates"]["review"]["report"] == ""


def test_record_requires_a_name(repo):
    implement(repo)
    result = cycle(repo, "record", "U1", "implement", "--verdict", "pass", "--by", " ")
    assert result.returncode == 2
    assert "--by が空" in result.stderr


@pytest.mark.parametrize("args, missing", [
    ([], "command"),
    (["record", "U1", "implement", "--by", "impl-2"], "--verdict"),
    (["record", "U1", "implement", "--verdict", "pass"], "--by"),
    (["record-ci", "U1"], "--result"),
])
def test_missing_required_arguments_are_refused_without_changing_records(repo, args, missing):
    """コマンドと、記録に要る引数（判定、担当、結果のファイル）が無ければ、使い方の誤りとして終わる
    （判定や担当の無い記録を作らない）。"""
    implement(repo)
    before = state(repo)
    result = cycle(repo, *args)
    assert result.returncode == 2
    assert "required" in result.stderr and missing in result.stderr
    assert state(repo) == before


# ------------------------------------------------------------ 決まり6: ヘッドレスの結果

def test_dry_run_shows_headless_command_for_the_right_agent(repo):
    implement(repo)
    out = cycle(repo, "run", "U1", "implement", "--dry-run").stdout
    assert "claude -p" in out and "--agent solver-engineer" in out
    assert "--output-format json" in out and "--json-schema" in out
    assert "--agent code-reviewer" in cycle(repo, "run", "U1", "review", "--dry-run").stdout
    assert "--agent verifier" in cycle(repo, "run", "U1", "verify", "--dry-run").stdout


def test_dry_run_line_starts_the_same_command_when_pasted_into_a_terminal(repo, tmp_path):
    """--dry-run の1行を端末（Windows ではコマンド プロンプト）に渡すと、同じ場所で同じ引数の claude が動く。

    引用符で囲まずに並べると、空白を含むもの（作業ツリーの場所、指示、結果の形の指定）が、途中で割れる。
    """
    wt = tmp_path / "work tree"
    assert cycle(repo, "start", "U1", "--worktree", str(wt)).returncode == 0
    line = cycle(repo, "run", "U1", "review", "--dry-run").stdout.strip()
    seen = tmp_path / "cwd.txt"
    script = f"pathlib.Path({str(seen)!r}).write_text(str(pathlib.Path.cwd()), encoding='utf-8')"
    env = {**TEST_ENV, **fake_claude(tmp_path, headless(), script=script)}
    proc = subprocess.run(
        line, shell=True, env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert claude_argv(tmp_path) == CYCLE_MODULE.agent_command(state(repo), "review", 5.0)[1:]
    assert Path(seen.read_text(encoding="utf-8")).resolve() == wt.resolve()


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


@pytest.mark.parametrize("gate, expected", [("verify", "fail"), ("review", "pass"), ("ux", "pass")])
def test_verification_pass_without_any_criterion_is_not_a_pass(implemented, tmp_path, gate, expected):
    """独立検証の合格は、確かめた基準が1つ以上あるときだけ。基準ゼロ件は、何も測っていない。"""
    env = fake_claude(tmp_path, headless({**PASS, "criteria": []}))
    result = cycle(implemented, "run", "U1", gate, env=env)
    assert f"{gate}: {expected}" in result.stdout
    assert state(implemented)["gates"][gate]["verdict"] == expected
    if expected == "fail":
        assert "確かめた基準（criteria）が1つも無い" in result.stdout


def test_estimated_cost_is_shown_only_when_the_cli_reports_it(implemented, tmp_path):
    env = fake_claude(tmp_path, headless(total_cost_usd=0.0125))
    with_cost = cycle(implemented, "run", "U1", "review", env=env)
    assert "費用の概算 $0.0125" in with_cost.stdout
    without = cycle(implemented, "run", "U1", "review", env=fake_claude(tmp_path / "2", headless()))
    assert "費用の概算" not in without.stdout


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


# 実行の間に、製品のコードを書き換える（レビューに許している Bash での書き込みの模擬）
CHANGES_PRODUCT = "pathlib.Path('product.py').write_text('x = 7', encoding='utf-8')"


@pytest.mark.parametrize("gate, verdict, expected", [
    ("review", "pass", "error"), ("verify", "pass", "error"), ("review", "fail", "fail"),
])
def test_confirmation_run_that_changes_the_content_is_not_a_pass(repo, tmp_path, gate, verdict, expected):
    """確認系のゲートの実行の間に内容が変わったら（Bash での書き込みなど。起動の側の道具の指定では
    止まらない）、合格にしない（error）。不合格の判定は、そのまま。記録の対象は、実行前の内容。"""
    implement(repo)
    before = snap(repo)
    env = fake_claude(tmp_path, headless({**PASS, "verdict": verdict}), script=CHANGES_PRODUCT)
    result = cycle(repo, "run", "U1", gate, env=env)
    assert result.returncode == 1
    entry = state(repo)["gates"][gate]
    assert (entry["verdict"], entry["tree"]) == (expected, before)
    changed = f"{gate}: error（実行の間に、作業ツリーの内容が変わった（product.py）。"
    assert (changed in result.stdout) is (expected == "error")


def test_confirmation_run_may_only_add_new_files_in_the_exception_places(repo, tmp_path):
    """検証の実行が、受け入れ試験を新しいファイルとして足すのは、合格のまま（記録は実行前の内容）。"""
    implement(repo)
    before = snap(repo)
    script = (
        "pathlib.Path('tests/acceptance').mkdir(parents=True); "
        "pathlib.Path('tests/acceptance/test_u1.py').write_text('def test_ok(): pass', encoding='utf-8')"
    )
    env = fake_claude(tmp_path, headless(), script=script)
    assert cycle(repo, "run", "U1", "verify", env=env).returncode == 0
    assert state(repo)["gates"]["verify"]["tree"] == before != snap(repo)


def test_headless_run_that_times_out_is_not_a_pass(repo, tmp_path):
    implement(repo)
    env = fake_claude(tmp_path, headless(), script="time.sleep(20)")
    started = time.monotonic()
    result = cycle(repo, "run", "U1", "review", "--timeout", "1", env=env)
    assert result.returncode == 1
    assert "review: error（1 秒で終わらなかったので打ち切った）" in result.stdout
    entry = state(repo)["gates"]["review"]
    assert entry["verdict"] == "error"
    assert (repo / entry["report"]).read_text(encoding="utf-8") == "timeout"      # 記録が指す生の出力
    # 打ち切ったら、起動した先（Windows では claude.cmd が呼んだプロセス）も止めて、すぐに戻る
    assert time.monotonic() - started < 15


# 出力の口を受け継いだ子プロセスを残して、止まり続けるコマンド。
# 子は、印のファイルが置かれるまで（最長 30 秒）いる
LEAVES_A_CHILD = """\
import pathlib
import subprocess
import sys
import time

child = (
    "import pathlib, sys, time\\n"
    "pathlib.Path(sys.argv[1] + '.started').touch()\\n"
    "end = time.monotonic() + 30\\n"
    "while time.monotonic() < end and not pathlib.Path(sys.argv[1]).exists():\\n"
    "    time.sleep(0.05)\\n"
)
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]])
while not pathlib.Path(sys.argv[1] + ".started").exists():
    time.sleep(0.05)
print("started", flush=True)
time.sleep(60)
"""


# 子プロセスを残して動き続けるコマンド。子は、合図のファイルが置かれたら、終わった印を置く
LEAVES_A_WORKING_CHILD = """\
import pathlib, subprocess, sys, time

child = (
    "import pathlib, sys, time\\n"
    "pathlib.Path(sys.argv[1] + '.started').touch()\\n"
    "end = time.monotonic() + 30\\n"
    "while time.monotonic() < end and not pathlib.Path(sys.argv[1]).exists():\\n"
    "    time.sleep(0.05)\\n"
    "pathlib.Path(sys.argv[1] + '.done').touch()\\n"
)
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]], stdout=subprocess.DEVNULL)
while not pathlib.Path(sys.argv[1] + ".started").exists():
    time.sleep(0.05)
time.sleep(60)
"""


def test_timeout_stops_the_processes_the_command_started(tmp_path):
    """打ち切りでは、起動したコマンドが起動したプロセス（確認の先の pytest、claude.cmd の先の本体）も止める。

    Linux / macOS では、プロセスのまとまりごと止める。Windows では taskkill /T でたどる。
    """
    release = tmp_path / "release"
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            command = [sys.executable, "-c", LEAVES_A_WORKING_CHILD, str(release)]
            CYCLE_MODULE.run_captured(command, tmp_path, 3)
    finally:
        release.touch()
    assert Path(f"{release}.started").exists()          # 打ち切りの前に、子プロセスは起動していた
    time.sleep(2)
    assert not Path(f"{release}.done").exists(), "打ち切りの後も、子プロセスが動いていた"


def test_stopping_a_command_that_has_already_ended_is_not_an_error(tmp_path):
    """中断が、起動したプロセスの終わった後に届いても、止める処理は例外にしない（後始末を続けられる）。"""
    with subprocess.Popen([sys.executable, "-c", "pass"], **CYCLE_MODULE.group_options()) as proc:
        proc.wait()
    CYCLE_MODULE.kill_tree(proc)


def test_timeout_does_not_wait_for_a_child_that_still_holds_the_output(tmp_path):
    """打ち切りは、止めたコマンドが残した子プロセス（出力の口を持ったまま）の終わりを待たない。

    cycle.py run、doctor.py --headless、版の確認は、どれもこの関数を通る。
    待つ実装だと、子が終わる 30 秒後まで戻らない。
    """
    release = tmp_path / "release"
    began = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            CYCLE_MODULE.run_captured([sys.executable, "-c", LEAVES_A_CHILD, str(release)], tmp_path, 3)
        waited = time.monotonic() - began
    finally:
        release.touch()                                 # 残った子プロセスを終わらせる
    assert Path(f"{release}.started").exists()          # 打ち切りの前に、子プロセスは起動していた
    assert waited < 15


JAPANESE = "要約は日本語で届く（10 µF、1 kΩ）"


def test_japanese_in_the_cli_output_reaches_the_screen_and_the_record(repo, tmp_path):
    """claude の出力（UTF-8）の日本語と記号が、崩れずに、判定の表示と記録に届く。"""
    implement(repo)
    env = fake_claude(tmp_path, headless({**PASS, "summary": JAPANESE}))
    result = cycle(repo, "run", "U1", "review", env=env)
    assert result.returncode == 0 and f"review: pass（{JAPANESE}）" in result.stdout
    raw = (repo / state(repo)["gates"]["review"]["report"]).read_text(encoding="utf-8")
    assert json.loads(raw)["structured_output"]["summary"] == JAPANESE


# 既定の文字コードが UTF-8 でない Python の中で、run_captured に UTF-8 の出力を読ませる
READS_OUTPUT = """\
import importlib.util
import locale
import pathlib
import sys

spec = importlib.util.spec_from_file_location("cycle_script", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
emit = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))"
proc = module.run_captured([sys.executable, "-c", emit, sys.argv[2]], pathlib.Path.cwd(), 60)
print(locale.getencoding())
print(ascii(proc.stdout))
"""


def test_cli_output_is_read_as_utf8_where_the_default_encoding_is_not_utf8(tmp_path):
    """出力の読み取りは、OSの既定の文字コード（日本語 Windows の cp932 など）に左右されない。

    既定が UTF-8 でない Python を起動して確かめる（Linux と macOS では C ロケール。Windows は素のまま）。
    文字コードの指定を外すと、Linux でも落ちる。
    """
    env = {**TEST_ENV, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    proc = subprocess.run(
        [sys.executable, "-c", READS_OUTPUT, str(CYCLE), JAPANESE.encode("utf-8").hex()],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    default, shown = proc.stdout.splitlines()
    assert codecs.lookup(default).name != "utf-8"       # この試験の前提: 既定は UTF-8 でない
    assert shown == ascii(JAPANESE)


def test_missing_claude_is_reported_clearly(repo, tmp_path):
    implement(repo)
    (tmp_path / "empty").mkdir()
    result = cycle(repo, "run", "U1", "review", env={"PATH": str(tmp_path / "empty")})
    assert result.returncode == 2
    assert "claude コマンドが見つからない" in result.stderr
    assert "Traceback" not in result.stderr
    assert "review" not in state(repo)["gates"]


@pytest.mark.parametrize("gate, agent, tools", [
    ("implement", "solver-engineer", "Read,Edit,Write,Bash,Grep,Glob"),
    ("review", "code-reviewer", "Read,Bash,Grep,Glob"),             # レビューは、書き換える道具を持たない
    ("verify", "verifier", "Read,Edit,Write,Bash,Grep,Glob"),
    ("ux", "ux-designer", "Read,Edit,Write,Bash,Grep,Glob"),
])
def test_headless_command_reaches_the_cli_unchanged(repo, tmp_path, gate, agent, tools):
    """役割、指示、結果の形の指定が、起動用のファイル（Windows では claude.cmd）を通っても崩れない。"""
    define_role(implement(repo))
    env = fake_claude(tmp_path, headless())
    assert cycle(repo, "run", "U1", gate, "--budget", "2.5", env=env).returncode == 0
    entry = state(repo)["gates"][gate]
    assert entry["source"] == "headless"
    assert entry["report"].startswith(f".cycle/runs/U1-{gate}-") and (repo / entry["report"]).is_file()
    argv = claude_argv(tmp_path)
    assert argv[0] == "-p"
    assert argv[1] == CYCLE_MODULE.GATE_PROMPT[gate].format(spec="docs/plan/U1-demo.md")
    options = dict(zip(argv[2::2], argv[3::2], strict=True))
    assert json.loads(options.pop("--json-schema")) == CYCLE_MODULE.VERDICT_SCHEMA
    # レビューは、起動の側でも編集の道具を禁じ、編集を自動で受け入れない
    review = {"--permission-mode": "default", "--disallowedTools": "Edit,Write,NotebookEdit,MultiEdit"}
    assert options == {
        "--agent": agent, "--output-format": "json", "--permission-mode": "acceptEdits",
        "--max-budget-usd": "2.5", "--allowedTools": tools, **(review if gate == "review" else {}),
    }


def test_headless_cli_does_not_get_the_callers_standard_input(repo, tmp_path):
    """人が見ていない実行で、claude が呼び出し元の入力を待って止まり続けないようにする。"""
    implement(repo)
    seen = tmp_path / "stdin.txt"
    script = f"pathlib.Path({str(seen)!r}).write_text(ascii(sys.stdin.readline()), encoding='utf-8')"
    env = fake_claude(tmp_path, headless(), script=script)
    proc = start_cycle(repo, "run", "U1", "review", env=env, stdin=subprocess.PIPE)
    proc.communicate("input of the caller\n", timeout=30)
    assert proc.returncode == 0
    assert seen.read_text(encoding="utf-8") == "''"


def test_file_named_claude_that_cannot_be_launched_is_not_picked(repo, tmp_path):
    """Windows の npm は、拡張子の無い claude（シェル用のスクリプト）も置く。起動できるほうを使う。"""
    implement(repo)
    env = fake_claude(tmp_path, headless())
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "claude").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")      # 実行の印は付けない
    env = {"PATH": f"{decoy}{os.pathsep}{env['PATH']}"}
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0


def test_directory_named_like_the_command_is_not_taken_for_it(repo, tmp_path):
    implement(repo)
    decoy = tmp_path / "decoy"
    for name in CLAUDE_FILES:
        (decoy / name).mkdir(parents=True)
    env = fake_claude(tmp_path, headless())
    env = {"PATH": f"{decoy}{os.pathsep}{env['PATH']}"}
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0


def test_claude_is_found_when_pathext_is_not_set(repo, tmp_path):
    """Windows で環境変数 PATHEXT が無くても、claude.exe、claude.cmd、claude.bat は見つかる。"""
    implement(repo)
    env = {**fake_claude(tmp_path, headless()), "PATHEXT": ""}
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0


# 実行した場所（カレント）を指す、PATH の項目の書き方。None は、PATH に書かない場合
# （Windows の端末と shutil.which は、書かれていなくても、カレントを PATH より先に探す）
HERE_ON_PATH = {
    "絶対パス": lambda here: str(here),
    "相対パス（.）": lambda here: ".",
    "空の項目": lambda here: "",
    "相対パス（下の場所）": lambda here: "bin",
    "PATH に無い": lambda here: None,
}


@pytest.mark.parametrize("form", HERE_ON_PATH)
def test_claude_in_the_place_of_execution_is_not_used(repo, tmp_path, monkeypatch, form):
    """実行した場所に置かれた claude（Windows では claude.cmd など）は、使わない。

    確認を受けていないファイルが、レビューや検証の合格を返せないようにする。
    """
    implement(repo)
    here = tmp_path / "here"
    planted = here / "bin" if form == "相対パス（下の場所）" else here
    install_claude(planted, headless(session="planted"))
    (tmp_path / "empty").mkdir()
    entry = HERE_ON_PATH[form](here)
    path = [str(tmp_path / "empty")] if entry is None else [entry, str(tmp_path / "empty")]
    monkeypatch.chdir(here)
    result = cycle(repo, "run", "U1", "review", env={"PATH": os.pathsep.join(path)})
    assert result.returncode == 2
    assert "claude コマンドが見つからない" in result.stderr
    assert not was_run(planted) and "review" not in state(repo)["gates"]


INSIDE_THE_TREES = {
    "作業ツリーの直下": "wt/U1",
    "作業ツリーの下の場所": "wt/U1/tools/bin",
    "本体の直下": "repo",
    "本体の下の場所": "repo/tools/bin",
    "作業ツリーを .. で回り込んで指す": "elsewhere/../wt/U1/tools/bin",
}


@pytest.mark.parametrize("where", INSIDE_THE_TREES)
def test_claude_inside_the_worktree_or_the_main_checkout_is_not_used(repo, tmp_path, where):
    """作業ツリーや本体の中の claude は、PATH に書かれていても使わない。

    無視の対象のファイル（node_modules など）は、ゲートが見る内容に入らないので、確認を受けていない。
    """
    implement(repo)
    (tmp_path / "elsewhere").mkdir()
    entry = tmp_path / INSIDE_THE_TREES[where]
    install_claude(entry, headless(session="planted"))
    result = cycle(repo, "run", "U1", "review", env={"PATH": str(entry)})
    assert result.returncode == 2
    assert "claude コマンドが見つからない" in result.stderr
    assert not was_run(entry) and "review" not in state(repo)["gates"]


def test_git_inside_the_main_checkout_is_not_started(repo, tmp_path):
    """git も claude と同じ探し方で、絶対パスで起動する（本体の中や、実行した場所の git を動かさない）。"""
    implement(repo)
    expected = snap(repo)
    planted = repo / "tools" / "bin"
    planted.mkdir(parents=True)
    marker = tmp_path / "planted-git-ran"
    source = f"import pathlib\npathlib.Path({str(marker)!r}).touch()\nraise SystemExit(1)\n"
    install_command(planted, "git", source)
    env = {"PATH": f"{planted}{os.pathsep}{os.environ[SAFE_PATH_VAR]}"}       # 見張り役を含まない PATH
    assert cycle(repo, "snapshot", "U1", env=env).stdout.strip() == expected
    assert not marker.exists()
    with mock.patch.dict(os.environ, {"PATH": str(planted), "CYCLE_ROOT": str(repo)}):
        with pytest.raises(CYCLE_MODULE.CycleError, match="git が見つからない"):
            CYCLE_MODULE.git_program()


def test_git_is_taken_only_as_an_exe_on_windows(tmp_path):
    """Windows で、PATH の先に git.cmd や git.bat があっても、PATHEXT によらず git.exe を採る
    （cmd.exe を通ると、merge -m の改行や --by の % と & が崩れる）。claude は PATHEXT のとおり
    （npm 版の claude.cmd を採る）。

    Linux でも、Windows の探し方（区切りの ;、拡張子の付く名前）を模擬して確かめる。大文字と小文字を区別しない
    ファイルの名前は、拡張子を小文字にして模擬する。
    """
    first, later = tmp_path / "first", tmp_path / "later"
    for place, names in ((first, ("git.cmd", "git.bat", "claude.cmd")), (later, ("git.exe",))):
        place.mkdir()
        for name in names:
            (place / name).write_text("", encoding="utf-8")
            (place / name).chmod(0o755)
    env = {"PATH": f"{first};{later}", "PATHEXT": ".cmd;.bat;.exe", "CYCLE_ROOT": str(tmp_path / "root")}
    with (
        mock.patch.object(os, "pathsep", ";"),
        mock.patch.object(CYCLE_MODULE, "WINDOWS", True),
        mock.patch.dict(os.environ, env),
    ):
        assert CYCLE_MODULE.git_program() == str(later / "git.exe")
        assert CYCLE_MODULE.find_command("claude") == str(first / "claude.cmd")
        os.environ["PATH"] = str(first)                     # git.cmd と git.bat しか無い
        with pytest.raises(CYCLE_MODULE.CycleError, match="git が見つからない"):
            CYCLE_MODULE.git_program()


def test_commands_started_by_the_harness_are_told_not_to_search_the_place_of_execution(repo, tmp_path):
    """Windows の cmd.exe は、名前だけのコマンド（claude.cmd の中の node）を、PATH より先に、実行した場所で
    探す。

    NoDefaultCurrentDirectoryInExePath があると探さない。起動する claude（と、その先）に引き継ぐ。
    """
    implement(repo)
    seen = tmp_path / "seen.txt"
    script = (
        "import os\n"
        f"pathlib.Path({str(seen)!r}).write_text(os.environ.get('NoDefaultCurrentDirectoryInExePath', '-'))"
    )
    env = fake_claude(tmp_path, headless(), script=script)
    assert cycle(repo, "run", "U1", "review", env=env).returncode == 0
    assert seen.read_text() == "1"


def test_claude_on_path_is_used_when_another_one_is_in_the_place_of_execution(repo, tmp_path, monkeypatch):
    """実行した場所の claude を飛ばして、PATH の上の claude を使う（見つからない扱いにはしない）。"""
    implement(repo)
    here = tmp_path / "here"
    install_claude(here, headless(session="planted"))
    installed = fake_claude(tmp_path, headless(session="installed"))["PATH"]
    monkeypatch.chdir(here)
    path = os.pathsep.join([str(here), ".", installed])
    assert cycle(repo, "run", "U1", "review", env={"PATH": path}).returncode == 0
    assert state(repo)["gates"]["review"]["by"] == "installed"
    assert not was_run(here)


def test_places_to_avoid_are_compared_after_resolving_them(tmp_path):
    """採らない場所は、書き方が違っても（.. を含むなど）、同じ場所として見分ける。"""
    tools = tmp_path / "tree" / "tools"
    install_claude(tools, headless())
    (tmp_path / "other").mkdir()
    with mock.patch.dict(os.environ, {"PATH": str(tools)}):
        assert CYCLE_MODULE.find_command("claude", [tmp_path / "other" / ".." / "tree"]) is None
        found = CYCLE_MODULE.find_command("claude", [tmp_path / "other"])
    assert found is not None and Path(found).is_absolute() and Path(found).parent == tools


def test_place_reached_through_a_link_counts_as_the_place_it_leads_to(tmp_path):
    """別の名前（リンクなど）で指していても、たどった先が採らない場所なら採らない。

    リンクを作れない環境（権限の無い Windows）でも確かめられるよう、たどり方（os.path.realpath）を差し替えて、
    alias が tree/tools へのリンクであるかのように振る舞わせる。
    """
    alias, tree = tmp_path / "alias", tmp_path / "tree"
    install_claude(alias, headless())
    tree.mkdir()
    realpath = os.path.realpath
    target = os.path.join(realpath(tree), "tools")

    def through_link(path, **kwargs):
        resolved = realpath(path, **kwargs)
        return target if resolved == realpath(alias) else resolved

    with mock.patch.dict(os.environ, {"PATH": str(alias)}):
        assert CYCLE_MODULE.find_command("claude", [tree]) is not None      # 差し替える前は、見つかる
        with mock.patch.object(os.path, "realpath", through_link):
            # PATH の項目が、採らない場所（tree の中）へのリンク
            assert CYCLE_MODULE.find_command("claude", [tree]) is None
            # 実行した場所が、リンクの名前で返ってくる（PATH の項目と同じ場所）
            with mock.patch.object(os, "getcwd", return_value=str(alias)):
                assert CYCLE_MODULE.find_command("claude") is None
            assert CYCLE_MODULE.find_command("claude") is not None


def test_cli_that_cannot_be_started_is_reported_without_changing_records(repo, tmp_path):
    """起動できない claude は、エラー（記録は変えない）。

    Windows では、実行ファイルの形をしていない claude.exe を起動すると、OSが「サポートされていない 16 ビット
    アプリケーション」の画面を出すことがある（試験のプロセスの中で起動するので、画面が出ると試験の全体が
    止まる）。ここでは、書き込み中で開けない claude.exe にして、画面の出ない形で起動に失敗させる。
    壊れた中身の claude.exe で画面が出ずにエラーになることは、別のプロセスで、時間を限って確かめる
    （tests/test_error_dialogs.py）。
    """
    implement(repo)
    broken = tmp_path / "broken"
    broken.mkdir()
    command = broken / ("claude.exe" if WINDOWS else "claude")
    command.write_text("#!/no/such/interpreter\n", encoding="utf-8")      # 実行ファイルの形をしていない
    command.chmod(0o755)
    env = {"PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"}
    with open(command, "ab") if WINDOWS else contextlib.nullcontext():     # 書き込み中の exe は起動できない
        result = cycle(repo, "run", "U1", "review", env=env)
    assert result.returncode == 2
    assert "claude を起動できない" in result.stderr and "Traceback" not in result.stderr
    assert "review" not in state(repo)["gates"]


@pytest.mark.parametrize("found", ["C:/npm/claude.cmd", "C:/npm/claude.CMD", "C:/npm/claude.bat"])
@pytest.mark.parametrize("unsafe", ["%", "^", "&", "|", "<", ">", "!", "(", ")", "\r", "\n"])
def test_arguments_that_cmd_exe_would_reinterpret_are_refused_for_batch_launchers(found, unsafe):
    """claude.cmd は cmd.exe を通る。そこで別の意味になる文字を含む指示は、崩れて届く前に断る。

    ! は遅延展開、( ) は引数をかっこの中に置く起動ファイルへの備え。
    """
    with mock.patch.object(CYCLE_MODULE, "find_command", return_value=found):
        with pytest.raises(CYCLE_MODULE.CycleError, match="cmd.exe を通る"):
            CYCLE_MODULE.launchable(["claude", "-p", f"前{unsafe}後", "--agent", "x"])
        with pytest.raises(CYCLE_MODULE.CycleError, match="cmd.exe を通る"):
            CYCLE_MODULE.launchable(["claude", "-p", "指示", "--agent", unsafe])


@pytest.mark.parametrize("found", ["C:/npm/claude.cmd", "C:/bin/claude.exe", "/usr/bin/claude"])
def test_arguments_are_passed_as_they_are_when_nothing_is_reinterpreted(found):
    unsafe_only_for_batch = [] if found.endswith(".cmd") else ["a & b | c > d", "100%\n", "(x)!"]
    cmd = ["claude", "-p", '指示（かっこ）、"引用"、{"a": [1, "b"]}', *unsafe_only_for_batch]
    with mock.patch.object(CYCLE_MODULE, "find_command", return_value=found):
        assert CYCLE_MODULE.launchable(cmd) == [found, *cmd[1:]]


def test_interrupted_headless_run_stops_the_cli(repo, tmp_path):
    """中断（Ctrl-C）されたら、起動した claude を止める（Windows では claude.cmd が呼んだ先も）。"""
    implement(repo)
    env, started, go = blocking_claude(tmp_path, "rev-s")
    communicate = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        if "claude" in Path(self.args[0]).name:
            wait_for(started)
            raise KeyboardInterrupt
        return communicate(self, *args, **kwargs)

    # 止め損なっても試験が止まり続けないよう、少し後で偽の claude に合図を出す
    release = threading.Timer(1.5, go.touch)
    release.start()
    with mock.patch.object(subprocess.Popen, "communicate", interrupted), pytest.raises(KeyboardInterrupt):
        cycle(repo, "run", "U1", "review", env=env)
    release.join()
    time.sleep(0.5)
    assert not Path(f"{go}.seen").exists()          # 止められたので、合図を見ていない
    assert "review" not in state(repo)["gates"]


# ------------------------------------------------------------ 記録を失わない（並行実行）

def blocking_claude(tmp_path: Path, session: str) -> tuple[dict[str, str], Path, Path]:
    """起動したら started を置き、go が置かれるまで待ってから合格を返す、偽の claude。"""
    started, go = tmp_path / "started", tmp_path / "go"
    script = (
        f"pathlib.Path({str(started)!r}).touch()\n"
        f"while not pathlib.Path({str(go)!r}).exists():\n"
        "    time.sleep(0.02)\n"
        f"pathlib.Path({str(go)!r} + '.seen').touch()"
    )
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
    (wt / "scripts" / "check.py").write_text(WAITING_CHECK, encoding="utf-8")
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
    with held_lock(repo / ".cycle" / lock):                     # 別の実行がロックを持っている
        proc = start_cycle(repo, *command)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
        assert state(repo) == before
        assert main_log(repo) == ["init"]
    assert proc.wait(timeout=30) == 0
    assert state(repo) != before


def busy_wait(seconds: float) -> contextlib.AbstractContextManager:
    """ファイルが使われている間、待ってやり直す秒数（Windows 以外では、ふだんは 0）を差し替える。"""
    return mock.patch.object(CYCLE_MODULE, "BUSY_WAIT_SECONDS", seconds)


def test_state_write_waits_while_another_process_has_the_file_open(repo):
    """Windows では、他のプロセスが開いている間は置き換えが拒まれる。待ってやり直し、記録を失わない。"""
    implement(repo)
    replace, calls = os.replace, []

    def busy_twice(source, target):
        calls.append(target)
        if len(calls) <= 2:
            raise PermissionError(13, "他のプロセスが使っている")
        replace(source, target)

    with busy_wait(5.0), mock.patch.object(os, "replace", busy_twice):
        record(repo, "implement", "impl-2")
    assert len(calls) == 3
    assert state(repo)["gates"]["implement"]["by"] == "impl-2"


@only_windows("開いているファイルの置き換えが拒まれるのは Windows だけ")
def test_record_is_written_while_another_program_has_the_state_file_open(repo):
    """模擬ではなく、本当に開いたままにして確かめる（エディタやウイルス対策が、一時的に開くことがある）。"""
    implement(repo)
    handle = open(repo / ".cycle" / "state" / "U1.json", encoding="utf-8")
    closing = threading.Timer(0.5, handle.close)
    closing.start()
    try:
        record(repo, "implement", "impl-2")
    finally:
        closing.join()
    assert state(repo)["gates"]["implement"]["by"] == "impl-2"


def test_busy_state_file_is_retried_after_a_short_pause(repo):
    """拒まれた置き換えは、POLL_SECONDS ずつ休んでからやり直す（回り続けない）。"""
    implement(repo)
    replace, refused, sleeps = os.replace, [], []

    def busy_three_times(source, target):
        if len(refused) < 3:
            refused.append(target)
            raise PermissionError(13, "他のプロセスが使っている")
        replace(source, target)

    with (
        busy_wait(5.0), mock.patch.object(os, "replace", busy_three_times),
        mock.patch.object(CYCLE_MODULE.time, "sleep", sleeps.append),
    ):
        result = cycle(repo, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-2")
    assert result.returncode == 0, result.stderr
    assert sleeps == [CYCLE_MODULE.POLL_SECONDS] * 3 and state(repo)["gates"]["implement"]["by"] == "impl-2"


def test_state_that_cannot_be_written_is_an_error(repo):
    implement(repo)

    def refused(source, target):
        raise PermissionError(13, "書けない")

    with busy_wait(0.2), mock.patch.object(os, "replace", refused):
        result = cycle(repo, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-2")
    assert result.returncode == 2
    assert "U1.json を書けない" in result.stderr
    assert state(repo)["gates"]["implement"]["by"] == "impl-1"
    assert sorted(path.name for path in (repo / ".cycle" / "state").iterdir()) == ["U1.json", "U1.lock"]


def write_text_like_windows() -> contextlib.AbstractContextManager:
    """改行の指定が無い書き込みを、Windows と同じ（LF を CRLF に変える）に振る舞わせる。

    Windows 以外では、指定しなくても LF のまま書かれるので、指定を外しても違いが出ない。
    """
    write_text = Path.write_text

    def like_windows(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data, newline = data.replace("\n", "\r\n"), ""
        return write_text(self, data, encoding=encoding, errors=errors, newline=newline)

    return mock.patch.object(Path, "write_text", like_windows)


def test_state_file_keeps_the_text_as_utf8(repo):
    """状態ファイルは、UTF-8 のまま書く（日本語の担当の名前を \\u の形にしない。人が開いて読める）。"""
    implement(repo)
    record(repo, "review", "レビュー担当", tree=snap(repo))
    assert "レビュー担当".encode() in (repo / ".cycle" / "state" / "U1.json").read_bytes()


def test_state_write_whose_partial_file_vanished_is_an_error_not_a_crash(repo):
    """書きかけのファイルが、置き換えの前に消えても（ほかのプログラムが片付けた）、記録を変えずにエラーで終わる。"""
    implement(repo)

    def vanished(source, target):
        Path(source).unlink()
        raise FileNotFoundError(2, "書きかけのファイルが無い")

    with mock.patch.object(os, "replace", vanished):
        result = cycle(repo, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-2")
    assert result.returncode == 2 and "U1.json を書けない" in result.stderr
    assert state(repo)["gates"]["implement"]["by"] == "impl-1"


def test_state_file_ends_lines_with_lf_where_text_files_get_crlf(repo):
    """状態ファイルの改行は、テキストの書き込みが改行を CRLF に変えるOS（Windows）でも LF。"""
    with write_text_like_windows():
        implement(repo)
    data = (repo / ".cycle" / "state" / "U1.json").read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n") and data.count(b"\n") > 10


@pytest.mark.parametrize("failures, wait, code", [(2, 5.0, 0), (10**6, 0.2, 2)])
def test_state_read_waits_while_the_file_is_being_replaced(repo, failures, wait, code):
    implement(repo)
    read_text, refused = Path.read_text, []

    def busy(self, *args, **kwargs):
        if self.name == "U1.json" and len(refused) < failures:
            refused.append(self)
            raise PermissionError(13, "置き換えの最中")
        return read_text(self, *args, **kwargs)

    with busy_wait(wait), mock.patch.object(Path, "read_text", busy):
        result = cycle(repo, "status", "U1")
    assert result.returncode == code
    if code:
        assert "U1.json を読めない" in result.stderr and "Traceback" not in result.stderr
    else:
        assert len(refused) == 2 and "implement  pass  impl-1" in result.stdout


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
        ".gitignore", "docs/plan/U1-demo.md", "product.py", "scripts/check.py", "staged.py", "untracked.py",
    ]


def other_repo(tmp_path: Path) -> Path:
    other = tmp_path / "other"
    other.mkdir()
    sh(other, "git", "init", "-q")
    (other / "q.txt").write_text("q\n", encoding="utf-8")
    sh(other, "git", "add", "q.txt")
    return other


def location_case(name: str, wt: Path, other: Path) -> dict[str, str]:
    """呼び出し元に置かれていると、対象でないリポジトリ、索引、オブジェクト、参照を見せる値。"""
    objects = other / ".git" / "objects"
    return {
        "GIT_DIR": {"GIT_DIR": str(other / ".git")},
        "GIT_WORK_TREE": {"GIT_WORK_TREE": str(other)},
        "GIT_INDEX_FILE": {"GIT_INDEX_FILE": str(other / ".git" / "index")},
        "GIT_OBJECT_DIRECTORY": {"GIT_OBJECT_DIRECTORY": str(objects)},
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects)},
        # GIT_DIR が無くても、git（2.43 で確かめた）は読む。参照（refs/heads/main）とオブジェクトを、
        # 別のリポジトリから読ませる（作業ツリーの HEAD は、作業ツリーの側に残る）
        "GIT_COMMON_DIR": {"GIT_COMMON_DIR": str(other / ".git")},
    }[name]


@pytest.mark.parametrize("name", [
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
])
def test_each_git_location_variable_of_the_caller_is_ignored(repo, tmp_path, name):
    """変数ごとに、置かれていても、対象の作業ツリーの内容を見る（別のリポジトリの内容を受け入れない）。
    記録する分かれ目（main との merge-base）も、対象のリポジトリのもの。"""
    wt = implement(repo)
    (wt / "forced.log").write_text("f\n", encoding="utf-8")
    sh(wt, "git", "add", "-f", "forced.log")
    (wt / ".gitignore").write_text(".cycle/\n*.log\n", encoding="utf-8")
    expected = snap(repo)
    other = other_repo(tmp_path)
    sh(other, "git", "commit", "-q", "-m", "other")         # 別のリポジトリにも main がある
    foreign = sh(other, "git", "write-tree")                 # 別のリポジトリにだけあるツリー
    env = location_case(name, wt, other)
    assert cycle(repo, "snapshot", "U1", env=env).stdout.strip() == expected
    args = ["record", "U1", "review", "--verdict", "pass", "--by", "rev-1", "--tree", foreign]
    assert cycle(repo, *args, env=env).returncode == 2
    args = ["record", "U1", "review", "--verdict", "pass", "--by", "rev-1", "--tree", expected]
    recorded = cycle(repo, *args, env=env)
    assert recorded.returncode == 0, recorded.stderr
    entry = state(repo)["gates"]["review"]
    assert (entry["tree"], entry["base"]) == (expected, sh(wt, "git", "merge-base", "main", "HEAD"))
    assert name in CYCLE_MODULE.GIT_LOCATION_VARS


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


def test_failure_of_git_listing_files_is_an_error_not_an_empty_list(repo):
    """ファイルの一覧を返す git が失敗したら、空の一覧（変更なし、強制追加なし）として扱わない。"""
    wt = implement(repo)
    (wt / "forced.log").write_text("f\n", encoding="utf-8")
    sh(wt, "git", "add", "-f", "forced.log")                # 無視の対象だが、追跡しているファイル
    (wt / ".gitignore").write_text(".cycle/\n*.log\n", encoding="utf-8")
    with pytest.raises(CYCLE_MODULE.CycleError, match="ls-files"):
        CYCLE_MODULE.git_paths(wt, "ls-files", "-z", "--no-such-option")
    index = Path(sh(wt, "git", "rev-parse", "--absolute-git-dir")) / "index"
    saved = index.read_bytes()
    index.write_bytes(b"broken")                            # 作業ツリーの索引が壊れている
    try:
        result = cycle(repo, "snapshot", "U1")
        assert result.returncode == 2 and "ls-files" in result.stderr
    finally:
        index.write_bytes(saved)
    assert "forced.log" in sh(wt, "git", "ls-tree", "-r", "--name-only", snap(repo))


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

def test_missing_worktree_is_named(repo):
    """作業ツリーが無くなっていたら、その場所を示して、統合できないと言う。"""
    wt = implement(repo)
    shutil.rmtree(wt)
    for command in ("status", "merge"):
        result = cycle(repo, command, "U1")
        assert f"作業ツリー {wt} が無い" in result.stdout
    assert main_log(repo) == ["init"]


def test_merge_requires_committed_worktree(repo):
    implement(repo)
    pass_all(repo)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "作業ツリーに未コミットの変更がある" in result.stdout
    assert main_log(repo) == ["init"]


def test_status_gives_the_same_reason_as_merge_for_uncommitted_changes(repo):
    """status が「できる」と言うのに merge が拒む、が無い。違うファイルの名前を示す。"""
    wt = implement(repo)
    pass_all(repo)
    status = cycle(repo, "status", "U1").stdout
    refused = cycle(repo, "merge", "U1")
    reason = "作業ツリーに未コミットの変更がある（product.py）。コミットしてから統合する"
    assert "統合: まだできない" in status and reason in status
    assert refused.returncode == 1 and reason in refused.stdout
    commit(wt)
    assert "統合: できる" in cycle(repo, "status", "U1").stdout


def test_uncommitted_files_beyond_the_first_five_are_counted(repo):
    """未コミットの変更のファイルは、先頭の5つの名前と、残りの数を示す。"""
    wt = implement(repo)                                        # product.py が未コミット
    for index in range(5):
        (wt / f"new{index}.py").write_text("", encoding="utf-8")
    status = cycle(repo, "status", "U1").stdout
    listed = "new0.py, new1.py, new2.py, new3.py, new4.py ほか 1 件"
    assert f"作業ツリーに未コミットの変更がある（{listed}）" in status


def test_files_only_the_users_git_settings_ignore_are_named_with_the_way_out(repo, tmp_path):
    """利用者の無視の設定（core.excludesFile）だけで外したファイルは、git status に出ないが、内容に数える
    （ツリーIDは利用者の無視の指定を読まない。決まり9）。status と merge は同じ理由で止まり、そのファイルの
    名前と、リポジトリの無視の指定に足す案内を出す。案内のとおりに足せば、内容から外れる。"""
    users = users_setting_file(tmp_path, "excludesFile", ".vscode/")
    users_env = {**TEST_ENV, **users}
    wt = implement(repo)
    (wt / ".vscode").mkdir()
    (wt / ".vscode" / "settings.json").write_text("{}\n", encoding="utf-8")
    pass_all(repo)
    for args in (("add", "-A"), ("commit", "-q", "-m", "work")):
        assert subprocess.run(["git", *args], cwd=wt, env=users_env, capture_output=True).returncode == 0
    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=wt, env=users_env, capture_output=True)
    assert porcelain.stdout == b""                                  # 利用者の git では、変更が無い
    ignored = "利用者の git の設定（core.excludesFile など）だけで無視しているファイルがある"
    ignored += "（.vscode/settings.json）"
    way_out = "リポジトリの .gitignore か .git/info/exclude に足す"
    status = cycle(repo, "status", "U1", env=users).stdout
    refused = cycle(repo, "merge", "U1", env=users)
    assert "統合: まだできない" in status and ignored in status and way_out in status
    assert refused.returncode == 1 and ignored in refused.stdout and way_out in refused.stdout
    assert "未コミットの変更がある" not in refused.stdout
    # ほかの変更は、別に示す
    (wt / "extra.py").write_text("y = 1\n", encoding="utf-8")
    status = cycle(repo, "status", "U1", env=users).stdout
    assert "作業ツリーに未コミットの変更がある（extra.py）" in status and ignored in status
    (wt / "extra.py").unlink()
    # 案内のとおり、リポジトリの無視の指定に足す（内容が変わるので、確認はやり直す）
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n.vscode/\n")
    pass_all(repo)
    assert "統合: できる" in cycle(repo, "status", "U1", env=users).stdout
    merged = cycle(repo, "merge", "U1", env=users)
    assert merged.returncode == 0, merged.stdout + merged.stderr


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
    """下の階層を作業ツリーとして渡すと、そこに置いた別の scripts/check.py が使われてしまう。"""
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


# ------------------------------------------------------------ 土台の付け替え（main との分かれ目）

def main_moves_on(root: Path) -> None:
    """main に、この作業単位とは別の成果が入る。"""
    (root / "other.py").write_text("other = 1\n", encoding="utf-8")
    commit(root, "別の成果")


@pytest.mark.parametrize("rebase", [
    ["reset", "--soft", "main"],                    # 付け替えて、同じツリーのままコミットし直す
    ["merge", "-q", "-s", "ours", "main", "-m", "main を取り込む（中身は採らない）"],
])
def test_gates_go_stale_when_the_branch_is_moved_onto_main_without_its_changes(repo, rebase):
    """ツリーIDが同じでも、土台を付け替えた内容を取り込むと、main に入った他の成果が消える。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    main_moves_on(repo)
    sh(wt, "git", *rebase)
    if rebase[0] == "reset":
        sh(wt, "git", "commit", "-q", "-m", "付け替え")
    assert sh(wt, "git", "rev-parse", "HEAD^{tree}") == state(repo)["gates"]["review"]["tree"]
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    for gate in ("check", "review", "verify"):
        assert f"{gate}: 確認の後で、main との分かれ目（土台）が変わっている" in result.stdout
    assert "統合: まだできない" in cycle(repo, "status", "U1").stdout
    assert_main_untouched(repo, before)
    assert (repo / "other.py").exists()


def test_branch_that_is_only_behind_main_is_merged_as_before(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    main_moves_on(repo)
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "other.py").exists() and (repo / "product.py").read_text(encoding="utf-8") == "x = 2\n"


def test_gate_recorded_without_the_base_is_stale(repo):
    """分かれ目を控えていない記録（H2 より前の記録など）は、比べられないので古いものとして扱う。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    data = state(repo)
    data["gates"]["verify"].pop("base")
    (repo / ".cycle" / "state" / "U1.json").write_text(json.dumps(data), encoding="utf-8")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "verify: 確認の後で、main との分かれ目（土台）が変わっている" in result.stdout
    assert "review: 確認の後で、main との分かれ目" not in result.stdout
    # 内容も変わっていても、分かれ目の古いゲートは、その理由だけを挙げる
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")
    commit(wt)
    refused = cycle(repo, "merge", "U1").stdout
    lines = [line for line in refused.splitlines() if line.startswith("  - verify:")]
    assert len(lines) == 1 and "分かれ目（土台）が変わっている" in lines[0]


def test_base_of_each_gate_is_the_one_seen_when_it_was_recorded(repo, tmp_path):
    """記録ごとに、その時点の分かれ目を控える（run、record、check、record-ci のどれでも）。"""
    wt = implement(repo)
    base = sh(wt, "git", "merge-base", "main", "HEAD")
    pass_all(repo)
    assert cycle(repo, "run", "U1", "review", env=fake_claude(tmp_path, headless())).returncode == 0
    assert {gate: entry["base"] for gate, entry in state(repo)["gates"].items()} == dict.fromkeys(
        ["implement", "review", "verify", "check"], base,
    )
    assert CYCLE_MODULE.main_base(tmp_path) is None        # git の作業ツリーでない場所では、求められない


@pytest.mark.parametrize("rebase", [
    ["reset", "--soft", "main"],
    ["merge", "-q", "-s", "ours", "main", "-m", "main を取り込む（中身は採らない）"],
])
def test_confirmation_is_recorded_with_the_base_seen_at_the_snapshot(repo, rebase):
    """確認系のゲートは、snapshot（確認の始め）の時点の分かれ目を記録する（record の時点のものではない）。
    record の時点で違っていれば、そのことを知らせ、統合は、その記録を古いものとして拒む。

    レビューの再現: snapshot の後、レビューの最中に土台を付け替えてから record --tree <控えたツリー>
    とすると、record の時点の（新しい）分かれ目が控えられ、merge が通って main の other.py が消えていた。
    """
    wt = implement(repo)
    commit(wt)
    tree = snap(repo)                       # レビューの前に控える
    seen = sh(wt, "git", "merge-base", "main", "HEAD")
    assert state(repo)["snapshots"][tree]["base"] == seen
    main_moves_on(repo)
    sh(wt, "git", *rebase)
    if rebase[0] == "reset":
        sh(wt, "git", "commit", "-q", "-m", "付け替え")      # ツリーは同じ、土台は main の先頭
    assert sh(wt, "git", "rev-parse", "HEAD^{tree}") == tree
    for gate, by in (("review", "rev-1"), ("verify", "ver-1")):
        result = cycle(repo, "record", "U1", gate, "--verdict", "pass", "--by", by, "--tree", tree)
        assert result.returncode == 0, result.stderr
        assert "注意: snapshot の後で、main との分かれ目（土台）が変わっている" in result.stderr
        assert f"（控えた分かれ目 {seen[:10]}、いま " in result.stderr
        assert state(repo)["gates"][gate]["base"] == seen
    assert cycle(repo, "check", "U1").returncode == 0
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1 and (repo / "other.py").exists()
    for gate in ("review", "verify"):
        assert f"{gate}: 確認の後で、main との分かれ目（土台）が変わっている" in refused.stdout
    assert "check: 確認の後で、main との分かれ目" not in refused.stdout        # check は付け替えた後に流した
    # 付け替えた後の内容で、確認を始めからやり直せば（snapshot を控え直せば）、その分かれ目で記録する
    assert snap(repo) == tree
    result = cycle(repo, "record", "U1", "review", "--verdict", "pass", "--by", "rev-1", "--tree", tree)
    assert result.returncode == 0 and "注意" not in result.stderr
    assert state(repo)["gates"]["review"]["base"] == sh(wt, "git", "merge-base", "main", "HEAD") != seen


def test_second_base_made_by_taking_in_another_unit_without_its_content_is_stale(repo):
    """分かれ目は、すべて（git merge-base --all）を比べる。

    H2 の独立検証の手口: main に入った別の作業単位 A の先端（U1 の分かれ目より古い日時）を、U1 に -s ours で
    取り込むと、分かれ目が2つになる。git merge-base は控えた方を返すので、1つだけ比べると統合が通り、
    A の成果が main から消えていた。
    """
    sh(repo, "git", "branch", "A")                          # A は、main の最初のコミットから
    main_moves_on(repo)                                     # M1
    wt = implement(repo)                                    # U1 は M1 から
    commit(wt)
    a_tree = repo.parent / "wt" / "A"
    sh(repo, "git", "worktree", "add", "-q", str(a_tree), "A")
    (a_tree / "a_unit.py").write_text("a = 1\n", encoding="utf-8")
    old = {"GIT_AUTHOR_DATE": "2000-01-01T00:00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00"}
    subprocess.run(["git", "add", "-A"], cwd=a_tree, env=TEST_ENV, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "A1"], cwd=a_tree, env={**TEST_ENV, **old}, check=True)
    a1 = sh(a_tree, "git", "rev-parse", "HEAD")
    pass_all(repo)
    seen = state(repo)["gates"]["review"]["base"]
    sh(repo, "git", "merge", "-q", "--no-ff", "A", "-m", "A を統合")
    sh(wt, "git", "merge", "-q", "-s", "ours", a1, "-m", "A1 を中身なしで取り込む")
    bases = sh(wt, "git", "merge-base", "--all", "main", "HEAD").split()
    assert len(bases) == 2 and sh(wt, "git", "merge-base", "main", "HEAD") == seen     # 1つだけなら同じ
    assert CYCLE_MODULE.main_base(wt) == " ".join(sorted(bases))
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1
    assert "review: 確認の後で、main との分かれ目（土台）が変わっている" in refused.stdout
    assert (repo / "a_unit.py").exists()


def test_main_moving_on_between_the_snapshot_and_the_record_is_merged_as_before(repo):
    """遅れているだけのブランチ（main だけが先に進んだ）は、分かれ目が変わらないので、今までどおり通る。"""
    wt = implement(repo)
    commit(wt)
    tree = snap(repo)
    main_moves_on(repo)
    record(repo, "review", "rev-1", tree=tree)
    record(repo, "verify", "ver-1", tree=tree)
    assert cycle(repo, "check", "U1").returncode == 0
    merged = cycle(repo, "merge", "U1")
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert (repo / "other.py").exists() and (repo / "product.py").read_text(encoding="utf-8") == "x = 2\n"


def test_record_needs_a_snapshot_of_the_tree_it_names(repo):
    """確認系のゲートの --tree は、snapshot で控えた内容に限る（控えた分かれ目が無いと、確認の始めの
    土台が分からない）。start のやり直しでは控えを引き継ぎ、start --reset では消す（消す前に始めた確認は
    やり直す）。"""
    wt = implement(repo)
    commit(wt)
    named = sh(wt, "git", "rev-parse", "HEAD^{tree}")
    result = cycle(repo, "record", "U1", "review", "--verdict", "pass", "--by", "rev-1", "--tree", "HEAD")
    assert result.returncode == 2
    assert f"--tree の内容（{named[:10]}）を snapshot で控えた記録が無い" in result.stderr
    tree = snap(repo)
    assert cycle(repo, "start", "U1").returncode == 0           # やり直し（記録と控えは引き継ぐ）
    record(repo, "review", "rev-1", tree=tree)
    assert cycle(repo, "start", "U1", "--reset").returncode == 0
    result = cycle(repo, "record", "U1", "verify", "--verdict", "pass", "--by", "ver-1", "--tree", tree)
    assert result.returncode == 2 and "snapshot で控えた記録が無い" in result.stderr
    assert state(repo)["gates"] == {} and state(repo)["snapshots"] == {}
    # implement は、控えを要らない（内容とも分かれ目とも比べない）
    record(repo, "implement", "impl-1", tree=named)


def test_merge_is_undone_when_check_cannot_be_started_on_main(repo):
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    sh(repo, "git", "rm", "-q", "scripts/check.py")             # main 側で確認スクリプトが無くなっている
    commit(repo, "確認スクリプトを消す")
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "取り込みを取り消した" in result.stdout
    assert_main_untouched(repo, before)


# 取り込んだ内容の確認スクリプトは、本体の中で動く。.cycle/ の中を片付けて（控えを消して）、失敗する
CLEANS_UP_AND_FAILS_ON_MAIN = """\
import pathlib
import sys

pending = pathlib.Path(".cycle", "merge-pending.json")
if pending.exists():            # 本体での確認（取り込みの控えがある）
    pending.unlink()
    sys.exit(1)
"""


def test_merge_is_undone_when_the_check_on_main_removes_the_pending_note_and_fails(repo):
    """控えが、取り込んだ内容の確認で消されていても、取り込みを取り消して、理由を表示して終わる。"""
    wt = implement(repo)
    (wt / "scripts" / "check.py").write_text(CLEANS_UP_AND_FAILS_ON_MAIN, encoding="utf-8")
    pass_all(repo)
    commit(wt)
    before = sh(repo, "git", "rev-parse", "HEAD")
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "取り込みを取り消した" in result.stdout
    assert_main_untouched(repo, before)


def merge_stopped_during_check(root: Path, tmp_path: Path, stop: str) -> str:
    """取り込んだ後の check が走っている最中に、merge を止める。取り込み前の main のコミットを返す。

    stop は合図の名前（signal の中の名前）。KILL なら、後始末の機会が無い強制終了。
    """
    wt = implement(root)
    (wt / "scripts" / "check.py").write_text(HANGING_CHECK, encoding="utf-8")
    pass_all(root)
    commit(wt)
    before = sh(root, "git", "rev-parse", "HEAD")
    marker = tmp_path / "check-started"
    proc = start_cycle(root, "merge", "U1", env={"HANG": str(marker)})
    try:
        wait_for(marker)                                            # 取り込んだ後の check が走っている
        assert sh(root, "git", "rev-parse", "HEAD^1") == before     # 取り込みコミットが、もう main にある
        if stop == "KILL":
            proc.kill()
        else:
            proc.send_signal(getattr(signal, stop))
        assert proc.wait(timeout=30) != 0
    finally:
        Path(f"{marker}.stop").touch()          # 残った確認スクリプトを終わらせる
    return before


@pytest.mark.parametrize("stop", [
    pytest.param("SIGINT", marks=only_posix("Windows には、他のプロセスへ SIGINT を送る手段が無い")),
    pytest.param("SIGTERM", marks=only_posix("Windows の SIGTERM は強制終了で、後始末の機会が無い")),
    pytest.param("SIGHUP", marks=only_posix("Windows には SIGHUP が無い")),
    pytest.param("CTRL_BREAK_EVENT", marks=only_windows("Ctrl-Break の合図は Windows にだけある")),
])
def test_merge_is_undone_when_interrupted_during_check_on_main(repo, tmp_path, stop):
    before = merge_stopped_during_check(repo, tmp_path, stop)
    assert_main_untouched(repo, before)


def test_merge_is_undone_when_interrupted_from_the_keyboard(repo):
    """Ctrl-C（KeyboardInterrupt）。他のプロセスへ SIGINT を送れないOSでも確かめるため、試験の中で起こす。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before = sh(repo, "git", "rev-parse", "HEAD")

    def interrupted(cwd: Path) -> tuple[str, str, str]:
        assert sh(repo, "git", "rev-parse", "HEAD^1") == before     # 取り込みコミットが、もう main にある
        raise KeyboardInterrupt

    with mock.patch.object(CYCLE_MODULE, "run_check", interrupted), pytest.raises(KeyboardInterrupt):
        cycle(repo, "merge", "U1")
    assert_main_untouched(repo, before)


def test_merge_killed_during_check_is_undone_by_the_next_merge(repo, tmp_path):
    before = merge_stopped_during_check(repo, tmp_path, "KILL")
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
    merge_stopped_during_check(repo, tmp_path, "KILL")
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
    pending = json.loads((repo / ".cycle" / "merge-pending.json").read_text(encoding="utf-8"))
    expected = f"取り込み前のコミット {pending['before']}、取り込もうとしたコミット {pending['head']}"
    assert expected in result.stdout
    assert sh(repo, "git", "for-each-ref") == refs                 # 人が確かめるまで、何も動かさない
    assert sh(repo, "git", "status", "--porcelain") == files
    assert (repo / ".cycle" / "merge-pending.json").exists()


def test_recovery_leaves_main_alone_when_the_merge_had_not_started(repo):
    """控えを置いた直後（取り込みを始める前）に止まっていたら、本体に触らずに控えを片付ける（その後で本体に
    入った手の変更を、取り込み前に戻す処理で消さない）。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before, head = sh(repo, "git", "rev-parse", "HEAD"), sh(wt, "git", "rev-parse", "HEAD")
    pending = {"id": "U1", "before": before, "head": head}
    (repo / ".cycle" / "merge-pending.json").write_text(json.dumps(pending), encoding="utf-8")
    (repo / "product.py").write_text("x = 5\n", encoding="utf-8")       # その後で、本体に手が入った
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1 and "本体（main）に未コミットの変更がある" in result.stdout
    assert (repo / "product.py").read_text(encoding="utf-8") == "x = 5\n"
    assert sh(repo, "git", "rev-parse", "HEAD") == before
    assert not (repo / ".cycle" / "merge-pending.json").exists()


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


def test_captured_command_gets_the_environment_it_is_given(tmp_path):
    code = "import os; print(os.environ.get('GIVEN'), os.environ.get('NOT_GIVEN'))"
    env = {**TEST_ENV, "GIVEN": "yes"}
    with mock.patch.dict(os.environ, {"NOT_GIVEN": "leaked"}):
        proc = CYCLE_MODULE.run_captured([sys.executable, "-c", code], tmp_path, 60, env)
    assert proc.stdout.split() == ["yes", "None"]


# ------------------------------------------------------------ ツリーIDは、どのOSでも同じ

def test_tree_id_matches_the_commit_where_the_executable_mark_cannot_be_shown(repo):
    """実行できる印を表せないファイルシステム（Windows の既定）でも、同じ内容なら、コミットのツリーと同じID。

    空の索引から組み直すと、git は、印の付いたファイルを「実行できない普通のファイル」として足し、
    同じ内容なのに、コミットのツリーとも、Linux で求めたIDとも食い違う。
    """
    (repo / "tool.py").write_text("print(1)\n", encoding="utf-8")
    sh(repo, "git", "add", "tool.py")
    sh(repo, "git", "update-index", "--chmod=+x", "tool.py")
    sh(repo, "git", "commit", "-q", "-m", "実行できる印の付いたファイル")
    assert sh(repo, "git", "ls-files", "-s", "tool.py").startswith("100755 ")
    # 実行できる印を信用しない設定にする（Windows では、もとからこの値）
    sh(repo, "git", "config", "core.fileMode", "false")
    committed = sh(repo, "git", "rev-parse", "HEAD^{tree}")
    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
        assert CYCLE_MODULE.tree_id(repo) == committed
        # 中身を変えれば別のIDになる。印は引き継ぐ
        (repo / "tool.py").write_text("print(2)\n", encoding="utf-8")
        changed = CYCLE_MODULE.tree_id(repo)
    assert changed != committed
    assert sh(repo, "git", "ls-tree", changed, "tool.py").startswith("100755 ")


def test_tree_id_works_before_the_first_commit(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    sh(root, "git", "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
        tree = CYCLE_MODULE.tree_id(root)
    assert sh(root, "git", "ls-tree", "--name-only", tree) == "a.txt"


def users_git_config(folder: Path, text: str) -> dict[str, str]:
    """利用者（またはシステム）の git の設定の代わり。試験のプロセスの git に読ませる環境変数を返す。

    Git for Windows は、システムの設定で core.autocrlf を true にする。試験の子プロセスの git は、その設定から
    切り離してある（TEST_ENV）が、利用者の端末で動くハーネスと、試験のプロセスの中の呼び出しは、それを読む。
    """
    config = folder / "users.gitconfig"
    config.write_text(text, encoding="utf-8")
    return {"GIT_CONFIG_GLOBAL": str(config)}


def users_files(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def users_setting_file(folder: Path, key: str, line: str) -> dict[str, str]:
    """利用者の設定で、core.<key> に、1行のファイル（無視の指定、属性）を指す。"""
    path = users_files(folder, key, line + "\n")
    return users_git_config(folder, f"[core]\n\t{key} = {path.as_posix()}\n")


def attribute_source(root: Path) -> str:
    """属性のファイル（*.txt text）だけを持つツリー。GIT_ATTR_SOURCE に渡すと、git は属性をそこから読む。"""
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=root, input="*.txt text\n", capture_output=True,
        text=True, encoding="utf-8", env=TEST_ENV, check=True,
    ).stdout.strip()
    return subprocess.run(
        ["git", "mktree"], cwd=root, input=f"100644 blob {blob}\t.gitattributes\n", capture_output=True,
        text=True, encoding="utf-8", env=TEST_ENV, check=True,
    ).stdout.strip()


# 内容の読み方を変える、利用者とシステムの設定（どれも、リポジトリの中には無い）: 試験の場所 → 環境変数
USERS_GIT_SETTINGS = {
    "改行の変換（Git for Windows のシステムの設定）": lambda tmp, root: users_git_config(
        tmp, "[core]\n\tautocrlf = true\n",
    ),
    "改行の変換の警告で止める": lambda tmp, root: users_git_config(tmp, "[core]\n\tsafecrlf = true\n"),
    "利用者の無視の指定": lambda tmp, root: users_setting_file(tmp, "excludesFile", "new.txt"),
    "利用者の属性": lambda tmp, root: users_setting_file(tmp, "attributesFile", "*.txt text"),
    "決まった場所（XDG）の無視の指定": lambda tmp, root: {
        "XDG_CONFIG_HOME": str(users_files(tmp, "xdg/git/ignore", "new.txt\n").parents[1]),
    },
    "決まった場所（XDG）の属性": lambda tmp, root: {
        "XDG_CONFIG_HOME": str(users_files(tmp, "xdg/git/attributes", "*.txt text\n").parents[1]),
    },
    "属性を読むツリーの指定（GIT_ATTR_SOURCE）": lambda tmp, root: {
        "GIT_ATTR_SOURCE": attribute_source(root),
    },
    "属性を読むツリーの設定（attr.tree）": lambda tmp, root: users_git_config(
        tmp, f"[attr]\n\ttree = {attribute_source(root)}\n",
    ),
}


@pytest.mark.parametrize("case", USERS_GIT_SETTINGS)
def test_tree_id_does_not_depend_on_the_users_git_settings(repo, tmp_path, case):
    """同じ内容なら、利用者やシステムの git の設定に関係なく、同じツリーID（Windows の実機で食い違った形）。

    Windows では、改行の指定の無い書き込み（write_text）が CRLF を書き、Git for Windows はシステムの設定で
    core.autocrlf を true にする。その設定を読む呼び出しだけが、新しいファイルの CRLF を LF に直していた。
    期待値は git から取る（新しいファイルは書いたままのバイト列。リポジトリの属性 text の付いたファイルは
    LF）。
    """
    (repo / ".gitattributes").write_text("*.md text eol=lf\n", encoding="utf-8")
    commit(repo, "リポジトリの属性")
    with write_text_like_windows():
        (repo / "new.txt").write_text("n\n", encoding="utf-8")
        (repo / "new.md").write_text("m\n", encoding="utf-8")
    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
        plain = CYCLE_MODULE.tree_id(repo)
    settings = USERS_GIT_SETTINGS[case](tmp_path, repo)
    with mock.patch.dict(os.environ, {**TEST_ENV, **settings}, clear=True):
        users = CYCLE_MODULE.tree_id(repo)
    assert users == plain
    raw = sh(repo, "git", "hash-object", "--no-filters", "new.txt")
    assert sh(repo, "git", "rev-parse", f"{plain}:new.txt") == raw
    assert sh(repo, "git", "cat-file", "-p", f"{plain}:new.md") == "m"
    assert b"\r" in (repo / "new.md").read_bytes()


def test_git_for_the_content_does_not_read_the_attributes_of_the_system(repo):
    """システムの属性のファイル（Git for Windows では etc/gitattributes）と、属性を読むツリーの指定は
    使わない。"""
    calls, real = [], subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(kwargs["env"])
        return real(cmd, *args, **kwargs)

    with mock.patch.dict(os.environ, {**TEST_ENV, "GIT_ATTR_SOURCE": "HEAD"}, clear=True), \
            mock.patch.object(CYCLE_MODULE.subprocess, "run", spy):
        CYCLE_MODULE.tree_id(repo)
    assert calls
    assert all(env.get("GIT_ATTR_NOSYSTEM") == "1" and "GIT_ATTR_SOURCE" not in env for env in calls)


def install_hook(root: Path, name: str, marker: Path) -> None:
    """作業フォルダの git のフック。動くと marker に1行を足す。"""
    hook = root / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)


def test_tree_id_does_not_run_the_hooks_of_the_working_folder(repo, tmp_path):
    """ツリーIDを求める間、作業フォルダのフック（索引を書くたびに動く post-index-change）は動かさない。

    索引を書く呼び出しのすべて（組み直し、足す、強制追加したファイルを足す）で確かめる。
    """
    (repo / ".gitignore").write_text(".cycle/\nforced.log\n", encoding="utf-8")
    commit(repo, "無視の対象")
    (repo / "forced.log").write_text("f\n", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    marker = tmp_path / "hook-ran"
    install_hook(repo, "post-index-change", marker)
    sh(repo, "git", "add", "-f", "forced.log")                  # 無視の対象を、強制追加した（コミットはまだ）
    assert marker.exists()                                      # フックは、ふつうの git では動く
    marker.unlink()
    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
        tree = CYCLE_MODULE.tree_id(repo)
    assert not marker.exists()
    assert sh(repo, "git", "ls-tree", "--name-only", tree, "forced.log") == "forced.log"


# ------------------------------------------------------------ Windows での確認（needs_windows、record-ci）

def ci_result(tree: str, **changes: object) -> dict:
    """手元の確認（scripts/local_ci.py）が Windows で書く形の、合格の結果。changes の項目は差し替える。"""
    result = {
        "schema": 1, "kind": "local-ci", "at": "2026-11-15T00:00:00Z", "mode": "checkout", "ref": "origin/U1",
        "commit": "c" * 40, "tree": tree, "branch": "origin/U1", "dirty": False,
        "os": {"system": "Windows", "release": "11", "version": "10.0.26100", "machine": "AMD64"},
        "python": {"version": "3.13.1", "implementation": "CPython", "supported": True},
        "encoding": {"stdout": "cp932", "locale": "cp932", "filesystem": "utf-8", "utf8_mode": False},
        "check": {"script": "scripts/check.py", "returncode": 0, "seconds": 260.0, "timed_out": False},
        "tests": {"total": 12, "passed": 10, "failed": 0, "errors": 0, "skipped": 2},
        "failed": [], "changed": [], "verdict": "pass", "reason": "ok",
    }
    return {**result, **changes}


def failed_result(tree: str) -> dict:
    """試験が1件失敗した、Windows での結果。"""
    result = ci_result(tree, verdict="fail", reason="tests_failed", failed=["tests/test_a.py::test_x"])
    result["check"] = {**result["check"], "returncode": 1}
    result["tests"] = {"total": 12, "passed": 9, "failed": 1, "errors": 0, "skipped": 2}
    return result


def record_ci(root: Path, result: object) -> subprocess.CompletedProcess:
    """結果をファイルに書いて、record-ci で取り込む。result が文字列かバイト列なら、そのまま書く。

    書き方は、scripts/local_ci.py と同じ（字下げつき、最後に改行）。
    """
    path = root.parent / "result.json"
    if isinstance(result, dict) and result.get("commit") == "c" * 40:   # 確かめたコミットは、HEAD
        result = {**result, "commit": sh(root.parent / "wt" / "U1", "git", "rev-parse", "HEAD")}
    if not isinstance(result, str | bytes):
        result = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(result if isinstance(result, bytes) else result.encode("utf-8"))
    return cycle(root, "record-ci", "U1", "--result", str(path))


def windows_unit(root: Path) -> tuple[Path, str]:
    """Windows での確認が必要な作業単位を、その記録だけが無い状態まで進める。

    作業ツリーと、内容のツリーIDを返す。
    """
    set_header(root, "needs_windows: true\n")
    wt = implement(root)
    pass_all(root)
    commit(wt)
    return wt, snap(root)


@pytest.fixture(scope="module")
def waiting_for_windows(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Windows での記録だけが無い作業単位。取り込めない結果を、同じ状態から何通りも確かめるのに使い回す。"""
    root = make_repo(tmp_path_factory.mktemp("waiting-for-windows"))
    _, tree = windows_unit(root)
    return root, tree


def test_needs_windows_requires_a_windows_result_for_the_content(repo):
    wt, tree = windows_unit(repo)
    status = cycle(repo, "status", "U1").stdout
    assert "windows    未実施" in status and "windows: まだ記録が無い" in status
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1 and "windows: まだ記録が無い" in refused.stdout
    assert main_log(repo) == ["仕様の見出し", "init"]

    result = ci_result(tree, commit=sh(wt, "git", "rev-parse", "HEAD"))
    recorded = record_ci(repo, result)
    assert recorded.returncode == 0, recorded.stderr
    assert f"U1 windows: pass を記録（対象 {tree[:10]}、Windows）" in recorded.stdout
    entry = state(repo)["gates"]["windows"]
    assert (entry["verdict"], entry["tree"], entry["by"], entry["source"]) == (
        "pass", tree, "scripts/local_ci.py", "ci",
    )
    assert (entry["os"], entry["commit"]) == ("Windows", sh(wt, "git", "rev-parse", "HEAD"))
    # 取り込んだ結果の写しが、中身を変えずに、記録として残る（記録するパスの区切りは /）
    assert entry["report"].startswith(".cycle/runs/U1-windows-") and entry["report"].endswith(".json")
    assert (repo / entry["report"]).read_bytes() == (repo.parent / "result.json").read_bytes()
    assert json.loads((repo / entry["report"]).read_text(encoding="utf-8")) == result
    assert state(repo)["history"][-1]["gate"] == "windows"
    assert "windows    pass  scripts/local_ci.py" in cycle(repo, "status", "U1").stdout

    merged = cycle(repo, "merge", "U1")
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "windows: pass（scripts/local_ci.py）" in sh(repo, "git", "log", "-1", "--format=%B")


def test_units_without_needs_windows_do_not_need_a_windows_result(repo):
    set_header(repo, "needs_windows: false\n")
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    assert "windows" not in cycle(repo, "status", "U1").stdout
    assert cycle(repo, "merge", "U1").returncode == 0


def in_part(key: str, **changes: object):
    """結果の中の項目（os、check、tests など）の一部を差し替える。"""
    return lambda result: {**result, key: {**result[key], **changes}}


def replaced(**changes: object):
    return lambda result: {**result, **changes}


def dropped(key: str):
    return lambda result: {name: value for name, value in result.items() if name != key}


# 取り込めない結果: （結果の作り方、エラーの文面）
NOT_A_WINDOWS_PASS = {
    "Linux での結果": (in_part("os", system="Linux"), "Windows での結果ではない（Linux）"),
    "macOS での結果": (in_part("os", system="Darwin"), "Windows での結果ではない（Darwin）"),
    "OSの名前の書き方が違う": (in_part("os", system="windows"), "Windows での結果ではない（windows）"),
    "JSON でない": (lambda result: "{", "を読めない"),
    "UTF-8 でない": (lambda result: json.dumps(result).encode("utf-16"), "を読めない"),
    "JSON の一覧": (lambda result: "[]", "JSON のオブジェクトでない"),
    "形の版が違う": (replaced(schema=2), "scripts/local_ci.py の結果ではない"),
    "別の種類のファイル": (replaced(kind="doctor"), "scripts/local_ci.py の結果ではない"),
    "commit が無い": (dropped("commit"), "commit か tree が、git のIDの形でない"),
    "commit がIDの形でない": (replaced(commit="HEAD"), "commit か tree が、git のIDの形でない"),
    "tree がIDの形でない": (replaced(tree="HEAD"), "commit か tree が、git のIDの形でない"),
    "os が無い": (dropped("os"), "os.system が無い"),
    "os.system が文字列でない": (in_part("os", system=None), "os.system が無い"),
    "python が無い": (dropped("python"), "python.version が無い"),
    "python.version が文字列でない": (in_part("python", version=3.13), "python.version が無い"),
    "verdict が無い": (dropped("verdict"), "verdict が pass、fail、error のどれでもない"),
    "verdict が知らない値": (replaced(verdict="passed"), "verdict が pass、fail、error のどれでもない"),
    "check が無い": (dropped("check"), "check が無い"),
    "件数が負": (in_part("tests", skipped=-1), "tests の件数が、0 以上の整数でない"),
    "件数が真偽": (in_part("tests", failed=False), "tests の件数が、0 以上の整数でない"),
    "件数が文字列": (in_part("tests", passed="10"), "tests の件数が、0 以上の整数でない"),
    "件数の項目が足りない": (replaced(tests={"passed": 10}), "tests の件数が、0 以上の整数でない"),
    "tests が一覧": (replaced(tests=[10]), "tests の件数が、0 以上の整数でない"),
    "failed が一覧でない": (replaced(failed="none"), "failed が、名前の一覧でない"),
    "failed に名前でないものがある": (replaced(failed=[1]), "failed が、名前の一覧でない"),
    "合格だが、終了コードが 1": (in_part("check", returncode=1), "確認の終了コードが 0 でない"),
    "合格だが、終了コードが無い": (in_part("check", returncode=None), "確認の終了コードが 0 でない"),
    "合格だが、終了コードが真偽": (in_part("check", returncode=False), "確認の終了コードが 0 でない"),
    "合格だが、試験の件数が無い": (replaced(tests=None), "試験の件数が無い"),
    "合格だが、通過が 0": (in_part("tests", passed=0), "通過した試験が無いか、失敗した試験がある"),
    "合格だが、失敗が 1": (in_part("tests", failed=1), "通過した試験が無いか、失敗した試験がある"),
    "合格だが、エラーが 1": (in_part("tests", errors=1), "通過した試験が無いか、失敗した試験がある"),
    "合格だが、失敗した試験の名前がある": (
        replaced(failed=["tests/test_a.py::test_x"]), "通過した試験が無いか、失敗した試験がある",
    ),
    # 取り込むのは、コミットを取り出して、その内容のまま確かめた結果だけ
    "作業フォルダで確かめた結果（--current）": (
        replaced(mode="current", ref=None), "コミットを取り出して確かめた結果ではない（--current の結果は",
    ),
    "取り出したかどうかが無い": (dropped("mode"), "コミットを取り出して確かめた結果ではない"),
    "未コミットの変更を含む内容の結果": (replaced(dirty=True), "未コミットの変更を含む内容に対する結果"),
    "未コミットの変更の有無が無い": (dropped("dirty"), "未コミットの変更を含む内容に対する結果"),
    "未コミットの変更の有無が真偽でない": (replaced(dirty=0), "未コミットの変更を含む内容に対する結果"),
    "内容の食い違いが書かれた結果": (
        replaced(changed=["product.py"]), "確かめた内容が、対象のコミットと食い違っていた結果（changed）",
    ),
    "内容の食い違いの項目が無い": (dropped("changed"), "食い違っていた結果（changed）"),
    "取り出した内容が違って、確認を実行しなかった結果": (
        lambda result: {
            **failed_result(result["tree"]), "verdict": "error", "reason": "content_mismatch", "tests": None,
            "failed": [], "changed": ["product.py"],
        },
        "食い違っていた結果（changed）",
    ),
}


@pytest.mark.parametrize("case", NOT_A_WINDOWS_PASS)
def test_result_that_is_not_a_consistent_windows_result_is_not_recorded(waiting_for_windows, case):
    root, tree = waiting_for_windows
    change, message = NOT_A_WINDOWS_PASS[case]
    result = record_ci(root, change(ci_result(tree)))
    assert result.returncode == 2
    assert message in result.stderr and "Traceback" not in result.stderr
    assert "windows" not in state(root)["gates"]
    assert not [entry for entry in state(root)["history"] if entry.get("gate") == "windows"]
    assert not list((root / ".cycle" / "runs").glob("U1-windows-*"))


def test_result_file_that_does_not_exist_is_reported(waiting_for_windows):
    root, _ = waiting_for_windows
    result = cycle(root, "record-ci", "U1", "--result", str(root.parent / "no-such-file.json"))
    assert result.returncode == 2 and "を読めない" in result.stderr and "Traceback" not in result.stderr


def test_result_for_another_content_is_not_recorded(waiting_for_windows):
    """別のコミットに対する結果は、使い回せない。"""
    root, tree = waiting_for_windows
    other = sh(root, "git", "rev-parse", "main^{tree}")         # 作業単位の変更が入る前の内容
    result = record_ci(root, ci_result(other))
    assert result.returncode == 2
    assert f"結果のツリー（{other[:10]}）が、作業ツリーのいまの内容（{tree[:10]}）と" in result.stderr
    assert "違うファイル: product.py" in result.stderr
    result = record_ci(root, ci_result("1" * 40))               # このリポジトリに無い内容
    assert result.returncode == 2 and "結果のツリー（1111111111）が、このリポジトリに無い" in result.stderr
    assert "windows" not in state(root)["gates"]
    refused = cycle(root, "merge", "U1")
    assert refused.returncode == 1 and "windows: まだ記録が無い" in refused.stdout


def test_result_whose_commit_does_not_hold_the_checked_tree_is_not_recorded(waiting_for_windows):
    """分かれ目は、結果のコミットから求める。そのコミットが、結果のツリーを持たなければ（別のコミットを書いた、
    このリポジトリに無い）、取り込まない。"""
    root, tree = waiting_for_windows
    before = sh(root, "git", "rev-parse", "main")               # 作業単位の変更が入る前のコミット
    for commit_id in (before, "1" * 40):
        result = record_ci(root, ci_result(tree, commit=commit_id))
        assert result.returncode == 2
        assert f"結果のコミット（{commit_id[:10]}）が、このリポジトリに無いか、そのツリーが" in result.stderr
    assert "windows" not in state(root)["gates"]
    assert not list((root / ".cycle" / "runs").glob("U1-windows-*"))


def test_windows_pass_with_a_single_passing_test_is_recorded(repo):
    """通過が1件でも（失敗とエラーが 0 なら）、合格として取り込む。"""
    _, tree = windows_unit(repo)
    result = ci_result(tree, tests={"total": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0})
    recorded = record_ci(repo, result)
    assert recorded.returncode == 0, recorded.stderr
    assert state(repo)["gates"]["windows"]["verdict"] == "pass"


@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_windows_result_that_did_not_pass_is_recorded_and_blocks_merge(repo, verdict):
    wt, tree = windows_unit(repo)
    assert record_ci(repo, ci_result(tree)).returncode == 0
    failed = failed_result(tree)
    if verdict == "error":      # 結果を得られなかった実行
        failed = {**failed, "verdict": "error", "reason": "no_test_results", "tests": None, "failed": []}
    result = record_ci(repo, failed)
    assert result.returncode == 1 and f"U1 windows: {verdict} を記録" in result.stdout
    assert state(repo)["gates"]["windows"]["verdict"] == verdict        # 前の合格は、残らない
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1 and f"windows: {verdict}" in refused.stdout
    assert main_log(repo) == ["仕様の見出し", "init"]


def test_windows_result_goes_stale_when_the_content_changes(repo):
    wt, tree = windows_unit(repo)
    result = ci_result(tree)
    assert record_ci(repo, result).returncode == 0
    (wt / "product.py").write_text("x = 3\n", encoding="utf-8")         # 記録の後で、製品のコードが変わる
    pass_all(repo)
    commit(wt)
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1
    assert "windows: 確認の後で変わったファイルがある（product.py）。やり直す" in refused.stdout
    assert main_log(repo) == ["仕様の見出し", "init"]
    again = record_ci(repo, result)                                      # 前の内容の結果は、取り込み直せない
    assert again.returncode == 2 and "一致しない（違うファイル: product.py）" in again.stderr
    assert record_ci(repo, ci_result(snap(repo))).returncode == 0
    assert cycle(repo, "merge", "U1").returncode == 0


@pytest.mark.parametrize("when", ["before_record", "after_record"])
def test_windows_result_goes_stale_when_the_branch_is_moved_onto_main(repo, when):
    """分かれ目は、確かめたコミットのものを控える（取り込む時点の HEAD のものではない）。"""
    wt, tree = windows_unit(repo)
    result = ci_result(tree, commit=sh(wt, "git", "rev-parse", "HEAD"))
    if when == "after_record":
        assert record_ci(repo, result).returncode == 0
    main_moves_on(repo)
    sh(wt, "git", "reset", "--soft", "main")
    sh(wt, "git", "commit", "-q", "-m", "付け替え")
    if when == "before_record":
        assert record_ci(repo, result).returncode == 0
    pass_all(repo)                          # ほかのゲートは、付け替えた後の内容で取り直す
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1
    assert "windows: 確認の後で、main との分かれ目（土台）が変わっている" in refused.stdout
    assert [line for line in refused.stdout.splitlines() if line.startswith("  - ")] == [
        line for line in refused.stdout.splitlines() if line.startswith("  - windows:")
    ]


def test_verifier_may_add_acceptance_tests_after_the_windows_result(repo):
    """Windows での結果の後で増えてよいのは、ほかの確認系のゲートと同じ。例外の場所への新しいファイルだけ。"""
    wt, tree = windows_unit(repo)
    result = ci_result(tree, commit=sh(wt, "git", "rev-parse", "HEAD"))     # このコミットを確かめた
    (wt / "tests" / "acceptance").mkdir(parents=True)
    (wt / "tests" / "acceptance" / "test_u1.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    assert cycle(repo, "check", "U1").returncode == 0
    commit(wt)
    recorded = record_ci(repo, result)          # 取り込みの時点で、受け入れ試験が増えている
    assert recorded.returncode == 0, recorded.stderr
    assert state(repo)["gates"]["windows"]["tree"] == tree != snap(repo)
    assert "統合: できる" in cycle(repo, "status", "U1").stdout

    # 例外の場所でも、結果の時点で既にあったファイルの書き換えは「変わった」と見なす
    with_test = snap(repo)
    assert record_ci(repo, ci_result(with_test)).returncode == 0    # 受け入れ試験を含む内容で、確かめ直した
    changed = "def test_ok():\n    assert 1\n"
    (wt / "tests" / "acceptance" / "test_u1.py").write_text(changed, encoding="utf-8")
    status = cycle(repo, "status", "U1").stdout
    assert "windows: 確認の後で変わったファイルがある（tests/acceptance/test_u1.py）。やり直す" in status
    refused = record_ci(repo, ci_result(with_test))
    assert refused.returncode == 2 and "違うファイル: tests/acceptance/test_u1.py" in refused.stderr


def test_windows_gate_can_only_be_recorded_from_a_result_file(waiting_for_windows):
    """Windows での合格は、record と run では記録できない（自己申告や、エージェントの判定では作れない）。"""
    root, tree = waiting_for_windows
    result = cycle(root, "record", "U1", "windows", "--verdict", "pass", "--by", "someone", "--tree", tree)
    assert result.returncode == 2 and "invalid choice: 'windows'" in result.stderr
    result = cycle(root, "run", "U1", "windows")
    assert result.returncode == 2 and "invalid choice: 'windows'" in result.stderr
    assert "windows" not in state(root)["gates"]


def test_needs_windows_adds_the_windows_gate_also_to_the_gates_listed_in_the_header(repo):
    """gates: で必要なゲートを絞っても、needs_windows: true なら windows は外れない（食い違いのエラーにも
    しない）。"""
    set_header(repo, "needs_windows: true\ngates: implement, check\n")
    wt = implement(repo)
    commit(wt)
    assert cycle(repo, "check", "U1").returncode == 0
    refused = cycle(repo, "merge", "U1")
    assert refused.returncode == 1
    assert "windows: まだ記録が無い" in refused.stdout and "review" not in refused.stdout
    assert record_ci(repo, ci_result(snap(repo))).returncode == 0
    merged = cycle(repo, "merge", "U1")
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "windows: pass（scripts/local_ci.py）" in sh(repo, "git", "log", "-1", "--format=%B")


def test_windows_gate_cannot_be_listed_in_the_gates_header(repo):
    set_header(repo, "gates: implement, windows\n")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2 and "知らないゲート" in result.stderr


@pytest.mark.parametrize("value", ["yes", '"true"', "true  # Windows で確かめる", "1", ""])
def test_unknown_needs_windows_value_is_rejected(repo, value):
    set_header(repo, f"needs_windows: {value}\n")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2
    assert "needs_windows は true か false だけ" in result.stderr
    assert not (repo / ".cycle" / "state" / "U1.json").exists()


def test_needs_windows_cannot_be_dropped_once_records_exist(repo):
    """ゲートの記録がある作業単位で、Windows での確認を外すには、記録を消してやり直す。"""
    set_header(repo, "needs_windows: true\n")
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    (repo / "docs" / "plan" / "U1-demo.md").write_text(SPEC, encoding="utf-8")
    commit(repo, "仕様の見出しから needs_windows を外す")
    result = cycle(repo, "start", "U1")
    assert result.returncode == 2 and "start --reset" in result.stderr
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1 and "start の後で変わっている" in result.stdout
    assert cycle(repo, "start", "U1", "--reset").returncode == 0
    assert "windows" not in cycle(repo, "status", "U1").stdout
    assert state(repo)["needs_windows"] is False


@pytest.mark.parametrize("extra, added", [("needs_windows: true\n", "windows"), ("needs_ux: true\n", "ux")])
def test_header_that_only_adds_gates_keeps_the_records(repo, extra, added):
    """必要なゲートが増えるだけの見出しの変更は、記録を残したまま start で取り込める。

    統合は増えたゲートを待つ。
    """
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before = state(repo)
    set_header(repo, extra)
    result = cycle(repo, "merge", "U1")                 # start をやり直すまでは、統合できない
    assert result.returncode == 1 and "start の後で変わっている" in result.stdout
    assert cycle(repo, "start", "U1").returncode == 0
    after = state(repo)
    assert after["gates"] == before["gates"]
    assert after["history"][:-1] == before["history"]
    assert after["history"][-1]["event"] == "gates_added"
    assert after["history"][-1]["gates_after"] == [*after["history"][-1]["gates_before"], added]
    status = cycle(repo, "status", "U1").stdout
    assert f"{added:<10} 未実施" in status and "review     pass" in status
    result = cycle(repo, "merge", "U1")
    assert result.returncode == 1
    assert [line for line in result.stdout.splitlines() if line.startswith("  - ")] == [
        f"  - {added}: まだ記録が無い",
    ]
