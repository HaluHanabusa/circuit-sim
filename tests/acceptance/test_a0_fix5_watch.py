"""A0 の独立検証（verifier、5回目）: 受け入れ試験の見張りそのものの確かめ。

記録は docs/verification/A0-verifier-5.md。きっかけは、PO の PC（Windows 11、Python 3.13.2）での確認で
分かった3つ: 長い ID の試験が準備の段階でエラーになった、偽のブラウザが失敗すると本物のブラウザへ進みうる、
試験が起動したアプリ本体が終わった後も待ち受け続けていた。

1. 受け入れ試験の ID は ID_LIMIT 文字まで（pytest は、実行中の試験の ID を環境変数 PYTEST_CURRENT_TEST に
   入れる。Windows の環境変数は「名前=値」で 32767 文字まで。CPython の Modules/posixmodule.c の
   win32_putenv）。200 は、その上限ではなく、結果の一覧で読める長さとして決めた（実装者の試験と同じ値）。
2. 偽のブラウザの見張り（a0_fake_browser.py）: 偽が 0 以外で終わっても、起動できなくても、本物のブラウザを
   開く経路は呼ばれない。守りの 1 を外した対照では、本物の経路（Linux は身代わりの xdg-open、Windows は
   os.startfile）が呼ばれかけ、守りの 2 が止める。
3. プロセスの見張り（acceptance_watch.py と conftest.py）: 試験が残したプロセスは、止められ、その試験が
   失敗になる。準備の途中で失敗したフィクスチャが起動したものは、その場で止まる。実行の終わりに、この実行
   から生まれたアプリ本体が残っていれば、実行の全体が失敗になる。始める前から動いているものは、知らせる
   だけで止めない。

見張りの配線（conftest.py）は、見張りの3つのファイルを一時フォルダに写し、わざと残す試験と一緒に、別の
pytest で流して確かめる（このリポジトリの試験を入れ子で流すのではない）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import a0_fake_browser as fake
import a0_marked
import acceptance_watch as watch
import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
ID_LIMIT = 200
# アプリ本体らしいコマンドライン（circuit_sim.app の字がある）で、何もせずに待つだけのプロセス。
# 止め損ねても残り続けないよう、自分で終わる
SLEEPER = "import time  # circuit_sim.app\ntime.sleep(120)\n"
PLAIN_SLEEPER = "import time\ntime.sleep(120)\n"


def plain_env(**extra: str) -> dict[str, str]:
    """別の pytest に渡す環境変数。呼び出し元の pytest の指定は渡さない。

    scripts/local_ci.py は PYTEST_ADDOPTS に --junitxml を入れる。渡すと、別の pytest が結果のファイルを
    書き換える。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env.update({"PYTHONIOENCODING": "utf-8", **extra})
    return env


def alive(pid: int) -> bool:
    """プロセスが動いているか（終わって、親の受け取りを待つだけのものは、動いていないと数える）。"""
    if WINDOWS:
        import ctypes  # noqa: PLC0415  Windows でだけ読む
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8", errors="replace")
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


# ------------------------------------------------------------------ 1. 試験の ID の長さ


def collected_ids(target: Path, cwd: Path) -> list[str]:
    """target の試験を集めるだけの pytest を流し、ID の一覧を返す（実行はしない）。"""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(target)],
        cwd=cwd, env=plain_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return [line.strip() for line in done.stdout.splitlines() if "::" in line]


def test_acceptance_test_ids_stay_short():
    """受け入れ試験の ID は、どれも ID_LIMIT 文字まで（一部だけ流したときも、全部を集めて確かめる）。

    parametrize の値が長い・日本語のときは、短い ids を付ける（日本語は、ID の中で1文字が 6 文字になる）。"""
    ids = collected_ids(HERE, ROOT)
    assert len(ids) > 500, f"受け入れ試験を集められていない（{len(ids)} 件）"
    too_long = [f"{len(name)} 文字: {name[:100]}…" for name in ids if len(name) > ID_LIMIT]
    assert not too_long, too_long


