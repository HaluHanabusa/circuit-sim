"""A0 の独立検証（verifier、4回目）: 最後の修正（56c95fc）のうち、実際のブラウザで確かめるもの。

記録は docs/verification/A0-verifier-4.md。
- 起動し直し（D-061）: 画面を開いたまま（待ちの接続を残したまま）止め、--port 無しで起動し直すと、前回の
  ポートに戻り、画面が自分でつなぎ直す（「つながっていない」の帯が消え、開いていた仕様はそのまま、書き込みも通る）
- UX 確認3回目の改善1: 好みを保存し直せたら「保存できませんでした」が消える。仕様を開いているときは、
  そのお知らせだけが消え、ほかのお知らせは残る
- 改善2: 起動直後の画面で最後の1件をキーボードだけで閉じると、フォーカスは「ファイルを選ぶ…」。仕様を
  開いているときは帯の見出し。保存し直せて自動で消えたときも同じであるべき（帯の開閉そのものが好みの保存。
  いまは本文に落ちる。軽微。xfail）
- 改善3: 読み上げ用の領域の文は約 5 秒で消える。消えた後に言語を替えても入り直さない。同じお知らせが
  続けて起きても、毎回入る
- 「使い終わった URL」の案内: 別のポートのページの iframe・object・embed（Sec-Fetch-Mode: navigate で、
  Sec-Fetch-Dest が document でない）からは出ない。そのページから location で開くと出る（対照）

保存できない状態は、設定ファイルと控えの名前を、中身のあるフォルダにして作る（root でも Windows でも効く）。
ポートは空いている番号を決めて渡す（28470 番台を使わない。起動し直しは、前回のポートに戻れなかったときだけ
既定の並びに入る）。
時間の基準: 読み上げの文が消えるまでは 4 秒以上 7 秒未満（仕様の値は UX 確認3回目の「例 5 s」。上下は
混んだ機械のため）。ほかの待つ時間の上限は、落ちないように長めにとった（合否には使わない）。
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
WINDOWS = os.name == "nt"
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
    reason="playwright が無いので、実際のブラウザでの確認（A0 の4回目の検証の追加分）をしなかった",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """アプリ本体（python -m circuit_sim.app）を別のプロセスで起動し、端末の出力を読む。"""

    def __init__(self, config: Path, port: int | None = None) -> None:
        self.config = config
        self.lines: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(config), "LANG": "ja_JP.UTF-8",
               "PYTHONIOENCODING": "utf-8"}
        args = ["--port", str(port)] if port is not None else []
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", *args],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.url = self.next_url()
        self.port = int(URL_RE.search(self.url).group(1))
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

    def interrupt(self) -> int:
        """Ctrl+C（Windows は Ctrl+Break）で止め、終了コードを返す。"""
        self.proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        return self.proc.wait(timeout=20)

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                self.interrupt()
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc.kill()
            self.proc.wait(timeout=20)


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
                    "Chromium 系のブラウザが無いので、実際のブラウザでの確認（A0 の4回目の検証の追加分）を"
                    "しなかった"
                )
        yield launched
        launched.close()


STATE_JS = """() => ({
    active: document.activeElement?.dataset?.testid || document.activeElement?.tagName || null,
    band: !!document.querySelector('[data-testid=notices]'),
    rows: [...document.querySelectorAll('[data-testid=notice-row]')].map(r => r.dataset.code),
    announce: document.querySelector('[data-testid=announce]').textContent.trim(),
    lang: document.documentElement.lang,
})"""


def state(page) -> dict:
    return page.evaluate(STATE_JS)


def fail_settings(config: Path) -> tuple[Path, Path]:
    """設定ファイルに書けなくする（設定ファイルと控えの名前を、中身のあるフォルダにする）。"""
    settings, backup = config / "settings.json", config / "settings.json.broken"
    if settings.exists():
        settings.unlink()
    settings.mkdir()
    backup.mkdir(exist_ok=True)
    (backup / "x").write_text("x", encoding="utf-8")
    return settings, backup


def unfail_settings(settings: Path, backup: Path) -> None:
    if backup.is_dir():
        for child in backup.iterdir():
            child.unlink()
        backup.rmdir()
    if settings.is_dir():
        settings.rmdir()


def flip_theme(page) -> None:
    """テーマの2つの選びのうち、選ばれていない方を押す（好みが1つ変わり、保存が1回走る）。"""
    page.click("[data-testid=toggle-theme] [role=radio][aria-checked=false] >> nth=0")


def wait_save_notice(page) -> None:
    page.wait_for_selector("[data-testid=notices]")
    page.wait_for_function("() => document.querySelector('[data-code=N-PREFS-SAVE]') || "
                           "(document.querySelector('[data-testid=notices-latest]')?.innerText || '')"
                           ".includes('設定ファイルに保存できませんでした')")


def open_band(page) -> None:
    page.wait_for_selector("[data-testid=notices-toggle]")
    page.wait_for_timeout(500)  # 誤りのお知らせは帯を自分で開くことがある（開いた後に押すと閉じてしまう）
    if page.get_attribute("[data-testid=notices-toggle]", "aria-expanded") != "true":
        page.click("[data-testid=notices-toggle]")
    page.wait_for_selector("[data-testid=notice-row]")


# ------------------------------------------------------------------ 起動し直し（D-061）


def test_restart_without_port_returns_to_the_previous_port_and_the_page_reconnects(browser, tmp_path):
    config = tmp_path / "config"
    first = App(config, port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    second = None
    try:
        page = ctx.new_page()
        page.goto(first.url)
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.wait_for_timeout(2500)  # 接続の確かめが1回は回り、待ちの接続が残る
        try:
            assert first.interrupt() == 0, "".join(first.lines)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        page.get_by_test_id("conn-banner").wait_for(timeout=15000)
        second = App(config)  # --port 無し
        assert second.port == first.port, ("前回のポートに戻らなかった", "".join(second.lines))
        page.get_by_test_id("conn-banner").wait_for(state="detached", timeout=15000)
        assert page.get_by_test_id("frame").is_visible()
        # つなぎ直した後も書き込める（好みの保存が通る）
        with page.expect_response(lambda r: "/api/settings" in r.url and r.request.method == "PUT") as resp:
            flip_theme(page)
        assert resp.value.status == 200
    finally:
        ctx.close()
        first.stop()
        if second is not None:
            second.stop()


# ------------------------------------------------------------------ 改善1: 保存し直せたら消える


def test_save_notice_goes_away_but_other_notices_stay_with_a_spec_open(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    settings = backup = None
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.get_by_test_id("file-input").set_input_files(
            files=[{"name": "bad.json", "mimeType": "application/json", "buffer": b"{"}])
        page.wait_for_selector("[data-testid=notices]")
        settings, backup = fail_settings(app.config)
        flip_theme(page)
        wait_save_notice(page)
        open_band(page)
        page.wait_for_function("() => document.querySelectorAll('[data-testid=notice-row]').length === 2")
        unfail_settings(settings, backup)
        flip_theme(page)
        page.wait_for_function("() => document.querySelectorAll('[data-testid=notice-row]').length === 1",
                               timeout=5000)
        row = page.inner_text("[data-testid=notice-row]")
        assert "bad.json" in row and "保存できませんでした" not in row, row
    finally:
        if settings is not None:
            unfail_settings(settings, backup)
        ctx.close()
        app.stop()


# ------------------------------------------------------------------ 改善2: 閉じた後のフォーカス


def test_keyboard_only_close_on_the_start_screen_focuses_choose_file(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    settings, backup = None, None
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("start").wait_for()
        settings, backup = fail_settings(app.config)
        flip_theme(page)
        wait_save_notice(page)
        page.focus("[data-testid=notices-toggle]")
        if page.get_attribute("[data-testid=notices-toggle]", "aria-expanded") != "true":
            page.keyboard.press("Enter")
        page.wait_for_selector("[data-testid=notice-row]")
        for _ in range(8):  # Tab で行の「閉じる」へ
            page.keyboard.press("Tab")
            if state(page)["active"] == "notice-dismiss":
                break
        assert state(page)["active"] == "notice-dismiss", state(page)
        page.keyboard.press("Enter")
        page.wait_for_selector("[data-testid=notices]", state="detached")
        page.wait_for_function("() => document.activeElement?.dataset.testid === 'choose-file'",
                               timeout=3000)
    finally:
        if settings is not None:
            unfail_settings(settings, backup)
        ctx.close()
        app.stop()


def test_closing_the_last_notice_with_a_spec_open_focuses_the_band_header(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.get_by_test_id("file-input").set_input_files(
            files=[{"name": "bad.json", "mimeType": "application/json", "buffer": b"{"}])
        open_band(page)
        page.focus("[data-testid=notice-row]")
        page.keyboard.press("Delete")
        page.wait_for_function("() => document.activeElement?.dataset.testid === 'notices-toggle'",
                               timeout=3000)
        assert state(page)["band"]  # 仕様を開いているときは、帯は残る
    finally:
        ctx.close()
        app.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "製品の不具合(軽微): 起動直後の画面で、保存できないお知らせが保存し直せて自動で消えると"
        "（帯の見出しの Enter で開閉したとき。開閉も好みの保存）、帯ごと消えてフォーカスが本文に落ちる"
        "（AC-1-5。次の Tab は"
        "ヘッダーの「開く」）。閉じる操作のときは「ファイルを選ぶ…」へ移すが、自動で消えるときは移さない"
    ),
)
def test_auto_cleared_save_notice_on_the_start_screen_keeps_focus_visible(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    settings, backup = None, None
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("start").wait_for()
        settings, backup = fail_settings(app.config)
        flip_theme(page)
        wait_save_notice(page)
        unfail_settings(settings, backup)
        settings = None
        page.focus("[data-testid=notices-toggle]")
        page.keyboard.press("Enter")  # 帯の開閉（好みの保存。保存でき、お知らせが消える）
        page.wait_for_selector("[data-testid=notices]", state="detached", timeout=5000)
        page.wait_for_timeout(300)
        assert state(page)["active"] == "choose-file", state(page)
    finally:
        if settings is not None:
            unfail_settings(settings, backup)
        ctx.close()
        app.stop()


# ------------------------------------------------------------------ 改善3: 読み上げの文を消す


def test_announcement_is_cleared_after_about_five_seconds(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    ctx = browser.new_context(locale="ja-JP")
    bad = {"name": "bad.json", "mimeType": "application/json", "buffer": b"{"}
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.get_by_test_id("file-input").set_input_files(files=[bad])
        t0 = time.monotonic()
        page.wait_for_function(
            "() => document.querySelector('[data-testid=announce]').textContent.trim() !== ''")
        assert "bad.json" in state(page)["announce"]
        page.wait_for_function(
            "() => document.querySelector('[data-testid=announce]').textContent.trim() === ''", timeout=15000)
        cleared = time.monotonic() - t0
        assert 4.0 <= cleared < 7.0, cleared
        # 消えた後に言語を替えても、前の出来事を入れ直さない
        page.get_by_test_id("toggle-lang").get_by_text("English", exact=True).click()
        page.wait_for_timeout(800)
        assert state(page)["announce"] == "", state(page)
        page.get_by_test_id("toggle-lang").get_by_text("日本語", exact=True).click()
        page.wait_for_timeout(500)
        # 同じお知らせ（同じファイルの同じ誤り）がもう一度起きると、また入る
        page.get_by_test_id("file-input").set_input_files(files=[])
        page.get_by_test_id("file-input").set_input_files(files=[bad])
        page.wait_for_function("() => document.querySelector('[data-testid=announce]').textContent"
                               ".includes('bad.json')", timeout=5000)
    finally:
        ctx.close()
        app.stop()


# -------------------------------------------------- 「使い終わった URL」の案内（Sec-Fetch-Dest）


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


FRAMES = ('<iframe src="TARGET"></iframe><iframe src="TARGET" style="display:none"></iframe>'
          '<object data="TARGET" type="text/html"></object><embed src="TARGET" type="text/html">')


def test_stale_url_report_not_from_frames_of_another_page(browser, tmp_path):
    app = App(tmp_path / "config", port=free_port())
    used = app.url
    ctx = browser.new_context()
    other = None
    try:
        page = ctx.new_page()
        page.goto(used)  # 起動の番号を使う
        page.wait_for_url(re.compile(r"/$"))
        time.sleep(0.5)
        assert app.stale_reports() == 0, "".join(app.lines)
        fresh = browser.new_context()  # クッキーの無いブラウザ
        try:
            probe = fresh.new_page()
            dests: list[str] = []
            probe.on("request",
                     lambda r: dests.append(r.headers.get("sec-fetch-dest", "")) if r.url == used else None)
            other = OtherPage(FRAMES.replace("TARGET", used))
            probe.goto(other.url)
            probe.wait_for_timeout(2500)
            assert len(dests) >= 3, dests  # 枠の読み込みが、たしかに起きた
            assert app.stale_reports() == 0, (dests, "".join(app.lines))
            probe.evaluate("u => { location.href = u; }", used)  # 対照: ページとして開く
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
