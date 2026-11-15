"""アプリ本体のサーバー（circuit_sim/app/server.py）の試験。

本物の uvicorn を別のスレッドで動かし、urllib で要求を送る。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from circuit_sim.app.cli import bind
from circuit_sim.app.server import SAMPLES, STATIC_DIR, Workspace, create_app

ROOT = Path(__file__).resolve().parents[1]


class Running:
    def __init__(self) -> None:
        self.workspace = Workspace()
        app = create_app(workspace=self.workspace)
        self.sock = bind(0)
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self) -> Running:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "サーバーが起動しない"
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()

    def call(self, path: str, body: dict | None = None, method: str | None = None, host: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        method = method or ("POST" if data else "GET")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, path: str, body: dict | None = None, method: str | None = None) -> dict:
        status, _, raw = self.call(path, body, method)
        assert status == 200, raw
        return json.loads(raw)


@pytest.fixture(scope="module")
def srv():
    running = Running().start()
    yield running
    running.stop()


def test_health_returns_instance_that_changes_per_start(srv):
    first = srv.json("/api/health")
    assert first["instance"] and first["version"]
    assert srv.json("/api/health") == first
    other = create_app()
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
         {"found": "9.9", "supported": "0.1"}),
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
    status, _, _ = srv.call("/boot.js")
    assert status == 200


def test_only_local_host_names_are_accepted(srv):
    """別の名前（DNS の付け替えで、外のページから届く要求）は受け付けない。"""
    status, _, _ = srv.call("/api/health", host="evil.example")
    assert status == 400
    for host in ("127.0.0.1", "localhost"):
        assert srv.call("/api/health", host=host)[0] == 200


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
