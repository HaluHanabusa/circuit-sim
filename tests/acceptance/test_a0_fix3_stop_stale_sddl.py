"""A0 の独立検証（verifier、3回目）: 最後の修正（70a1e1d）の4件のうち、ブラウザの要らない3件。

記録は docs/verification/A0-verifier-3.md。ブラウザも Node.js も要らない（POの Windows の PC でも動く）。

- 止めるとき: 読むのが遅い相手（受け取りの量 4096 バイト）に、画面のファイル（約 300 KB）が最後まで届く。
  1) 製品のまま（POSIX は RST の印を付けない）。アプリ本体を別のプロセスで起動し、SIGINT で止める。
     読む速さを変える（POSIX だけ。Windows の止め方は Ctrl+Break で、実装の試験が見ている）。
  2) RST の印を付ける分岐（Windows だけで効く）を、どの OS でも強制して同じことを確かめる。RST（SO_LINGER
     0 秒）で閉じると、OS の送りの待ちに残るデータは捨てられる（Windows も同じ: closesocket の hard close）。
     transport の送りの待ちが空でも、OS が受け取って相手がまだ読んでいない分が残るので、Linux では届かない
     （軽微。製品の不具合なので xfail。Windows で OS が受け取る量は確かめていない）。
- 「使い終わった URL」の案内: 使い終わった番号の URL を、ブラウザがページとして開いた要求（Sec-Fetch-Mode:
  navigate）でだけ端末に出す。画像・スクリプト・cors・道具（ヘッダーなし）では出さない。アプリ本体を別の
  プロセスで起動し、端末の出力で見る（対照: navigate なら出る）。
- SDDL の解析（Windows の ACL の判定のうち、OS に依らない部分）: 継承するだけの項目（IO）、条件つきの項目
  （XA、ZA）、オブジェクト型（OA）を、本人だけと誤らない。期待値は SDDL の書式（MS-DTYP 2.5.1）から自分で
  決めた。条件の文字列の中に `)` があると、後ろの項目を読み落として本人だけと誤る（軽微。xfail）。

時間の基準は無い（待つ時間の上限は、混んだ機械で落ちないように長めにとった。合否には使わない）。
製品の不具合で落ちる試験は xfail(strict=True)。直ったら XPASS で落ちるので、印を外す。
"""

from __future__ import annotations

import functools
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
ASSETS = ROOT / "circuit_sim" / "app" / "static" / "assets"
WINDOWS = os.name == "nt"
POSIX = os.name == "posix"
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
WAIT = 30.0
STALE_TEXTS = ("使い終わったか期限が切れた URL", "already used or has expired")
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def big_asset() -> Path:
    asset = max(ASSETS.glob("*.js"), key=lambda p: p.stat().st_size)
    assert asset.stat().st_size > 100_000, "画面のファイルが小さすぎて、遅い相手の確かめにならない"
    return asset


class App:
    """アプリ本体（python -m circuit_sim.app、circuit-sim と同じ）を別のプロセスで起動する。"""

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
        self.number = self._first_number()
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

    def _first_number(self) -> str:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            assert line is not None, "".join(self.lines)
            m = URL_RE.search(line)
            if m:
                return m.group(2)
        raise AssertionError("起動の URL が出ない: " + "".join(self.lines))

    def stale_reports(self) -> int:
        return sum(any(t in ln for t in STALE_TEXTS) for ln in self.lines)

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


def get(port: int, path: str, headers: dict[str, str]) -> int:
    """生の GET（ヘッダーをそのまま送る）。状態の番号を返す。"""
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", *(f"{k}: {v}" for k, v in headers.items()),
             "Connection: close", "", ""]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall("\r\n".join(lines).encode("latin-1"))
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


def read_response(conn: socket.socket, pause: float) -> tuple[bytes, str | None]:
    """応答を、4096 バイトずつ pause 秒おきに読む。（読めた全部, 途中で切れたときの例外の名前）。"""
    data = b""
    conn.settimeout(20)
    try:
        while chunk := conn.recv(4096):
            data += chunk
            if pause:
                time.sleep(pause)
    except OSError as exc:
        return data, type(exc).__name__
    return data, None


def body_of(data: bytes) -> bytes:
    return data.partition(b"\r\n\r\n")[2]


# ------------------------------------------------------------------ 止めるとき、遅い相手に最後まで届く


@pytest.mark.skipif(not POSIX, reason="SIGINT で止める（Windows は実装の試験が Ctrl+Break の経路で見ている）")
@pytest.mark.parametrize("pause", [0.0, 0.002, 0.02])
def test_slow_reader_gets_the_whole_asset_when_the_app_is_stopped(tmp_path, pause):
    """読まない間に止め、その後ゆっくり読む。0.02 秒おきだと、読み終えるのに約 1.5 秒かかる。"""
    asset = big_asset()
    app = App(tmp_path / "config")
    conn = socket.socket()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        conn.connect(("127.0.0.1", app.port))
        conn.sendall(f"GET /assets/{asset.name} HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\n\r\n".encode())
        time.sleep(1.0)  # 読まない: アプリ本体は応答を渡し終え、残りは送りの待ち
        app.proc.send_signal(signal.SIGINT)
        data, error = read_response(conn, pause)
        assert error is None, (error, len(body_of(data)))
        assert body_of(data) == asset.read_bytes()
        assert app.proc.wait(timeout=20) == 0
    finally:
        conn.close()
        app.stop()


