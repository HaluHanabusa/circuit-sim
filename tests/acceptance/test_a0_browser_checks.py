"""A0 の独立検証（verifier）: 実際のブラウザでの確認のうち、実装の試験が見ていない所。

記録は docs/verification/A0-verifier.md。
- AC-1-1: 長いファイル名でも、ヘッダーの②（最大幅 320）が欠けない
  （中ほどを省略し、拡張子とテンプレート名が残る）
- AC-2-6: 起動直後の画面のサンプルの説明で、数値と単位の間で改行されない
- 3.4 と AC-1-5: サンプルを開いた後、フォーカスが「実行」にある。
  お知らせを閉じた後、フォーカスが見えない所に落ちない
- AC-10-3: 画面に出ている文字（起動直後、開けなかった、枠、お知らせ、切断の帯）に、訳し忘れが無い
  （直書きの検出の補い）
- AC-10-4: 画面に出ている文字の、実際の色と背景のコントラスト（同じ状態を両テーマで）
- D8: アプリ本体が固まった（接続は受けるが応えない）ときも、5 秒以内に帯が出る（POSIX だけ）

Python の playwright と Chromium 系のブラウザがある環境でだけ動く。無ければ、確かめなかったことを
スキップの理由として残す（POの Windows の PC など）。ブラウザは CIRCUIT_SIM_CHROME で指定できる。
製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = (ROOT / "circuit_sim" / "app" / "samples" / "rlc_series.json").read_bytes()
BROWSERS = [
    os.environ.get("CIRCUIT_SIM_CHROME", ""),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff01-\uff60]")
# 日本語の表示に出てよい英字の語（10.4 の「変わらない」もの: アプリ名、言語名、記号・単位、ファイル名、
# テンプレートの名前と JSON の欄の名前（データ）、ショートカットの書き方）
LATIN_OK = {"circuit", "sim", "English", "JSON", "json", "RLC", "rlc_series", "rc_lowpass", "Ctrl", "Enter",
            "UTF", "PC", "kHz", "MHz", "null", "plant", "value", "template", "schema_version", "targets",
            "missing",
            "zzz", "nope"}  # この試験が開かせる誤りのファイルに書いた、欄の名前とテンプレート名（データ）

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright が無いので、実際のブラウザでの確認（A0 の検証の追加分）をしなかった",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(self.port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(self.url + "api/health", timeout=1).close()
                return
            except OSError:
                assert time.monotonic() < deadline, "アプリ本体が起動しない"
                time.sleep(0.05)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            if sys.platform != "win32":
                os.kill(self.proc.pid, signal.SIGCONT)  # 固めたまま終わっても、確実に止める
            self.proc.kill()
            self.proc.wait(timeout=10)

    def reset(self) -> None:
        req = urllib.request.Request(self.url + "api/session", data=b'{"doc": null, "dirty": false}',
                                     method="PUT", headers={"Content-Type": "application/json"})
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
                pytest.skip("Chromium 系のブラウザが無いので、実際のブラウザでの確認（A0 の検証の追加分）を"
                            "しなかった")
        yield launched
        launched.close()


@pytest.fixture
def page_of(browser, app):
    contexts = []

    def make(width=1280, height=720, lang="ja", theme="light"):
        app.reset()
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  locale="ja-JP" if lang == "ja" else "en-US",
                                  color_scheme="dark" if theme == "dark" else "light")
        contexts.append(ctx)
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("sample-rlc_series").wait_for()
        page.wait_for_timeout(300)  # 色の変化（80 ms）が収まってから測る
        return page

    yield make
    for ctx in contexts:
        ctx.close()


def open_file(page, name: str, data: bytes) -> None:
    page.get_by_test_id("file-input").set_input_files(files=[{"name": name, "mimeType": "application/json",
                                                              "buffer": data}])


# ------------------------------------------------------------ AC-1-1: 長いファイル名


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(基準不適合 AC-1-1): ファイル名を 40 文字で中ほど省略してから、さらに幅 320 で"
    "末尾を CSS で切るので、31 文字（英語）・37 文字（日本語）以上の名前で拡張子とテンプレート名が欠ける"
    "（例: buck_converter_rev12_final.json）"))
@pytest.mark.parametrize("lang", ["ja", "en"])
def test_long_file_name_keeps_extension_and_template_name(page_of, lang):
    page = page_of(lang=lang)
    name = "buck_converter_rev12_final.json" if lang == "en" else "motor_drive_inverter_rev3_review.json"
    open_file(page, name, SAMPLE)
    page.get_by_test_id("frame").wait_for()
    page.wait_for_timeout(200)
    cut = page.evaluate("""() => [...document.querySelectorAll('[data-testid=header-file] *')]
        .filter(e => e.children.length === 0 && e.textContent.trim() && e.scrollWidth > e.clientWidth + 0.5)
        .map(e => e.textContent.trim())""")
    assert cut == [], f"ヘッダーの②で文字が欠けている: {cut}"
    shown = page.get_by_test_id("header-file").inner_text()
    assert ".json" in shown, f"拡張子が残っていない: {shown!r}"
    width = page.get_by_test_id("header-file").bounding_box()["width"]
    assert width <= 320.5


# ------------------------------------------------------------ AC-2-6 と 3.2: サンプルの説明


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(軽微、AC-2-6・design-system 4.2): 辞書の sample.rlc.desc の数値と単位の間が普通の空白"
    "（U+0020）。英語の 1280×720 では説明が2行に折れ、「1.59」と「kHz」が別の行に分かれる"))
@pytest.mark.parametrize("lang", ["ja", "en"])
def test_sample_description_keeps_number_with_unit(page_of, lang):
    """数値と単位の間は改行されない空白（U+00A0）で、画面でも同じ行にある。"""
    page = page_of(lang=lang)
    row = page.get_by_test_id("sample-rlc_series")
    text = row.inner_text()
    pairs = list(re.finditer(r"(\d[\d.]*)([ \u00a0])([fpnµmkMG]?(?:Hz|V|A|H|F|s|Ω))", text))
    assert pairs, f"数値と単位の組が見つからない: {text!r}"
    for m in pairs:
        assert m.group(2) == "\u00a0", f"「{m.group(0)}」の数値と単位の間が改行される空白"
    tops = page.evaluate("""() => { const row = document.querySelector('[data-testid=sample-rlc_series]');
        const out = []; const walk = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
        while (walk.nextNode()) { const n = walk.currentNode;
          const re = /(\\d[\\d.]*)[ \\u00a0]([fpnµmkMG]?(Hz|V|A|H|F|s|Ω))/g;
          let m; while ((m = re.exec(n.textContent))) { const r = document.createRange();
            r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
            out.push([m[0], r.getClientRects().length]); } }
        return out; }""")
    for piece, boxes in tops:
        assert boxes == 1, f"「{piece}」が {boxes} 行に分かれている"


# ------------------------------------------------------------ 3.4 と AC-1-5: フォーカス


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(軽微、3.4・AC-1-5): サンプルの行を Enter で開くと、行が消えてフォーカスが body に落ちる"
    "（3.4 は「開いた後、フォーカスが実行にある」）"))
def test_focus_is_on_run_after_opening_a_sample_with_the_keyboard(page_of):
    page = page_of()
    page.get_by_test_id("sample-rlc_series").focus()
    page.keyboard.press("Enter")
    page.get_by_test_id("frame").wait_for()
    page.wait_for_timeout(200)
    assert page.evaluate("document.activeElement.getAttribute('data-testid')") == "run-button"


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合(軽微、AC-1-5): お知らせの行で Delete を押して閉じると、フォーカスが body に落ちる"))
def test_focus_stays_visible_after_dismissing_a_notice(page_of):
    page = page_of()
    page.get_by_test_id("sample-rlc_series").click()
    page.get_by_test_id("frame").wait_for()
    open_file(page, "broken.json", b'{"a":\n}')
    row = page.get_by_test_id("notice-row").first
    row.wait_for()
    row.focus()
    page.keyboard.press("Delete")
    page.wait_for_timeout(150)
    assert page.evaluate("document.activeElement !== document.body"), "フォーカスが body に落ちた"


# ------------------------------------------------------------ AC-10-3: 画面に出ている文字の訳し忘れ


BAD_FILES = {
    "syntax.json": b'{"a":\n}',
    "schema.json": b'{"schema_version":"0.1","template":"rlc_series",'
                   b'"plant":{"R":{"value":"x","unit":"ohm"}},"zzz":1,"targets":{},"missing":[]}',
    "template.json": b'{"schema_version":"0.1","template":"nope","plant":{},"targets":{},"missing":[]}',
    "version.json": b'{"schema_version":"9","template":"rlc_series"}',
    "encoding.json": b"\xff\xfe{",
    "type.txt": b"{}",
}


def screens_text(page) -> dict[str, str]:
    """A0 の画面の状態を順に出し、そのときに見えている文字（ツールチップを含む）を集める。"""
    out = {"start": page.inner_text("body")}
    for name, data in BAD_FILES.items():
        open_file(page, name, data)
        page.get_by_test_id("open-error").wait_for()
        page.wait_for_timeout(100)
        out[f"open-error {name}"] = page.get_by_test_id("open-error").inner_text()
    page.get_by_test_id("sample-rlc_series").click()
    page.get_by_test_id("frame").wait_for()
    out["frame"] = page.inner_text("body")
    open_file(page, "syntax.json", BAD_FILES["syntax.json"])
    page.get_by_test_id("notice-row").first.wait_for()
    out["notices"] = page.inner_text("body")
    tips = []
    for el in page.locator("header button, [role=separator], [data-testid=frame] button").all():
        el.focus()
        page.wait_for_timeout(30)
        tip = page.locator("[role=tooltip]")
        if tip.count():
            tips.append(tip.first.inner_text())
    out["tooltips"] = "\n".join(tips)
    page.route("**/api/health", lambda r: r.abort())
    page.get_by_test_id("conn-banner").wait_for(timeout=8000)
    out["disconnected"] = page.inner_text("body")
    page.unroute("**/api/health")
    return out


def test_english_screens_show_no_japanese(page_of):
    texts = screens_text(page_of(lang="en"))
    found = {k: [ln for ln in v.splitlines() if CJK.search(ln) and ln.strip() != "日本語"]
             for k, v in texts.items()}
    assert {k: v for k, v in found.items() if v} == {}


def test_japanese_screens_show_no_untranslated_english(page_of):
    texts = screens_text(page_of(lang="ja"))
    files = set(BAD_FILES) | {"rlc_series.json"}
    found = {}
    for k, v in texts.items():
        for f in files:
            v = v.replace(f, "")
        words = sorted({w for w in re.findall(r"[A-Za-z][A-Za-z_]{2,}", v) if w not in LATIN_OK})
        if words:
            found[k] = words
    assert found == {}


# ------------------------------------------------------------ AC-10-4: 実際の文字と背景のコントラスト


CONTRAST_SCAN = r"""
() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
  const bgOf = (el) => { const layers = [];
    for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; } }
    let base = [255, 255, 255];
    for (const c of layers.reverse()) base = base.map((v, k) => c[k] * c[3] + v * (1 - c[3]));
    return base; };
  const low = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) {
    const n = walk.currentNode; if (!n.textContent.trim()) continue;
    const el = n.parentElement; const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    if (!r.width || !r.height || s.visibility === 'hidden' || el.closest('.visually-hidden')) continue;
    // text-disabled は基準の外（design-system 2.3）
    if (el.closest('[aria-disabled=true], :disabled')) continue;
    let op = 1; for (let e = el; e; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
    const fg = parse(s.color); const bg = bgOf(el); const a = fg[3] * op;
    const mixed = [0, 1, 2].map(k => fg[k] * a + bg[k] * (1 - a));
    const [hi, lo] = [lum(mixed), lum(bg)].sort((x, y) => y - x);
    const ratio = (hi + 0.05) / (lo + 0.05);
    if (ratio < 4.5) low.push(`${n.textContent.trim().slice(0, 30)} ${ratio.toFixed(2)}`);
  }
  return low;
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_rendered_text_contrast_in_every_a0_state(page_of, theme):
    """起動直後、開けなかった、枠、お知らせ（開いた）、メニュー、切断の帯で、文字のコントラストが
    4.5 以上。"""
    page = page_of(theme=theme)
    low = {"start": page.evaluate(CONTRAST_SCAN)}
    open_file(page, "syntax.json", BAD_FILES["syntax.json"])
    page.get_by_test_id("open-error").wait_for()
    page.wait_for_timeout(300)
    low["open-error"] = page.evaluate(CONTRAST_SCAN)
    page.get_by_test_id("sample-rlc_series").click()
    page.get_by_test_id("frame").wait_for()
    open_file(page, "syntax.json", BAD_FILES["syntax.json"])
    page.get_by_test_id("notice-row").first.wait_for()
    page.wait_for_timeout(300)
    low["notices"] = page.evaluate(CONTRAST_SCAN)
    page.get_by_test_id("header-samples").click()
    page.wait_for_timeout(300)
    low["menu"] = page.evaluate(CONTRAST_SCAN)
    page.keyboard.press("Escape")
    page.route("**/api/health", lambda r: r.abort())
    page.get_by_test_id("conn-banner").wait_for(timeout=8000)
    page.wait_for_timeout(300)
    low["disconnected"] = page.evaluate(CONTRAST_SCAN)
    page.unroute("**/api/health")
    assert {k: v for k, v in low.items() if v} == {}


# ------------------------------------------------------------ D8: 固まったアプリ本体


@pytest.mark.skipif(sys.platform == "win32", reason="プロセスを固める（SIGSTOP）のは POSIX だけ")
def test_hung_app_is_noticed_within_five_seconds(browser):
    """接続は受けるが応えない状態（固まった）でも、5 秒以内に帯が出る。

    確かめの周期の中で、固める時機をずらす。"""
    app = App()
    app.start()
    ctx = browser.new_context(locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(app.url)
        page.get_by_test_id("sample-rlc_series").wait_for()
        worst = 0.0
        for delay in (2000, 2700, 3400):
            page.wait_for_timeout(delay)
            os.kill(app.proc.pid, signal.SIGSTOP)
            t0 = time.monotonic()
            page.get_by_test_id("conn-banner").wait_for(state="visible", timeout=15000)
            worst = max(worst, time.monotonic() - t0)
            os.kill(app.proc.pid, signal.SIGCONT)
            page.get_by_test_id("conn-banner").wait_for(state="detached", timeout=15000)
        # 5 秒は D8 の基準そのもの。確かめの周期 2 s ＋ 応答の待ち 2 s の作りで、理屈の上の最悪は約 4 s
        assert worst < 5.0, f"固まってから帯が出るまで {worst:.2f} s"
    finally:
        ctx.close()
        app.stop()
