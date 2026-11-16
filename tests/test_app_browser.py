"""実際のブラウザでの通し試験（A0。画面仕様 1章、3章、9章、10章の受け入れ基準）。

Python の playwright と Chromium 系のブラウザ（Chrome、Edge、playwright のもの）がある環境でだけ動く。
無ければ、確かめなかったことをスキップの理由として結果に残す。ブラウザは CIRCUIT_SIM_CHROME で指定できる。
CIRCUIT_SIM_SHOTS にフォルダを指定すると、確かめた画面の画像をそこに残す（作業ツリーの外に置く）。
アプリ本体は別のプロセス（python -m circuit_sim.app）で起動し、止めたり起動し直したりする。
設定の場所はモジュールごとの一時フォルダ。画面は、起動の URL（一度だけ使える起動の番号つき）から
開く（D-051）。番号は1回しか使えないので、ブラウザの場を作るたびに、端末に Enter を送って新しい URL を
出させる（利用者と同じ）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from circuit_sim.app import cli
from circuit_sim.app.config import PREFS, load_token
from circuit_sim.app.server import COOKIE

ROOT = Path(__file__).resolve().parents[1]
SHOTS = os.environ.get("CIRCUIT_SIM_SHOTS")
BROWSERS = [
    os.environ.get("CIRCUIT_SIM_CHROME", ""),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
VIEWS = [(1280, 720), (1920, 1080)]
COMBOS = [(w, h, lang, theme) for (w, h) in VIEWS for lang in ("ja", "en") for theme in ("light", "dark")]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright が無いので、実際のブラウザでの通し試験を確かめなかった"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """アプリ本体のプロセス。止めて、同じポートで起動し直せる。設定の場所（合言葉、画面の好み）は config。"""

    def __init__(self, config: Path, port: int | None = None) -> None:
        self.config = config
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.token = load_token(config)  # アプリ本体と同じ合言葉（同じ設定の場所から読む。API を呼ぶため）
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()

    @property
    def login_url(self) -> str:
        """新しい起動の URL（端末に Enter を送って出させる）。1回だけ使える。"""
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + 10
        while True:
            line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            m = re.search(r"http://127\.0\.0\.1:\d+/\?k=[A-Za-z0-9_-]+", line)
            if m and line.lstrip().startswith(("新しい URL", "New URL")):
                return m.group(0)

    def start(self) -> None:
        env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(self.config), "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.lines), daemon=True).start()
        deadline = time.monotonic() + 15
        while True:
            try:
                self.api("api/health")
                return
            except OSError:
                assert time.monotonic() < deadline, "アプリ本体が起動しない"
                time.sleep(0.05)

    @staticmethod
    def _read(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", "replace"))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def api(self, path: str, body: dict | None = None, method: str = "GET") -> dict:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Cookie": f"{COOKIE}={self.token}"}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def reset(self) -> None:
        """開いている仕様と、設定ファイルの画面の好みを消す（試験の間で持ち越さない）。"""
        self.api("api/session", {"doc": None, "dirty": False}, "PUT")
        self.api("api/settings", {"settings": dict.fromkeys(PREFS)}, "PUT")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    running = App(tmp_path_factory.mktemp("app-config"))
    running.start()
    yield running
    running.stop()


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
                pytest.skip("Chromium 系のブラウザが無いので、実際のブラウザでの通し試験を確かめなかった")
        yield launched
        launched.close()


class Page:
    """1つの画面。外部への通信を記録し、終わりに1件も無いことを確かめる（AC-1-7）。"""

    def __init__(self, browser, app: App, width=1280, height=720, lang="ja", theme="light", scale=1):
        app.reset()
        self.app = app
        self.context = browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=scale,
            locale="ja-JP" if lang == "ja" else "en-US", color_scheme=theme,
        )
        # 最初に開いたときの言語とテーマ（2回目からは、画面で選んだ値をそのまま使う）
        self.context.add_init_script(
            "try{if(!sessionStorage.getItem('seeded')){sessionStorage.setItem('seeded','1');"
            f"localStorage.setItem('circuit-sim.lang','{lang}');"
            f"localStorage.setItem('circuit-sim.theme','{theme}')}}}}catch(e){{}}"
        )
        self.page = self.context.new_page()
        self.external: list[str] = []
        self.errors: list[str] = []
        self.page.on("request", self._request)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def _request(self, request) -> None:
        if not (request.url.startswith(self.app.url) or request.url.startswith("data:")):
            self.external.append(request.url)

    def open(self, wait: str = "[data-testid=start]"):
        """起動の URL（起動の番号つき）から開く。画面は番号の無い URL へ移る。"""
        self.page.goto(self.app.login_url)
        self.page.wait_for_selector(wait)
        assert self.page.url == self.app.url, "合言葉の無い URL へ移っていない"
        return self.page

    def shot(self, name: str) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    def close(self) -> None:
        self.context.close()
        assert self.external == [], f"外部への通信: {self.external}"
        assert self.errors == [], f"画面のエラー: {self.errors}"


# ページの中で、文字の欠け・はみ出し、コントラスト、ヘッダーの重なりを測る（AC-1-1、AC-10-2、AC-10-4）
MEASURE = (ROOT / "tests" / "data" / "measure_layout.js").read_text(encoding="utf-8")


def check_layout(page, where: str) -> None:
    page.wait_for_timeout(200)  # 色の変化（80 ms）が終わってから測る
    m = page.evaluate(MEASURE)
    assert m["headerHeight"] == 48, f"{where}: ヘッダーが1行でない"
    assert m["overlaps"] == [], f"{where}: ヘッダーの要素が重なる {m['overlaps']}"
    assert m["outside"] == [], f"{where}: ヘッダーの外にはみ出す {m['outside']}"
    assert m["cut"] == [], f"{where}: 文字が欠ける {m['cut']}"
    assert m["low"] == [], f"{where}: コントラストが 4.5 未満 {m['low']}"


@pytest.mark.parametrize(("width", "height", "lang", "theme"), COMBOS, ids=lambda v: str(v))
def test_eight_combinations_start_and_frame(browser, app, width, height, lang, theme):
    """AC-1-1、AC-10-2（A0 の領域）、AC-10-4: 8通りで、起動直後と仕様を開いた枠の文字が読める。"""
    p = Page(browser, app, width, height, lang, theme)
    page = p.open()
    assert page.evaluate("document.documentElement.lang") == lang
    assert page.evaluate("document.documentElement.dataset.theme") == theme
    check_layout(page, f"起動直後 {width}x{height} {lang} {theme}")
    p.shot(f"start-{width}x{height}-{lang}-{theme}")
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    page.click("[data-testid=notices-toggle]")
    check_layout(page, f"枠 {width}x{height} {lang} {theme}")
    p.shot(f"frame-{width}x{height}-{lang}-{theme}")
    page.click("[data-testid=notices-toggle]")
    p.close()


def test_zoom_150_percent(browser, app):
    """AC-10-6: ブラウザの拡大 150%（1920×1080 の画面で、表示の幅は 1280×720）で、欠け・重なりが無い。"""
    p = Page(browser, app, 1280, 720, "ja", "light", scale=1.5)
    page = p.open()
    check_layout(page, "150%")
    p.shot("start-zoom150-ja-light")
    p.close()


def test_start_screen_appears_within_one_second(browser, app):
    """AC-3-1: ページを開いてから起動直後の画面が出るまで 1秒以内。最初のフォーカスは「ファイルを選ぶ…」。"""
    p = Page(browser, app)
    started = time.monotonic()
    page = p.open()
    assert time.monotonic() - started < 1.0
    wait_focus(page, "choose-file")
    assert page.inner_text("[data-testid=run-status]") == "仕様を開いてください"
    assert page.get_attribute("[data-testid=run-button]", "aria-disabled") == "true"
    assert page.get_attribute("[data-testid=header-save]", "aria-disabled") == "true"
    p.close()


def test_tab_order_in_header_and_visible_focus(browser, app):
    """AC-1-5（A0 の範囲）: Tab がヘッダーの順（開く → サンプル → 保存 → 実行 → 言語 → テーマ）に回る。

    フォーカスの輪が見える。
    """
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=header-app]")  # 順番を数え始める位置を、ページの先頭（アプリ名）に置く
    order = []
    for _ in range(8):
        page.keyboard.press("Tab")
        info = page.evaluate(
            "(() => { const e = document.activeElement; const s = getComputedStyle(e);"
            " return {id: e.dataset.testid || e.closest('[data-testid]')?.dataset.testid,"
            " outline: s.outlineStyle + ' ' + s.outlineWidth}; })()"
        )
        order.append(info["id"])
        assert info["outline"] == "solid 2px", info
    assert order == [
        "header-open", "header-samples", "header-save", "run-button", "toggle-lang", "toggle-theme",
        "choose-file", "sample-rlc_series",
    ]
    p.close()


def test_samples_loading_and_error_states(browser, app):
    """3.3 と AC-3-5: 一覧が遅いと「読み込んでいます」、取れないと「読み込めませんでした」と「もう一度」。

    一覧が取れなくても、ファイルを開く操作は使える。
    """
    p = Page(browser, app)
    page = p.page
    page.route("**/api/samples", lambda route: route.abort())
    p.open("[data-testid=samples-error]")
    assert "サンプルを読み込めませんでした。" in page.inner_text("[data-testid=samples-error]")
    assert page.get_attribute("[data-testid=choose-file]", "aria-disabled") is None
    p.shot("start-samples-error-ja-light")
    page.unroute("**/api/samples")
    held = []
    # 応答を止めておき、読み込み中の表示を確かめてから流す
    page.route("**/api/samples", lambda route: held.append(route))
    page.click("text=もう一度")
    page.wait_for_selector("[data-testid=samples-loading]")
    assert page.inner_text("[data-testid=samples-loading]") == "サンプルを読み込んでいます…"
    p.shot("start-samples-loading-ja-light")
    held[0].continue_()
    page.wait_for_selector("[data-testid=sample-rlc_series]")
    p.close()


def test_connecting_state(browser, app):
    """1.3 読み込み中: つながるまで 200 ms を超えると、中央に「アプリ本体に接続しています…」。"""
    p = Page(browser, app)
    page = p.page

    def slow(route):
        time.sleep(0.8)
        route.continue_()

    page.route("**/api/health", slow)
    page.goto(app.login_url)
    page.wait_for_selector("text=アプリ本体に接続しています…")
    p.shot("connecting-ja-light")
    page.wait_for_selector("[data-testid=start]")
    p.close()


def test_open_errors_on_start_screen_and_in_notices(browser, app, tmp_path):
    """3.3 エラー（開けなかった）: 起動直後は枠の下に、仕様を開いた後はお知らせに出る（自動で開く）。"""
    bad = tmp_path / "broken.json"
    bad.write_text('{\n  "template": "rlc_series",\n  "plant": {,}\n}', encoding="utf-8")
    sjis = tmp_path / "sjis.json"
    sjis.write_bytes('{"template": "日本"}'.encode("shift_jis"))
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + (ROOT / "examples" / "rlc_series.json").read_bytes())
    text = tmp_path / "notes.txt"
    text.write_text("x", encoding="utf-8")

    p = Page(browser, app)
    page = p.open()
    page.set_input_files("[data-testid=file-input]", str(bad))
    error = page.wait_for_selector("[data-testid=open-error]")
    assert "broken.json を開けませんでした" in error.inner_text()
    assert "broken.json を JSON として読めません（3 行目、13 文字目）。" in error.inner_text()
    assert "その位置の近くで、コンマ、引用符、かっこの対応を確かめてください。" in error.inner_text()
    p.shot("start-open-error-ja-light")
    page.set_input_files("[data-testid=file-input]", str(sjis))
    page.wait_for_selector("text=sjis.json の文字コードが UTF-8 ではありません。")
    page.set_input_files("[data-testid=file-input]", str(text))
    page.wait_for_selector("text=notes.txt は仕様JSON（.json）ではありません。")
    page.set_input_files("[data-testid=file-input]", str(bom))
    page.wait_for_selector("[data-testid=frame]")
    assert "bom.json" in page.inner_text("[data-testid=header-file]")

    page.set_input_files("[data-testid=file-input]", str(bad))
    row = page.wait_for_selector("[data-testid=notice-row][data-code=N-OPEN-SYNTAX]")
    assert "broken.json" in row.inner_text()
    header = page.inner_text("[data-testid=header-file]")
    assert "bom.json" in header, "開けなかったのに、開いていた仕様が変わった"
    p.shot("frame-notice-error-ja-light")
    p.close()


def test_notices_keyboard_and_language_switch(browser, app, tmp_path):
    """AC-9-7、AC-10-1、AC-10-5、AC-10-8: キーボードで帯を操作できる。

    言語の切り替えで、出ているお知らせの文も変わり、再読み込みは起きない。
    """
    bad = tmp_path / "broken.json"
    bad.write_text("{", encoding="utf-8")
    p = Page(browser, app, theme="dark")
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    page.evaluate("window.__marker = 1")
    page.set_input_files("[data-testid=file-input]", str(bad))
    page.wait_for_selector("[data-testid=notice-row]")
    assert "エラー 1" in page.inner_text("[data-testid=notices-counts]")

    # 言語の切り替えが 200 ms 以内に終わり、再読み込みが起きない
    took = page.evaluate(
        """async () => {
          const start = performance.now();
          document.querySelector('[data-testid=toggle-lang] [lang=en]').click();
          while (!document.querySelector('[data-testid=notice-row]').textContent.includes("isn't valid JSON"))
            await new Promise((r) => requestAnimationFrame(r));
          await new Promise((r) => requestAnimationFrame(r));
          return performance.now() - start;
        }"""
    )
    assert took < 200
    assert page.evaluate("window.__marker") == 1
    assert page.evaluate("document.documentElement.lang") == "en"
    assert "Error 1" in page.inner_text("[data-testid=notices-counts]")
    p.shot("frame-notice-en-dark")

    # キーボードだけで、帯をたたんで開き、行を移り、閉じる
    page.focus("[data-testid=notices-toggle]")
    page.keyboard.press("Enter")
    assert page.query_selector("[data-testid=notices-list]") is None
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.dataset.testid") == "notice-row"
    page.keyboard.press("Delete")
    page.wait_for_function("() => !document.querySelector('[data-testid=notice-row]')")
    # 最後の1件を閉じたら、フォーカスは帯の見出しへ（body に落とさない。AC-1-5）。0 件なので帯はたたむ（9.3）
    wait_focus(page, "notices-toggle")
    assert page.get_attribute("[data-testid=notices-toggle]", "aria-expanded") == "false"
    assert page.query_selector("[data-testid=notices-list]") is None

    # 再読み込みしても、言語とテーマと開いている仕様が戻る（AC-10-5、1.3）
    page.reload()
    page.wait_for_selector("[data-testid=frame]")
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    p.close()


def test_no_light_flash_when_dark_is_chosen(browser, app):
    """AC-10-7: 暗いテーマで再読み込みしても、明るいテーマが一瞬も出ない（本文より先にテーマが当たる）。"""
    p = Page(browser, app, theme="dark")
    p.context.add_init_script(
        """new MutationObserver((list, obs) => {
             if (!document.body) return;
             window.__themeAtBody = document.documentElement.dataset.theme;
             obs.disconnect();
           }).observe(document, {childList: true, subtree: true});"""
    )
    page = p.open()
    assert page.evaluate("window.__themeAtBody") == "dark"
    page.reload()
    page.wait_for_selector("[data-testid=start]")
    assert page.evaluate("window.__themeAtBody") == "dark"
    p.close()


def test_layout_is_remembered_after_reload(browser, app):
    """AC-1-6: 仕切りの位置とたたんだ状態が、再読み込みの後も同じ。ダブルクリックで既定に戻る。"""
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    panel = "document.querySelector('[data-testid=spec-panel]')"
    width = lambda: page.evaluate(f"{panel}.getBoundingClientRect().width")  # noqa: E731
    assert width() == 360
    page.focus("[data-testid=splitter-spec]")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert width() == 392
    page.click("[data-testid=schematic] button")
    assert page.get_attribute("[data-testid=schematic]", "data-collapsed") == "true"
    page.reload()
    page.wait_for_selector("[data-testid=frame]")
    assert width() == 392
    assert page.get_attribute("[data-testid=schematic]", "data-collapsed") == "true"
    page.dblclick("[data-testid=splitter-spec]")
    assert width() == 360
    p.close()


def test_disconnect_banner_and_restart(browser, app):
    """AC-1-3、D8: アプリ本体を止めると 5秒以内に帯が出る。

    起動し直すと 5秒以内に帯が消え、開いていた仕様が残る。
    """
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    app.stop()
    started = time.monotonic()
    page.wait_for_selector("[data-testid=conn-banner]", timeout=5000)
    took = time.monotonic() - started
    assert took < 5
    for testid in ("header-open", "header-samples", "header-save", "run-button"):
        assert page.get_attribute(f"[data-testid={testid}]", "aria-disabled") == "true", testid
    assert "rlc_series.json" in page.inner_text("[data-testid=header-file]")
    p.shot("frame-disconnected-ja-light")
    app.start()
    started = time.monotonic()
    page.wait_for_selector("[data-testid=conn-banner]", state="detached", timeout=5000)
    assert time.monotonic() - started < 5
    assert "rlc_series.json" in page.inner_text("[data-testid=header-file]")
    page.wait_for_selector("[data-testid=notices] >> text=注意 1")
    session = json.dumps(app.api("api/session"))
    assert "rlc_series.json" in session, "起動し直したアプリ本体に、仕様が送り直されていない"
    p.shot("frame-reconnected-ja-light")
    p.close()


# ------------------------------------------------------------------ フォーカス（3.4、4.3、AC-1-5）


def wait_focus(page, testid: str) -> None:
    """フォーカスが testid の部品に来るまで待つ。

    関数の形で渡す（文字列の式は、CSP の unsafe-eval に掛かる）。"""
    page.wait_for_function("(id) => document.activeElement?.dataset?.testid === id", arg=testid)


def test_focus_moves_to_run_after_opening_with_keyboard_or_menu(browser, app):
    """サンプルを Enter で開いても、ヘッダーのメニューで開いても、開いた後のフォーカスは「実行」。

    フォーカスの輪が見える。"""
    p = Page(browser, app)
    page = p.open()
    page.focus("[data-testid=sample-rlc_series]")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-testid=frame]")
    wait_focus(page, "run-button")
    assert page.evaluate("document.activeElement.matches(':focus-visible')")
    # キーボードでフォーカスが来たので、実行のツールチップが出る。文字は地の中に収まる（折り返す）
    page.wait_for_selector("[role=tooltip]:not([hidden])")
    inside = page.evaluate("""() => { const tip = document.querySelector('[role=tooltip]:not([hidden])');
        const box = tip.getBoundingClientRect(); const r = document.createRange(); r.selectNodeContents(tip);
        return [...r.getClientRects()].every((x) => x.right <= box.right + 0.5 && x.left >= box.left - 0.5)
          && box.right <= innerWidth; }""")
    assert inside, "ツールチップの文字が地の外にはみ出す"
    p.shot("kbd-after-open-sample-ja-light")
    # メニュー: 開いていても、もう一度サンプルを選べる
    page.focus("[data-testid=header-samples]")
    page.keyboard.press("ArrowDown")
    page.wait_for_selector("[role=menuitem]")
    page.keyboard.press("Enter")
    wait_focus(page, "run-button")
    p.close()


def test_focus_after_menu_on_start_screen(browser, app):
    p = Page(browser, app, lang="en")
    page = p.open("[data-testid=sample-rlc_series]")  # 一覧が届くまで、メニューは使えない
    page.focus("[data-testid=header-samples]")
    page.keyboard.press("Enter")
    page.wait_for_selector("[role=menuitem]")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-testid=frame]")
    wait_focus(page, "run-button")
    p.close()


def test_focus_after_dismissing_the_last_notice(browser, app, tmp_path):
    """最後の1件を閉じたら、フォーカスは帯の見出し（body に落とさない）。

    次の行・前の行へ移ることは、部品の試験で確かめる。"""
    bad = tmp_path / "a.json"
    bad.write_text("{", encoding="utf-8")
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    page.set_input_files("[data-testid=file-input]", str(bad))
    page.wait_for_selector("[data-testid=notice-row]")
    page.focus("[data-testid=notice-row]")
    page.keyboard.press("Delete")
    wait_focus(page, "notices-toggle")
    p.close()


# ------------------------------------------------------------------ 長いファイル名（AC-1-1）


LONG_NAMES = {
    "en": ["buck_converter_rev12_final.json", "2026-11-15_buck_converter_prototype_rev3_final_check.json"],
    "ja": ["motor_drive_inverter_rev3_review.json",
           "降圧コンバータ_試作3回目_最終確認_レビュー用_コピー.json"],
}
VIEWS_12 = [(w, h, lang, theme, 1) for (w, h, lang, theme) in COMBOS] + [
    (1280, 720, lang, theme, 1.5) for lang in ("ja", "en") for theme in ("light", "dark")
]
SAMPLE_BYTES = (ROOT / "examples" / "rlc_series.json").read_bytes()
TEMPLATE_NAME = {"ja": "直列RLC", "en": "Series RLC"}


@pytest.mark.parametrize(("width", "height", "lang", "theme", "scale"), VIEWS_12, ids=lambda v: str(v))
def test_long_file_names_keep_extension_and_template_in_12_views(
    browser, app, width, height, lang, theme, scale
):
    """言語×テーマ×（1280×720、1920×1080、150%）の 12 通りで、長い名前でもヘッダーの②が欠けない。

    名前は幅で中ほどを省略し、拡張子とテンプレート名を必ず残す。CSS では切らない（二重に省略しない）。"""
    p = Page(browser, app, width, height, lang, theme, scale=scale)
    page = p.open()
    for name in LONG_NAMES[lang]:
        page.set_input_files("[data-testid=file-input]", files=[{"name": name, "mimeType": "application/json",
                                                                  "buffer": SAMPLE_BYTES}])
        # ②のツールチップ（省略した全文）が、いま開いた名前になるまで待つ
        page.wait_for_function(
            "(name) => document.querySelector("
            "'[data-testid=header-file-name] + [role=tooltip]')?.textContent === name", arg=name)
        page.wait_for_timeout(100)
        m = page.evaluate("""() => {
            const box = document.querySelector('[data-testid=header-file]').getBoundingClientRect();
            const nameEl = document.querySelector('[data-testid=header-file-name]');
            const tmpl = document.querySelector('[data-testid=header-file-template]');
            const n = nameEl.getBoundingClientRect(), t = tmpl.getBoundingClientRect();
            return {box: box.width, boxRight: box.right, nameRight: n.right, tmplRight: t.right,
                    shown: nameEl.textContent, template: tmpl.textContent,
                    nameCut: nameEl.scrollWidth > nameEl.clientWidth + 0.5,
                    tmplCut: tmpl.scrollWidth > tmpl.clientWidth + 0.5,
                    overflowName: getComputedStyle(nameEl).textOverflow};
        }""")
        where = f"{name} {width}x{height} {lang} {theme} x{scale}"
        assert m["shown"].endswith(".json"), f"{where}: 拡張子が残っていない {m['shown']!r}"
        assert m["template"] == TEMPLATE_NAME[lang], f"{where}: テンプレート名が欠けた {m['template']!r}"
        assert not m["nameCut"] and not m["tmplCut"], f"{where}: 文字が欠ける"
        assert m["overflowName"] != "ellipsis", f"{where}: CSS でも省略している（二重の省略）"
        assert m["box"] <= 320.5, f"{where}: ②が最大幅を超えた {m['box']}"
        assert m["tmplRight"] <= m["boxRight"] + 0.5 and m["nameRight"] <= m["boxRight"] + 0.5, where
        if m["shown"] != name:
            assert "…" in m["shown"], where
            head, tail = m["shown"].split("…")
            assert name.startswith(head) and name.endswith(tail), where
        check_layout(page, where)
    p.shot(f"longname-{width}x{height}-{lang}-{theme}-x{scale}")
    p.close()


# ------------------------------------------------------------------ 画面の好みがポートをまたいで戻る（D-050）


def test_settings_survive_a_port_change_in_the_same_browser_profile(browser, tmp_path):
    """同じプロファイルのブラウザで、ポートを変えて起動し直しても、言語・テーマ・仕切り・たたみ・帯の開閉が戻る。

    ブラウザの保存はポートごとに分かれるので、設定ファイル（同じ設定の場所）から戻ることを確かめる。"""
    config = tmp_path / "config"
    first = App(config)
    first.start()
    context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="ja-JP",
                                  color_scheme="light")
    try:
        page = context.new_page()
        page.goto(first.login_url)
        page.wait_for_selector("[data-testid=start]")
        page.click("[data-testid=sample-rlc_series]")
        page.wait_for_selector("[data-testid=frame]")
        page.click("[data-testid=toggle-lang] [lang=en]")
        page.click("[data-testid=toggle-theme] [role=radio] >> nth=1")
        page.focus("[data-testid=splitter-spec]")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        page.click("[data-testid=schematic] button")
        page.click("[data-testid=notices-toggle]")   # 利用者が押して開いた（覚える）
        page.wait_for_timeout(800)                   # 設定ファイルへの書き込み（まとめて送る）を待つ
        saved = json.loads((config / "settings.json").read_text(encoding="utf-8"))
        assert saved["lang"] == "en" and saved["theme"] == "dark", saved
        assert saved["layout.specWidth"] == "392" and saved["layout.schemCollapsed"] == "1", saved
        assert saved["layout.noticesOpen"] == "1", saved
        first.stop()

        second = App(config, port=free_port())
        assert second.port != first.port and second.token == first.token
        second.start()
        try:
            page.add_init_script("""new MutationObserver((list, obs) => {
                 if (!document.body) return;
                 window.__themeAtBody = document.documentElement.dataset.theme;
                 obs.disconnect();
               }).observe(document, {childList: true, subtree: true});""")
            page.goto(second.login_url)
            # 新しいプロセスなので、仕様は開いていない
            page.wait_for_selector("[data-testid=sample-rlc_series]")
            assert page.evaluate("window.__themeAtBody") == "dark", "最初の描画が明るいテーマだった"
            assert page.evaluate("document.documentElement.lang") == "en"
            assert page.evaluate("document.documentElement.dataset.theme") == "dark"
            page.click("[data-testid=sample-rlc_series]")
            page.wait_for_selector("[data-testid=frame]")
            width = page.evaluate(
                "document.querySelector('[data-testid=spec-panel]').getBoundingClientRect().width")
            assert width == 392
            assert page.get_attribute("[data-testid=schematic]", "data-collapsed") == "true"
        finally:
            second.stop()
    finally:
        context.close()
        first.stop()


def test_notice_bar_remembers_only_what_the_user_pressed(browser, app, tmp_path):
    """エラーの出来事で自動で開いた帯は覚えない。0 件のときは、覚えた値によらず たたむ（9.3）。"""
    bad = tmp_path / "broken.json"
    bad.write_text("{", encoding="utf-8")
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    page.set_input_files("[data-testid=file-input]", str(bad))
    page.wait_for_selector("[data-testid=notices-list]")          # 自動で開いた
    page.wait_for_timeout(500)
    assert "layout.noticesOpen" not in app.api("api/settings")["settings"]
    assert page.evaluate("localStorage.getItem('circuit-sim.layout.noticesOpen')") is None
    page.reload()
    page.wait_for_selector("[data-testid=frame]")
    assert page.get_attribute("[data-testid=notices-toggle]", "aria-expanded") == "false"
    assert page.query_selector("[data-testid=notices-list]") is None
    p.close()


# ------------------------------------------------------------------ 合言葉の無いページ（D-051）


def test_page_without_the_key_explains_how_to_reopen(browser, app):
    """合言葉のクッキーが無いページ（URL だけを写した、別のブラウザ）は、API が 401。案内を日英で出す。

    同じブラウザで起動したときの URL を開くと、案内を出していたページも自動でつながる。"""
    app.reset()
    context = browser.new_context(locale="ja-JP")
    try:
        page = context.new_page()
        page.goto(app.url)
        page.wait_for_selector("[data-testid=unauthorized]")
        text = page.inner_text("[data-testid=unauthorized]")
        assert "ターミナルに出ている URL から開き直してください" in text
        assert page.get_attribute("[data-testid=header-open]", "aria-disabled") == "true"
        page.evaluate("document.querySelector('[data-testid=toggle-lang] [lang=en]').click()")
        page.wait_for_function("() => document.querySelector('[data-testid=unauthorized]').innerText"
                               ".includes('Open it again from the URL shown in the terminal')")
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / "state-unauthorized-en-light.png"))
        other = context.new_page()
        other.goto(app.login_url)
        other.wait_for_selector("[data-testid=start]")
        page.wait_for_selector("[data-testid=start]", timeout=8000)
    finally:
        context.close()


def test_launch_url_works_once_and_another_browser_gets_the_guidance(browser, app):
    """起動の URL は1回だけ使える。同じ URL を別のブラウザ（別の場）で開いても、アプリ本体は使えない。

    （URL が漏れても、使い終わっていれば役に立たない。レビュー2回目の中1）"""
    app.reset()
    url = app.login_url
    first, second = browser.new_context(locale="ja-JP"), browser.new_context(locale="ja-JP")
    try:
        page = first.new_page()
        page.goto(url)
        page.wait_for_selector("[data-testid=start]")
        assert page.url == app.url
        other = second.new_page()
        other.goto(url)
        other.wait_for_selector("[data-testid=unauthorized]")
        assert other.url == app.url, "使い終わった番号が URL に残った"
    finally:
        first.close()
        second.close()


def test_launch_page_opens_the_workspace_with_saved_preferences_and_no_flash(browser, app, tmp_path):
    """起動コマンドがブラウザに渡す転送用の HTML（file://）から開く。番号をクッキーと交換して作業画面が開き、

    入口のページに好み（data-prefs）が付く（file:// から来ると、303 では続く / にクッキーが付かない）。
    暗いテーマを覚えていれば、明るい画面は一瞬も出ない。番号の URL は履歴に残らない。"""
    app.reset()
    app.api("api/settings", {"settings": {"theme": "dark", "lang": "en"}}, "PUT")
    url = app.login_url
    launch = tmp_path / "open-test.html"
    launch.write_bytes(cli.launch_page(url, "dark"))
    context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="ja-JP",
                                  color_scheme="light")
    try:
        context.add_init_script("""new MutationObserver((list, obs) => {
             if (!document.body || !document.getElementById('root')) return;
             window.__themeAtBody = document.documentElement.dataset.theme;
             obs.disconnect();
           }).observe(document, {childList: true, subtree: true});""")
        page = context.new_page()
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        page.goto(launch.as_uri())
        page.wait_for_selector("[data-testid=start]")
        assert page.url == app.url
        assert page.evaluate("document.documentElement.hasAttribute('data-prefs')"), \
            "入口のページにクッキーが付かなかった（好みが出ない）"
        assert page.evaluate("window.__themeAtBody") == "dark", "最初の描画が明るいテーマだった"
        assert page.evaluate("document.documentElement.lang") == "en"
        assert [r for r in requests if "?k=" in r] == [url], requests
        assert not page.evaluate("JSON.stringify(performance.getEntriesByType('navigation'))").count("?k=")
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / "launch-page-en-dark.png"))
    finally:
        context.close()
        app.reset()


def test_page_from_another_site_reads_settings_before_writing(browser, app):
    """別のサイトのリンクから開くと、SameSite=Strict のクッキーが入口の要求に付かず、data-prefs が無い。

    そのときも、設定ファイルの好みを既定の値で上書きしない（読んでから書く）。読んだ好み（幅、テーマ）で開く。
    （レビュー2回目の軽2）"""
    app.reset()
    app.api("api/settings", {"settings": {"layout.specWidth": "420", "theme": "dark"}}, "PUT")
    app.api("api/open", {"sample": "rlc_series"}, "POST")  # 開くと枠が出る（仕切りの幅を書き戻す場面）
    context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="ja-JP",
                                  color_scheme="light")
    try:
        context.add_cookies([{"name": COOKIE, "value": app.token, "url": app.url, "httpOnly": True,
                              "sameSite": "Strict"}])
        # ほかのサイトのページ（外には出ない。playwright が中身を返す）から、リンクでアプリを開く
        context.route("http://other.example/", lambda route: route.fulfill(
            content_type="text/html", body=f'<a id="go" href="{app.url}">circuit-sim</a>'))
        page = context.new_page()
        page.goto("http://other.example/")
        with page.expect_navigation():
            page.click("#go")
        page.wait_for_selector("[data-testid=frame]")
        assert not page.evaluate("document.documentElement.hasAttribute('data-prefs')"), \
            "クッキーが付いた（別のサイトからの移動を再現できていない）"
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        width = page.evaluate(
            "document.querySelector('[data-testid=spec-panel]').getBoundingClientRect().width")
        assert width == 420
        page.wait_for_timeout(1000)  # 書き戻すなら、とうに送っている（まとめて送るまで 300 ms）
        assert app.api("api/settings")["settings"] == {"layout.specWidth": "420", "theme": "dark"}
        # 利用者が変えた値は書く
        page.focus("[data-testid=splitter-spec]")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(800)
        assert app.api("api/settings")["settings"]["layout.specWidth"] == "436"  # 1回 16 px
    finally:
        context.close()


def test_preferences_that_cannot_be_saved_are_told_once(browser, app, tmp_path):
    """設定ファイルに書けないとき（ここでは、設定ファイルと控えの場所が中身のあるフォルダ）、画面は送り直し続けず、

    お知らせの帯で知らせる。起動直後の画面でも帯を出す。利用者が次に好みを変えたら、もう一度送り、
    保存できたらお知らせを消す（レビュー2回目の軽3、UX 確認3回目の改善1）。"""
    settings = app.config / "settings.json"
    backup = app.config / "settings.json.broken"
    p = Page(browser, app)
    page = p.open()
    puts: list[str] = []
    page.on("request",
            lambda r: puts.append(r.url) if r.method == "PUT" and "/api/settings" in r.url else None)
    if settings.exists():
        settings.unlink()
    settings.mkdir()
    backup.mkdir()
    (backup / "x").write_text("x", encoding="utf-8")
    try:
        page.click("[data-testid=toggle-theme] [role=radio] >> nth=1")
        page.wait_for_selector("[data-testid=notices]")
        page.wait_for_function("document.querySelector('[data-testid=notices-latest]')?.innerText"
                               ".includes('設定ファイルに保存できませんでした')")
        p.shot("prefs-save-failed-start-ja-dark")
        page.wait_for_timeout(5000)  # 2 秒ごとの確かめを 2 回以上
        assert len(puts) == 1, puts
        page.click("[data-testid=notices-toggle]")
        row = page.inner_text("[data-testid=notice-row]")
        assert str(app.config) in row and "空き容量" in row, row
        p.shot("prefs-save-failed-open-ja-dark")
        # 直ったら、次に変えたときに保存し直す。保存できたら、お知らせは消える（起動直後の画面では帯ごと）
        (backup / "x").unlink()
        backup.rmdir()
        settings.rmdir()
        page.click("[data-testid=toggle-lang] [lang=en]")
        page.wait_for_timeout(800)
        assert len(puts) == 2
        saved = json.loads(settings.read_text(encoding="utf-8"))
        assert saved["theme"] == "dark" and saved["lang"] == "en", saved
        page.wait_for_selector("[data-testid=notices]", state="detached", timeout=3000)
        assert page.locator("[data-testid=notice-row]").count() == 0
    finally:
        if backup.is_dir():
            for child in backup.iterdir():
                child.unlink()
            backup.rmdir()
        if settings.is_dir():
            settings.rmdir()
        p.close()


def fail_settings(app: App) -> tuple[Path, Path]:
    """設定ファイルに書けなくする（設定ファイルと控えの場所を、中身のあるフォルダにする）。"""
    settings, backup = app.config / "settings.json", app.config / "settings.json.broken"
    if settings.exists():
        settings.unlink()
    settings.mkdir()
    backup.mkdir()
    (backup / "x").write_text("x", encoding="utf-8")
    return settings, backup


def unfail_settings(settings: Path, backup: Path) -> None:
    if backup.is_dir():
        for child in backup.iterdir():
            child.unlink()
        backup.rmdir()
    if settings.is_dir():
        settings.rmdir()


@pytest.mark.parametrize("how", ["Delete", "close-button"])
def test_closing_the_last_notice_on_the_start_screen_focuses_choose_file(browser, app, how):
    """起動直後の画面で最後のお知らせを閉じると帯ごと消えるので、フォーカスを「ファイルを選ぶ…」に置く

    （AC-1-5、3.2 の最初のフォーカス。UX 確認3回目の改善2）。"""
    p = Page(browser, app)
    page = p.open()
    settings, backup = fail_settings(app)
    try:
        page.click("[data-testid=toggle-theme] [role=radio] >> nth=1")
        page.wait_for_selector("[data-testid=notices]")
        page.click("[data-testid=notices-toggle]")
        page.wait_for_selector("[data-testid=notice-row]")
        if how == "Delete":
            page.focus("[data-testid=notice-row]")
            page.keyboard.press("Delete")
        else:
            page.focus("[data-testid=notice-dismiss]")
            page.keyboard.press("Enter")
        page.wait_for_selector("[data-testid=notices]", state="detached")
        page.wait_for_function("() => document.activeElement?.dataset.testid === 'choose-file'", timeout=3000)
        assert page.evaluate("document.activeElement.matches(':focus-visible')")
    finally:
        unfail_settings(settings, backup)
        p.close()


ANNOUNCED = "() => document.querySelector('[data-testid=announce]').textContent.includes('broken.json')"


def test_announcement_is_cleared_after_a_few_seconds(browser, app, tmp_path):
    """読み上げに伝えた文は数秒で消える。消えた後に言語を替えても、前の出来事を読み直さない

    （UX 確認3回目の改善3）。同じ出来事が続けて起きても、空から入れ直す。"""
    bad = tmp_path / "broken.json"
    bad.write_text("{", encoding="utf-8")
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    page.set_input_files("[data-testid=file-input]", str(bad))
    page.wait_for_function(ANNOUNCED)
    page.wait_for_function("() => document.querySelector('[data-testid=announce]').textContent === ''",
                           timeout=8000)
    page.click("[data-testid=toggle-lang] [lang=en]")
    page.wait_for_timeout(300)
    assert page.inner_text("[data-testid=announce]") == ""
    # 同じ出来事をもう一度: 空から入れ直す
    page.evaluate("""() => { window.__changes = 0; new MutationObserver(() => window.__changes++)
        .observe(document.querySelector('[data-testid=announce]'), {childList: true, subtree: true,
                                                                   characterData: true}); }""")
    page.set_input_files("[data-testid=file-input]", str(bad))
    page.wait_for_function(ANNOUNCED)
    assert page.evaluate("window.__changes") >= 1
    p.close()


def test_tooltip_goes_away_when_pressed_with_the_keyboard(browser, app):
    """キーボードで開いた後に「実行」に出たツールチップは、Enter で押すと消え、

    フォーカスが残っても出し直さない。

    （UX の改善2）"""
    p = Page(browser, app)
    page = p.open()
    page.focus("[data-testid=sample-rlc_series]")
    page.keyboard.press("Enter")
    wait_focus(page, "run-button")
    page.wait_for_selector("[role=tooltip]:not([hidden])")
    page.keyboard.press("Enter")
    page.wait_for_selector("[role=tooltip]:not([hidden])", state="detached", timeout=2000)
    page.wait_for_timeout(800)
    assert page.locator("[role=tooltip]:not([hidden])").count() == 0
    assert page.evaluate("document.activeElement.dataset.testid") == "run-button"
    p.shot("tip-run-after-enter-ja-light")
    p.close()


# ------------------------------------------------------------------ UX の細部


def test_splitters_have_their_own_names(browser, app):
    """縦の仕切りは「幅を変える」、横の仕切りは「高さを変える」（1.5）。"""
    p = Page(browser, app)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    assert page.get_attribute("[data-testid=splitter-spec]", "aria-label").startswith("幅を変える")
    assert page.get_attribute("[data-testid=splitter-schematic]", "aria-label").startswith("高さを変える")
    p.close()


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_separator_has_space_on_both_sides(browser, app, lang):
    """ヘッダーの②とお知らせの件数の区切りは、両言語とも前後に 4px の空き。"""
    p = Page(browser, app, lang=lang)
    page = p.open()
    page.click("[data-testid=sample-rlc_series]")
    page.wait_for_selector("[data-testid=frame]")
    pads = page.evaluate("""() => [...document.querySelectorAll('.sep')].map((e) => {
        const s = getComputedStyle(e, '::before'); return [s.paddingLeft, s.paddingRight]; })""")
    assert pads and all(pad == ["4px", "4px"] for pad in pads), pads
    p.close()


@pytest.mark.parametrize(("width", "height", "lang"),
                         [(1280, 720, "en"), (1280, 720, "ja"), (1920, 1080, "en")])
def test_sample_description_is_one_line(browser, app, width, height, lang):
    """起動直後のサンプルの説明は1行（3.2）。名前の列は固定の幅にせず、数値と単位は同じ行（AC-2-6）。"""
    p = Page(browser, app, width, height, lang)
    page = p.open("[data-testid=sample-rlc_series]")
    p.shot(f"start-sample-desc-{width}x{height}-{lang}")
    lines = page.evaluate("""() => {
        const d = document.querySelector('[data-testid=sample-rlc_series] .sample-desc');
        const r = document.createRange(); r.selectNodeContents(d);
        return new Set([...r.getClientRects()].map((x) => Math.round(x.top))).size; }""")
    assert lines == 1
    p.close()
