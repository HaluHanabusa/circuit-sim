"""アプリ本体のサーバー（circuit_sim/app/server.py）の試験。

本物の uvicorn を別のスレッドで動かし、urllib で要求を送る。合言葉（クッキー）は既定で付けて送る。
安全（D-051）: Origin・Sec-Fetch-Site、Content-Type、合言葉、画面のファイルのパスを、それぞれ確かめる。
"""
from __future__ import annotations

import http.client
import json
import ntpath
import os
import posixpath
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

import circuit_sim
from circuit_sim.app import server as server_module
from circuit_sim.app.cli import GuardedServer, bind
from circuit_sim.app.config import SETTINGS_FILE, Settings
from circuit_sim.app.server import (
    COOKIE,
    LAUNCH_KEPT,
    SAMPLES,
    STATIC_DIR,
    Launches,
    Workspace,
    check_spec,
    create_app,
    request_problem,
    safe_parts,
    static_file,
    token_ok,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43
WINDOWS = os.name == "nt"
# 読める版はエンジンが公開する一覧（circuit_sim.SUPPORTED_SCHEMA_VERSIONS）。試験は決め打ちしない
READABLE = tuple(circuit_sim.SUPPORTED_SCHEMA_VERSIONS)
# 読めない版の例: 読める版の次の、まだ無い版（いまは 0.3）
UNREADABLE = next(f"0.{n}" for n in range(1, 1000) if f"0.{n}" not in READABLE)


class Running:
    def __init__(self, config: Path, token: str = TOKEN, launches: Launches | None = None, port: int = 0,
                 settings: Settings | None = None) -> None:
        self.workspace = Workspace()
        self.settings = settings or Settings(config)
        self.launches = launches or Launches()
        app = create_app(token=token, settings=self.settings, workspace=self.workspace,
                         launches=self.launches)
        self.sock = bind(port)
        self.port = self.sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        config_ = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
        self.server = GuardedServer(config_, self.workspace, say=lambda text: None)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self) -> Running:
        """別のスレッドで動かし、受け付けを始めるまで待つ（プロセスは起動しない）。

        始まらないまま落ちるときは、止めてから落ちる。"""
        self.thread.start()
        deadline = time.monotonic() + 10
        try:
            while not self.server.started:
                assert time.monotonic() < deadline, "サーバーが起動しない"
                time.sleep(0.02)
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()

    def raw(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
            cookie: bool = True):
        """urllib が足したり直したりしない形で送る（Origin、Content-Type、Host をそのまま）。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie:
            all_headers["Cookie"] = f"{COOKIE}={TOKEN}"
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        data = resp.read()
        result = resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        conn.close()
        return result

    def call(self, path: str, body: dict | None = None, method: str | None = None, host: str | None = None,
             cookie: bool = True):
        data = None if body is None else json.dumps(body).encode()
        method = method or ("POST" if data else "GET")
        headers = {} if data is None else {"Content-Type": "application/json"}
        if host:
            headers["Host"] = host
        return self.raw(method, path, data, headers, cookie)

    def json(self, path: str, body: dict | None = None, method: str | None = None) -> dict:
        status, _, raw = self.call(path, body, method)
        assert status == 200, raw
        return json.loads(raw)


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    running = Running(tmp_path_factory.mktemp("config")).start()
    try:
        yield running
    finally:
        running.stop()


def test_health_returns_instance_that_changes_per_start(srv, tmp_path):
    first = srv.json("/api/health")
    assert first["instance"] and first["version"]
    assert srv.json("/api/health") == first
    other = create_app(token=TOKEN, settings=Settings(tmp_path))
    assert other.state.instance != first["instance"]


def test_samples_list_names_by_dictionary_key(srv):
    samples = srv.json("/api/samples")["samples"]
    assert [s["id"] for s in samples] == ["rlc_series"]
    assert samples[0]["name"] == "sample.rlc.name" and samples[0]["desc"] == "sample.rlc.desc"


def test_bundled_samples_match_examples():
    """同梱のサンプルは examples/ と同じ中身（食い違わない）。"""
    for sample in SAMPLES:
        bundled = ROOT / "circuit_sim" / "app" / "samples" / sample.file
        assert bundled.read_bytes() == (ROOT / "examples" / sample.file).read_bytes()


def test_open_sample_enters_session_and_reload_restores(srv):
    srv.json("/api/session", {"doc": None, "dirty": False}, "PUT")
    opened = srv.json("/api/open", {"sample": "rlc_series"})
    assert opened["ok"] and opened["doc"]["template"] == "rlc_series"
    assert opened["doc"]["file"] == "rlc_series.json" and opened["doc"]["sample"] == "rlc_series"
    session = srv.json("/api/session")
    assert session["doc"] == opened["doc"] and session["dirty"] is False


@pytest.mark.parametrize(
    ("text", "code", "params"),
    [
        ('{"a": 1,,}', "N-OPEN-SYNTAX", {"file": "x.json", "line": 1, "col": 9}),
        ('{\n  "template": "rlc_series",\n  "plant": {\n}', "N-OPEN-SYNTAX", {"line": 4, "col": 2}),
        ('{"schema_version": "9.9", "template": "rlc_series", "plant": {}}', "N-OPEN-VERSION",
         {"found": "9.9", "supported": ", ".join(READABLE)}),
        ('{"schema_version": "' + UNREADABLE + '", "template": "rlc_series", "plant": {}}', "N-OPEN-VERSION",
         {"found": UNREADABLE, "supported": ", ".join(READABLE)}),
        ('{"template": "nope", "plant": {}}', "N-OPEN-TEMPLATE", {"template": "nope"}),
    ],
)
def test_open_errors_come_as_codes_and_values(srv, text, code, params):
    result = srv.json("/api/open", {"file": "x.json", "text": text})
    assert result["ok"] is False
    assert result["notice"]["code"] == code
    for key, value in params.items():
        assert result["notice"]["params"][key] == value
    if code == "N-OPEN-TEMPLATE":
        assert "rlc_series" in result["notice"]["params"]["list"]


@pytest.mark.parametrize("version", READABLE)
def test_every_readable_version_opens(srv, version):
    """エンジンが読める版（circuit_sim.SUPPORTED_SCHEMA_VERSIONS）の仕様は、どれも開ける。"""
    text = '{"schema_version": "' + version + '", "template": "rlc_series", "plant": {}}'
    result = srv.json("/api/open", {"file": "v.json", "text": text})
    assert result["ok"] is True, result


@pytest.mark.parametrize("version", ["0.1", "0.2", "1", "null", "true", "[0.1]"])
def test_version_that_is_not_a_string_is_a_format_error(srv, version):
    """版が数値の 0.1 のとき「0.1 は対応外、対応は 0.1」と出さない。文字列が要る、という形の誤りにする。"""
    text = '{"schema_version": ' + version + ', "template": "rlc_series", "plant": {}}'
    notice = srv.json("/api/open", {"file": "v.json", "text": text})["notice"]
    assert notice["code"] == "N-OPEN-SCHEMA", notice
    assert {"path": "schema_version", "problem": "schema.string"} in notice["items"]


# 試験の ID には、値そのものではなく短い名前を付ける。pytest は、実行中の試験の ID を環境変数
# PYTEST_CURRENT_TEST に入れる（_pytest/runner.py の _update_current_test_var）。Windows の環境変数は
# 32767 文字までで、超えると Python が ValueError を出す（Modules/posixmodule.c の os.putenv）。値が 20 万
# 文字の ID は、Windows で試験の準備の段階でエラーになる（POのPCで 3 件）
@pytest.mark.parametrize("text", ["[" * 200_000, '{"plant": ' + "[" * 200_000 + "]" * 200_000 + "}",
                                  "[" * 200_000 + "]" * 200_000],
                         ids=["open-only", "inside-plant", "open-and-close"])
def test_deeply_nested_json_is_reported_not_500(srv, text):
    """入れ子の深い JSON（[ が 20 万）で 500 にしない。開けなかった（仕様の形に合わない）として案内する。"""
    status, _, raw = srv.call("/api/open", {"file": "deep.json", "text": text})
    assert status == 200, raw[:200]
    notice = json.loads(raw)["notice"]
    assert notice["code"] in ("N-OPEN-SCHEMA", "N-OPEN-SYNTAX")
    assert notice["params"]["file"] == "deep.json"


def test_check_spec_deep_nesting_directly():
    summary, problem = check_spec("deep.json", "[" * 100_000 + "]" * 100_000)
    assert summary is None and problem["code"] == "N-OPEN-SCHEMA"
    assert problem["items"] == [{"path": "$", "problem": "schema.other"}]


def test_open_schema_error_lists_places(srv):
    text = json.dumps({"template": "rlc_series", "plant": {"R": {"value": "a", "unit": "ohm"}}, "extra": 1})
    result = srv.json("/api/open", {"file": "x.json", "text": text})
    notice = result["notice"]
    assert notice["code"] == "N-OPEN-SCHEMA"
    assert notice["params"]["n"] == len(notice["items"]) + notice["more"]
    problems = {item["path"]: item["problem"] for item in notice["items"]}
    assert problems["plant.R.value"] == "schema.number"
    assert problems["extra"] == "schema.extra"
    assert notice["details"]  # 内部の文は「詳細」に入れる（主文には出さない）


def test_failed_open_keeps_current_session(srv):
    srv.json("/api/open", {"sample": "rlc_series"})
    srv.json("/api/open", {"file": "x.json", "text": "{"})
    assert srv.json("/api/session")["doc"]["file"] == "rlc_series.json"


def test_session_put_and_unsaved_flag(srv):
    doc = {"file": "a.json", "template": "rlc_series", "text": "{}", "sample": None}
    srv.json("/api/session", {"doc": doc, "dirty": True}, "PUT")
    assert srv.workspace.unsaved() == "a.json"
    srv.json("/api/session", {"doc": doc, "dirty": False}, "PUT")
    assert srv.workspace.unsaved() is None
    srv.json("/api/session", {"doc": None, "dirty": True}, "PUT")
    assert srv.workspace.unsaved() is None


def test_static_page_and_security_headers(srv):
    status, headers, body = srv.call("/")
    assert status == 200 and b'<div id="root">' in body
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"].startswith("text/html")
    status, headers, _ = srv.call("/boot.js")
    assert status == 200 and headers["content-type"] == "text/javascript; charset=utf-8"
    asset = next(p for p in (STATIC_DIR / "assets").iterdir() if p.suffix == ".js")
    status, headers, _ = srv.call(f"/assets/{asset.name}")
    assert status == 200 and headers["content-type"] == "text/javascript; charset=utf-8"
    assert "immutable" in headers["cache-control"]


def test_only_local_host_names_are_accepted(srv):
    """別の名前（DNS の付け替えで、外のページから届く要求）は受け付けない。"""
    status, _, _ = srv.call("/api/health", host="evil.example")
    assert status == 400
    for host in ("127.0.0.1", "localhost"):
        assert srv.call("/api/health", host=f"{host}:{srv.port}")[0] == 200


def test_unknown_api_is_404(srv):
    assert srv.call("/api/nothing")[0] == 404


def test_built_page_has_no_external_references():
    """同梱した画面のファイルが、外部の場所（http、https、//）を読み込まない。"""
    assert (STATIC_DIR / "index.html").is_file(), "画面のビルド結果が無い（cd frontend && npm run build）"
    for path in STATIC_DIR.rglob("*"):
        if path.suffix in (".html", ".css"):
            text = path.read_text(encoding="utf-8")
            markers = ('src="http', "src='http", 'href="http', "url(http", 'src="//', 'href="//', "@import")
            for marker in markers:
                assert marker not in text, f"{path.name}: {marker}"


# ------------------------------------------------------------------ 合言葉（同じPCの別の利用者。D-051）


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/health"), ("GET", "/api/session"),
                                               ("GET", "/api/samples"), ("GET", "/api/settings"),
                                               ("PUT", "/api/session"), ("POST", "/api/open"),
                                               ("PUT", "/api/settings"), ("GET", "/api/nothing")])
