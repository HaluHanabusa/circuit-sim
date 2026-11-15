"""A0 の独立検証（verifier、3回目）: 最後の修正（70a1e1d）のうち、実際のブラウザで確かめるもの。

記録は docs/verification/A0-verifier-3.md。
- 「使い終わった URL」の案内: 別のポートのページが、使い終わった番号の URL を画像・スクリプト・スタイル・
  fetch（no-cors と cors）・prefetch・音声と動画として読んでも、端末に案内は出ない。そのページから
  ブラウザでページとして開くと（location）、出る（対照）
- 10.4: 読み上げ用の領域（aria-live）の文は、表示中の言語の文になる。日本語のファイル名でも、日本語 → English
  → 日本語と切り替えるたびに、お知らせの行に見える文と同じ。次の開けなかったお知らせが来たら、それに置き換わる

Python の playwright と Chromium 系のブラウザがある環境でだけ動く。無ければ、確かめなかったことを
スキップの理由として残す（POの Windows の PC など）。ブラウザは CIRCUIT_SIM_CHROME で指定できる。
"""

from __future__ import annotations

import http.server
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
STALE_TEXTS = ("使い終わったか期限が切れた URL", "already used or has expired")
BROWSERS = [
    os.environ.get("CIRCUIT_SIM_CHROME", ""),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
WAIT = 30.0

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright が無いので、実際のブラウザでの確認（A0 の3回目の検証の追加分）をしなかった",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """アプリ本体（python -m circuit_sim.app）を別のプロセスで起動し、端末の出力を読む。"""

    def __init__(self, config: Path) -> None:
        self.port = free_port()
        self.lines: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(config), "LANG": "ja_JP.UTF-8",
               "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.url = self.next_url()
        deadline = time.monotonic() + WAIT
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                assert self.proc.poll() is None and time.monotonic() < deadline, "".join(self.lines)
                time.sleep(0.05)

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace")
            self.lines.append(line)
            self.q.put(line)
        self.q.put(None)

    def next_url(self) -> str:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            assert line is not None, "".join(self.lines)
            m = URL_RE.search(line)
            if m and line.lstrip().startswith(("circuit-sim", "新しい", "New")):
                return m.group(0)
        raise AssertionError("起動の URL が出ない: " + "".join(self.lines))

    def login_url(self) -> str:
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()
        return self.next_url()

    def stale_reports(self) -> int:
        return sum(any(t in ln for t in STALE_TEXTS) for ln in self.lines)

    def stop(self) -> None:
        if self.proc.poll() is None:
            if os.name == "posix":
                self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=20)
                    return
                except subprocess.TimeoutExpired:
                    pass
            self.proc.kill()
            self.proc.wait(timeout=20)


class OtherPage:
    """別のポートで、決めた HTML を出すだけのページ。"""

    def __init__(self, html: str) -> None:
        data = html.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        launched = None
        for path in [b for b in BROWSERS if b and Path(b).is_file()]:
            launched = p.chromium.launch(executable_path=path, args=["--disable-background-networking"])
            break
        if launched is None:
            try:
                launched = p.chromium.launch()
            except Exception:
                pytest.skip(
                    "Chromium 系のブラウザが無いので、実際のブラウザでの確認（A0 の3回目の検証の追加分）を"
                    "しなかった"
                )
        yield launched
        launched.close()


# ------------------------------------------------------------------ 「使い終わった URL」の案内

SUBRESOURCES = (
    '<img src="TARGET">'
    '<link rel="stylesheet" href="TARGET">'
    '<script src="TARGET"></script>'
    '<link rel="prefetch" href="TARGET">'
    '<audio src="TARGET" preload="auto"></audio><video src="TARGET" preload="auto"></video>'
    '<script>fetch("TARGET", {mode: "no-cors"}).catch(() => 0);'
    'fetch("TARGET", {mode: "cors"}).catch(() => 0);</script>'
)


def test_stale_url_report_not_from_subresources_of_another_page(browser, tmp_path):
    app = App(tmp_path / "config")
    used = app.url
    ctx = browser.new_context()
    other = None
    try:
        page = ctx.new_page()
        page.goto(used)  # 起動の番号を使う
        page.wait_for_url(re.compile(r"/$"))
        time.sleep(0.5)
        assert app.stale_reports() == 0, "".join(app.lines)
        seen: list[str] = []
        fresh = browser.new_context()  # クッキーの無いブラウザ
        try:
            probe = fresh.new_page()
            probe.on("request", lambda r: seen.append(r.resource_type) if r.url == used else None)
            other = OtherPage(SUBRESOURCES.replace("TARGET", used))
            probe.goto(other.url)
            probe.wait_for_timeout(2000)
            assert {"image", "stylesheet", "script", "fetch", "media"} <= set(seen), seen
            assert app.stale_reports() == 0, (seen, "".join(app.lines))
            # 対照: 同じページから、ブラウザでページとして開くと出る
            probe.evaluate("u => { location.href = u; }", used)
            deadline = time.monotonic() + 10
            while app.stale_reports() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert app.stale_reports() == 1, "".join(app.lines)
        finally:
            fresh.close()
    finally:
        ctx.close()
        if other is not None:
            other.close()
        app.stop()


# ------------------------------------------------------------------ 10.4: 読み上げ用の領域の言語

STATE_JS = """() => ({
    lang: document.documentElement.lang,
    announce: document.querySelector('[data-testid=announce]').textContent.trim(),
    rows: [...document.querySelectorAll('[data-testid=notice-row]')]
        .map(r => r.innerText.replace(/\\s+/g, ' ')),
})"""
KANA_KANJI = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def test_announcement_matches_the_visible_notice_in_each_language(browser, tmp_path):
    app = App(tmp_path / "config")
    ctx = browser.new_context(locale="ja-JP")
    name = "回路_試作.json"
    try:
        page = ctx.new_page()
        page.goto(app.login_url())
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        assert page.evaluate(STATE_JS)["announce"] == ""
        page.get_by_test_id("file-input").set_input_files(
            files=[{"name": name, "mimeType": "application/json", "buffer": b'{"a": 1,'}]
        )
        page.get_by_test_id("notice-row").first.wait_for()
        page.wait_for_timeout(300)
        seen = {}
        for lang, label in [("ja", None), ("en", "English"), ("ja", "日本語")]:
            if label:
                page.get_by_test_id("toggle-lang").get_by_text(label, exact=True).click()
                page.wait_for_timeout(300)
            s = page.evaluate(STATE_JS)
            assert s["lang"] == lang, s
            assert s["announce"] and name in s["announce"], s
            assert len(s["rows"]) == 1 and s["announce"] in s["rows"][0], s  # 見えている文と同じ
            if lang == "en":
                assert not KANA_KANJI.search(s["announce"].replace(name, "")), s
            seen.setdefault(lang, s["announce"])
            assert seen[lang] == s["announce"]  # 戻すと同じ文
        assert seen["ja"] != seen["en"]
        # 2つ目のお知らせ（English のまま）: 読み上げはそれに置き換わり、日本語に戻すと日本語になる
        page.get_by_test_id("toggle-lang").get_by_text("English", exact=True).click()
        page.get_by_test_id("file-input").set_input_files(
            files=[{"name": "second.json", "mimeType": "application/json", "buffer": b"[1, 2]"}]
        )
        page.wait_for_function(
            "() => [...document.querySelectorAll('[data-testid=notice-row]')]"
            ".some(r => r.innerText.includes('second.json'))"
        )
        page.wait_for_timeout(300)
        s = page.evaluate(STATE_JS)
        assert "second.json" in s["announce"] and not KANA_KANJI.search(s["announce"]), s
        assert any(s["announce"] in row for row in s["rows"]), s
        page.get_by_test_id("toggle-lang").get_by_text("日本語", exact=True).click()
        page.wait_for_timeout(300)
        s = page.evaluate(STATE_JS)
        assert s["lang"] == "ja" and "second.json" in s["announce"] and KANA_KANJI.search(s["announce"]), s
        assert any(s["announce"] in row for row in s["rows"]), s
    finally:
        ctx.close()
        app.stop()