def test_the_id_check_sees_long_and_japanese_ids(tmp_path):
    """上の確かめが、長い ID を見つけられること（対照）。日本語 40 文字の値は、240 文字の ID になる。"""
    (tmp_path / "test_long.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('value', ['あ' * 40, b'a' * 300, 'short'])\n"
        "def test_it(value):\n    pass\n",
        encoding="utf-8",
    )
    lengths = sorted(len(name) for name in collected_ids(tmp_path, tmp_path))
    assert len(lengths) == 3 and lengths[0] < 40
    assert lengths[1] > ID_LIMIT and lengths[2] > 300, lengths


# ------------------------------------------------------------------ 2. 偽のブラウザの見張り


def run_guard(tmp_path: Path, **env: str) -> tuple[str, list[tuple[str, str]]]:
    """見張りの入口を、アプリ本体を始めない形（PROBE_VAR か STARTFILE_VAR）で流す。（出力、記録の行）。"""
    notes = tmp_path / "probe.notes"
    notes.unlink(missing_ok=True)
    done = subprocess.run(
        fake.app_argv(), cwd=ROOT, env=plain_env(**{fake.NOTES_VAR: str(notes), **env}), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip(), fake.read_notes(notes)


def fake_script(tmp_path: Path, code: int) -> tuple[Path, Path]:
    """呼ばれたことを控えて、終了コード code で終わる偽のブラウザ。（スクリプト、控えのファイル）。"""
    script, called = tmp_path / "fake_browser.py", tmp_path / "called.txt"
    script.write_text(
        "import sys\n"
        f"with open({str(called)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    return script, called


NEVER = "file:///a0-acceptance/never-opened.html"


@pytest.mark.parametrize("code", [0, 1, 3], ids=["fake-ok", "fake-exits-1", "fake-exits-3"])
def test_failing_fake_browser_does_not_lead_to_the_real_one(tmp_path, code):
    """偽のブラウザが 0 以外で終わっても、webbrowser.open はそこで終わる（本物の経路は呼ばれない）。"""
    script, called = fake_script(tmp_path, code)
    out, notes = run_guard(tmp_path, BROWSER=fake.command(script), **{fake.PROBE_VAR: NEVER})
    assert out == "opened: True", out          # 次の候補（OS の既定のブラウザ）へ進まない
    assert called.read_text(encoding="utf-8").splitlines() == [NEVER], "偽のブラウザが、ちょうど1回呼ばれる"
    assert [kind for kind, _ in notes if kind == "real-route"] == [], notes
    assert notes == ([] if code == 0 else [("fake-failed", NEVER)]), notes


def test_fake_browser_that_cannot_start_does_not_lead_to_the_real_one(tmp_path):
    """偽のブラウザを起動できないとき（コマンドが無い）も、本物の経路は呼ばれない。"""
    missing = (tmp_path / "no-such-browser").as_posix()
    out, notes = run_guard(tmp_path, BROWSER=f"{missing} %s", **{fake.PROBE_VAR: NEVER})
    assert out == "opened: True", out
    assert [kind for kind, _ in notes] == ["fake-failed"], notes


@pytest.mark.skipif(not LINUX, reason="本物の経路の身代わり（PATH に置いた xdg-open）を使えるのは Linux "
                                      "だけ。Windows の経路は、下の os.startfile の試験で確かめる")
def test_without_the_first_guard_the_real_route_is_reached_and_the_second_guard_blocks_it(tmp_path):
    """対照: 守りの 1（偽だけで終わる候補）を外すと、偽が失敗したとき、webbrowser は OS の既定の候補へ進む。

    守りの 2（監査）が、その起動を止めて記録する。本物の代わりに置いた xdg-open は、実行されない。
    この試験が、「偽が失敗すると本物へ進む」という前提と、進んだことを見つけられることの両方を確かめる。"""
    script, called = fake_script(tmp_path, 1)
    bin_dir, reached = tmp_path / "bin", tmp_path / "xdg-open-was-run.txt"
    bin_dir.mkdir()
    stub = bin_dir / "xdg-open"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> '{reached}'\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {"BROWSER": fake.command(script), fake.PROBE_VAR: NEVER, fake.STOP_VAR: "off", "DISPLAY": ":0",
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    out, notes = run_guard(tmp_path, **env)
    assert out.startswith("blocked: subprocess.Popen") and "xdg-open" in out, out
    routes = [text for kind, text in notes if kind == "real-route"]
    assert len(routes) == 1 and "xdg-open" in routes[0] and NEVER in routes[0], notes
    assert not reached.exists(), "本物の代わりの xdg-open が実行された（守りの 2 が止めていない）"
    assert called.read_text(encoding="utf-8").count(NEVER) >= 1
    # 同じ条件で、守りの 1 があれば、本物の経路は呼ばれない
    del env[fake.STOP_VAR]
    out, notes = run_guard(tmp_path, **env)
    assert out == "opened: True" and notes == [("fake-failed", NEVER)], (out, notes)
    assert not reached.exists()


@pytest.mark.skipif(not WINDOWS, reason="os.startfile は Windows だけ（POのPCで流れる）")
def test_startfile_is_blocked_by_the_second_guard_on_windows(tmp_path):
    """Windows の本物の経路（os.startfile）は、守りの 2 が、開く前に止めて記録する。

    渡すのは、無い場所（止まらなかったとしても、何も開かずに FileNotFoundError になる）。"""
    script, _ = fake_script(tmp_path, 0)
    missing = str(tmp_path / "never-opened.html")
    out, notes = run_guard(tmp_path, BROWSER=fake.command(script), **{fake.STARTFILE_VAR: missing})
    assert out.startswith("blocked: os.startfile"), out
    assert [kind for kind, _ in notes] == ["real-route"], notes


def test_sentinel_browser_records_a_launch_that_skipped_the_guard(tmp_path):
    """conftest.py が BROWSER に置く見張り役は、呼ばれたら記録を残して 0 で終わる。

    webbrowser は、0 で終わった候補で終わりにする（本物へ進まない）。

    ここでは、見張り役のコマンドをそのまま呼ぶ（a0_fake_browser を通さずにブラウザを開く起動をした試験と
    同じことが起きる）。残った記録は、この試験の後始末で失敗にされるので、確かめた後に消す。"""
    import shlex  # noqa: PLC0415

    command = [part.replace("%s", NEVER) for part in shlex.split(os.environ["BROWSER"])]
    assert "sentinel_browser.py" in command[1], "受け入れ試験の間、BROWSER に見張り役が置かれていない"
    done = subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    assert fake.read_notes() == [("unguarded", NEVER)]
    fake.clear_notes()


# ------------------------------------------------------------------ 3. プロセスの見張り（部品）


def test_tracker_stops_what_is_still_running_and_says_what_it_was():
    tracker = watch.Tracker()
    tracker.install()
    try:
        mark = tracker.count()
        tracker.where = "somewhere::test_x"
        left = subprocess.Popen([sys.executable, "-c", PLAIN_SLEEPER])
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait(timeout=60)
        assert [item.proc for item in tracker.since(mark)] == [left, ended]
        reported = tracker.reap(tracker.since(mark), grace=0.2)
    finally:
        tracker.uninstall()
        if left.poll() is None:
            left.kill()
    assert subprocess.Popen is not tracker.popen_class, "外した後も、差し替えが残っている"
    assert left.poll() is not None, "動いていたプロセスを止めていない"
    assert len(reported) == 1 and f"PID {left.pid}" in reported[0] and "somewhere::test_x" in reported[0]
    assert tracker.since(mark) == [], "確かめたものを、控えに持ち続けている（パイプが閉じられない）"
    assert {left.pid, ended.pid} <= tracker.pids


def test_tracker_does_not_count_a_process_that_is_just_finishing():
    """止める合図を受けて終わりかけているプロセスは、猶予のうちに終われば、残りと数えない。"""
    tracker = watch.Tracker()
    tracker.install()
    try:
        proc = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(0.5)\n"])
        assert tracker.reap(tracker.since(0), grace=20.0) == []
        assert proc.poll() == 0
    finally:
        tracker.uninstall()
        if proc.poll() is None:
            proc.kill()


def test_scope_owned_processes_are_not_taken_for_the_tests_own():
    """モジュールの間だけ動かすフィクスチャの準備で起動したものは、試験の持ち物（owner=test）に入らない。"""
    tracker = watch.Tracker()
    tracker.install()
    try:
        tracker.scoped += 1
        kept = subprocess.Popen([sys.executable, "-c", PLAIN_SLEEPER])
        tracker.scoped -= 1
        assert tracker.since(0, owner="test") == [] and len(tracker.since(0)) == 1
        assert tracker.reap(tracker.since(0, owner="test"), grace=0.0) == [] and kept.poll() is None
    finally:
        tracker.uninstall()
        kept.kill()
        kept.wait(timeout=20)


P = watch.Process


@pytest.mark.parametrize(("processes", "sure", "maybe"), [
    # 子と孫: 動いている親をたどって、試験のプロセス（100）に届く
    ([P(100, 1, "pytest", 10.0), P(200, 100, "app", 11.0), P(300, 200, "browser", 12.0)], [200, 300], []),
    # 親（250）がもう無く、その番号は控えにある: たぶん、この実行のもの。その子（310）も
    ([P(100, 1, "pytest", 10.0), P(300, 250, "app", 12.0), P(310, 300, "x", 13.0)], [], [300, 310]),
    # 親（999）がもう無く、控えにも無い: この実行のものとは言えない
    ([P(100, 1, "pytest", 10.0), P(300, 999, "app", 12.0)], [], []),
    # 親より先に作られている: 親の番号は使い回し（別のプロセス）
    ([P(100, 1, "pytest", 10.0), P(200, 100, "old", 5.0)], [], []),
    # 見張りを始める（since=9.0）より前に作られたものは、数えない
    ([P(100, 1, "pytest", 8.0), P(200, 100, "early", 8.5), P(210, 100, "later", 9.5)], [210], []),
    # 作られた時刻が分からない一覧（ps）: 親子の関係だけでたどる
    ([P(100, 1, "pytest"), P(200, 100, "app"), P(300, 1, "orphan")], [200], []),
    # 親子が輪になっていても、止まらない
    ([P(100, 1, "pytest", 10.0), P(400, 401, "a", 11.0), P(401, 400, "b", 11.0)], [], []),
], ids=["child-and-grandchild", "orphan-of-a-tracked-process", "orphan-of-an-unknown-process",
        "older-than-its-parent", "before-the-watch-began", "no-creation-times", "cycle"])
def test_descendants_are_told_apart_from_other_processes(processes, sure, maybe):
    found_sure, found_maybe = watch.descendants(processes, 100, known_dead={250}, since=9.0)
    assert [p.pid for p in found_sure] == sure and [p.pid for p in found_maybe] == maybe


@pytest.mark.parametrize(("command", "expected"), [
    # PO の PC に残っていたもの
    (r"C:\Users\u\AppData\Local\Programs\Python\Python313\python.exe -m circuit_sim.app --no-browser"
     " --port 50000", True),
    ("/usr/bin/python3 -m circuit_sim.app --no-browser", True),
    ("python -c import os, runpy ... runpy.run_module(\"circuit_sim.app\", run_name=\"__main__\")", True),
    ("python /repo/circuit_sim/app/cli.py --no-browser", True),
    (r"C:\dev\circuit-sim\.venv\Scripts\circuit-sim.exe --no-browser", True),
    ("/home/u/.venv/bin/circuit-sim", True),
    ("/usr/bin/python3 /home/u/.venv/bin/circuit-sim --port 8765", True),
    (r'"C:\Program Files\cs\Scripts\circuit-sim.exe" --no-browser', True),
    # アプリ本体ではないもの（リポジトリの場所に circuit-sim の字があっても）
    (r"C:\dev\circuit-sim\.venv\Scripts\python.exe -c from multiprocessing.resource_tracker import main;"
     "main(5)", False),
    ("python -m circuit_sim examples/rlc_series.json", False),
    ("python /tmp/circuit-sim-ci-abc/checkout/scripts/check.py", False),
    ("python -m pytest -q", False),
    ("timeout 3000 python benchmarks/bench.py --baseline /home/claude/circuit-sim --sections all", False),
    ("/bin/bash -c cd /home/claude/circuit-sim && ls", False),
], ids=["left-on-the-po-pc", "module-run", "guard-entry", "file-run", "exe-in-the-repo-venv",
        "console-script", "console-script-by-python", "exe-in-a-folder-with-a-space",
        "helper-in-the-repo-venv", "calculation-command", "check-in-a-ci-folder", "pytest",
        "repo-folder-as-an-argument", "repo-folder-in-a-shell-line"])
def test_app_like_command_lines_are_recognized(command, expected):
    assert bool(watch.APP_LIKE.search(command)) is expected


def test_windows_and_ps_listings_are_parsed():
    sep = "\x1f"
    text = (
        f"4{sep}0{sep}0{sep}\r\n"
        f"32092{sep}8120{sep}1794827217000{sep}C:\\Python313\\python.exe -m circuit_sim.app --port 50000\r\n"
        "broken line\r\n"
    )
    assert watch.parse_windows_list(text) == [
        P(4, 0, "", None),
        P(32092, 8120, r"C:\Python313\python.exe -m circuit_sim.app --port 50000", 1794827217.0),
    ]
    listing = "    1     0 /sbin/init\n  812   640 python -m circuit_sim.app --no-browser\n  900   812\n"
    assert watch.parse_ps_list(listing) == [
        P(1, 0, "/sbin/init"), P(812, 640, "python -m circuit_sim.app --no-browser"), P(900, 812, ""),
    ]


def test_shown_command_hides_the_home_folder_and_is_cut(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    text = watch.shown(f"{tmp_path}/bin/python  -m   circuit_sim.app " + "x" * 400)
    assert text.startswith("~/bin/python -m circuit_sim.app x") and len(text) == 201 and text.endswith("…")


def start_marked(code: str, token: str, tmp_path: Path) -> subprocess.Popen:
    """印（MARK_VAR=token）を持つプロセスを、この実行の印とは別の印で起動する。"""
    return subprocess.Popen([sys.executable, "-c", code], env={**os.environ, watch.MARK_VAR: token},
                            cwd=tmp_path, stdin=subprocess.DEVNULL)


@pytest.mark.skipif(not LINUX, reason="環境変数の印でプロセスを見分けるのは Linux だけ（/proc）")
def test_marked_app_is_found_even_after_its_parent_is_gone_and_others_are_left_alone(tmp_path):
    """Linux: 印を持つアプリ本体らしいプロセスは、親が先に終わっていても見つかり、止められる。

    印の違うもの（ほかの実行のもの）と、アプリ本体らしくないものには、触らない。"""
    token = f"test-{os.getpid()}-{time.monotonic_ns()}"
    pid_file = tmp_path / "grandchild.pid"
    parent_code = (
        "import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}], stdin=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
    )
    parent = start_marked(parent_code, token, tmp_path)
    other_run = start_marked(SLEEPER, token + "-other", tmp_path)
    not_an_app = start_marked(PLAIN_SLEEPER, token, tmp_path)
    grandchild = None
    try:
        assert parent.wait(timeout=60) == 0
        grandchild = int(pid_file.read_text())
        marked = {p.pid for p in watch.marked_processes(token)}
        assert {grandchild, not_an_app.pid} <= marked and other_run.pid not in marked
        left = watch.leftover_apps(token, known_dead=set(), since=0.0, grace=0.3)
        assert left.checked and [p.pid for p in left.killed] == [grandchild] and left.reported == []
        assert wait_gone(grandchild), "親のいないアプリ本体を止めていない"
        assert other_run.poll() is None and not_an_app.poll() is None, "触ってはいけないプロセスを止めた"
        again = watch.leftover_apps(token, known_dead=set(), since=0.0, grace=0.3)
        assert again.checked and not again.any()
    finally:
        for proc in (other_run, not_an_app):
            proc.kill()
            proc.wait(timeout=20)
        if grandchild is not None:     # 印で確かめてから止める（D-078。8回目の検証で直した）
            a0_marked.kill_if_marked(grandchild, environ=f"{watch.MARK_VAR}={token}")


@pytest.mark.skipif(WINDOWS or shutil.which("ps") is None,
                    reason="ps の一覧で親子をたどる形（Linux の印を使わない形）を、POSIX で流す")
def test_child_app_is_found_by_following_parents_when_marks_cannot_be_used(tmp_path):
    """印を使えない OS の形（プロセスの一覧の親子をたどる）で、子のアプリ本体が見つかり、止められる。"""
    child = start_marked(SLEEPER, "unused", tmp_path)
    plain = start_marked(PLAIN_SLEEPER, "unused", tmp_path)
    try:
        left = watch.leftover_apps("unused", known_dead=set(), since=0.0, grace=0.3, linux=False)
        assert left.checked and [p.pid for p in left.killed] == [child.pid], left
        assert child.wait(timeout=20) != 0 and plain.poll() is None
    finally:
        for proc in (child, plain):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=20)


def test_running_apps_lists_an_app_like_process_without_touching_it(tmp_path):
    """始める前の知らせに使う一覧: 動いているアプリ本体らしいプロセスが載る。止めはしない。"""
    listed = watch.list_processes()
    if listed is None:
        pytest.skip("この PC では、プロセスの一覧を作れない（実行の前後の確かめは、効いていない）")
    proc = start_marked(SLEEPER, "unused", tmp_path)
    try:
        deadline = time.monotonic() + 30
        while proc.pid not in {p.pid for p in watch.running_apps() or []}:
            assert time.monotonic() < deadline, "起動したアプリ本体らしいプロセスが、一覧に載らない"
            time.sleep(0.2)
        assert proc.poll() is None
        assert os.getpid() not in {p.pid for p in watch.running_apps()}
    finally:
        proc.kill()
        proc.wait(timeout=20)


# ------------------------------------------------------------------ 3. プロセスの見張り（配線）

INNER_TESTS = r'''
import os, shlex, subprocess, sys
from pathlib import Path

import pytest

OUT = Path(os.environ["A0_INNER_OUT"])
PLAIN = [sys.executable, "-c", "import time\ntime.sleep(120)\n"]
STARTED = {}


def start(name):
    STARTED[name] = subprocess.Popen(PLAIN, stdin=subprocess.DEVNULL)
    (OUT / (name + ".pid")).write_text(str(STARTED[name].pid))
    return STARTED[name]


def test_01_stops_what_it_starts():
    proc = start("stopped-by-the-test")
    proc.kill()
    proc.wait()


def test_02_leaves_a_process_behind():
    start("left-by-a-passing-test")


def test_03_the_left_process_was_stopped_right_after_that_test():
    assert STARTED["left-by-a-passing-test"].poll() is not None


def test_04_leaves_a_process_when_it_fails():
    start("left-by-a-failing-test")
    raise AssertionError("途中で落ちる")


@pytest.fixture(scope="module")
def broken_fixture():
    start("left-by-a-broken-fixture")
    raise RuntimeError("準備の途中で失敗する")


def test_05_uses_a_fixture_that_fails_in_setup(broken_fixture):
    pass


def test_06_the_process_of_the_broken_fixture_was_stopped_at_once():
    assert STARTED["left-by-a-failing-test"].poll() is not None
    assert STARTED["left-by-a-broken-fixture"].poll() is not None


@pytest.fixture(scope="module")
def kept_for_the_module():
    yield start("kept-for-the-module")     # 後始末で止め忘れている


def test_07_module_fixture_process_is_running(kept_for_the_module):
    assert kept_for_the_module.poll() is None


def test_08_module_fixture_process_is_still_running_in_the_next_test(kept_for_the_module):
    assert kept_for_the_module.poll() is None


def test_09_opens_a_browser_without_the_guard():
    command = [part.replace("%s", "file:///x.html") for part in shlex.split(os.environ["BROWSER"])]
    assert subprocess.run(command, stdin=subprocess.DEVNULL).returncode == 0


def test_10_leaves_an_app_whose_parent_is_gone():
    if not os.environ.get("A0_INNER_ORPHAN"):
        pytest.skip("この回は、親のいないアプリ本体を残さない")
    app = "import os, time  # circuit_sim.app\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
    app = app % str(OUT / "orphan-app.pid")
    parent = "import subprocess as s, sys\ns.Popen([sys.executable, '-c', %r], stdin=s.DEVNULL)\n"
    done = subprocess.run([sys.executable, "-c", parent % app], stdin=subprocess.DEVNULL)
    assert done.returncode == 0
'''

GOOD_TESTS = '''
import subprocess, sys


def test_starts_and_stops():
    assert subprocess.run([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL).returncode == 0
'''


def run_watched_copy(tmp_path: Path, tests: str, **env: str) -> tuple[subprocess.CompletedProcess, Path]:
    """見張りの3つのファイルと tests を一時フォルダに写し、別の pytest で流す。（結果、控えの場所）。"""
    place = tmp_path / "copy" / "tests" / "acceptance"
    place.mkdir(parents=True)
    for name in ("conftest.py", "acceptance_watch.py", "a0_fake_browser.py"):
        shutil.copy(HERE / name, place / name)
    (place / "test_inner.py").write_text(tests, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", str(place)],
        cwd=tmp_path / "copy", env=plain_env(A0_INNER_OUT=str(out), **env), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, stdin=subprocess.DEVNULL,
    )
    return done, out


def counts(output: str) -> dict[str, int]:
    """pytest の最後の行（1 failed, 5 passed, 4 errors in 1.2s）を、種類ごとの件数にする。"""
    last = [line for line in output.splitlines() if re.search(r"\bin [0-9.]+s\b", line)][-1]
    return {kind.rstrip("s"): int(number)
            for number, kind in re.findall(r"(\d+) (passed|failed|errors?|skipped)", last)}


def test_watch_fails_the_tests_that_leave_processes_and_stops_them(tmp_path):
    """配線: 残したプロセスは止められ、残した試験は失敗になる。部品を通さないブラウザの起動も、失敗になる。"""
    done, out = run_watched_copy(tmp_path, INNER_TESTS)
    text = done.stdout + done.stderr
    pids = {path.stem: int(path.read_text()) for path in out.glob("*.pid")}
    try:
        assert set(pids) == {"stopped-by-the-test", "left-by-a-passing-test", "left-by-a-failing-test",
                             "left-by-a-broken-fixture", "kept-for-the-module"}, text
        running = [name for name, pid in pids.items() if alive(pid)]
        assert not running, f"止められていないプロセスがある: {running}\n{text}"
        # エラーは 5 つ: 02 と 04 と 09 の後始末、05 の準備、最後の試験（10）の後始末（モジュールの持ち物の
        # 止め忘れ。モジュールの終わりの確かめは、そのモジュールの最後の試験の後始末に付く）
        assert counts(done.stdout) == {"failed": 1, "passed": 7, "error": 5, "skipped": 1}, text
        assert done.returncode == 1, text
        left = "この試験が起動したプロセスが、試験の終わりにも動いていた"
        for nodeid, phrase in [
            ("test_02_leaves_a_process_behind", left),
            ("test_04_leaves_a_process_when_it_fails", left),
            ("test_09_opens_a_browser_without_the_guard", "a0_fake_browser を通さずに、ブラウザを開く起動"),
        ]:
            assert re.search(rf"ERROR at teardown of {nodeid}\b", text) and phrase in text, (nodeid, text)
        assert "このモジュールの試験が起動したプロセスが、モジュールの終わりにも動いていた" in text, text
        for name in ("kept-for-the-module", "left-by-a-passing-test", "left-by-a-failing-test"):
            assert f"PID {pids[name]}" in text, (name, text)
        assert "起動した所: フィクスチャ kept_for_the_module" in text, text
        assert "起動した所: tests/acceptance/test_inner.py::test_02_leaves_a_process_behind" in text, text
        assert "この実行から生まれたアプリ本体は残っていない" in text, text
    finally:
        # 印（内側の実行の環境変数）で確かめてから止める（D-078。8回目の検証で直した）。印を読めない OS では
        # 止めない（どれも 120 秒で自分で終わる眠るだけのプロセスで、内側の見張りが止めているはず）
        for pid in pids.values():
            a0_marked.kill_if_marked(pid, environ=f"A0_INNER_OUT={out}")


def test_watch_fails_the_run_when_an_app_is_left_at_the_end(tmp_path):
    """配線: 実行の終わりに、この実行から生まれたアプリ本体が残っていたら、実行の全体が失敗になる。

    残すのは、親が先に終わったアプリ本体らしいプロセス（試験ごとの見張りには掛からない）。
    Linux は印で見つけて止める。Windows は「たぶん」として知らせる（止めない）。親をたどれない OS
    （macOS など）では見つからないので、ここでは確かめない。"""
    if not (LINUX or WINDOWS):
        pytest.skip("親が先に終わったプロセスを見つけられるのは、Linux（印）と Windows（親の番号）だけ")
    if watch.list_processes() is None:
        pytest.skip("この PC では、プロセスの一覧を作れない（実行の前後の確かめは、効いていない）")
    parts = INNER_TESTS.split("\n\n\n")     # 始めの部分（import と部品）と、親のいないアプリ本体を残す試験
    tests = "\n\n\n".join(part for part in parts if "test_0" not in part and "fixture" not in part)
    done, out = run_watched_copy(tmp_path, tests, A0_INNER_ORPHAN="1")
    text = done.stdout + done.stderr
    orphan = int((out / "orphan-app.pid").read_text())
    try:
        assert counts(done.stdout) == {"passed": 1}, text
        assert done.returncode == watch.LEFTOVER_EXIT_CODE, (done.returncode, text)
        assert "失敗: 試験が起動したプロセスが、実行の終わりにも動いていた" in text, text
        assert f"PID {orphan}" in text, text
        if LINUX:
            assert "止めた（この実行から生まれたアプリ本体）" in text and wait_gone(orphan), text
        else:
            assert "止めていない（たぶんこの実行のアプリ本体" in text and alive(orphan), text
    finally:
        # 印で確かめてから止める（D-078。8回目の検証で直した）。Linux は内側の実行の環境変数と引数、
        # ほかの OS は引数（コードに書き込んだ、控えのファイルの名前）
        a0_marked.kill_if_marked(orphan, environ=f"A0_INNER_OUT={out}" if LINUX else None,
                              command="orphan-app.pid")


def test_watch_reports_apps_that_were_running_before_and_leaves_them_alone(tmp_path):
    """配線: 始める前から動いているアプリ本体らしいプロセスは、知らせるだけ（止めない。失敗にもしない）。

    行儀のよい試験だけの実行は、終了コード 0 で、残りが無かったことを1行で知らせる。"""
    if watch.list_processes() is None:
        pytest.skip("この PC では、プロセスの一覧を作れない（実行の前後の確かめは、効いていない）")
    earlier = start_marked(SLEEPER, "an-earlier-run", tmp_path)
    try:
        deadline = time.monotonic() + 30
        while earlier.pid not in {p.pid for p in watch.running_apps() or []}:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        done, _ = run_watched_copy(tmp_path, GOOD_TESTS)
        text = done.stdout + done.stderr
        assert done.returncode == 0 and counts(done.stdout) == {"passed": 1}, text
        assert "試験を始める前から、アプリ本体らしいプロセスが" in text, text
        assert f"PID {earlier.pid}:" in text, text
        assert "この実行から生まれたアプリ本体は残っていない" in text, text
        assert earlier.poll() is None, "前から動いていたプロセスを止めた"
    finally:
        earlier.kill()
        earlier.wait(timeout=20)