def test_api_without_the_key_is_401(srv, method, path):
    body = b"{}" if method != "GET" else None
    headers = {"Content-Type": "application/json"} if body else {}
    status, _, raw = srv.raw(method, path, body, headers, cookie=False)
    assert status == 401, (method, path, raw)
    assert b'"doc"' not in raw and b'"settings"' not in raw


@pytest.mark.parametrize("cookie", ["", "x", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), "t" * 42 + "u"])
def test_wrong_key_is_401(srv, cookie):
    status, _, _ = srv.raw("GET", "/api/session", headers={"Cookie": f"{COOKIE}={cookie}"}, cookie=False)
    assert status == 401


def test_screen_files_do_not_need_the_key(srv):
    """画面のファイル（秘密ではない）は合言葉なしで出す。開いた画面が 401 を受けて、案内を出せるように。"""
    status, _, body = srv.raw("GET", "/", cookie=False)
    assert status == 200 and b'<div id="root">' in body
    assert b"data-prefs" not in body, "合言葉の無いページに、画面の好みを書き込んだ"
    assert srv.raw("GET", "/boot.js", cookie=False)[0] == 200


def test_launch_number_sets_cookie_and_moves_to_plain_url(srv):
    """起動したときの URL（/?k=起動の番号）で、合言葉のクッキーを付け、/ へ移す。

    クッキーは HttpOnly、SameSite=Strict、Path=/。"""
    number = srv.launches.issue()
    assert number != TOKEN and TOKEN not in number
    status, headers, _ = srv.raw("GET", f"/?k={number}", cookie=False)
    assert status == 303 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={TOKEN};")
    parts = {p.strip().split("=")[0].lower(): p.strip() for p in cookie.split(";")}
    assert "httponly" in parts and parts["samesite"].lower() == "samesite=strict"
    assert parts["path"] == "Path=/"
    status, headers, _ = srv.raw("GET", "/?k=wrong", cookie=False)
    assert status == 303 and "set-cookie" not in headers


