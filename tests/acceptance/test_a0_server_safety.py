"""A0 の独立検証（verifier）: アプリ本体のサーバーの安全。

外のホスト、別のサイトのページ、Host の偽装、範囲外のファイルを確かめる。

記録は docs/verification/A0-verifier.md。ブラウザも Node.js も要らない（POの Windows の PC でも動く）。
アプリ本体は別のプロセス（python -m circuit_sim.app）で起動し、HTTP で直接突く。製品のコードは import しない
（期待値を実装から作らないため）。

別のサイトのページがブラウザから送れる要求（プリフライトの要らない「単純な要求」）は、方法が POST、
Content-Type が無いか text/plain・application/x-www-form-urlencoded・multipart/form-data のどれか。
fetch(url, {method: "POST", mode: "no-cors", body: new Blob([...])}) は Content-Type を付けずに送れる。
その要求で作業の状態（開いている仕様）が書き換わらないことを確かめる。

製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIL = "http://evil.example"
# 別のサイトから差し込まれたら困る仕様（値を変えてある）。サンプルと同じ形で、開ける
EVIL_SPEC = json.dumps({
    "schema_version": "0.1", "template": "rlc_series",
    "plant": {"Vin": {"value": 999, "unit": "V", "source": "手入力"},
              "R": {"value": 1, "unit": "ohm", "source": "手入力"},
              "L": {"value": 1e-3, "unit": "H", "source": "手入力"},
              "C": {"value": 10e-6, "unit": "F", "source": "手入力"}},
    "targets": {}, "missing": [],
})
EVIL_BODY = json.dumps({"file": "evil.json", "text": EVIL_SPEC}).encode("utf-8")
# FastAPI は 0.132.0 から、Content-Type の無い本文を JSON として読まない（strict_content_type、既定で有効）。
# それより前の版は読むので、上の no-cors の POST が通る
# （0.115.0 で実際のブラウザから再現した。A0-verifier.md）
FASTAPI_STRICT_SINCE = (0, 132, 0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while True:
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).close()
            break
        except OSError:
            assert proc.poll() is None, "アプリ本体が起動しない"
            assert time.monotonic() < deadline, "アプリ本体が 20 秒たっても応えない"
            time.sleep(0.05)
    yield base, port
    proc.kill()
    proc.wait(timeout=10)


def call(base: str, path: str, *, method: str = "GET", body: bytes | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def raw_request(port: int, head: str, body: bytes = b"") -> tuple[int, bytes]:
    """urllib が直してしまう要求（Content-Type なし、Host の偽装、.. を含む道）を、そのまま送る。"""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(head.encode("latin-1") + body)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""


def get(path: str, host: str) -> str:
    return f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"


def open_sample(base: str) -> None:
    status, _ = call(base, "/api/open", method="POST", body=b'{"sample": "rlc_series"}',
                     headers={"Content-Type": "application/json"})
    assert status == 200


def session_file(base: str) -> str | None:
    status, data = call(base, "/api/session")
    assert status == 200
    doc = json.loads(data)["doc"]
    return doc["file"] if doc else None


def post_cross_site(port: int, content_type: str | None, origin: str = EVIL) -> int:
    lines = [
        "POST /api/open HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Origin: {origin}",
        "Sec-Fetch-Site: cross-site", "Sec-Fetch-Mode: no-cors", f"Content-Length: {len(EVIL_BODY)}",
        "Connection: close",
    ]
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    status, _ = raw_request(port, "\r\n".join(lines) + "\r\n\r\n", EVIL_BODY)
    return status


# ------------------------------------------------------------ 外のホスト、Host の偽装（DNS の付け替え）


def test_listens_only_on_loopback(server):
    """このPCの外から届く場所で待ち受けない（127.0.0.1 だけ）。

    外向きの番地が無い PC では、その確認を飛ばす。"""
    _, port = server
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        # 外へ向かうときに使う番地（UDP の connect は相手に何も送らない。文書用の番地 192.0.2.1 を使う）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    outward = sorted(a for a in addresses if not a.startswith("127."))
    if not outward:
        pytest.skip("このPCに外向きの IPv4 の番地が無いので、外の番地からつながらないことを確かめなかった")
    for address in outward:
        with pytest.raises(OSError):
            socket.create_connection((address, port), timeout=2).close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1.evil.example",
                                  "localhost.evil.example", "127.0.0.1.nip.io", ""])
def test_foreign_host_header_is_refused(server, host):
    """DNS を付け替えて 127.0.0.1 を指させた名前（Host が別の名前）からの要求を受け付けない。"""
    _, port = server
    status, body = raw_request(port, get("/api/session", host))
    assert status >= 400, (host, status)
    assert b'"doc"' not in body


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_local_host_names_are_accepted(server, host):
    """偽装を拒む確認が、何でも拒んで通っているのではないこと（対照）。"""
    _, port = server
    status, _ = raw_request(port, get("/api/health", f"{host}:{port}"))
    assert status == 200


# ------------------------------------------------------------ 別のサイトのページから（CSRF）


@pytest.mark.parametrize("content_type", [None, "text/plain", "text/plain;charset=UTF-8",
                                          "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x"])
def test_simple_cross_site_post_does_not_replace_the_open_spec(server, content_type):
    """ブラウザがプリフライトなしで送れる形の POST で、開いている仕様が別のものに替わらない。"""
    base, port = server
    open_sample(base)
    assert session_file(base) == "rlc_series.json"
    post_cross_site(port, content_type)
    assert session_file(base) == "rlc_series.json", (
        f"Content-Type={content_type!r} の別サイトの POST で書き換わった")


def fastapi_floor() -> tuple[int, ...]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    for dep in deps:
        m = re.match(r"fastapi\s*>=\s*([\d.]+)", dep)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
    return (0,)


def test_cross_site_protection_does_not_depend_on_the_installed_fastapi(server):
    """下限の版の FastAPI でも守られること: 下限が 0.132.0 以上か、サーバー自身が別サイトの Origin を拒む。"""
    base, port = server
    floor_ok = fastapi_floor() >= FASTAPI_STRICT_SINCE
    open_sample(base)
    status = post_cross_site(port, "application/json")  # 版に関わらず本文を読む形。Origin だけが別サイト
    origin_ok = status in (400, 403) and session_file(base) == "rlc_series.json"
    open_sample(base)
    assert floor_ok or origin_ok, (
        f"fastapi の下限 {fastapi_floor()}、別サイトの Origin の POST への応答 {status}"
    )


def test_cross_site_preflight_is_not_granted(server):
    """作業の状態を書き換える PUT のプリフライトに、許可（Access-Control-Allow-Origin）を返さない。"""
    _, port = server
    head = (
        "OPTIONS /api/session HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\nOrigin: {EVIL}\r\nAccess-Control-Request-Method: PUT\r\n"
        "Access-Control-Request-Headers: content-type\r\nConnection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(head.encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    assert b"access-control-allow-origin" not in data.lower()


# ------------------------------------------------------------ 範囲外のファイル


TRAVERSALS = [
    "/../../../../etc/passwd", "/..%2f..%2f..%2f..%2fetc%2fpasswd", "/assets/..%2f..%2fserver.py",
    "/%2e%2e/%2e%2e/server.py", "/assets/../../server.py", "/..%5c..%5cserver.py", "/server.py", "/cli.py",
    "/samples/rlc_series.json", "/../samples/rlc_series.json", "/api/../server.py",
    "/%2e%2e%2fpyproject.toml",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_static_files_do_not_escape_the_screen_folder(server, path):
    """画面のビルド結果の外（パッケージのソース、作業ツリー、OS のファイル）を読ませない。"""
    _, port = server
    status, body = raw_request(port, get(path, f"127.0.0.1:{port}"))
    assert status in (400, 403, 404), (path, status)
    for leak in (b"root:", b"import ", b"[project]", b'"schema_version"'):
        assert leak not in body, (path, leak)


@pytest.mark.parametrize("sample", ["../../../../etc/passwd", "rlc_series.json", "../samples/rlc_series",
                                    "/etc/passwd", "rlc_series\x00", "..\\..\\server.py"])
def test_open_by_sample_cannot_name_a_path(server, sample):
    """サンプルは番号（id）で選ぶだけで、道（パス）として読まない。"""
    base, _ = server
    status, body = call(base, "/api/open", method="POST", body=json.dumps({"sample": sample}).encode(),
                        headers={"Content-Type": "application/json"})
    assert status == 404, (sample, status)
    assert b"root:" not in body and b"import " not in body


def test_open_does_not_read_a_file_named_by_the_request(server):
    """「file」は表示に使う名前だけで、サーバーがその道のファイルを読まない。

    中身は画面が読んで text で送る。"""
    base, _ = server
    status, body = call(base, "/api/open", method="POST", body=json.dumps({"file": "/etc/passwd"}).encode(),
                        headers={"Content-Type": "application/json"})
    assert status == 422
    assert b"root:" not in body
