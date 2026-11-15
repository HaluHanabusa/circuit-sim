"""Linux / macOS で、起動したプロセスを別のまとまり（セッション）で動かすことに伴う止め方の試験。

ハーネスは、claude と確認を新しいセッションで起動し、打ち切りと中断では、まとまりごと止める
（cycle.group_options）。
- 端末を閉じたとき（SIGHUP）や終了の合図（SIGTERM）は、こちらにだけ届く。こちらが止めないと、起動した
  claude が動き続ける（W2 のレビュー M2）。
- 打ち切りの後は、出力の口を持ったままの子の終わりを待たない（W1）。子が自分で別のセッションに移ると、
  まとまりごと止めても残るので、待つ実装だと、子が終わるまで戻らない（W2 のレビュー M3）。
Windows には、このまとまりの仕組みも SIGHUP も無い（taskkill /T でたどる。tests/test_harness.py が確かめる）。
"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from test_harness import (
    CYCLE,
    CYCLE_MODULE,
    TEST_ENV,
    blocking_claude,
    cycle_env,
    implement,
    make_repo,
    state,
    wait_for,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux / macOS のプロセスのまとまりと合図を試す")

ROOT = Path(__file__).resolve().parents[1]
STOPS = ["SIGHUP", "SIGTERM"]


def stopped(go: Path) -> bool:
    """blocking_claude の偽が、止められていたか（合図を置いても、それを見て終わる印が現れない）。"""
    go.touch()
    time.sleep(1.0)
    return not Path(f"{go}.seen").exists()


@pytest.mark.parametrize("to_group", [False, True], ids=["プロセスへ", "まとまりへ"])
@pytest.mark.parametrize("name", STOPS)
def test_stop_signal_to_a_headless_run_stops_the_cli_too(tmp_path, name, to_group):
    """cycle.py run が終了の合図を受けたら、起動した claude を止めてから終わる（記録は変えない）。

    まとまりへ送る場合は、端末を閉じたときと同じ形（cycle.py を、前面のジョブのように、自分のまとまりの先頭
    として起動し、そのまとまりへ送る）。claude は別のまとまりにいるので、合図は cycle.py にだけ届く。
    """
    repo = make_repo(tmp_path)
    implement(repo)
    env, started, go = blocking_claude(tmp_path, "rev-s")
    proc = subprocess.Popen(
        [sys.executable, str(CYCLE), "run", "U1", "review"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cycle_env(repo, env), process_group=0,
    )
    wait_for(started)
    sig = getattr(signal, name)
    if to_group:
        os.killpg(proc.pid, sig)
    else:
        proc.send_signal(sig)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + sig
    assert stopped(go), "合図の後も、claude が動いていた"
    assert "review" not in state(repo)["gates"]


@pytest.mark.parametrize("to_group", [False, True], ids=["プロセスへ", "まとまりへ"])
@pytest.mark.parametrize("name", STOPS)
def test_stop_signal_to_the_headless_probe_of_doctor_stops_the_cli_too(tmp_path, name, to_group):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text("", encoding="utf-8")
    env, started, go = blocking_claude(tmp_path, "probe")
    elsewhere = tmp_path / "started-here"
    elsewhere.mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "doctor.py"), "--no-check", "--headless"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**TEST_ENV, "CYCLE_ROOT": str(root), **env}, cwd=elsewhere, process_group=0,
    )
    wait_for(started)
    sig = getattr(signal, name)
    if to_group:
        os.killpg(proc.pid, sig)
    else:
        proc.send_signal(sig)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + sig
    assert stopped(go), "合図の後も、claude が動いていた"


# 出力の口を受け継いだ子を、別のセッションに移して残し、止まり続けるコマンド。
# 子は、自分のプロセス番号を印のファイルに書き、release のファイルが置かれるまで（最長 30 秒）いる
LEAVES_A_CHILD_IN_ANOTHER_SESSION = """\
import pathlib
import subprocess
import sys
import time

child = (
    "import os, pathlib, sys, time\\n"
    "pathlib.Path(sys.argv[1] + '.pid').write_text(str(os.getpid()), encoding='utf-8')\\n"
    "end = time.monotonic() + 30\\n"
    "while time.monotonic() < end and not pathlib.Path(sys.argv[1]).exists():\\n"
    "    time.sleep(0.05)\\n"
)
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]], start_new_session=True)
while not pathlib.Path(sys.argv[1] + ".pid").exists():
    time.sleep(0.05)