@pytest.mark.parametrize(("headers", "hop"), [
    ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, True),   # 転送用の HTML から
    ({"Sec-Fetch-Site": "same-site"}, True),
    ({"Sec-Fetch-Site": "none", "Accept": "text/html"}, False),              # URL を打った、OS から開いた
    ({"Sec-Fetch-Site": "same-origin"}, False),
    ({"Accept": "text/html,application/xhtml+xml"}, True),  # Sec-Fetch-Site を送らないブラウザ
    ({}, False),                                                              # ブラウザでない道具
])
def test_launch_from_another_page_moves_on_from_this_page(srv, headers, hop):
    """ほかのページ（転送用の HTML）から来たときは、303 ではなく、このページ自身から / へ移る。

    303 だと、続く / の要求も「別のサイトから」のままで SameSite=Strict のクッキーが付かず、入口のページに
    好み（data-prefs）が出ない（暗いテーマで明るい画面が一瞬出る）。移るまでの地の色は、覚えたテーマ。"""
    srv.json("/api/settings", {"settings": {"theme": "dark"}}, "PUT")
    try:
        number = srv.launches.issue()
        status, got, body = srv.raw("GET", f"/?k={number}", headers=headers, cookie=False)
        assert got["set-cookie"].startswith(f"{COOKIE}={TOKEN};")
        if hop:
            assert status == 200 and got["content-type"].startswith("text/html")
            assert b'http-equiv="refresh" content="0;url=/"' in body and b'content="dark"' in body
            assert "script-src 'self'" in got["content-security-policy"] and b"<script" not in body
        else:
            assert status == 303 and got["location"] == "/"
    finally:
        srv.json("/api/settings", {"settings": {"theme": None}}, "PUT")


