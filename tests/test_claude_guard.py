"""試験から本物の claude に行き着く経路を断つ仕組み（tests/conftest.py の遮断と見張り役）の試験。

本物の claude の代わりに、起動されたら印を残す「身代わり」を置く。身代わりは、試験のセッションが始まる前から
PATH にあった本物を表す。どの試験も、身代わりが起動されないことを確かめる。
レビューで見つかった3つの経路（偽が起動に失敗したシェル、実行できない一時フォルダ、Windows の拡張子）は、
遮断の無い PATH で身代わりに届くこと（穴の再現）と、遮断した PATH では届かないことを、組で確かめる。
"""
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import conftest as guard
import pytest
from test_error_dialogs import FakeKernel32
from test_harness import CYCLE_MODULE, cycle, headless, implement, install_claude, make_repo, state, was_run

WINDOWS = os.name == "nt"
only_posix = pytest.mark.skipif(WINDOWS, reason="Linux / macOS の仕組み（実行の印、/bin/sh、リンク）を試す")
only_windows = pytest.mark.skipif(not WINDOWS, reason="Windows でだけ起こる止め方を試す")


def stand_in(directory: Path) -> Path:
    """本物の claude の身代わり（起動されると argv.json を残し、合格の結果を返す）を置く。"""
    install_claude(directory, headless(session="the-real-one"))
    return directory


def guarded(tmp_path: Path, original: str, windows: bool = WINDOWS) -> tuple[str, Path]:
    """original の PATH を、試験のセッションと同じ組み方で遮断した PATH と、その見張り役の記録のファイル。"""
    place = tmp_path / "guard"
    _, path = guard.guard_path(original, None, place, windows)
    return path, place / "sentinel" / guard.SENTINEL_LOG


# ------------------------------------------------------------ 遮断の組み方

def test_this_session_runs_with_claude_taken_off_the_path():
    """この試験のセッションの PATH では、claude として見つかるのは、末尾の見張り役だけ。"""
    sentinel = os.environ[guard.SENTINEL_VAR]
    entries = os.environ["PATH"].split(os.pathsep)
    assert entries[-1] == sentinel
    assert os.environ[guard.SAFE_PATH_VAR] == os.pathsep.join(entries[:-1])
    names = guard.claude_names(os.environ.get("PATHEXT"))
    assert not [entry for entry in entries[:-1] if guard.removal_reason(entry, names)]
    assert Path(shutil.which("claude")).parent == Path(sentinel)


