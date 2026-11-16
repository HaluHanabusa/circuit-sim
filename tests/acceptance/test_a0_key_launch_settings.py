"""A0 の独立検証（verifier、2回目）: 合言葉と起動の番号、別サイトの書き込み、設定。

決定: D-050（設定）、D-051・D-053（安全と合言葉）、D-056（起動の番号と転送用の HTML）。

記録は docs/verification/A0-verifier-2.md。ブラウザも Node.js も要らない（POの Windows の PC でも動く。
POSIX の権限と、別の利用者から読めるかの確かめは、POSIX だけ。別の利用者は root で流したときだけ試す）。
アプリ本体は別のプロセス（python -m circuit_sim.app、circuit-sim と同じ）で、試験ごとの設定の場所で起動し、
HTTP で直接突く。製品のコードは import しない（期待値を実装から作らないため）。

- /api/ は、合言葉のクッキーが無い・違う・欠けた・大文字にしたものでは 401（パスの書き方を変えても）
- 起動の番号は1回だけ使え、クッキーの値は設定の場所の合言葉。知らない番号ではクッキーが付かない。
  まだ使われていない番号は 8 個まで（9 個目を出すと、いちばん古いものが使えなくなる）
- 端末の出力に合言葉が出ない。ブラウザ（BROWSER）に渡す引数に、番号も合言葉も載らない。渡すのは設定の場所の
  中の転送用の HTML で、使われると消える
- 別の利用者（nobody）は、合言葉・転送用の HTML・設定ファイルを読めない（POSIX、root のときだけ）
- どの応答も Referrer-Policy: no-referrer
- 書き込みの要求: 自分の Origin 以外（別のポート、localhost と 127.0.0.1 の取り違え、null、空、https、
  後ろに足したもの）と、Origin が無く Sec-Fetch-Site が same-origin でないものは 403。
  JSON 以外の Content-Type は 415。
  どれも作業の状態を変えない（対照: 自分の Origin なら 200 で変わる）
- 画面の好みは、ポートを変えて起動し直しても戻る（設定ファイル。合言葉を持つページにだけ data-prefs を書く）
- 壊れた設定ファイルでも起動し、既定で開き、端末で知らせる。書き直すときに元を settings.json.broken に控える
- 設定の場所に書けないときは、理由を出して終了コード 2

時間の基準は無い（待つ時間の上限は、混んだ機械でも落ちないように長めにとった。合否には使わない）。

5回目の検証（docs/verification/A0-verifier-5.md）で、期待している中身を変えずに直した所:
- ブラウザを開く起動は、a0_fake_browser を通す（偽のブラウザが失敗しても、本物のブラウザへ進まない）
- 起動したアプリ本体は、途中で失敗しても必ず止める（App の準備の途中、試験の途中）
- 壊れた設定ファイルの試験に、短い ID を付けた（pytest は ID を環境変数に入れる。Windows は 32767 文字まで）
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import a0_fake_browser
import pytest

ROOT = Path(__file__).resolve().parents[2]
COOKIE = "circuit_sim_key"
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
SAMPLE = (ROOT / "circuit_sim" / "app" / "samples" / "rlc_series.json").read_text(encoding="utf-8")
EVIL = json.dumps({"file": "evil.json", "text": SAMPLE}).encode("utf-8")
OPEN_SAMPLE = json.dumps({"sample": "rlc_series"}).encode("utf-8")
POSIX = os.name == "posix"
WAIT = 30.0  # 起動を待つ上限（混んだ機械で落ちないように長め）

# BROWSER の代わりに置く記録係: 渡された引数を JSON で書いて、すぐに 0 で終わる（ブラウザは開かない）
RECORDER = (
    "import json, os, sys\n"
    "json.dump({'argv': sys.argv[1:], 'pid': os.getpid()}, open(os.environ['A0V2_RECORD'], 'w'))\n"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """アプリ本体を、この試験だけの設定の場所で起動する。端末の出力を読み続け、Enter を送れる。"""

    def __init__(
        self,
        config: Path,
        *,
        port: int | None = None,
        browser: Path | None = None,
        record: Path | None = None,
    ) -> None:
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
        if browser is None:
            args = [sys.executable, "-m", "circuit_sim.app", "--port", str(self.port), "--no-browser"]
        else:
            # 偽のブラウザだけで起動する（偽が失敗しても、本物のブラウザへ進まない。a0_fake_browser）
            args = a0_fake_browser.app_argv("--port", str(self.port))
            env["BROWSER"] = a0_fake_browser.command(browser)
            env["A0V2_RECORD"] = str(record)
        self.proc = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE
        )
        try:
            threading.Thread(target=self._pump, daemon=True).start()
            self.url, self.number = self.next_url()
            deadline = time.monotonic() + WAIT
            while True:
                try:
                    socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                    break
                except OSError:
                    assert self.proc.poll() is None, "アプリ本体が起動しない: " + self.out
                    assert time.monotonic() < deadline, "アプリ本体が応えない"
                    time.sleep(0.05)
        except BaseException:
            # 準備の途中で失敗しても、起動したアプリ本体を残さない（呼んだ側の finally には届かない）
            self.stop()
            raise

    @property
    def out(self) -> str:
        return "".join(self.lines)

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
            assert line is not None, "アプリ本体が終わった: " + self.out
            m = URL_RE.search(line)
            if m and line.lstrip().startswith(("circuit-sim", "新しい", "New")):
                return m.group(0), m.group(2)
        raise AssertionError("起動の URL が出ない: " + self.out)

    def enter(self) -> str:
        """端末で Enter を押し、新しい番号を受け取る。"""
        self.proc.stdin.write(b"\n")
        self.proc.stdin.flush()
        return self.next_url()[1]

    def stop(self) -> None:
        if self.proc.poll() is None:
            if POSIX:
                self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=20)
                    return
                except subprocess.TimeoutExpired:
                    pass
            self.proc.kill()
            self.proc.wait(timeout=20)

    @property
    def token(self) -> str:
        return (self.config / "token").read_text(encoding="ascii").strip()


def http(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    host: str | None = None,
) -> tuple[int, str, bytes]:
    """生の HTTP 要求（見出しを好きに組む）。（状態の番号, 見出し, 本文）。"""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    if body or method not in ("GET", "HEAD", "OPTIONS"):
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    text = head.decode("latin-1")
    return int(text.split(" ", 2)[1]), text, rest


def set_cookie(head: str) -> str | None:
    m = re.search(r"(?im)^set-cookie:\s*([^\r\n]+)", head)
    return m.group(1) if m else None


def with_key(app: App, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={app.token}", **(extra or {})}


def open_file(app: App) -> str | None:
    st, _, body = http(app.port, "GET", "/api/session", with_key(app))
    assert st == 200
    doc = json.loads(body)["doc"]
    return doc and doc["file"]


@pytest.fixture
def app(tmp_path):
    running = App(tmp_path / "config")
    yield running
    running.stop()


# ------------------------------------------------------------------ 合言葉


API = [
    ("GET", "/api/health"),
    ("GET", "/api/samples"),
    ("GET", "/api/session"),
    ("GET", "/api/settings"),
    ("HEAD", "/api/session"),
    ("OPTIONS", "/api/session"),
    ("GET", "/api/nope"),
    ("GET", "/api"),
    ("GET", "/api/"),
    ("GET", "/api/session?x=1"),
    ("GET", "/%61pi/session"),
    ("GET", "/api/%73ession"),
    ("GET", "/api//session"),
    ("GET", "/api/./session"),
    ("GET", "/api/../api/session"),
    ("GET", "//api/session"),
    ("GET", "/./api/session"),
    ("GET", "/API/session"),
    ("GET", "/x/../api/session"),
]
WRITES = [
    ("POST", "/api/open"),
    ("PUT", "/api/session"),
    ("PUT", "/api/settings"),
    ("DELETE", "/api/session"),
    ("PATCH", "/api/session"),
]


def test_api_refuses_requests_without_the_key(app):
    token = app.token
    wrong = [
        None,
        f"{COOKIE}=",
        f"{COOKIE}={token[:-1]}",
        f"{COOKIE}={token}x",
        f"{COOKIE}={token.upper()}",
        f"{COOKIE}={app.number}",
        f"other={token}",
        f"{COOKIE}=あ",
    ]
    leaked = []
    for cookie in wrong:
        headers = {"Cookie": cookie} if cookie else {}
        for method, path in API:
            st, _, body = http(app.port, method, path, headers)
            if st not in (401, 404) or (st == 200 and body):
                leaked.append((cookie, method, path, st))
            if path.startswith("/api") and st != 401:
                leaked.append((cookie, method, path, st, "401 でない"))
        for method, path in WRITES:
            st, _, _ = http(
                app.port,
                method,
                path,
                {**headers, "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{app.port}"},
                OPEN_SAMPLE,
            )
            if st != 401:
                leaked.append((cookie, method, path, st))
    assert leaked == []
    # 対照: 合言葉があれば読める
    for method, path in API[:4]:
        st, _, body = http(app.port, method, path, with_key(app))
        assert st == 200 and body, (method, path, st)


def test_launch_number_is_used_once_and_only_eight_are_kept(app):
    st, head, _ = http(app.port, "GET", f"/?k={app.number}")
    cookie = set_cookie(head)
    assert cookie, head
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert parts[0] == f"{COOKIE}={app.token}".lower()
    assert "httponly" in parts and "samesite=strict" in parts and "path=/" in parts, cookie
    # 2回目は使えない
    st, head, _ = http(app.port, "GET", f"/?k={app.number}")
    assert set_cookie(head) is None
    # 知らない番号・形の違う番号
    for bad in [
        "",
        "x",
        app.number[:-1],
        app.number + "A",
        app.token,
        "%00",
        "a" * 4000,
        "../../",
        "%3Cscript%3E",
    ]:
        st, head, _ = http(app.port, "GET", f"/?k={bad}")
        assert set_cookie(head) is None, bad
    # 9 個出すと、いちばん古いものが使えなくなる（使われていない番号は 8 個まで。D-056）
    numbers = [app.enter() for _ in range(9)]
    assert len(set(numbers)) == 9
    st, head, _ = http(app.port, "GET", f"/?k={numbers[0]}")
    assert set_cookie(head) is None, "9 個目を出した後も、いちばん古い番号が使える"
    st, head, _ = http(app.port, "GET", f"/?k={numbers[1]}")
    assert set_cookie(head), "8 個の中の番号が使えない"
    # 端末の出力に合言葉が出ない
    assert app.token not in app.out


def test_every_response_says_no_referrer(app):
    number = app.enter()
    checks = [
        ("/", {}),
        (f"/?k={number}", {"Sec-Fetch-Site": "cross-site", "Accept": "text/html"}),
        (f"/?k={app.enter()}", {"Sec-Fetch-Site": "none"}),
        ("/boot.js", {}),
        ("/api/health", with_key(app)),
        ("/api/health", {}),
        ("/nope", {}),
    ]
    st, _, page = http(app.port, "GET", "/")
    assets = re.findall(rb"(/assets/[A-Za-z0-9._-]+)", page)
    assert assets, page[:300]
    checks += [(a.decode(), {}) for a in assets]
    missing = []
    for path, headers in checks:
        st, head, _ = http(app.port, "GET", path, headers)
        if not re.search(r"(?im)^referrer-policy:\s*no-referrer\s*$", head):
            missing.append((path, st))
    assert missing == []


# ------------------------------------------------------------------ ブラウザに渡すもの


def test_browser_gets_only_the_location_of_a_private_page(tmp_path):
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER, encoding="utf-8")
    record = tmp_path / "record.json"
    config = tmp_path / "config"
    app = App(config, browser=recorder, record=record)
    try:
        deadline = time.monotonic() + WAIT
        while not record.exists() or not record.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline, (
                "ブラウザ（BROWSER）が呼ばれない" + a0_fake_browser.problem() + ": " + app.out
            )
            time.sleep(0.05)
        time.sleep(0.1)
        assert a0_fake_browser.read_notes() == [], "偽のブラウザが失敗した、または本物の経路が呼ばれた"
        argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
        joined = " ".join(argv)
        assert app.number not in joined and app.token not in joined, argv
        assert len(argv) == 1 and argv[0].startswith("file:"), argv
        from urllib.parse import unquote, urlparse  # noqa: PLC0415

        page_path = Path(
            unquote(urlparse(argv[0]).path.lstrip("/") if os.name == "nt" else urlparse(argv[0]).path)
        )
        assert page_path.parent.resolve() == config.resolve(), "転送用の HTML が設定の場所の中に無い"
        html = page_path.read_text(encoding="utf-8")
        assert f"?k={app.number}" in html and app.token not in html
        assert 'name="referrer" content="no-referrer"' in html
        if POSIX:
            assert stat.S_IMODE(page_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(config.stat().st_mode) == 0o700
            assert stat.S_IMODE((config / "token").stat().st_mode) == 0o600
        # ブラウザと同じに使うと、HTML は消える
        st, head, _ = http(
            app.port, "GET", f"/?k={app.number}", {"Sec-Fetch-Site": "cross-site", "Accept": "text/html"}
        )
        assert set_cookie(head)
        deadline = time.monotonic() + 10
        while page_path.exists():
            assert time.monotonic() < deadline, "使われた番号の転送用の HTML が残る"
            time.sleep(0.05)
        assert app.token not in app.out
    finally:
        app.stop()


@pytest.mark.skipif(
    not POSIX, reason="止めたときに消えることの確かめは POSIX だけ（Ctrl+C と SIGTERM を送れる）"
)
@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM"])
def test_unused_launch_page_is_removed_when_stopped(tmp_path, sig):
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER, encoding="utf-8")
    record = tmp_path / "record.json"
    config = tmp_path / "config"
    app = App(config, browser=recorder, record=record)
    try:
        deadline = time.monotonic() + WAIT
        while not record.exists() or not record.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline, "ブラウザ（BROWSER）が呼ばれない" + a0_fake_browser.problem()
            time.sleep(0.05)
        time.sleep(0.1)
        pages = list(config.glob("*.html"))
        assert len(pages) == 1
        app.proc.send_signal(getattr(signal, sig))
        app.proc.wait(timeout=20)
        assert list(config.glob("*.html")) == [], "止めた後も、使われなかった番号の転送用の HTML が残る"
    finally:
        app.stop()  # 途中で失敗しても、待ち時間が切れても、起動したアプリ本体を残さない


@pytest.mark.skipif(
    not POSIX or os.geteuid() != 0,
    reason="別の利用者から読めないことの確かめは、POSIX で root のときだけ（nobody になって読む）",
)
def test_another_user_cannot_read_the_key_launch_page_or_settings(tmp_path):
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER, encoding="utf-8")
    record = tmp_path / "record.json"
    # pytest の一時フォルダは本人だけが通れる（0700）ので、ほかの人も通れる場所に置く（守るのは設定の場所）
    shared = Path(tempfile.mkdtemp(prefix="a0v2-shared-"))
    shared.chmod(0o755)
    (shared / "open.txt").write_text("ok", encoding="utf-8")
    (shared / "open.txt").chmod(0o644)
    config = shared / "config"
    app = App(config, browser=recorder, record=record)
    try:
        st, _, _ = http(
            app.port,
            "PUT",
            "/api/settings",
            with_key(app, {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{app.port}"}),
            json.dumps({"settings": {"theme": "dark"}}).encode(),
        )
        assert st == 200
        deadline = time.monotonic() + WAIT
        while not record.exists() or not record.read_text(encoding="utf-8"):
            assert time.monotonic() < deadline, "ブラウザ（BROWSER）が呼ばれない" + a0_fake_browser.problem()
            time.sleep(0.05)
        time.sleep(0.1)
        targets = [config / "token", config / "settings.json", *config.glob("*.html")]
        assert len(targets) == 3
        probe = (
            "import os, sys, json\nres = {}\n"
            "for p in sys.argv[1:]:\n"
            "    try:\n"
            "        res[p] = open(p, 'rb').read()[:8].hex() if os.path.isfile(p) else os.listdir(p)\n"
            "    except OSError as e:\n"
            "        res[p] = 'denied:' + type(e).__name__\n"
            "print(json.dumps(res))\n"
        )
        r = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(shared / "open.txt"), str(config), *map(str, targets)],
            user=65534,
            group=65534,
            extra_groups=[],
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/",
        )
        got = json.loads(r.stdout)
        # 対照: 同じ場所の、誰でも読めるファイルは nobody にも読める（場所まで届いている）
        assert got.pop(str(shared / "open.txt")) == b"ok".hex(), r.stdout
        assert all(str(v).startswith("denied:") for v in got.values()), got
        # 合言葉なしで API を読めない
        st, _, _ = http(app.port, "GET", "/api/session")
        assert st == 401
    finally:
        app.stop()
        shutil.rmtree(shared, ignore_errors=True)


# ------------------------------------------------------------------ 別サイトの書き込み


def test_writes_from_other_origins_are_refused(app):
    own = f"http://127.0.0.1:{app.port}"
    refused = [
        {"Origin": f"http://localhost:{app.port}"},
        {"Origin": f"http://127.0.0.1:{app.port + 1}"},
        {"Origin": "http://127.0.0.1"},
        {"Origin": f"https://127.0.0.1:{app.port}"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": "http://evil.example"},
        {"Origin": own + ".evil.example"},
        {"Origin": own + "/x"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "none"},
    ]
    st, _, _ = http(
        app.port,
        "POST",
        "/api/open",
        with_key(app, {"Content-Type": "application/json", "Origin": own}),
        OPEN_SAMPLE,
    )
    assert st == 200 and open_file(app) == "rlc_series.json"
    wrong = []
    for extra in refused:
        for method, path, body in [
            ("POST", "/api/open", EVIL),
            ("PUT", "/api/session", json.dumps({"doc": None, "dirty": False}).encode()),
            ("PUT", "/api/settings", json.dumps({"settings": {"theme": "dark"}}).encode()),
        ]:
            st, _, _ = http(
                app.port, method, path, with_key(app, {"Content-Type": "application/json", **extra}), body
            )
            if st != 403:
                wrong.append((extra, method, path, st))
    assert wrong == []
    assert open_file(app) == "rlc_series.json"
    st, _, body = http(app.port, "GET", "/api/settings", with_key(app))
    assert json.loads(body)["settings"] == {}
    # 対照: 自分の Origin（大文字小文字は問わない）と、Origin も Sec-Fetch-Site も無いもの
    # （ブラウザでないもの。合言葉は要る）は通る
    for extra in ({"Origin": own.upper()}, {"Sec-Fetch-Site": "same-origin"}, {}):
        st, _, _ = http(
            app.port, "POST", "/api/open", with_key(app, {"Content-Type": "application/json", **extra}), EVIL
        )
        assert st == 200 and open_file(app) == "evil.json", extra
        http(
            app.port,
            "POST",
            "/api/open",
            with_key(app, {"Content-Type": "application/json", "Origin": own}),
            OPEN_SAMPLE,
        )


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/json-patch+json",
        "application/vnd.api+json",
        "text/json",
        "application/jsonx",
        "application/json, text/plain",
        "text/plain; application/json",
    ],
    ids=repr,
)
def test_writes_must_be_json(app, content_type):
    own = f"http://127.0.0.1:{app.port}"
    headers = with_key(app, {"Origin": own})
    if content_type is not None:
        headers["Content-Type"] = content_type
    for method, path, body in [
        ("POST", "/api/open", EVIL),
        ("PUT", "/api/settings", json.dumps({"settings": {"theme": "dark"}}).encode()),
    ]:
        st, _, _ = http(app.port, method, path, headers, body)
        assert st == 415, (method, path, st)
    assert open_file(app) is None
    st, _, body = http(app.port, "GET", "/api/settings", with_key(app))
    assert json.loads(body)["settings"] == {}
    # 対照: application/json（引数つき・大文字）は通る
    for ct in ("application/json; charset=utf-8", "Application/JSON"):
        st, _, _ = http(
            app.port, "POST", "/api/open", with_key(app, {"Origin": own, "Content-Type": ct}), EVIL
        )
        assert st == 200


# ------------------------------------------------------------------ 設定（D-050）


PREFS = {
    "lang": "en",
    "theme": "dark",
    "layout.specWidth": "424",
    "layout.schemHeight": "228",
    "layout.specCollapsed": "0",
    "layout.schemCollapsed": "1",
    "layout.noticesOpen": "1",
}


def test_preferences_follow_to_another_port(tmp_path):
    config = tmp_path / "config"
    first = App(config)
    try:
        own = f"http://127.0.0.1:{first.port}"
        st, _, _ = http(
            first.port,
            "PUT",
            "/api/settings",
            with_key(first, {"Content-Type": "application/json", "Origin": own}),
            json.dumps({"settings": PREFS}).encode(),
        )
        assert st == 200
    finally:
        first.stop()
    second = App(config, port=free_port())
    try:
        assert second.port != first.port
        st, _, body = http(second.port, "GET", "/api/settings", with_key(second))
        assert json.loads(body)["settings"] == PREFS
        # 合言葉を持つページにだけ、最初の描画の前に当てる好み（data-prefs）を書く
        st, _, page = http(second.port, "GET", "/", with_key(second))
        m = re.search(rb'data-prefs="([^"]*)"', page)
        assert m, page[:300]
        import html as htmlmod  # noqa: PLC0415

        assert json.loads(htmlmod.unescape(m.group(1).decode())) == PREFS
        st, _, page = http(second.port, "GET", "/")
        assert b"data-prefs" not in page, "合言葉の無いページに好みを書いている"
    finally:
        second.stop()


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("garbage", b"{not json"),
        ("array", b"[1, 2]"),
        ("bad utf-8", b"\xff\xfe\x00{"),
        ("too large", b'{"lang": "en", "x": "' + b"a" * 70000 + b'"}'),
        ("deep", b"[" * 30000),
    ],
    # 中身（数万バイト）を ID に入れない。pytest は ID を環境変数 PYTEST_CURRENT_TEST に入れ、Windows の
    # 環境変数は 32767 文字までなので、長い ID の試験は準備の段階でエラーになる
    ids=["garbage", "array", "bad-utf-8", "too-large", "deep"],
)
def test_broken_settings_file_starts_with_defaults(tmp_path, name, content):
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "settings.json").write_bytes(content)
    if POSIX:
        os.chmod(config / "settings.json", 0o600)
    app = App(config)
    try:
        st, _, body = http(app.port, "GET", "/api/settings", with_key(app))
        assert st == 200 and json.loads(body)["settings"] == {}
        deadline = time.monotonic() + 10
        while "settings.json" not in app.out:
            assert time.monotonic() < deadline, "壊れた設定ファイルを端末で知らせない: " + app.out
            time.sleep(0.05)
        own = f"http://127.0.0.1:{app.port}"
        st, _, _ = http(
            app.port,
            "PUT",
            "/api/settings",
            with_key(app, {"Content-Type": "application/json", "Origin": own}),
            json.dumps({"settings": {"theme": "dark"}}).encode(),
        )
        assert st == 200
        assert (config / "settings.json.broken").read_bytes() == content, "元のファイルを控えていない"
        assert json.loads((config / "settings.json").read_text(encoding="utf-8")) == {"theme": "dark"}
        assert "Traceback" not in app.out
    finally:
        app.stop()


def test_unwritable_settings_location_stops_with_a_reason(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    for place in (blocker, blocker / "sub"):
        r = subprocess.run(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", "0"],
            cwd=ROOT,
            env={**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(place), "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert r.returncode == 2, (place, r.returncode, r.stdout, r.stderr)
        assert str(place) in r.stderr and "Traceback" not in r.stderr, r.stderr
        assert "http://" not in r.stdout