def test_spent_numbers_are_reported(tmp_path):
    """番号が使われた・期限が切れた・古くて捨てたときに知らせる（転送用の HTML を消すため）。"""
    now = [0.0]
    spent: list[str] = []
    launches = Launches(seconds=10, clock=lambda: now[0], on_spent=spent.append)
    used, expires = launches.issue(), launches.issue()
    assert launches.redeem(used) and spent == [used]
    now[0] = 11
    assert not launches.is_open(expires)
    launches.sweep()
    assert spent == [used, expires]
    numbers = [launches.issue() for _ in range(LAUNCH_KEPT + 1)]
    assert spent[-1] == numbers[0]


def test_launch_number_works_only_once(srv):
    """起動の番号は1回だけ使える（ブラウザの起動の引数や履歴から読まれても、もう使えない）。"""
    number = srv.launches.issue()
    assert "set-cookie" in srv.raw("GET", f"/?k={number}", cookie=False)[1]
    status, headers, _ = srv.raw("GET", f"/?k={number}", cookie=False)
    assert status == 303 and "set-cookie" not in headers


def test_the_key_itself_is_not_accepted_in_the_url(srv):
    """合言葉そのものを URL に載せても、クッキーは付かない（URL に合言葉を載せる形を残さない）。"""
    status, headers, _ = srv.raw("GET", f"/?k={TOKEN}", cookie=False)
    assert status == 303 and "set-cookie" not in headers


def test_launch_number_expires(tmp_path):
    now = [1000.0]
    launches = Launches(seconds=300, clock=lambda: now[0])
    first, second = launches.issue(), launches.issue()
    now[0] += 299.9
    assert launches.redeem(first)
    now[0] += 0.2  # 300 秒を過ぎた
    assert not launches.redeem(second)
    # HTTP でも: 期限の切れた番号ではクッキーが付かない
    running = Running(tmp_path, launches=Launches(seconds=0.2)).start()
    try:
        number = running.launches.issue()
        time.sleep(0.3)
        assert "set-cookie" not in running.raw("GET", f"/?k={number}", cookie=False)[1]
    finally:
        running.stop()


def test_only_a_few_unused_launch_numbers_are_kept():
    launches = Launches()
    numbers = [launches.issue() for _ in range(LAUNCH_KEPT + 2)]
    assert not launches.redeem(numbers[0]) and not launches.redeem(numbers[1])  # 古いものから捨てる
    assert all(launches.redeem(n) for n in numbers[2:])


def test_stale_launch_number_without_cookie_is_reported_to_the_terminal(tmp_path):
    """使い終わった・期限の切れた番号で、クッキーを持たないページが開かれたら、端末に知らせる（間隔をあけて）。

    クッキーを持っているページ（履歴から開き直した、など）では知らせない。"""
    reports = []
    running = Running(tmp_path, launches=Launches(on_stale=lambda: reports.append(1))).start()
    navigate = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"}
    try:
        number = running.launches.issue()
        running.raw("GET", f"/?k={number}", headers=navigate, cookie=False)
        assert reports == []
        running.raw("GET", f"/?k={number}", headers=navigate)  # クッキーあり
        assert reports == []
        running.raw("GET", f"/?k={number}", headers=navigate, cookie=False)
        running.raw("GET", "/?k=unknown", headers=navigate, cookie=False)
        assert reports == [1]  # 続けて来たものは1回にまとめる
    finally:
        running.stop()


