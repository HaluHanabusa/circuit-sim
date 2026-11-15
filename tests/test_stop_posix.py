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
import time
from pathlib import Path

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
