"""OSで動きが違うハーネスの決まりの試験（H2）。

tests/test_harness.py の試験は、どのOSでも同じものが走り、スキップするのは決まった数件だけ（W1 の受け入れ試験
が数えている）。OSに無い仕組みを試すものは、こちらに置く。
Windows の分岐のうち、OSの仕組みを模擬できるもの（msvcrt.locking、taskkill の呼び出し）は、どのOSでも、
模擬して流す（Linux で1か所ずつ壊す確認をしても、その分岐の壊し方が落ちるように）。
"""
import os
import subprocess
from unittest import mock

import pytest
from conftest import SAFE_PATH_VAR
from test_harness import (
    CYCLE_MODULE,
    commit,
    cycle,
    headless,
    held_lock,
    implement,
    install_claude,
    main_log,
    make_repo,
    pass_all,
    start_cycle,
    state,
    was_run,
)

WINDOWS = os.name == "nt"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def only_posix(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(WINDOWS, reason=reason)


def only_windows(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not WINDOWS, reason=reason)


@only_posix("Windows の msvcrt.locking には共有のロックが無い（LK_NBRLCK も排他のロックと同じ）")
@pytest.mark.parametrize("lock", ["state/U1.lock", "merge.lock"])
def test_lock_is_exclusive_and_waits_even_for_a_shared_holder(repo, lock):
    """記録と統合のロックは排他。共有のロックで済ませると、2つの実行が同時に状態を読み書きできてしまう。"""
    wt = implement(repo)
    pass_all(repo)
    commit(wt)
    before = state(repo)
    with held_lock(repo / ".cycle" / lock, shared=True):
        proc = start_cycle(repo, "merge", "U1")
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.0)
        assert state(repo) == before and main_log(repo) == ["init"]
    assert proc.wait(timeout=30) == 0


class FakeMsvcrt:
    """Windows の msvcrt.locking の模擬。取る呼び出しは、busy 回だけ「他の実行が持っている」で拒む。

    呼び出しごとに（種類、バイト数、ファイルの中の位置）を控える。
    """
    LK_UNLCK, LK_NBLCK = 0, 2

    def __init__(self, busy: int):
        self.busy, self.calls = busy, []

    def locking(self, fd: int, mode: int, size: int) -> None:
        self.calls.append((mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise PermissionError(13, "他の実行が持っている")


def test_windows_lock_waits_by_sleeping_and_locks_the_first_byte(tmp_path):
    """Windows のロック: 他の実行が持っている間は、POLL_SECONDS ずつ休んで取り直す（回り続けない）。取るのも
    外すのも、ロックのファイルの先頭の1バイト（ファイルに中身があって、追記の位置が末尾でも）。"""
    path = tmp_path / "U1.lock"
    path.write_text("old", encoding="utf-8")         # 追記で開くと、位置は末尾（3）になる
    fake = FakeMsvcrt(busy=3)
    sleeps = []
    with (
        mock.patch.object(CYCLE_MODULE, "WINDOWS", True),
        mock.patch.object(CYCLE_MODULE, "msvcrt", fake, create=True),
        mock.patch.object(CYCLE_MODULE.time, "sleep", sleeps.append),
    ):
        with CYCLE_MODULE.locked(path):
            assert fake.calls == [(fake.LK_NBLCK, 1, 0)] * 4
        assert fake.calls[4:] == [(fake.LK_UNLCK, 1, 0)]
    assert sleeps == [CYCLE_MODULE.POLL_SECONDS] * 3


def test_windows_stop_goes_through_taskkill_for_the_whole_tree():
    """Windows の止め方: System32 の taskkill を絶対パスで、/T（その先のプロセスまで）と /F で起動する。"""
    calls = []
    proc = mock.Mock(pid=4321)
    with (
        mock.patch.object(CYCLE_MODULE, "WINDOWS", True),
        mock.patch.object(CYCLE_MODULE.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw))),
        mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}),
    ):
        CYCLE_MODULE.kill_tree(proc)
        taskkill = os.path.join(r"C:\Windows", "System32", "taskkill.exe")
    assert calls == [([taskkill, "/F", "/T", "/PID", "4321"], {"capture_output": True})]
    proc.kill.assert_called_once_with()


@only_windows("cmd.exe が、名前だけのコマンドを実行した場所で探すのは Windows だけ")
@pytest.mark.parametrize("told", [False, True], ids=["変数なし（穴の再現）", "変数あり"])
def test_batch_launcher_does_not_run_a_node_planted_in_the_place_of_execution(tmp_path, told):
    """npm 版の claude.cmd は、中で node を名前だけで呼ぶ。実行した場所の node.cmd が動かないこと（11番）。"""
    here = tmp_path / "worktree"
    here.mkdir()
    marker = tmp_path / "planted-node-ran"
    (here / "node.cmd").write_text(f'@echo off\r\ntype nul > "{marker}"\r\n', encoding="oem")
    launcher = tmp_path / "npm" / "claude.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@echo off\r\nnode --version\r\nexit /b 0\r\n", encoding="oem")
    env = {key: value for key, value in os.environ.items() if key != CYCLE_MODULE.NO_CURRENT_DIRECTORY_SEARCH}
    env["PATH"] = os.environ[SAFE_PATH_VAR]
    if told:
        env[CYCLE_MODULE.NO_CURRENT_DIRECTORY_SEARCH] = "1"
    subprocess.run([str(launcher)], cwd=here, env=env, capture_output=True, timeout=60)
    assert marker.exists() is not told


@only_posix("リンクを作れるOS（Windows では、リンクを作るのに権限が要る）")
def test_link_on_path_that_leads_inside_the_worktree_is_not_used(repo, tmp_path):
    """PATH の場所が使ってよい所でも、そこにあるリンクの先が作業ツリーの中なら使わない。"""
    wt = implement(repo)
    planted = wt / "node_modules" / ".bin"
    install_claude(planted, headless(session="planted"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "claude").symlink_to(planted / "claude")
    result = cycle(repo, "run", "U1", "review", env={"PATH": str(outside)})
    assert result.returncode == 2 and "claude コマンドが見つからない" in result.stderr
    assert not was_run(planted) and "review" not in state(repo)["gates"]