@pytest.mark.parametrize("headers", [
    {},                                                          # ブラウザでない道具（ほかの利用者を含む）
    {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},    # 別のサイトの <img src=…/?k=x>
    {"Sec-Fetch-Mode": "cors"},
    {"Accept": "text/html"},
    # 別のサイトの非表示の iframe（ページとして読むが、窓やタブではない）
    {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Mode": "navigate"},
])
def test_stale_report_only_comes_from_a_page_opened_in_the_browser(tmp_path, headers):
    """端末への案内は、ブラウザが窓やタブにページとして開いた要求（Sec-Fetch-Mode: navigate、

    Sec-Fetch-Dest: document）からだけ出す。別のサイトの画像・iframe の要求では出ない。"""
    reports = []
    running = Running(tmp_path, launches=Launches(on_stale=lambda: reports.append(1))).start()
    try:
        running.raw("GET", "/?k=unknown", headers=headers, cookie=False)
        assert reports == []
    finally:
        running.stop()


def test_launch_numbers_are_compared_in_constant_time(monkeypatch):
    calls = []
    real = server_module.hmac.compare_digest

    def record(a, b):
        calls.append((a, b))
        return real(a, b)

    launches = Launches()
    number = launches.issue()
    launches.issue()
    monkeypatch.setattr(server_module.hmac, "compare_digest", record)
    assert launches.redeem(number)
    assert len(calls) == 2  # 合ったところで止めない


def test_key_is_compared_in_constant_time(monkeypatch):
    """合言葉の比較は hmac.compare_digest（どこまで合っているかで時間が変わらない）。"""
    calls = []
    real = server_module.hmac.compare_digest

    def record(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(server_module.hmac, "compare_digest", record)
    assert token_ok(TOKEN, TOKEN) and not token_ok("x" * 43, TOKEN)
    assert len(calls) == 2
    assert not token_ok(None, TOKEN)


# ------------------------------------------------------------------ 別のサイトのページ（CSRF。D-051）


EVIL_BODY = json.dumps({"file": "evil.json", "text": "{}"}).encode()


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example", "Content-Type": "application/json"},
    {"Origin": "http://localhost:9", "Content-Type": "application/json"},
    {"Origin": "http://127.0.0.1:9", "Content-Type": "application/json"},
    {"Origin": "null", "Content-Type": "application/json"},
    {"Origin": "https://127.0.0.1", "Content-Type": "application/json"},
    {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
    {"Sec-Fetch-Site": "same-site", "Content-Type": "application/json"},
    {"Sec-Fetch-Site": "none", "Content-Type": "application/json"},
], ids=lambda h: ",".join(f"{k}={v}" for k, v in h.items()))
def test_writes_from_another_origin_are_403(srv, headers):
    """書き込みは、Origin がこのページと同じでなければ 403。Origin が無ければ Sec-Fetch-Site が same-origin。

    SameSite のクッキーはポートを区別しないので、同じPCの別のポートのページ（127.0.0.1:9）にはクッキーが付く。
    """
    srv.json("/api/open", {"sample": "rlc_series"})
    for method, path in (("POST", "/api/open"), ("PUT", "/api/session"), ("PUT", "/api/settings")):
        status, _, _ = srv.raw(method, path, EVIL_BODY, headers)
        assert status == 403, (method, path, headers, status)
    assert srv.json("/api/session")["doc"]["file"] == "rlc_series.json"


def test_writes_from_the_same_origin_are_accepted(srv):
    for headers in ({"Origin": f"http://127.0.0.1:{srv.port}"}, {"Sec-Fetch-Site": "same-origin"}, {}):
        status, _, raw = srv.raw("PUT", "/api/session", b'{"doc": null, "dirty": false}',
                                 {"Content-Type": "application/json", **headers})
        assert status == 200, (headers, raw)
    status, _, _ = srv.raw("PUT", "/api/session", b'{"doc": null}',
                           {"Content-Type": "application/json", "Origin": f"http://localhost:{srv.port}",
                            "Host": f"localhost:{srv.port}"})
    assert status == 200


@pytest.mark.parametrize("content_type", [None, "text/plain", "text/plain;charset=UTF-8",
                                          "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x",
                                          "application/jsonx", "json"])
def test_writes_without_json_content_type_are_415(srv, content_type):
    headers = {"Origin": f"http://127.0.0.1:{srv.port}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    srv.json("/api/open", {"sample": "rlc_series"})
    status, _, _ = srv.raw("POST", "/api/open", EVIL_BODY, headers)
    assert status == 415
    assert srv.json("/api/session")["doc"]["file"] == "rlc_series.json"
    headers["Content-Type"] = "application/json; charset=utf-8"
    assert srv.raw("POST", "/api/open", b'{"sample": "rlc_series"}', headers)[0] == 200


@pytest.mark.parametrize(("method", "headers", "cookie", "want"), [
    ("GET", {}, None, 401),
    # 読むだけの要求は Origin を見ない（別のサイトのページは、応答を読めない）
    ("GET", {"origin": "http://evil.example"}, TOKEN, None),
    ("POST", {"origin": "http://evil.example", "content-type": "application/json"}, TOKEN, 403),
    ("POST", {"origin": "http://evil.example", "content-type": "application/json"}, None, 403),
    ("POST", {"content-type": "application/json"}, None, 401),
    ("POST", {"content-type": "text/plain"}, TOKEN, 415),
    ("DELETE", {"sec-fetch-site": "cross-site"}, TOKEN, 403),
    ("PATCH", {"content-type": "application/json", "sec-fetch-site": "same-origin"}, TOKEN, None),
])
def test_request_problem_order(method, headers, cookie, want):
    got = request_problem(method, "/api/session", {"host": "127.0.0.1:1", **headers}, cookie, TOKEN)
    assert (got[0] if got else None) == want


# ------------------------------------------------------------------ 画面のファイルのパス（D-051）


REFUSED = [
    "/\\\\host\\share\\x", "//host/share/x", "/\\\\?\\C:\\x", "/C:/Windows/win.ini", "/C:\\x", "/c:x",
    "/assets\\..\\..\\server.py", "/../server.py", "/assets/../index.html", "/./index.html", "/assets//x.js",
    "/a/./b", "/%2e%2e/x", "/assets/x.js:stream", "/NUL", "/con.js", "/assets/COM1.txt", "/index.html.",
    "/x\x00.js", "/ünicode.js", "/a b.js", "", "/", "relative.js",
]


@pytest.mark.parametrize("path", REFUSED, ids=repr)
def test_paths_are_refused_before_building_a_file_path(path):
    """絶対パス、UNC（\\\\、//）、バックスラッシュ、ドライブ名、.. などは、パスを組み立てる前に拒む。"""
    assert safe_parts(path) is None


def recording(pathmod):
    """realpath を呼んだ記録を残す、パスの部品（Windows の ntpath を、この OS の上で使う）。"""
    calls: list[str] = []

    def realpath(p):
        calls.append(p)
        return pathmod.normpath(p)

    return SimpleNamespace(join=pathmod.join, realpath=realpath, commonpath=pathmod.commonpath), calls


def test_unc_path_on_windows_is_refused_before_realpath():
    """Windows: /%5c%5chost%5cshare%5cx を、starlette の StaticFiles（1.1 より前）と同じ組み立て方にすると、
    \\\\host\\share\\x（外の機械の共有）になる。realpath に渡すだけで SMB の接続が起きうる。
    このアプリは、組み立てる前に拒み、realpath を一度も呼ばない。"""
    decoded = "/\\\\host\\share\\x"  # URL の %5c は、復号されてバックスラッシュになる
    naive = ntpath.normpath(ntpath.join(*decoded.split("/")))
    assert naive == "\\\\host\\share\\x"
    assert ntpath.join("C:\\app\\static", naive) == "\\\\host\\share\\x"  # 画面のフォルダが消える
    fake, calls = recording(ntpath)
    for path in (decoded, "/\\\\host\\share", "//host/share/x", "/C:/Windows/win.ini", "/C:\\x",
                 "/assets\\..\\..\\x"):
        assert static_file(path, "C:\\app\\static", pathmod=fake) is None, path
    assert calls == [], f"拒む前に realpath を呼んだ: {calls}"


def test_allowed_path_stays_inside_the_screen_folder_on_windows_and_posix():
    fake, calls = recording(ntpath)
    assert static_file("/assets/index-abc.js", "C:\\app\\static", pathmod=fake) == \
        "C:\\app\\static\\assets\\index-abc.js"
    fake, calls = recording(posixpath)
    got = static_file("/assets/index-abc.js", "/app/static", pathmod=fake)
    assert got == "/app/static/assets/index-abc.js"
    assert static_file("/index.html", "/app/static", pathmod=fake) == "/app/static/index.html"


def test_symlink_out_of_the_screen_folder_is_refused(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "leak.txt").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("この環境ではシンボリックリンクを作れない")
    assert static_file("/leak.txt", str(root)) is None


@pytest.mark.parametrize("path", ["/%5c%5chost%5cshare%5cx", "/%5C%5Chost%5Cshare", "//host/share/x",
                                  "/C:/Windows/win.ini", "/C%3A%5Cx", "/assets/..%2f..%2fserver.py",
                                  "/..%5cserver.py", "/server.py", "/assets/%2e%2e/%2e%2e/cli.py"])
def test_static_route_refuses_paths_over_http(srv, path):
    status, _, body = srv.raw("GET", path, cookie=False)
    assert status in (400, 404), (path, status)
    assert b"import " not in body


# ------------------------------------------------------------------ 画面の好み（D-050）


def test_settings_are_saved_to_the_config_file(srv):
    srv.json("/api/settings", {"settings": {"lang": None, "theme": None, "layout.specWidth": None}}, "PUT")
    wanted = {"lang": "en", "theme": "dark", "layout.specWidth": "408"}
    saved = srv.json("/api/settings", {"settings": wanted}, "PUT")["settings"]
    assert saved == {"lang": "en", "theme": "dark", "layout.specWidth": "408"}
    assert srv.json("/api/settings")["settings"] == saved
    on_disk = json.loads(srv.settings.path.read_text(encoding="utf-8"))
    assert on_disk == saved
    if not WINDOWS:
        assert stat.S_IMODE(srv.settings.path.stat().st_mode) == 0o600
    # null で消す
    after = srv.json("/api/settings", {"settings": {"layout.specWidth": None}}, "PUT")["settings"]
    assert "layout.specWidth" not in after


@pytest.mark.parametrize("settings", [{"unknown": "1"}, {"lang": "fr"}, {"theme": "blue"},
                                      {"layout.specWidth": "-1"}, {"layout.specWidth": "1e3"},
                                      {"layout.specWidth": "99999"}, {"layout.noticesOpen": "true"},
                                      {"__proto__": "x"}, {"unknown": None}])
def test_unknown_or_malformed_settings_are_refused(srv, settings):
    before = srv.json("/api/settings")["settings"]
    status, _, _ = srv.call("/api/settings", {"settings": settings}, "PUT")
    assert status == 422
    assert srv.json("/api/settings")["settings"] == before


def test_page_carries_the_saved_settings_for_the_first_paint(srv):
    """設定ファイルの好みを、入口のページに data-prefs として書く（boot.js が最初の描画の前に読む）。"""
    srv.json("/api/settings", {"settings": {"lang": "en", "theme": "dark"}}, "PUT")
    _, _, body = srv.call("/")
    head = body.decode("utf-8").split("<head>")[0]
    assert "data-prefs=" in head
    assert "&quot;theme&quot;:&quot;dark&quot;" in head and "&quot;lang&quot;:&quot;en&quot;" in head
    assert srv.call("/index.html")[2] == body
    srv.json("/api/settings", {"settings": {"lang": None, "theme": None}}, "PUT")


def test_broken_settings_file_is_ignored(tmp_path):
    settings = Settings(tmp_path)
    settings.path.write_text("{not json", encoding="utf-8")
    assert settings.read() == {}
    settings.path.write_text(json.dumps({"lang": "en", "theme": "neon", "x": "1", "layout.specWidth": 400}),
                             encoding="utf-8")
    assert settings.read() == {"lang": "en"}


def test_deeply_nested_settings_file_still_serves_the_page(tmp_path):
    """深い入れ子の設定ファイルでも、/ と /api/settings は 200（既定で開く。レビュー2回目の軽3）。"""
    (tmp_path / SETTINGS_FILE).write_text("[" * 30000 + "]" * 30000, encoding="utf-8")
    reports = []
    running = Running(tmp_path, settings=Settings(tmp_path, on_broken=lambda *a: reports.append(a))).start()
    try:
        status, _, body = running.call("/")
        assert status == 200 and b'data-prefs="{}"' in body
        assert running.json("/api/settings") == {"settings": {}}
        saved = running.json("/api/settings", {"settings": {"theme": "dark"}}, "PUT")["settings"]
        assert saved == {"theme": "dark"}
        assert (tmp_path / (SETTINGS_FILE + ".broken")).exists()
        assert len(reports) == 1
    finally:
        running.stop()


def test_settings_that_cannot_be_written_are_reported_with_the_place(tmp_path):
    """書けない場所では、PUT は 500 と、設定の場所と理由を返す（画面がお知らせに出す）。"""
    (tmp_path / SETTINGS_FILE).mkdir()
    (tmp_path / (SETTINGS_FILE + ".broken")).mkdir()
    (tmp_path / (SETTINGS_FILE + ".broken") / "x").write_text("x", encoding="utf-8")
    running = Running(tmp_path).start()
    try:
        status, _, raw = running.call("/api/settings", {"settings": {"theme": "dark"}}, "PUT")
        assert status == 500
        body = json.loads(raw)
        assert body["path"] == str(tmp_path) and body["reason"]
        assert running.call("/")[0] == 200 and running.json("/api/settings") == {"settings": {}}
    finally:
        running.stop()


# ------------------------------------------------------------ 止めるとき（Windows の待ち受けの結び方）


def idle_connection(port: int) -> socket.socket:
    """要求を1つ送り、応答を読み終えて、つないだままにする（ブラウザの keep-alive と同じ）。"""
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn.sendall(f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {COOKIE}={TOKEN}\r\n\r\n"
                 .encode())
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(4096)
    head, rest = data.split(b"\r\n\r\n", 1)
    length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                      if line.lower().startswith(b"content-length")))
    while len(rest) < length:
        rest += conn.recv(4096)
    return conn