def test_places_that_hold_anything_launched_as_claude_are_taken_off(tmp_path):
    """claude と、拡張子の付いた名前（大文字と小文字を区別しない）、PATHEXT の拡張子のある場所を取り除く。
    相対パスの項目、空の項目、中を確かめられない場所（無い場所）も取り除く。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "git").write_text("", encoding="utf-8")
    (plain / "claude-code.txt").write_text("", encoding="utf-8")       # 名前の一部だけが同じ
    holding = []
    names = ("claude", "CLAUDE.EXE", "Claude.Cmd", "claude.bat", "claude.com", "claude.ps1", "claude.pyw")
    for name in names:
        place = tmp_path / f"has-{name.replace('.', '-')}"
        place.mkdir()
        (place / name).write_text("", encoding="utf-8")
        holding.append(str(place))
    path = os.pathsep.join([*holding, ".", "", "bin", str(tmp_path / "no-such-place"), str(plain)])
    assert guard.without_claude(path, ".COM;.EXE;.PYW") == [str(plain)]
    # PATHEXT に無い拡張子（.pyw）は、claude として起動されないので残す
    assert guard.without_claude(path, None) == [holding[-1], str(plain)]


@only_posix
def test_a_tool_found_only_next_to_claude_stays_reachable_through_a_link(tmp_path):
    """claude と同じ場所にしか無い git（Homebrew の bin など）は、リンクで残す。claude は残さない。"""
    both = tmp_path / "both"
    stand_in(both)
    os.symlink(shutil.which("git"), both / "git")
    safe, path = guard.guard_path(str(both), None, tmp_path / "guard")
    git = shutil.which("git", path=safe)
    assert git is not None and Path(git).parent == tmp_path / "guard" / "tools"
    assert subprocess.run([git, "--version"], capture_output=True).returncode == 0
    assert shutil.which("claude", path=safe) is None
    assert Path(shutil.which("claude", path=path)).parent == tmp_path / "guard" / "sentinel"


@only_windows
def test_a_tool_found_only_next_to_claude_stops_the_session_on_windows(tmp_path):
    """リンクを作れない Windows では、git が見つからなくなる遮断を組まずに止める。"""
    both = tmp_path / "both"
    stand_in(both)
    (both / "git.exe").write_bytes(b"")
    with pytest.raises(guard.GuardError, match="git が、claude と同じ場所"):
        guard.guard_path(str(both), None, tmp_path / "guard")


def test_session_start_report_names_the_places_taken_off_and_the_tools_kept(tmp_path):
    """セッションの始めに、PATH から外した場所と理由、リンクで残した道具を表示する（原因を追えるように）。
    利用者のホームの下は ~ から書く。"""
    has = tmp_path / "has"
    has.mkdir()
    (has / "claude.cmd").write_text("", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    tools = tmp_path / "guard" / "tools"
    tools.mkdir(parents=True)
    kept = []
    if not WINDOWS:                         # リンクを作れるOSでだけ、道具を残す（Windows では試験を始めない）
        os.symlink(shutil.which("git"), tools / "git")
        kept = [f"  リンクで残した道具: git → {shutil.which('git')}"]
    path = os.pathsep.join([str(has), "", "rel", str(tmp_path / "missing"), str(plain)])
    with mock.patch.object(Path, "home", return_value=tmp_path / "elsewhere"):
        lines = guard.guard_report(path, None, tools)
    assert lines == [
        "試験の遮断（本物の claude を起動しないため。tests/conftest.py）:",
        f"  PATH から外した場所: {has}（claude.cmd がある）",
        "  PATH から外した場所: （空の項目）",
        "  PATH から外した場所: rel（相対パスの項目）",
        f"  PATH から外した場所: {tmp_path / 'missing'}（中を確かめられない場所）",
        *kept,
    ]
    with mock.patch.object(Path, "home", return_value=tmp_path):
        assert guard.guard_report(str(has), None, tmp_path / "none")[1:] == [
            f"  PATH から外した場所: {Path('~', 'has')}（claude.cmd がある）",
        ]
        assert guard.guard_report(str(plain), None, tmp_path / "none")[1:] == ["  PATH から外した場所: なし"]


@pytest.mark.parametrize("windows", [True, False], ids=["Windows", "ほかのOS"])
def test_tests_turn_the_os_error_dialogs_off_on_windows(windows):
    """試験のプロセスは、Windows では、OSのエラー画面を出さない印を、もとの印に足す（ハーネスと同じ印）。"""
    kernel32 = FakeKernel32(mode=0x0004)
    with (
        mock.patch.object(ctypes, "WinDLL", kernel32, create=True),
        mock.patch.object(guard, "WINDOWS", windows),
    ):
        guard.quiet_error_dialogs()
    if windows:
        assert kernel32.loaded == ["kernel32"]
        assert kernel32.set_calls == [0x0004 | CYCLE_MODULE.QUIET_ERROR_MODE]
        assert guard.QUIET_ERROR_MODE == CYCLE_MODULE.QUIET_ERROR_MODE == 0x8003
    else:
        assert kernel32.loaded == [] and kernel32.set_calls == []


def test_every_test_process_turns_the_os_error_dialogs_off():
    """設定は、試験の設定の読み込み（pytest_configure。並列実行の子でも呼ばれる）で行う。"""
    with mock.patch.object(guard, "quiet_error_dialogs") as quiet:
        guard.pytest_configure(None)
    quiet.assert_called_once_with()


INNER_TESTS = """\
import os
import subprocess


def test_calls_claude():
    assert os.environ["STAND_IN"] not in os.environ["PATH"].split(os.pathsep)
    shell = ["cmd", "/c", "claude"] if os.name == "nt" else ["/bin/sh", "-c", "claude"]
    assert subprocess.run(shell, stdin=subprocess.DEVNULL).returncode == {code}


def test_quiet():
    with open(os.environ["SEEN"], "w", encoding="utf-8") as handle:      # 見張り役の場所を、外に知らせる
        handle.write(os.environ["CIRCUIT_SIM_CLAUDE_SENTINEL"])
