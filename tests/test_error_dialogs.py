"""Windows の、OSのエラー画面を出させない設定（scripts/cycle.py の quiet_error_dialogs）の試験。

壊れた claude.exe を起動すると、Windows は「サポートされていない 16 ビット アプリケーション」の画面を出し、
人が OK を押すまで、起動した側を止める（W2 で、POのPCの確認が止まった）。ハーネス（cycle.py、local_ci.py、
doctor.py）と試験のセッション（tests/conftest.py）は、始めに、画面を出さずにエラーとして返させる設定にする。
起動する子プロセスは、この設定を引き継ぐ。
Linux では、Windows の関数（ctypes.WinDLL）を差し替えて、Windows の分岐が呼ばれることを確かめる。
実物の画面の扱いは、Windows でだけ確かめる（このファイルの only_windows の試験。時間を限って、止まらない）。
local_ci.py と doctor.py の main の試験は、tests/test_local_ci.py と tests/test_dev_tools.py にある。
"""
import contextlib
import ctypes
import os
import subprocess
import sys
import tokenize
from pathlib import Path
from unittest import mock

import pytest
from test_harness import CYCLE, CYCLE_MODULE, WINDOWS, cycle, cycle_env, implement, make_repo, state

only_windows = pytest.mark.skipif(not WINDOWS, reason="Windows のエラー画面の設定を、実物で確かめる")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@only_windows
def test_broken_cli_image_is_an_error_without_an_os_dialog_on_windows(repo, tmp_path):
    """壊れた claude.exe で、cycle.py run が OSのエラー画面で止まらず、エラーで終わる（POのPCで止まった形）。

    cycle.py は、試験のプロセスのエラー画面の設定を引き継がない形（CREATE_DEFAULT_ERROR_MODE）で起動する。
    画面を出さない設定は、cycle.py が自分で行う。画面が出たら、時間切れで止めて、落ちる。
    """
    implement(repo)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "claude.exe").write_text("#!/no/such/interpreter\n", encoding="utf-8")
    env = cycle_env(repo, {"PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"})
    flags = subprocess.CREATE_DEFAULT_ERROR_MODE | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(CYCLE), "run", "U1", "review"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, creationflags=flags,
    )
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        CYCLE_MODULE.kill_tree(proc)
        proc.communicate()
        pytest.fail("cycle.py が 60 秒で終わらなかった（OSのエラー画面が出て、止まっていた可能性がある）")
    assert proc.returncode == 2, out + err
    assert "claude を起動できない" in err and "Traceback" not in err
    assert "review" not in state(repo)["gates"]


@only_windows
def test_error_dialogs_are_off_in_the_tests_and_in_the_processes_they_start():
    """試験のプロセス（tests/conftest.py）と、そこから起動したプロセスで、OSのエラー画面が出ない設定になっている。"""
    quiet = CYCLE_MODULE.QUIET_ERROR_MODE
    assert ctypes.WinDLL("kernel32").GetErrorMode() & quiet == quiet
    ask = "import ctypes; print(ctypes.WinDLL('kernel32').GetErrorMode())"
    inherited = subprocess.run([sys.executable, "-c", ask], capture_output=True, text=True, encoding="ascii")
    assert int(inherited.stdout) & quiet == quiet
    # 引き継がない形で起動しても、ハーネスの関数が設定する
    sets = (
        f"import sys; sys.path.insert(0, {str(CYCLE.parent)!r}); import cycle, ctypes;"
        f"cycle.quiet_error_dialogs(); print(ctypes.WinDLL('kernel32').GetErrorMode())"
    )
    fresh = subprocess.run(
        [sys.executable, "-c", sets], capture_output=True, text=True, encoding="ascii",
        creationflags=subprocess.CREATE_DEFAULT_ERROR_MODE,
    )
    assert int(fresh.stdout) & quiet == quiet, fresh.stderr


class FakeKernel32:
    """ctypes.WinDLL の代わり（Linux でも、Windows の分岐を通す）。エラー画面の設定の読み書きを控える。"""

    def __init__(self, mode: int):
        self.mode, self.loaded, self.set_calls = mode, [], []

    def __call__(self, name: str) -> "FakeKernel32":
        self.loaded.append(name)
        return self

    def GetErrorMode(self) -> int:     # Windows の関数の名前のまま
        return self.mode

    def SetErrorMode(self, mode: int) -> int:
        self.set_calls.append(mode)
        previous, self.mode = self.mode, mode
        return previous


def test_error_dialogs_are_turned_off_on_windows_keeping_the_existing_settings():
    """Windows では、起動できないファイル、異常終了、開けないファイルの画面を出さない印を、もとの印に足す。"""
    kernel32 = FakeKernel32(mode=0x0004)        # もとから付いている印（SEM_NOALIGNMENTFAULTEXCEPT）
    with (
        mock.patch.object(ctypes, "WinDLL", kernel32, create=True),
        mock.patch.object(CYCLE_MODULE, "WINDOWS", True),
    ):
        CYCLE_MODULE.quiet_error_dialogs()
    assert kernel32.loaded == ["kernel32"]
    assert kernel32.set_calls == [0x0004 | 0x0001 | 0x0002 | 0x8000]


def test_error_dialog_setting_is_not_touched_outside_windows():
    kernel32 = FakeKernel32(mode=0)
    with (
        mock.patch.object(ctypes, "WinDLL", kernel32, create=True),
        mock.patch.object(CYCLE_MODULE, "WINDOWS", False),
    ):
        CYCLE_MODULE.quiet_error_dialogs()
    assert kernel32.loaded == [] and kernel32.set_calls == []


def test_harness_turns_the_error_dialogs_off_first(repo):
    """cycle.py は、何かを起動する前に、エラー画面を出さない設定にする（git と claude に引き継がれる）。"""
    implement(repo)
    order = []
    run_git = CYCLE_MODULE.run_git

    def noted_git(*args, **kwargs):
        order.append("git")
        return run_git(*args, **kwargs)

    with (
        mock.patch.object(CYCLE_MODULE, "quiet_error_dialogs", lambda: order.append("quiet")),
        mock.patch.object(CYCLE_MODULE, "run_git", noted_git),
    ):
        cycle(repo, "status", "U1")
    assert order[0] == "quiet" and "git" in order


def test_no_process_is_started_without_the_error_mode_of_the_harness():
    """どの起動も、エラー画面の設定を引き継ぐ。引き継がない指定（CREATE_DEFAULT_ERROR_MODE、0x04000000）を、
    道具のコードのどこにも書かない（注釈と文字列は数えない）。"""
    for script in sorted(CYCLE.parent.glob("*.py")):
        with script.open(encoding="utf-8") as handle:
            tokens = list(tokenize.generate_tokens(handle.readline))
        names = {token.string for token in tokens if token.type == tokenize.NAME}
        numbers = set()
        for token in tokens:
            if token.type == tokenize.NUMBER:
                with contextlib.suppress(ValueError):       # 小数などは数えない
                    numbers.add(int(token.string, 0))
        assert "CREATE_DEFAULT_ERROR_MODE" not in names and 0x04000000 not in numbers, script.name
    with mock.patch.object(CYCLE_MODULE, "WINDOWS", True):
        assert "creationflags" not in CYCLE_MODULE.group_options()