def test_idle_connections_are_closed_normally_when_stopping(tmp_path):
    """止めるとき、待っている接続（keep-alive）はふつうに FIN で閉じる（RST で送りの待ちを捨てない）。"""
    running = Running(tmp_path).start()
    conns = [idle_connection(running.port) for _ in range(3)]
    try:
        running.stop()
        for conn in conns:
            conn.settimeout(5)
            assert conn.recv(4096) == b""
    finally:
        for conn in conns:
            conn.close()


@pytest.fixture(params=[False, True], ids=["os-path", "windows-path"])
def bind_path(request, monkeypatch):
    """結び方を、この OS のもの（POSIX は SO_REUSEADDR）と、Windows のもの（何も付けない）の両方で流す。"""
    from circuit_sim.app import cli

    if request.param:
        monkeypatch.setattr(cli, "WINDOWS", True)
    return request.param


def test_slow_reader_gets_the_whole_response_when_stopping(tmp_path, bind_path):
    """読むのが遅い相手（受け取りの量 4096）が大きな画面のファイルを求め、読み終える前に止めても、

    応答は最後まで届く（レビュー3回目の rst_check.py と同じ形）。Windows の結び方を強制した形でも流す。
    止めた後もゆっくり読む（4096 バイトずつ 2 ms おき）。"""
    asset = next((STATIC_DIR / "assets").glob("*.js"))
    size = asset.stat().st_size
    assert size > 100_000
    running = Running(tmp_path).start()
    conn = socket.socket()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    conn.connect(("127.0.0.1", running.port))
    try:
        conn.sendall(f"GET /assets/{asset.name} HTTP/1.1\r\nHost: 127.0.0.1:{running.port}\r\n\r\n".encode())
        time.sleep(1.0)  # 読まない。アプリ本体は最後まで transport に渡し終え、残りは送りの待ち
        stopper = threading.Thread(target=running.stop)
        stopper.start()
        time.sleep(1.0)
        data = b""
        conn.settimeout(10)
        while chunk := conn.recv(4096):  # 途中で RST なら ConnectionResetError
            data += chunk
            time.sleep(0.002)
        stopper.join(timeout=15)
        head, _, body = data.partition(b"\r\n\r\n")
        assert len(body) == size, (len(body), size)
        assert body == asset.read_bytes()
    finally:
        conn.close()


