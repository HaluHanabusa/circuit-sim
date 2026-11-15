"""A0 の独立検証（verifier、2回目）: 実際のブラウザでの確認のうち、実装の試験と1回目の検証が見ていない所。

記録は docs/verification/A0-verifier-2.md。
- D-050: ポートを変えて起動し直し、保存の無いまっさらなブラウザで開いても、言語・テーマ・仕切りが戻る。
  画面の本体のスクリプトが届く前から暗のまま（AC-10-7）
- D-056: ブラウザ（BROWSER）に渡された転送用の HTML から開くと、番号の無い / に移り、履歴・Referer・保存領域に
  番号が残らず、HTML は消える。転送用の HTML と中継のページは、暗を選んでいれば暗い地で描かれる
- 別のポートのページ（127.0.0.1 と localhost）から、実際の Chrome で書き込みを試しても、作業の状態が変わらない
  （クッキーは SameSite=Strict でもポートを区別しないので、同じ PC の別のポートのページには付く。D-053）
- AC-1-1: 長いファイル名でも、3つの大きさ（1280×720、1920×1080、150%）と日英で、拡張子とテンプレート名が残り、
  ヘッダーが1行で重ならない（1回目の検証の試験は 1280×720 だけ）
- 10.4: 言語を切り替えると、読み上げ用の領域（aria-live）の文も切り替わる（軽微。製品の不具合なので xfail）

Python の playwright と Chromium 系のブラウザがある環境でだけ動く。無ければ、確かめなかったことを
スキップの理由として残す（POの Windows の PC など）。ブラウザは CIRCUIT_SIM_CHROME で指定できる。
製品側の不具合で落ちる試験には xfail(strict=True) を付ける。直ったら XPASS で落ちるので、印を外す。
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = (ROOT / "circuit_sim" / "app" / "samples" / "rlc_series.json").read_bytes()
COOKIE = "circuit_sim_key"
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
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
RECORDER = "import json, os, sys\njson.dump({'argv': sys.argv[1:]}, open(os.environ['A0V2_RECORD'], 'w'))\n"

sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright が無いので、実際のブラウザでの確認（A0 の2回目の検証の追加分）をしなかった",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """アプリ本体を、決めた設定の場所で起動する。

    record を渡すと、BROWSER の記録係が転送用の HTML の場所を受け取る。"""

    def __init__(self, config: Path, port: int | None = None, record: Path | None = None) -> None:
        self.config = config
        self.port = port or free_port()
        self.lines: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        env = {
            **os.environ,
            "CIRCUIT_SIM_CONFIG_DIR": str(config),
            "LANG": "ja_JP.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        args = [sys.executable, "-m", "circuit_sim.app", "--port", str(self.port)]
        if record is None:
            args.append("--no-browser")
        else:
            recorder = record.with_suffix(".py")
            recorder.write_text(RECORDER, encoding="utf-8")
            exe = sys.executable.replace("\\", "/")
            env["BROWSER"] = f"{shlex.quote(exe)} {shlex.quote(str(recorder).replace(chr(92), '/'))} %s"
            env["A0V2_RECORD"] = str(record)
        self.proc = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.url, self.number = self.next_url()
        self.base = f"http://127.0.0.1:{self.port}"
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

    def next_url(self) -> tuple[str, str]:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            assert line is not None, "".join(self.lines)
            m = URL_RE.search(line)
            if m and line.lstrip().startswith(("circuit-sim", "新しい", "New")):
                return m.group(0), m.group(2)
        raise AssertionError("起動の URL が出ない: " + "".join(self.lines))

    def login_url(self) -> str:
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()
        return self.next_url()[0]

    @property
    def token(self) -> str:
        return (self.config / "token").read_text(encoding="ascii").strip()

    def api(self, path: str, body: dict | None = None, method: str = "GET") -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Cookie": f"{COOKIE}={self.token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

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
                    "Chromium 系のブラウザが無いので、実際のブラウザでの確認（A0 の2回目の検証の追加分）を"
                    "しなかった"
                )
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    running = App(tmp_path_factory.mktemp("a0v2-browser-config"))
    yield running
    running.stop()


def wait_for_record(record: Path) -> list[str]:
    deadline = time.monotonic() + WAIT
    while not record.exists() or not record.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "ブラウザ（BROWSER）が呼ばれない"
        time.sleep(0.05)
    time.sleep(0.1)
    return json.loads(record.read_text(encoding="utf-8"))["argv"]


def file_path(uri: str) -> Path:
    path = unquote(urlparse(uri).path)
    return Path(path.lstrip("/") if os.name == "nt" else path)


STATE_JS = """() => { const r = document.documentElement;
  const spec = document.querySelector('[data-testid=spec-panel]');
  const sch = document.querySelector('[data-testid=schematic]');
  return {lang: r.lang, theme: r.dataset.theme,
          specW: spec && Math.round(spec.getBoundingClientRect().width),
          schemH: sch && Math.round(sch.getBoundingClientRect().height)}; }"""


# ------------------------------------------------------------------ D-050: ポートを変えても戻る


def test_preferences_come_back_on_another_port_in_a_fresh_browser(browser, tmp_path):
    config = tmp_path / "config"
    first = App(config)
    try:
        ctx = browser.new_context(
            locale="ja-JP", color_scheme="light", viewport={"width": 1280, "height": 720}
        )
        page = ctx.new_page()
        page.goto(first.login_url())
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        before = page.evaluate(STATE_JS)
        assert before["lang"] == "ja" and before["theme"] == "light"
        page.get_by_test_id("toggle-lang").get_by_text("English", exact=True).click()
        page.get_by_test_id("toggle-theme").get_by_text("Dark", exact=True).click()
        page.get_by_test_id("splitter-spec").focus()
        for _ in range(4):
            page.keyboard.press("ArrowRight")
        page.get_by_test_id("splitter-schematic").focus()
        for _ in range(3):
            page.keyboard.press("ArrowDown")
        page.wait_for_timeout(1000)  # まとめて送るまでの時間（300 ms）を待つ
        changed = page.evaluate(STATE_JS)
        assert changed["specW"] != before["specW"] and changed["schemH"] != before["schemH"]
        ctx.close()
    finally:
        first.stop()
    second = App(config, port=free_port())
    try:
        # まっさらなブラウザ（ポートが違うので、ブラウザの保存はどのみち別）。OS は明、言語は日本語
        ctx = browser.new_context(
            locale="ja-JP", color_scheme="light", viewport={"width": 1280, "height": 720}
        )
        page = ctx.new_page()
        page.goto(second.login_url())
        page.get_by_test_id("start").wait_for()
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.wait_for_timeout(300)
        after = page.evaluate(STATE_JS)
        assert after == changed, (changed, after)
        ctx.close()
        # 画面の本体のスクリプトが届かない間も、暗で、英語（設定ファイルの好みを最初の描画の前に当てる）
        ctx = browser.new_context(locale="ja-JP", color_scheme="light")
        page = ctx.new_page()
        page.route("**/assets/*.js", lambda route: route.abort())
        page.goto(second.login_url())
        page.wait_for_timeout(500)
        got = page.evaluate(
            "[document.documentElement.dataset.theme, document.documentElement.lang,"
            " getComputedStyle(document.body).backgroundColor, location.pathname + location.search]"
        )
        assert got == ["dark", "en", "rgb(11, 13, 16)", "/"], got
        ctx.close()
    finally:
        second.stop()


# ------------------------------------------------------------------ D-056: 転送用の HTML からの流れ


def test_launch_page_flow_leaves_no_number_behind(browser, tmp_path):
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "settings.json").write_text(json.dumps({"theme": "dark", "lang": "en"}), encoding="utf-8")
    if os.name == "posix":
        os.chmod(config / "settings.json", 0o600)
    record = tmp_path / "record.json"
    app = App(config, record=record)
    try:
        argv = wait_for_record(record)
        assert len(argv) == 1 and argv[0].startswith("file:") and app.number not in argv[0], argv
        launch = file_path(argv[0])
        launch_html = launch.read_text(encoding="utf-8")
        ctx = browser.new_context(
            color_scheme="light", viewport={"width": 400, "height": 300}, java_script_enabled=False
        )
        page = ctx.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(launch_html.replace('http-equiv="refresh"', 'http-equiv="x-none"'))
        # 暗の地で描かれる（color-scheme が暗だと、既定の文字色 CanvasText が白になる。明の OS でも）
        assert page.evaluate("getComputedStyle(document.body).color") == "rgb(255, 255, 255)"
        ctx.close()
        # 本当の流れ（明の OS、日本語のブラウザ）
        ctx = browser.new_context(
            locale="ja-JP", color_scheme="light", viewport={"width": 1280, "height": 720}
        )
        page = ctx.new_page()
        requests = []
        page.on("request", lambda r: requests.append(r))
        page.goto(argv[0])
        page.get_by_test_id("start").wait_for()
        page.wait_for_timeout(300)
        assert page.url == app.base + "/"
        referers = [r.all_headers().get("referer") or "" for r in requests]
        assert not [ref for ref in referers if "k=" in ref], referers
        history = ctx.new_cdp_session(page).send("Page.getNavigationHistory")["entries"]
        assert not [e["url"] for e in history if "k=" in e["url"] or e["url"].startswith("file:")], history
        stored = page.evaluate("JSON.stringify([{...localStorage}, {...sessionStorage}, document.referrer])")
        assert app.number not in stored and app.token not in stored, stored
        assert page.evaluate("[document.documentElement.dataset.theme, document.documentElement.lang]") == [
            "dark",
            "en",
        ]
        deadline = time.monotonic() + 10
        while launch.exists():
            assert time.monotonic() < deadline, "使われた番号の転送用の HTML が残る"
            time.sleep(0.05)
        ctx.close()
        # 中継のページ（ほかのページから /?k= に来たときに出る）も、暗の地で描かれる
        req = urllib.request.Request(
            app.login_url(), headers={"Sec-Fetch-Site": "cross-site", "Accept": "text/html"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            hop = resp.read().decode("utf-8")
        assert 'http-equiv="refresh" content="0;url=/"' in hop, hop
        ctx = browser.new_context(color_scheme="light", java_script_enabled=False)
        page = ctx.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(hop.replace('http-equiv="refresh"', 'http-equiv="x-none"'))
        assert page.evaluate("getComputedStyle(document.body).color") == "rgb(255, 255, 255)", hop
        ctx.close()
    finally:
        app.stop()


# ------------------------------------------------------------------ 別のポートのページから


ATTACKS = {
    "fetch_blob": (
        "fetch(T+'/api/open',{method:'POST',mode:'no-cors',credentials:'include',body:new Blob([E])})"
    ),
    "fetch_text": (
        "fetch(T+'/api/open',{method:'POST',mode:'no-cors',credentials:'include',"
        "headers:{'Content-Type':'text/plain'},body:E})"
    ),
    "fetch_put_cors": (
        "fetch(T+'/api/session',{method:'PUT',credentials:'include',"
        "headers:{'Content-Type':'application/json'},body:'{\"doc\":null,\"dirty\":false}'}).catch(e=>0)"
    ),
    "beacon": "navigator.sendBeacon(T+'/api/open', E)",
    "form_textplain_top": (
        "(()=>{const f=document.createElement('form');f.method='POST';f.action=T+'/api/open';"
        "f.enctype='text/plain';const i=document.createElement('input');i.name=E.slice(0,-1)+',\"x\":\"';"
        "i.value='\"}';f.appendChild(i);document.body.appendChild(f);f.submit();return 1})()"
    ),
}
PAGE = b"<!doctype html><html><head><meta charset=utf-8></head><body></body></html>"
NOREF = (
    b"<!doctype html><html><head><meta charset=utf-8><meta name=referrer content=no-referrer></head>"
    b"<body></body></html>"
)


class _Attacker(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = NOREF if "noref" in self.path else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_pages_on_other_ports_cannot_write(browser, app):
    app.api("/api/session", {"doc": None, "dirty": False}, "PUT")
    port = free_port()
    attacker = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Attacker)
    threading.Thread(target=attacker.serve_forever, daemon=True).start()
    ctx = browser.new_context(locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(app.login_url())
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        assert app.api("/api/session")["doc"]["file"] == "rlc_series.json"
        assert any(c["name"] == COOKIE for c in ctx.cookies())
        evil = json.dumps({"file": "evil.json", "text": SAMPLE.decode("utf-8")})
        changed = []
        for target in (app.base, f"http://localhost:{app.port}"):
            for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                for path in ("/a", "/noref"):
                    for name, js in ATTACKS.items():
                        other = ctx.new_page()
                        other.goto(origin + path)
                        try:
                            other.evaluate(
                                f"(async()=>{{const T={json.dumps(target)};const E={json.dumps(evil)};"
                                f"return String(await ({js}))}})()"
                            )
                        except Exception:  # noqa: BLE001  ページ全体が移るもの
                            pass
                        other.wait_for_timeout(300)
                        doc = app.api("/api/session")["doc"]
                        if doc is None or doc["file"] != "rlc_series.json":
                            changed.append((origin + path, target, name))
                            app.api("/api/open", {"sample": "rlc_series"}, "POST")
                        other.close()
        assert changed == []
        # 対照: このページ自身からの書き込みは通る
        status = page.evaluate(
            "fetch('/api/session',{method:'PUT',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({doc:null,dirty:false})}).then(r=>r.status)"
        )
        assert status == 200 and app.api("/api/session")["doc"] is None
    finally:
        ctx.close()
        attacker.shutdown()


# ------------------------------------------------------------------ AC-1-1: 長いファイル名（3つの大きさ）


NAMES = [
    "buck_converter_rev12_final.json",
    "motor_drive_inverter_rev3_review.json",
    "very_long_design_review_file_name_for_the_buck_converter_project_2026_11_16_final_v3.json",
    "降圧コンバータ_設計レビュー_第3版_最終確認用_修正済み.json",
    "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q.r.json",
    "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW.json",
]
TEMPLATE = {"ja": "直列RLC", "en": "Series RLC"}
HEADER_JS = """() => { const h = document.querySelector('[data-testid=header]');
  const hb = h.getBoundingClientRect();
  const kids = [...h.children].map(e => ({n: e.dataset.testid || e.className, b: e.getBoundingClientRect()}))
    .filter(k => k.b.width > 0);
  const overlap = [];
  for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
    const a = kids[i].b, b = kids[j].b;
    if (a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5)
      overlap.push(kids[i].n + ' x ' + kids[j].n); }
  const name = document.querySelector('[data-testid=header-file-name]');
  const tpl = document.querySelector('[data-testid=header-file-template]');
  const file = document.querySelector('[data-testid=header-file]').getBoundingClientRect();
  return {height: Math.round(hb.height),
          rows: new Set(kids.map(k => Math.round(k.b.top + k.b.height / 2))).size,
          overlap, name: name.textContent, nameCut: name.scrollWidth > name.clientWidth + 1,
          tpl: tpl.textContent, tplCut: tpl.scrollWidth > tpl.clientWidth + 1, fileWidth: file.width,
          outside: kids.filter(k => k.b.right > hb.right + 0.5).map(k => k.n)}; }"""


@pytest.mark.parametrize(
    "size", [(1280, 720, 1), (1920, 1080, 1), (1280, 720, 1.5)], ids=["1280", "1920", "150pct"]
)
@pytest.mark.parametrize("lang", ["ja", "en"])
def test_long_file_names_keep_extension_in_every_size(browser, app, size, lang):
    width, height, scale = size
    app.api("/api/session", {"doc": None, "dirty": False}, "PUT")
    app.api("/api/settings", {"settings": {"lang": None, "theme": None}}, "PUT")
    ctx = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
        locale="ja-JP" if lang == "ja" else "en-US",
    )
    try:
        page = ctx.new_page()
        page.goto(app.login_url())
        page.get_by_test_id("sample-rlc_series").wait_for()
        assert page.evaluate("document.documentElement.lang") == lang
        bad = []
        for name in NAMES:
            page.get_by_test_id("file-input").set_input_files(
                files=[{"name": name, "mimeType": "application/json", "buffer": SAMPLE}]
            )
            page.wait_for_function(
                "n => document.querySelector('[data-testid=header-file-name]') && "
                "document.querySelector('[data-testid=header-file-name]').textContent.slice(0, 3)"
                " === n.slice(0, 3)",
                arg=name,
            )
            page.wait_for_timeout(150)
            m = page.evaluate(HEADER_JS)
            ok = (
                m["name"].endswith(".json")
                and m["name"][:3] == name[:3]
                and not m["nameCut"]
                and m["tpl"].strip().endswith(TEMPLATE[lang])
                and not m["tplCut"]
                and m["height"] == 48
                and m["rows"] == 1
                and not m["overlap"]
                and not m["outside"]
                and m["fileWidth"] <= 320.5
            )
            if not ok:
                bad.append((name, m))
        assert bad == []
    finally:
        ctx.close()


# ------------------------------------------------------------------ 10.4: 読み上げ用の領域の言語


def test_hidden_announcement_follows_the_language(browser, app):
    app.api("/api/session", {"doc": None, "dirty": False}, "PUT")
    app.api("/api/settings", {"settings": {"lang": None}}, "PUT")
    ctx = browser.new_context(locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(app.login_url())
        page.get_by_test_id("sample-rlc_series").click()
        page.get_by_test_id("frame").wait_for()
        page.get_by_test_id("file-input").set_input_files(
            files=[{"name": "bad.json", "mimeType": "application/json", "buffer": b"{"}]
        )
        page.get_by_test_id("notice-row").first.wait_for()
        page.wait_for_timeout(300)
        assert page.get_by_test_id("announce").text_content().strip()  # 読み上げに伝えている
        page.get_by_test_id("toggle-lang").get_by_text("English", exact=True).click()
        page.wait_for_timeout(300)
        assert page.evaluate("document.documentElement.lang") == "en"
        hidden = page.get_by_test_id("announce").text_content()
        assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", hidden), hidden
    finally:
        ctx.close()
        app.api("/api/settings", {"settings": {"lang": None}}, "PUT")
