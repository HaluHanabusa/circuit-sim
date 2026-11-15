"""起動コマンド circuit-sim（circuit_sim/app/cli.py）の試験。別のプロセスで起動して確かめる。"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
START_LIMIT_S = 5.0


def launch(*args: str, env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", *args],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )


def read_url(proc: subprocess.Popen, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "http://127.0.0.1:" in line:
            return line[line.index("http://"):].strip()
        if proc.poll() is not None:
            break
    raise AssertionError("起動した場所の表示が無い")


def get_json(url: str, body: dict | None = None, method: str = "GET") -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def wait_ready(url: str, timeout: float) -> float:
    started = time.monotonic()
    while True:
        try:
            get_json(url + "api/health")
            return time.monotonic() - started
        except OSError:
            if time.monotonic() - started > timeout:
                raise
            time.sleep(0.05)


def stop(proc: subprocess.Popen) -> str:
    if proc.poll() is None:
        proc.kill()
    out, _ = proc.communicate(timeout=10)
    return out


def test_starts_on_free_port_and_serves_page_quickly():
    started = time.monotonic()
    proc = launch("--no-browser")
    try:
        url = read_url(proc)
        wait_ready(url, START_LIMIT_S)
        assert time.monotonic() - started < START_LIMIT_S
        assert url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert b'<div id="root">' in resp.read()
    finally:
        stop(proc)


def test_busy_port_is_reported_and_exits_2():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        proc = launch("--no-browser", "--port", str(port))
        proc.wait(timeout=20)
        out = proc.communicate()[0]
    assert proc.returncode == 2
    assert f"番号 {port} は、ほかのプログラムが使っています" in out


def test_opens_default_browser_with_the_url(tmp_path):
    """既定のブラウザ（BROWSER の指定で、偽のブラウザに差し替える）に、作業画面の場所を渡す。"""
    record = tmp_path / "opened.txt"
    fake = tmp_path / "fake_browser.py"
    fake.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(sys.argv[1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    if WINDOWS:
        pytest.skip("Windows の BROWSER の指定は、空白を含む場所を扱えないため、ここでは確かめない")
    proc = launch(env={"BROWSER": f"{sys.executable} {fake} %s"})
    try:
        url = read_url(proc)
        deadline = time.monotonic() + START_LIMIT_S
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert record.read_text(encoding="utf-8") == url
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
def test_ctrl_c_stops_right_away_without_unsaved_changes():
    proc = launch("--no-browser")
    try:
        url = read_url(proc)
        wait_ready(url, START_LIMIT_S)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
        assert "circuit-sim を止めました" in proc.stdout.read()
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
def test_ctrl_c_with_unsaved_changes_warns_first_then_stops():
    """未決11: 保存していない変更があれば、1回目の Ctrl+C では止めずに知らせ、2回目で止める。"""
    proc = launch("--no-browser")
    try:
        url = read_url(proc)
        wait_ready(url, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        get_json(url + "api/session", {"doc": doc, "dirty": True}, "PUT")
        proc.send_signal(signal.SIGINT)
        line = ""
        deadline = time.monotonic() + 5
        while "保存していない変更があります" not in line and time.monotonic() < deadline:
            line = proc.stdout.readline()
        assert "mine.json" in line
        time.sleep(0.3)
        assert proc.poll() is None, "1回目の Ctrl+C で止まった"
        assert get_json(url + "api/health")["instance"]
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
    finally:
        stop(proc)