@pytest.mark.skipif(not WINDOWS, reason="Windows の待ち受けの結び方（POのPCの local_ci で流れる）")
def test_same_port_can_be_bound_again_right_after_stopping_on_windows(tmp_path):
    """Windows の結び方（どの設定も付けない。cli.bind）で、受け付けた接続が相手の側で開いたままでも、

    止めてすぐ同じポートに結べる（開いている画面が、起動し直したアプリ本体に、同じポートで戻れる）"""
    first = Running(tmp_path).start()
    port = first.port
    conns = [idle_connection(port) for _ in range(3)]
    try:
        first.stop()
        again = bind(port)  # 結べなければ OSError（WSAEADDRINUSE）
        again.close()
        second = Running(tmp_path, port=port).start()
        try:
            assert second.json("/api/health")["instance"]
        finally:
            second.stop()
    finally:
        for conn in conns:
            conn.close()


def test_urllib_without_cookie_gets_401(srv):
    """ふつうの道具（クッキーを持たない）からは読めない。"""
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(srv.base + "/api/session", timeout=5)
    assert info.value.code == 401


def test_unc_like_requests_never_reach_realpath(srv, monkeypatch):
    """HTTP で UNC・ドライブ名の道を送っても、realpath（Windows では SMB の接続が起きうる）まで届かない。

    Windows の local_ci では、本物の ntpath の上で確かめることになる。"""
    calls: list[str] = []
    real = os.path.realpath

    def record(path, *args, **kwargs):
        calls.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", record)
    for path in ("/%5c%5chost%5cshare%5cx", "/%5C%5C127.0.0.1%5Cc%24%5Cwin.ini", "//host/share/x",
                 "/C:/Windows/win.ini", "/C%3A%5CWindows%5Cwin.ini", "/%5c%5c%3f%5cUNC%5chost%5cs"):
        status, _, _ = srv.raw("GET", path, cookie=False)
        assert status == 404, (path, status)
    assert calls == [], f"拒む前に realpath を呼んだ: {calls}"
    srv.raw("GET", "/boot.js", cookie=False)
    assert calls, "対照: 認める道では realpath を呼ぶはず"


