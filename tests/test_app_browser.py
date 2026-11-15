"""実際のブラウザでの通し試験（A0。画面仕様 1章、3章、9章、10章の受け入れ基準）。

Python の playwright と Chromium 系のブラウザ（Chrome、Edge、playwright のもの）がある環境でだけ動く。無ければ、
確かめなかったことをスキップの理由として結果に残す。ブラウザは CIRCUIT_SIM_CHROME で指定できる。
CIRCUIT_SIM_SHOTS にフォルダを指定すると、確かめた画面の画像をそこに残す（作業ツリーの外に置く）。
アプリ本体は別のプロセス（python -m circuit_sim.app）で起動し、止めたり起動し直したりする。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

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
    """アプリ本体のプロセス。止めて、同じ番号で起動し直せる。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(self.port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(self.url + "api/health", timeout=1).close()
                return
            except OSError:
                assert time.monotonic() < deadline, "アプリ本体が起動しない"
                time.sleep(0.05)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def reset(self) -> None:
        req = urllib.request.Request(
            self.url + "api/session", data=b'{"doc": null, "dirty": false}', method="PUT",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).close()


@pytest.fixture(scope="module")
def app():
    running = App()
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

    def __init__(self, browser, app: App, width=1280, height=720, lang="ja", theme="light", scale=1, reset=True):
        if reset:
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
        self.page.goto(self.app.url)
        self.page.wait_for_selector(wait)
        return self.page

    def shot(self, name: str) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    def close(self) -> None:
        self.context.close()
        assert self.external == [], f"外部への通信: {self.external}"
        assert self.errors == [], f"画面のエラー: {self.errors}"


# ページの中で、見えている文字の欠け・はみ出しと、文字のコントラストを測る（AC-1-1、AC-10-2、AC-10-4）
MEASURE = r"""
() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const v = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:v[0], g:v[1], b:v[2], a: v.length > 3 ? v[3] : 1}; };
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const bgOf = (el) => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
    }
    let out = {r: 255, g: 255, b: 255};
    for (const c of layers.reverse()) out = {r: c.r * c.a + out.r * (1 - c.a), g: c.g * c.a + out.g * (1 - c.a), b: c.b * c.a + out.b * (1 - c.a)};
    return out;
  };
  const low = [], cut = [];
  for (const el of document.querySelectorAll("body *")) {
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.visibility === "hidden" || rect.width === 0 || rect.height === 0 || el.closest("[hidden]")) continue;
    if (el.closest(".visually-hidden")) continue;
    if (el.closest('[aria-disabled="true"]')) continue;   // 使えない操作の文字は基準の外（design-system 2.3）
    const fg = parse(style.color), bg = bgOf(el);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    if (ratio < 4.5) low.push(`${el.className || el.tagName}: ${el.textContent.trim().slice(0, 30)} ${ratio.toFixed(2)}`);
    if (el.scrollWidth > el.clientWidth + 1 && style.overflow !== "visible")
      cut.push(`${el.className || el.tagName}: ${el.textContent.trim().slice(0, 30)}`);
    if (rect.right > innerWidth + 0.5 || rect.left < -0.5) cut.push(`画面の外: ${el.textContent.trim().slice(0, 30)}`);
  }
  const header = document.querySelector("[data-testid=header]").getBoundingClientRect();
  const parts = [...document.querySelectorAll("[data-testid=header] > *")].filter((e) => e.getBoundingClientRect().width > 0)
    .map((e) => { const r = e.getBoundingClientRect(); return {name: e.dataset.testid || e.className, l: r.left, r: r.right, t: r.top, b: r.bottom}; });
  const overlaps = [];
  for (let i = 1; i < parts.length; i++) if (parts[i].l < parts[i - 1].r - 0.5) overlaps.push(`${parts[i - 1].name} / ${parts[i].name}`);
  const outside = parts.filter((p) => p.t < header.top - 0.5 || p.b > header.bottom + 0.5 || p.r > header.right + 0.5).map((p) => p.name);
  return {low, cut, overlaps, outside, headerHeight: header.height};
}
"""


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
    """AC-1-1、AC-10-2（A0 の領域）、AC-10-4: 8通りで、起動直後と、仕様を開いた枠の文字が欠けず重ならず、読める。"""
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
    page.wait_for_function("document.activeElement?.dataset.testid === 'choose-file'")
    assert page.inner_text("[data-testid=run-status]") == "仕様を開いてください"
    assert page.get_attribute("[data-testid=run-button]", "aria-disabled") == "true"
    assert page.get_attribute("[data-testid=header-save]", "aria-disabled") == "true"
    p.close()


def test_tab_order_in_header_and_visible_focus(browser, app):
    """AC-1-5（A0 の範囲）: Tab でヘッダーの順（開く → サンプル → 保存 → 実行 → 言語 → テーマ）に回り、輪が見える。"""
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
    """3.3 と AC-3-5: 一覧が遅いと「読み込んでいます」、取れないと「読み込めませんでした」と「もう一度」。開く操作は使える。"""
    p = Page(browser, app)
    page = p.page
    page.route("**/api/samples", lambda route: route.abort())
    p.open("[data-testid=samples-error]")
    assert "サンプルを読み込めませんでした。" in page.inner_text("[data-testid=samples-error]")
    assert page.get_attribute("[data-testid=choose-file]", "aria-disabled") is None
    p.shot("start-samples-error-ja-light")
    page.unroute("**/api/samples")
    held = []
    page.route("**/api/samples", lambda route: held.append(route))  # 応答を止めておき、読み込み中を確かめてから流す
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
    page.goto(app.url)
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
    assert "bom.json" in page.inner_text("[data-testid=header-file]"), "開けなかったのに、開いていた仕様が変わった"
    p.shot("frame-notice-error-ja-light")
    p.close()


def test_notices_keyboard_and_language_switch(browser, app, tmp_path):
    """AC-9-7、AC-10-1、AC-10-5、AC-10-8: キーボードで帯を操作し、言語の切り替えで出ている文も変わる。再読み込みしない。"""
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
    page.wait_for_function("!document.querySelector('[data-testid=notice-row]')")

    # 再読み込みしても、言語とテーマと開いている仕様が戻る（AC-10-5、1.3）
    page.reload()
    page.wait_for_selector("[data-testid=frame]")
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    p.close()


def test_no_light_flash_when_dark_is_chosen(browser, app):
    """AC-10-7: 暗いテーマを選んだ状態で再読み込みしても、明るいテーマが一瞬も出ない（本文より先にテーマが当たる）。"""
    p = Page(browser, app, theme="dark")
    p.context.add_init_script(
        """new MutationObserver((list, obs) => {
             if (document.body) { window.__themeAtBody = document.documentElement.dataset.theme; obs.disconnect(); }
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
    width = lambda: page.evaluate("document.querySelector('[data-testid=spec-panel]').getBoundingClientRect().width")  # noqa: E731
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
    """AC-1-3、D8: アプリ本体を止めると 5秒以内に帯が出る。起動し直すと 5秒以内に消え、開いていた仕様が残る。"""
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
    session = urllib.request.urlopen(app.url + "api/session", timeout=5).read().decode()
    assert "rlc_series.json" in session, "起動し直したアプリ本体に、仕様が送り直されていない"
    p.shot("frame-reconnected-ja-light")
    p.close()