print("started", flush=True)
time.sleep(60)
"""


def test_timeout_does_not_wait_for_a_child_that_moved_to_another_session(tmp_path):
    """打ち切りの後、まとまりの外に移って残った子（出力の口を持ったまま）の終わりを待たずに戻る。

    子は、まとまりごと止めても残る（下で確かめる）。待つ実装だと、子が終わる 30 秒後まで戻らない。
    """
    release = tmp_path / "release"
    command = [sys.executable, "-c", LEAVES_A_CHILD_IN_ANOTHER_SESSION, str(release)]
    began = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            CYCLE_MODULE.run_captured(command, tmp_path, 3)
        waited = time.monotonic() - began
        pid = int(Path(f"{release}.pid").read_text(encoding="utf-8"))
        os.kill(pid, 0)                                 # 子は、打ち切りの後も残っている（この試験の前提）
    finally:
        release.touch()                                 # 残った子を終わらせる
    assert waited < 15


# 止められるまでいる claude の代わり。始まったら印（.pid）を置き、最後まで進んだら .finished を置く
STAYS_UNTIL_STOPPED = """\
import os, pathlib, sys, time
pathlib.Path(sys.argv[1] + ".pid").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(float(sys.argv[2]))
pathlib.Path(sys.argv[1] + ".finished").touch()
"""
STAYS_SECONDS = 6.0


@pytest.mark.parametrize(
    ("name", "second"),
    [("SIGHUP", "SIGHUP"), ("SIGTERM", "SIGTERM"), ("SIGHUP", "SIGTERM"), ("SIGTERM", "SIGHUP")],
)
def test_second_stop_signal_while_stopping_the_cli_does_not_leave_it_running(tmp_path, name, second):
    """端末を閉じると、前面のジョブに SIGHUP が続けて2回届く（W2 の3回目のレビュー）。1回目の合図で、起動した
    claude を止めている途中に2回目が届いても、claude を止め終えてから終わる（claude の終わりを待たない）。

    2回目の合図は、止める処理（kill_tree）の始めに起こす（届く時刻の幅は、実物では 100〜500 マイクロ秒）。
    1回目と違う種類の合図（SIGHUP の後の SIGTERM など）も無視する。合図の扱いは、終わった後に元に戻る。
    """
    sig, again = getattr(signal, name), getattr(signal, second)
    marker = tmp_path / "cli"
    command = [sys.executable, "-c", STAYS_UNTIL_STOPPED, str(marker), str(STAYS_SECONDS)]
    kill_tree = CYCLE_MODULE.kill_tree

    def hit_again_while_stopping(proc):
        signal.raise_signal(again)
        kill_tree(proc)

    def first_signal():
        wait_for(Path(f"{marker}.pid"))
        signal.pthread_kill(threading.main_thread().ident, sig)      # 待っている、こちらのスレッドに届ける

    before = {stop: signal.getsignal(stop) for stop in (sig, again)}
    sender = threading.Thread(target=first_signal)
    began = time.monotonic()
    sender.start()
    with (
        mock.patch.object(CYCLE_MODULE, "kill_tree", hit_again_while_stopping),
        pytest.raises(SystemExit) as stopped_by,
    ):
        CYCLE_MODULE.run_captured(command, tmp_path, None)
    sender.join()
    assert stopped_by.value.code == 128 + sig
    assert time.monotonic() - began < STAYS_SECONDS, "claude が終わるまで待っていた（止められていない）"
    assert not Path(f"{marker}.finished").exists()
    assert {stop: signal.getsignal(stop) for stop in (sig, again)} == before


def test_stop_signal_stays_ignored_until_the_outermost_clean_up_is_done():
    """終了の合図を例外に変える枠が入れ子でも、合図を受けた後は、外側の枠の後始末が終わるまで、続く合図を無視する。"""
    sig = signal.SIGHUP
    before = signal.getsignal(sig)
    done = []
    with pytest.raises(SystemExit) as stopped_by, CYCLE_MODULE.termination_as_exception():
        try:
            with CYCLE_MODULE.termination_as_exception():
                signal.raise_signal(sig)            # 1回目: 内側の枠で例外になる
        finally:
            signal.raise_signal(sig)                # 2回目: 外側の後始末の途中（無視される）
            done.append("外側の後始末")
    assert stopped_by.value.code == 128 + sig
    assert done == ["外側の後始末"]
    assert signal.getsignal(sig) == before


def test_inner_frame_left_without_a_stop_signal_gives_the_outer_frame_its_handling_back():
    """合図を受けずに内側の枠を出たら、外側の枠の扱い（合図を例外に変える）に戻る。"""
    sig = signal.SIGTERM
    with pytest.raises(SystemExit) as stopped_by, CYCLE_MODULE.termination_as_exception():
        with CYCLE_MODULE.termination_as_exception():
            pass
        signal.raise_signal(sig)
    assert stopped_by.value.code == 128 + sig


def test_signal_ignored_before_the_frame_stays_ignored_inside_it():
    """無視に設定されていた合図（nohup の SIGHUP）は、枠に入っても差し替えない。届いても、そのまま続ける。"""
    sig = signal.SIGHUP
    before = signal.signal(sig, signal.SIG_IGN)
    try:
        with CYCLE_MODULE.termination_as_exception():
            assert signal.getsignal(sig) is signal.SIG_IGN
            signal.raise_signal(sig)                # 例外にならない
            assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL      # ほかの合図は、例外に変える
        assert signal.getsignal(sig) is signal.SIG_IGN
    finally:
        signal.signal(sig, before)


def test_headless_run_started_with_nohup_keeps_running_when_the_terminal_closes(tmp_path):
    """nohup で起動した run は、端末を閉じても（SIGHUP）、claude の終わりを待って記録する。"""
    repo = make_repo(tmp_path)
    implement(repo)
    env, started, go = blocking_claude(tmp_path, "rev-s")
    proc = subprocess.Popen(
        [sys.executable, str(CYCLE), "run", "U1", "review"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cycle_env(repo, env), process_group=0,
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),       # nohup と同じ
    )
    wait_for(started)
    os.killpg(proc.pid, signal.SIGHUP)
    time.sleep(0.5)
    assert proc.poll() is None, "SIGHUP で終わった"
    go.touch()
    proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert state(repo)["gates"]["review"]["verdict"] == "pass"