# ------------------------------------------------------------------ 仕様の版（E1b の読める版の一覧に備える）


def test_version_is_checked_in_one_place():
    """版を確かめるのは version_problem だけ（SCHEMA_VERSION との完全一致で断る所がほかに無い）。"""
    source = Path(server_module.__file__).read_text(encoding="utf-8")
    assert "!= SCHEMA_VERSION" not in source and "== SCHEMA_VERSION" not in source
    assert source.count('"N-OPEN-VERSION"') == 1
    assert server_module.supported_versions() == READABLE  # エンジンが公開する一覧そのもの


def test_list_of_supported_versions_is_used_when_published(monkeypatch):
    """E1b が circuit_sim.SUPPORTED_SCHEMA_VERSIONS を公開したら、その一覧の版は断らない。"""
    monkeypatch.setattr(server_module.circuit_sim, "SUPPORTED_SCHEMA_VERSIONS", ("0.1", "0.2"), raising=False)
    assert server_module.version_problem("a.json", {"schema_version": "0.1"}) is None
    assert server_module.version_problem("a.json", {"schema_version": "0.2"}) is None
    problem = server_module.version_problem("a.json", {"schema_version": "9"})
    assert problem["code"] == "N-OPEN-VERSION" and problem["params"]["supported"] == "0.1, 0.2"
    _, notice = check_spec("a.json", '{"schema_version": "0.2", "template": "rlc_series", "plant": {}}')
    assert notice is None or notice["code"] != "N-OPEN-VERSION"