"""


def run_session(tmp_path: Path, *args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess:
    """この仕組み（conftest.py の写し）を持つ、別の試験のセッションを実行する。PATH の先頭に身代わりがある。

    このリポジトリの試験は含まない（写した先の小さな試験だけ）ので、入れ子の目印を外して起動してよい。
    """
    inner = tmp_path / "inner"
    inner.mkdir(exist_ok=True)
    shutil.copy(Path(guard.__file__), inner / "conftest.py")
    (inner / "test_inner.py").write_text(INNER_TESTS.format(code=guard.SENTINEL_RETURNCODE), encoding="utf-8")
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    real = stand_in(tmp_path / "real")
    dropped = (guard.RUNNING, guard.SENTINEL_VAR, guard.SAFE_PATH_VAR)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k not in dropped}
    env.update(PATH=f"{real}{os.pathsep}{os.environ['PATH']}", STAND_IN=str(real), PYTHONIOENCODING=encoding)
    env.update(SEEN=str(tmp_path / "sentinel-place.txt"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py", *args],
        cwd=inner, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120,
    )
    assert not was_run(real), proc.stdout + proc.stderr
    return proc


def test_session_fails_when_the_sentinel_is_started_even_if_every_test_passes(tmp_path):
    """身代わりを PATH の先頭に置いても、セッションの始めに取り除かれ、シェルは末尾の見張り役に行き着く。
    見張り役が起動されたら、試験がすべて通っても、セッションは失敗する。起動した試験の名前を表示する。"""
    proc = run_session(tmp_path)
    assert "2 passed" in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == guard.SENTINEL_EXIT_CODE, proc.stdout + proc.stderr
    # 始めに、身代わりのある場所を外したことを表示している
    real = guard.shown_place(str(tmp_path / "real"))
    shown = [line for line in proc.stderr.splitlines() if line.startswith(f"  PATH から外した場所: {real}（")]
    assert shown, proc.stderr
    assert "見張り役の claude が 1 回起動された" in proc.stderr
    assert "起動した試験: test_inner.py::test_calls_claude" in proc.stderr


def test_session_passes_when_the_sentinel_is_not_started(tmp_path):
    proc = run_session(tmp_path, "-k", "test_quiet")
    assert proc.returncode == 0 and "1 passed" in proc.stdout, proc.stdout + proc.stderr
    assert "見張り役" not in proc.stderr
    place = Path((tmp_path / "sentinel-place.txt").read_text(encoding="utf-8"))
    assert place.name == "sentinel" and not place.parent.exists()          # 終わったら、置いた場所を消す


def test_failing_session_reports_on_a_terminal_that_cannot_show_the_text(tmp_path):
    """UTF-8 でない端末（出せない文字で止める設定）でも、知らせる文が落ちずに出て、セッションは失敗になる。"""
    proc = run_session(tmp_path, "-s", encoding="ascii:strict")         # -s: 画面に直に書く（取り込まない）
    assert proc.returncode == guard.SENTINEL_EXIT_CODE, proc.stdout + proc.stderr
    assert "claude" in proc.stderr and "INTERNALERROR" not in proc.stdout + proc.stderr


# ------------------------------------------------------------ レビューで見つかった経路

@only_posix
@pytest.mark.parametrize("shell", ["/bin/sh", "dash", "bash"])
def test_shell_that_searches_on_after_a_fake_that_cannot_start_does_not_reach_the_real_one(tmp_path, shell):
    """(a) 偽の claude の起動に失敗すると（Python の場所に空白、改行が CRLF など）、シェルによっては PATH の
    探索を続ける（dash は続け、bash は止まる）。遮断した PATH では、その先にあるのは見張り役だけ。"""
    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"{shell} が入っていない（ほかのシェルの場合が確かめる）")
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "claude").write_text("#!/no/such/python\n", encoding="utf-8")       # 起動に失敗する偽
    (fake / "claude").chmod(0o755)
    real = stand_in(tmp_path / "real")
    path, log = guarded(tmp_path, str(real))
    env = {"PATH": f"{fake}{os.pathsep}{path}"}
    proc = subprocess.run([program, "-c", "claude"], env=env, capture_output=True)
    assert proc.returncode != 0
    assert not was_run(real)
    if Path(program).resolve().name == "dash":      # 探索を続けるシェルは、見張り役に行き着く
        assert guard.sentinel_calls(log) and proc.returncode == guard.SENTINEL_RETURNCODE


@only_posix
@pytest.mark.skipif(shutil.which("dash") is None, reason="dash の無い環境（macOS など）では、この穴は無い")
def test_dash_reached_the_real_one_without_the_guard(tmp_path):
    """(a) の穴の再現: 遮断が無いと、dash は、起動に失敗した偽の後ろにある本物（身代わり）を起動する。"""
    dash = shutil.which("dash")
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "claude").write_text("#!/no/such/python\n", encoding="utf-8")
    (fake / "claude").chmod(0o755)
    real = stand_in(tmp_path / "real")
    subprocess.run([dash, "-c", "claude"], env={"PATH": f"{fake}{os.pathsep}{real}"}, capture_output=True)
    assert was_run(real)


@only_posix
@pytest.mark.parametrize("gate", ["implement", "review"])
def test_run_with_a_fake_that_cannot_be_executed_does_not_reach_the_real_one(tmp_path, gate):
    """(b) 一時フォルダが実行不可（noexec）だと、find_command は偽を飛ばして先を探す（実行の印を外して模擬）。
    遮断が無いと本物（身代わり）が動いて合格が記録され、遮断した PATH では見張り役が動いて error になる。"""
    repo = make_repo(tmp_path)
    implement(repo)
    fake = tmp_path / "fake"
    install_claude(fake, headless(session="fake"))
    (fake / "claude").chmod(0o644)
    real = stand_in(tmp_path / "real")
    # 後ろに続けるのは、git のある、claude の無い場所（この試験のセッションが組んだもの）
    original = os.pathsep.join([str(real), os.environ[guard.SAFE_PATH_VAR]])
    assert cycle(repo, "run", "U1", gate, env={"PATH": f"{fake}{os.pathsep}{original}"}).returncode == 0
    assert was_run(real) and state(repo)["gates"][gate]["by"] == "the-real-one"       # 穴の再現
    (real / "argv.json").unlink()
    path, log = guarded(tmp_path, original)
    result = cycle(repo, "run", "U1", gate, env={"PATH": f"{fake}{os.pathsep}{path}"})
    assert result.returncode == 1 and f"{gate}: error" in result.stdout
    assert not was_run(real) and not was_run(fake)
    assert len(guard.sentinel_calls(log)) == 1


def test_windows_extension_list_without_cmd_does_not_reach_the_real_exe(tmp_path):
    """(c) Windows で、拡張子の既定（DEFAULT_PATHEXT）から .CMD が外れると、偽の claude.cmd を飛ばして、
    PATH の後ろの本物の claude.exe を選んでいた（W1 の再検証の壊す確認。外側の試験は、内側の失敗を
    「壊すと落ちた」と数えるので気づけない）。Linux でも、Windows の探し方（区切りの ;、拡張子の付く名前）を
    模擬して確かめる。大文字と小文字を区別しないファイルの名前は、既定の拡張子を小文字にして模擬する。"""
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "claude.cmd").write_text("@echo fake\n", encoding="utf-8")
    real = tmp_path / "real"
    real.mkdir()
    (real / "claude.exe").write_text("", encoding="utf-8")
    with (
        mock.patch.object(os, "pathsep", ";"),
        mock.patch.object(CYCLE_MODULE, "WINDOWS", True),
        mock.patch.object(CYCLE_MODULE, "DEFAULT_PATHEXT", ".com;.exe;.bat"),       # .CMD を外した既定
    ):
        path, _ = guarded(tmp_path, str(real), windows=True)
        for place in (fake, real, tmp_path / "guard" / "sentinel"):
            for file in place.iterdir():
                file.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake};{real}", "PATHEXT": ""}):
            found_without_guard = CYCLE_MODULE.find_command("claude")
        with mock.patch.dict(os.environ, {"PATH": f"{fake};{path}", "PATHEXT": ""}):
            found_with_guard = CYCLE_MODULE.find_command("claude")
    assert found_without_guard == str(real / "claude.exe")                             # 穴の再現
    assert found_with_guard == str(tmp_path / "guard" / "sentinel" / "claude.bat")     # 見張り役（.bat）