@pytest.mark.xfail(
    strict=not WINDOWS,
    reason=(
        "製品の不具合(軽微): RST の印（Windows だけで効く）は、transport の送りの待ちが空なら付く。"
        "RST は OS の送りの待ちも捨てるので、OS が受け取って相手がまだ読んでいない分が届かない"
        "（Linux で強制すると、約 300 KB の画面のファイルが 10〜130 KB で切れる）。"
        "Windows で OS が受け取る量は未確認"
    ),
)
def test_slow_reader_gets_the_whole_asset_with_the_windows_reset_branch(tmp_path, monkeypatch):
    """RST の印を付ける分岐を強制して、読むのが遅い相手に最後まで届くかを見る（Windows では、ふだんの形）。"""
    import uvicorn

    from circuit_sim.app import cli
    from circuit_sim.app.config import Settings
    from circuit_sim.app.server import Launches, Workspace, create_app

    monkeypatch.setattr(cli, "reset_idle_connections",
                        functools.partial(cli.reset_idle_connections, windows=True))
    asset = big_asset()
    workspace = Workspace()
    app = create_app(token="v" * 43, settings=Settings(tmp_path / "config"), workspace=workspace,
                     launches=Launches())
    sock = cli.bind(0)
    port = sock.getsockname()[1]
    server = cli.GuardedServer(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off",
                                              timeout_graceful_shutdown=2),
                               workspace, say=lambda text: None)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT
    while not server.started:
        assert time.monotonic() < deadline, "サーバーが起動しない"
        time.sleep(0.02)
    conn = socket.socket()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        conn.connect(("127.0.0.1", port))
        conn.sendall(f"GET /assets/{asset.name} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        time.sleep(1.0)  # 読まない
        server.should_exit = True
        time.sleep(1.0)  # 止める手順が進む間も読まない
        data, error = read_response(conn, 0.0)
        assert error is None, (error, len(body_of(data)), asset.stat().st_size)
        assert body_of(data) == asset.read_bytes()
    finally:
        conn.close()
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


# ------------------------------------------------------------------ 「使い終わった URL」の案内


NOT_A_PAGE = [
    ("道具（ヘッダーなし）", {}),
    ("道具（Accept: text/html）", {"Accept": "text/html"}),
    ("別のサイトの画像",
     {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image", "Sec-Fetch-Site": "cross-site"}),
    ("別のポートのスクリプト",
     {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script", "Sec-Fetch-Site": "same-site"}),
    ("cors の fetch", {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Site": "same-site",
                       "Origin": "http://127.0.0.1:1"}),
    ("same-origin", {"Sec-Fetch-Mode": "same-origin", "Sec-Fetch-Dest": "empty"}),
    ("navigate に似た値", {"Sec-Fetch-Mode": "navigatex"}),
]


def test_stale_url_report_only_for_a_page_opened_in_the_browser(tmp_path):
    app = App(tmp_path / "config")
    try:
        path = f"/?k={app.number}"
        get(app.port, path, NAVIGATE)  # 起動の番号を使う（クッキーと交換）
        time.sleep(0.5)
        assert app.stale_reports() == 0, "".join(app.lines)
        reported = []
        for name, headers in NOT_A_PAGE:
            get(app.port, path, headers)  # 使い終わった番号、クッキーなし
            time.sleep(0.5)
            if app.stale_reports():
                reported.append(name)
                break  # 続けて来たものは 5 秒に1回にまとめられるので、ここで止める
        assert reported == [], "".join(app.lines)
        # 対照: ブラウザでページとして開くと出る（ここまでに1度も出ていないので、間隔の制限に掛からない）
        get(app.port, path, NAVIGATE)
        deadline = time.monotonic() + 5
        while app.stale_reports() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert app.stale_reports() == 1, "".join(app.lines)
    finally:
        app.stop()


# ------------------------------------------------------------------ SDDL の解析（Windows の ACL の判定）

ME = "S-1-5-21-111-222-333-1001"
OTHER = "S-1-5-21-111-222-333-1002"
TRUSTED = {"SY", "BA", "S-1-5-18", "S-1-5-32-544"}  # 本人のほかに、持っていても漏れとみなさない相手（D-051）

# (名前, SDDL, ファイルとして本人だけ, 場所として付け直さなくてよい)
BASE = f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)"  # 付ける形
SDDL_CASES = [
    ("付ける形", BASE, True, True),
    ("所有者とグループが前", f"O:{ME}G:SY" + BASE, True, True),
    ("並びが逆", f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;{ME})", True, True),
    ("継承だけ OICIIO の Everyone", BASE + "(A;OICIIO;FR;;;WD)", False, False),
    ("継承だけ OIIO のほかの利用者", BASE + f"(A;OIIO;FA;;;{OTHER})", False, False),
    ("継承だけ CIIO の Users", BASE + "(A;CIIO;FA;;;BU)", False, False),
    ("継承だけ IOOICI（フラグの並びが違う）", BASE + "(A;IOOICI;FA;;;AU)", False, False),
    ("受け継いだ継承だけ OICIIOID", f"D:AI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)(A;OICIIOID;FA;;;CO)",
     False, False),
    ("CREATOR OWNER の継承だけ", BASE + "(A;OICIIO;GA;;;CO)", False, False),
    ("条件つき XA の Everyone", BASE + '(XA;OICI;FA;;;WD;(@User.Dept == "R"))', False, False),
    ("条件つき XA（入れ子のかっこと中かっこ）",
     f"D:P(A;;FA;;;{ME})(XA;;FR;;;AU;((Member_of {{SID(BA)}}) || (@Device.x Any_of {{1, 2}})))",
     False, False),
    ("条件つき ZA", f"D:P(A;;FA;;;{ME})(ZA;;FA;;;WD;(@User.x == 1))", False, False),
    ("オブジェクト型 OA", f"D:P(A;;FA;;;{ME})(OA;;FA;bf967aba-0de6-11d0-a285-00aa003049e2;;WD)",
     False, False),
    ("条件つきの拒否 XD だけ", f"D:P(A;;FA;;;{ME})(A;;FA;;;SY)(XD;;FA;;;WD;(@User.x == 1))", True, False),
    ("拒否 D だけ", f"D:P(A;;FA;;;{ME})(D;;FA;;;WD)", True, False),
    ("後ろに SACL", f"D:P(A;;FA;;;{ME})(A;;FA;;;SY)S:(AU;SA;FA;;;WD)", True, False),
    ("DACL に監査の種類 AU", f"D:P(A;;FA;;;{ME})(AU;;FA;;;WD)", False, False),
    ("DACL が無い", f"O:{ME}G:SY", False, False),
    ("NULL DACL", "D:NO_ACCESS_CONTROL", False, False),
    ('条件の文字列に "(" があり、後ろに Everyone',
     f'D:P(A;;FA;;;{ME})(XA;;FA;;;SY;(@User.D == "R(D"))(A;;FA;;;WD)', False, False),
]

# SDDL の文字列（MS-DTYP 2.5.1.1: char-string = DQUOTE *(CHAR) DQUOTE。CHAR は ABNF の %x01-7F）には
# `(` と `)` も入る
SDDL_PAREN_IN_STRING = [
    ("条件の文字列に ) があり、後ろに Everyone",
     f'D:P(A;;FA;;;{ME})(XA;;FA;;;SY;(@User.D == "R)D"))(A;;FA;;;WD)', False, False),
    ("条件の拒否の文字列に ) があり、後ろに Everyone（場所の形）",
     BASE + f'(XD;;FA;;;{OTHER};(@User.x == "a)b"))(A;OICI;FA;;;WD)', False, False),
]


def file_is_private(sddl: str) -> bool:
    """_Win32.private と同じ合わせ方（本人・SYSTEM・管理者のほかに、許可の相手がいない）。"""
    from circuit_sim.app.config import sddl_trustees

    trustees = sddl_trustees(sddl)
    return trustees is not None and trustees <= TRUSTED | {ME}


@pytest.mark.parametrize(("name", "sddl", "file_ok", "folder_ok"), SDDL_CASES, ids=[c[0] for c in SDDL_CASES])
def test_sddl_is_not_mistaken_for_private(name, sddl, file_ok, folder_ok):
    from circuit_sim.app.config import folder_dacl_is_private

    assert file_is_private(sddl) is file_ok
    assert folder_dacl_is_private(sddl, ME) is folder_ok


@pytest.mark.xfail(
    strict=True,
    reason=(
        "製品の不具合(軽微): 条件つきの項目の文字列の中の `)` を項目の終わりと読み、その後ろの項目を"
        "黙って読み落とす（後ろに Everyone の許可があっても、ファイルを本人だけと判定する）。"
        "場所の判定（完全一致）は誤らない"
    ),
)
@pytest.mark.parametrize(("name", "sddl", "file_ok", "folder_ok"), SDDL_PAREN_IN_STRING,
                         ids=[c[0] for c in SDDL_PAREN_IN_STRING])
def test_sddl_with_parenthesis_inside_a_condition_string(name, sddl, file_ok, folder_ok):
    from circuit_sim.app.config import folder_dacl_is_private

    assert folder_dacl_is_private(sddl, ME) is folder_ok
    assert file_is_private(sddl) is file_ok
