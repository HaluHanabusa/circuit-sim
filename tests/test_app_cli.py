"""起動コマンド circuit-sim（circuit_sim/app/cli.py）の試験。別のプロセスで起動して確かめる。

設定の場所は、試験ごとの一時フォルダ（CIRCUIT_SIM_CONFIG_DIR）。端末の文言の言語は LC_ALL で決める。
起動したときの URL（/?k=…）に載るのは、一度だけ使える起動の番号（合言葉ではない）。試験も画面と同じく、
その URL を1回開いてクッキーを受け取る。2つ目の URL が要るときは、端末に Enter を送って出させる。
"""
from __future__ import annotations

import errno
import http.cookiejar
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from circuit_sim.app import cli
from circuit_sim.app.cli import (
    DEFAULT_PORT,
    FALLBACK_PORTS,
    MESSAGES,
    Texts,
    bind_any,
    bind_failure_text,
    port_problem,
    terminal_lang,
)
from circuit_sim.app.config import Settings
from circuit_sim.app.server import create_app

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
START_LIMIT_S = 5.0
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/(\?k=[A-Za-z0-9_-]+)?")


def launch(*args: str, config: Path, lang: str = "ja_JP.UTF-8", env: dict | None = None,
           stdin=subprocess.DEVNULL) -> subprocess.Popen:
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(config),
                "LC_ALL": lang, **(env or {})}
    return subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", *args],
        cwd=ROOT,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=stdin,
        text=True,
        encoding="utf-8",
    )


def read_lines_until_url(proc: subprocess.Popen, timeout: float = 15) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        lines.append(line)
        m = URL_RE.search(line)
        if m:
            return m.group(0), lines
        if proc.poll() is not None and not line:
            break
    raise AssertionError(f"起動した場所の表示が無い: {lines}")


class Client:
    """合言葉の URL を一度開いてクッキーを持ち、あとは合言葉の無い URL で API を使う（画面と同じ）。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.base = url.split("?")[0]
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def login(self) -> None:
        with self.opener.open(self.url, timeout=5) as resp:
            assert resp.geturl() == self.base, "合言葉の無い URL へ移っていない"
            assert b'<div id="root">' in resp.read()

    def json(self, path: str, body: dict | None = None, method: str = "GET") -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=5) as resp:
            return json.loads(resp.read())


def wait_up(base: str, timeout: float) -> float:
    """入口のページ（合言葉は要らない）が出るまで待つ。起動の番号は使わない。"""
    started = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(base, timeout=1) as resp:
                resp.read()
            return time.monotonic() - started
        except OSError:
            if time.monotonic() - started > timeout:
                raise
            time.sleep(0.05)


def wait_ready(client: Client, timeout: float) -> float:
    """受け付けを始めるまで待ってから、起動の URL を1回だけ開いてクッキーを受け取る。"""
    started = time.monotonic()
    wait_up(client.base, timeout)
    client.login()
    client.json("api/health")
    return time.monotonic() - started


def token_of(config: Path) -> str:
    return (config / "token").read_text(encoding="ascii").strip()


def stop(proc: subprocess.Popen) -> str:
    if proc.poll() is None:
        proc.kill()
    out, _ = proc.communicate(timeout=10)
    return out


def test_starts_and_serves_page_quickly(tmp_path):
    started = time.monotonic()
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        assert time.monotonic() - started < START_LIMIT_S
        port = int(URL_RE.match(url).group(1))
        # 28470 か、使われていれば 28471〜28479（どれも使われていれば OS が選んだポート）
        assert port in (DEFAULT_PORT, *FALLBACK_PORTS) or port > 0
        assert "?k=" in url and token_of(tmp_path) not in url
    finally:
        stop(proc)


def test_key_is_kept_across_restarts_so_open_pages_reconnect(tmp_path):
    """合言葉は起動のたびに変えない: 前の起動で付いたクッキーで、起動し直したアプリ本体にもつながる。"""
    proc = launch("--no-browser", config=tmp_path)
    try:
        url1, _ = read_lines_until_url(proc)
        client = Client(url1)
        wait_ready(client, START_LIMIT_S)
    finally:
        stop(proc)
    proc = launch("--no-browser", "--port", URL_RE.match(url1).group(1), config=tmp_path)
    try:
        url2, _ = read_lines_until_url(proc)
        assert url2.split("?")[0] == url1.split("?")[0] and url2 != url1  # 同じポート、新しい起動の番号
        deadline = time.monotonic() + START_LIMIT_S
        while True:
            try:
                assert client.json("api/health")["instance"]  # ログインし直さない
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        stop(proc)


def test_api_from_a_plain_client_is_refused(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(url.split("?")[0] + "api/session", timeout=5)
        assert info.value.code == 401
    finally:
        stop(proc)


def test_default_port_falls_back_to_the_next(tmp_path):
    """28470 が使われていれば 28471〜28479 を順に試す（D-050）。"""
    holder = socket.socket()
    if not WINDOWS:  # 前の試験のアプリ本体の TIME_WAIT で、押さえ損ねないように（アプリ本体と同じ設定）
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        holder.bind(("127.0.0.1", DEFAULT_PORT))
        holder.listen(1)
    except OSError:
        holder.close()
        holder = None  # ほかのプログラムが使っている（それでも、次を試すことは確かめられる）
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, lines = read_lines_until_url(proc)
        port = int(URL_RE.match(url).group(1))
        assert port != DEFAULT_PORT
        assert port in FALLBACK_PORTS or all(_busy(p) for p in FALLBACK_PORTS)
        notice = f"ポート {DEFAULT_PORT} が使えないため、ポート {port} で起動します。"
        assert any(notice in ln for ln in lines), lines
    finally:
        stop(proc)
        if holder:
            holder.close()


def _busy(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


@pytest.mark.parametrize(("lang", "expected"), [
    ("ja_JP.UTF-8", "ポート {port} は、ほかのプログラムが使っています"),
    ("en_US.UTF-8", "Port {port} is being used by another program"),
])
def test_busy_port_is_reported_and_exits_2(tmp_path, lang, expected):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        proc = launch("--no-browser", "--port", str(port), config=tmp_path, lang=lang)
        proc.wait(timeout=20)
        out = proc.communicate()[0]
    assert proc.returncode == 2
    assert expected.format(port=port) in out


def fake_binder(failing: dict[int, OSError]):
    tried: list[int] = []

    def binder(port: int):
        tried.append(port)
        if port in failing:
            raise failing[port]
        return f"socket:{port}"

    return binder, tried


def test_scan_order_is_28470_then_28471_to_28479_then_os():
    busy = OSError(errno.EADDRINUSE, "in use")
    binder, tried = fake_binder({p: busy for p in (DEFAULT_PORT, *FALLBACK_PORTS)})
    sock, failures = bind_any(None, binder)
    assert sock == "socket:0"
    assert tried == [28470, 28471, 28472, 28473, 28474, 28475, 28476, 28477, 28478, 28479, 0]
    assert [p for p, _ in failures] == tried[:-1]
    binder, tried = fake_binder({28470: busy, 28471: OSError(errno.EACCES, "denied")})
    assert bind_any(None, binder)[0] == "socket:28472"


def test_explicit_port_is_the_only_one_tried():
    binder, tried = fake_binder({8765: OSError(errno.EADDRINUSE, "in use")})
    sock, failures = bind_any(8765, binder)
    assert sock is None and tried == [8765] and len(failures) == 1


def winerror(code: int) -> OSError:
    exc = OSError(code, "windows")
    exc.winerror = code  # Windows の OSError の形
    return exc


@pytest.mark.parametrize(("exc", "kind"), [
    (OSError(errno.EADDRINUSE, "in use"), "in_use"),
    (winerror(10048), "in_use"),       # WSAEADDRINUSE
    (OSError(errno.EACCES, "denied"), "denied"),
    (winerror(10013), "denied"),       # WSAEACCES（OS が予約しているポートの範囲、ほかの利用者の排他）
    (OSError(errno.EADDRNOTAVAIL, "x"), "other"),
])
def test_bind_errors_are_told_apart(exc, kind):
    assert port_problem(exc) == kind


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_each_bind_error_has_its_own_guidance(lang):
    tr = Texts(lang)
    in_use = bind_failure_text(tr, 9000, [(9000, OSError(errno.EADDRINUSE, "in use"))])
    denied = bind_failure_text(tr, 9000, [(9000, winerror(10013))])
    other = bind_failure_text(tr, 9000, [(9000, OSError(errno.EADDRNOTAVAIL, "not available"))])
    none = bind_failure_text(tr, None, [(0, OSError(errno.EADDRNOTAVAIL, "not available"))])
    assert len({in_use, denied, other, none}) == 4
    assert "9000" in in_use and "9000" in denied and "netsh" in denied and "netstat" in denied
    assert "not available" in other and "not available" in none
    assert ("ポート" if lang == "ja" else "Port") in in_use


@pytest.mark.skipif(WINDOWS or os.geteuid() == 0,
                    reason="POSIX の一般の利用者でだけ、1024 未満のポートが拒まれる")
def test_privileged_port_is_reported_as_denied(tmp_path):
    proc = launch("--no-browser", "--port", "1", config=tmp_path)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
    assert "ポート 1 は使えません" in out


def excluded_port() -> int | None:
    """Windows で OS が予約しているポートの範囲（Hyper-V など）の、最初のポート。無ければ None。"""
    try:
        out = subprocess.run(["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)", line)
        if m and int(m.group(1)) >= 1024:
            return int(m.group(1))
    return None


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の、OS が予約しているポートの範囲（POのPCの local_ci で流れる）")
def test_windows_excluded_port_range_is_reported_as_denied(tmp_path):
    port = excluded_port()
    if port is None:
        pytest.skip("この PC には、OS が予約しているポートの範囲が無い（netsh で確かめた）")
    proc = launch("--no-browser", "--port", str(port), config=tmp_path)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
    assert f"ポート {port} は使えません" in out and "netsh" in out


@pytest.mark.parametrize(("lang", "words"), [
    ("ja_JP.UTF-8",
     ["使い方:", "オプション", "待ち受けるポート", "ブラウザを開かない", "この説明を出して終わる"]),
    ("en_US.UTF-8", ["usage:", "options", "port to listen on", "don't open a browser", "show this help"]),
])
def test_help_is_in_one_language(tmp_path, lang, words):
    proc = launch("--help", config=tmp_path, lang=lang)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 0
    for word in words:
        assert word in out, (word, out)
    if lang.startswith("en"):
        assert not re.search(r"[぀-ヿ一-鿿]", out), out
    else:
        assert "show this help" not in out and "usage:" not in out


@pytest.mark.parametrize(("args", "lang", "text"), [
    (["--port", "70000"], "ja_JP.UTF-8",
     "circuit-sim: エラー: --port には 0〜65535 のポートを指定してください（指定: 70000）。"),
    (["--port", "abc"], "en_US.UTF-8",
     "circuit-sim: error: --port must be a port from 0 to 65535 (given: abc)."),
    (["--bogus"], "ja_JP.UTF-8",
     "circuit-sim: エラー: 知らないオプションです: --bogus。"
     "使えるオプションは circuit-sim --help で確かめてください。"),
    (["--bogus"], "en_US.UTF-8",
     "circuit-sim: error: Unknown option: --bogus. See circuit-sim --help for the options."),
    (["spec.json"], "ja_JP.UTF-8", "知らないオプションです: spec.json。"),
    (["--port"], "ja_JP.UTF-8", "circuit-sim: エラー: --port の後ろに値を指定してください。"),
    (["--port"], "en_US.UTF-8", "circuit-sim: error: --port needs a value."),
    (["--no-browser=yes"], "ja_JP.UTF-8", "circuit-sim: エラー: --no-browser には値を付けられません。"),
    (["--no-browser=yes"], "en_US.UTF-8", "circuit-sim: error: --no-browser doesn't take a value."),
])
def test_bad_arguments_are_explained_in_one_language(tmp_path, args, lang, text):
    """引数の誤りの行に、argparse の英語（argument --port:、unrecognized arguments など）を残さない。

    （UX の改善5）"""
    proc = launch(*args, config=tmp_path, lang=lang)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2 and text in out, out
    for english in ("argument ", "unrecognized", "expected one", "ignored explicit", "invalid"):
        assert english not in out, out
    if lang.startswith("ja"):
        assert "使い方:" in out and "usage:" not in out


def test_parser_falls_back_to_the_table_for_other_errors(capsys):
    """argparse が自分で error を呼ぶ誤り（見分けられないもの）も、言語の表の文で出す。"""
    parser = cli.Parser(Texts("ja"))
    with pytest.raises(SystemExit) as info:
        parser.error("something odd")
    assert info.value.code == 2
    assert "circuit-sim: エラー: 引数が正しくありません（something odd）。" in capsys.readouterr().err


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_failures_tell_how_to_fix(lang):
    """設定の場所に書けない、待ち受けるポートを用意できないときは、どう直すかも出す（UX の改善4）。"""
    tr = Texts(lang)
    config = tr("config.failed", path="/x", reason="Not a directory")
    none = bind_failure_text(tr, None, [(0, OSError(errno.EADDRNOTAVAIL, "not available"))])
    if lang == "ja":
        assert "フォルダの権限、空き容量、同じ名前のファイルが無いか" in config
        assert "セキュリティソフトなどが、このPCの中の通信（127.0.0.1）を止めていないか" in none
    else:
        assert "permissions, free space, no file with the same name" in config
        assert "security software isn't blocking connections within this computer (127.0.0.1)" in none


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_access_denied_mentions_both_causes(lang):
    """WSAEACCES（10013）は、ほかのプログラムがポートを専用で使っているときにも出る。予約の範囲と決めつけない。"""
    text = bind_failure_text(Texts(lang), 9000, [(9000, winerror(10013))])
    assert "netstat -ano" in text and "netsh interface ipv4 show excludedportrange" in text
    other = {"ja": "ほかのプログラムがこのポートを専用で使っている",
             "en": "Another program may be using it"}[lang]
    assert other in text


@pytest.mark.parametrize(("env", "windows", "lang"), [
    ({"LC_ALL": "ja_JP.UTF-8"}, False, "ja"),
    ({"LANG": "ja_JP.eucJP"}, False, "ja"),
    ({"LC_ALL": "C", "LANG": "ja_JP.UTF-8"}, False, "en"),   # LC_ALL が先
    ({"LC_MESSAGES": "ja_JP", "LANG": "en_US"}, False, "ja"),
    ({"LANG": "en_US.UTF-8"}, False, "en"),
    ({}, False, "en"),
    ({"LANG": "ja"}, True, "ja"),
])
def test_terminal_language_follows_the_os_setting(env, windows, lang):
    assert terminal_lang(env, windows=windows) == lang


def test_terminal_texts_exist_in_both_languages():
    for key, (ja, en) in MESSAGES.items():
        assert ja and en, key
        assert sorted(re.findall(r"\{(\w+)\}", ja)) == sorted(re.findall(r"\{(\w+)\}", en)), key
        assert not re.search(r"[぀-ヿ一-鿿]", en), key
        assert "番号" not in ja, f"{key}: 「番号」ではなく「ポート」と書く（--port と同じ言葉）"


def test_there_is_no_switch_to_skip_the_key():
    """製品に、合言葉を確かめない抜け道（環境変数など）が無い。"""
    source = Path(cli.__file__).read_text(encoding="utf-8") + Path(cli.__file__).with_name(
        "server.py").read_text(encoding="utf-8")
    assert "NO_TOKEN" not in source and "PYTEST" not in source
    with pytest.raises(ValueError):
        create_app(token="", settings=Settings(ROOT / "unused"))


def test_browser_gets_a_one_time_number_and_never_the_key(tmp_path):
    """既定のブラウザ（BROWSER の指定で、偽のブラウザに差し替える）に渡すのは、端末に出したのと同じ、

    一度だけ使える起動の番号の URL。合言葉そのものは、ブラウザの起動の引数にも、端末にも出ない
    （/proc/<pid>/cmdline から読まれても、使い終われば役に立たない。レビュー2回目の中1）。"""
    record = tmp_path / "opened.json"
    fake = tmp_path / "fake_browser.py"
    fake.write_text(
        "import json, sys, pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    if WINDOWS:
        pytest.skip("Windows の BROWSER の指定は、空白を含む場所を扱えないため、ここでは確かめない")
    config = tmp_path / "config"
    proc = launch(config=config, env={"BROWSER": f"{sys.executable} {fake} %s"})
    try:
        url, lines = read_lines_until_url(proc)
        deadline = time.monotonic() + START_LIMIT_S
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        argv = json.loads(record.read_text(encoding="utf-8"))
        token = token_of(config)
        assert argv[-1] == url and "?k=" in url
        assert not any(token in arg for arg in argv), argv
        wait_up(url.split("?")[0], START_LIMIT_S)
        lines += [proc.stdout.readline() for _ in range(3)]
        assert not any(token in line for line in lines), lines
        # 偽のブラウザは開かないので、ここで1回使う。2回目は使えない
        first, second = Client(url), Client(url)
        first.login()
        assert first.json("api/health")["instance"]
        second.login()
        with pytest.raises(urllib.error.HTTPError) as info:
            second.json("api/health")
        assert info.value.code == 401
    finally:
        stop(proc)


@pytest.mark.parametrize(("lang", "note", "renew", "new", "stale"), [
    ("ja_JP.UTF-8", "この URL は1回だけ、5 分以内に使えます", "ここで Enter を押すと新しい URL を出します",
     "新しい URL（1回だけ、5 分以内に使えます）: ", "使い終わったか期限が切れた URL で、画面が開かれました"),
    ("en_US.UTF-8", "This URL works once, within 5 minutes", "press Enter here for a new URL",
     "New URL (works once, within 5 minutes): ", "opened from a URL that was already used or has expired"),
])
def test_enter_in_the_terminal_gives_a_new_url(tmp_path, lang, note, renew, new, stale):
    """端末で Enter を押すと、新しい起動の URL を出す。

    使い終わった URL で開かれたら、端末で出し方を案内する。"""
    proc = launch("--no-browser", config=tmp_path, lang=lang, stdin=subprocess.PIPE)
    try:
        url, lines = read_lines_until_url(proc)
        lines += [proc.stdout.readline() for _ in range(3)]
        assert any(note in ln for ln in lines) and any(renew in ln for ln in lines), lines
        wait_ready(Client(url), START_LIMIT_S)
        Client(url).login()  # 使い終わった URL
        line = proc.stdout.readline()
        assert stale in line, line
        proc.stdin.write("\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line.startswith(new), line
        fresh = URL_RE.search(line).group(0)
        assert fresh != url and fresh.split("?")[0] == url.split("?")[0]
        client = Client(fresh)
        client.login()
        assert client.json("api/health")["instance"]
    finally:
        stop(proc)


def test_enter_source_tells_inputs_apart(tmp_path):
    """Enter を読める入力（パイプ、端末）と、読めない入力（/dev/null、Windows の NUL、ファイル）を見分ける。

    Windows の os.isatty は NUL でも True を返すので、GetConsoleMode で見分ける（Windows でも流れる）。"""
    r, w = os.pipe()
    try:
        assert cli.enter_source(r) == "pipe"
    finally:
        os.close(r)
        os.close(w)
    with open(os.devnull, "rb") as null:
        assert cli.enter_source(null.fileno()) is None
    path = tmp_path / "input.txt"
    path.write_text("x", encoding="utf-8")
    with open(path, "rb") as f:
        assert cli.enter_source(f.fileno()) is None


def test_without_a_readable_input_restart_is_suggested(tmp_path):
    """Enter を読めない起動（入力が /dev/null など）では、起動し直す方法を案内する。"""
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, lines = read_lines_until_url(proc)
        lines += [proc.stdout.readline() for _ in range(3)]
        assert any("circuit-sim を起動し直してください" in ln for ln in lines), lines
        assert not any("Enter" in ln for ln in lines), lines
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
def test_ctrl_c_stops_right_away_without_unsaved_changes(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
        assert "circuit-sim を止めました" in proc.stdout.read()
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
@pytest.mark.parametrize(("lang", "warning", "stopped"), [
    ("ja_JP.UTF-8", "保存していない変更があります", "circuit-sim を止めました"),
    ("en_US.UTF-8", "There are unsaved changes", "circuit-sim has stopped"),
])
def test_ctrl_c_with_unsaved_changes_warns_first_then_stops(tmp_path, lang, warning, stopped):
    """未決11: 保存していない変更があれば、1回目の Ctrl+C では止めずに知らせ、2回目で止める。"""
    proc = launch("--no-browser", config=tmp_path, lang=lang)
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
        proc.send_signal(signal.SIGINT)
        line = ""
        deadline = time.monotonic() + 5
        while warning not in line and time.monotonic() < deadline:
            line = proc.stdout.readline()
        assert "mine.json" in line
        time.sleep(0.3)
        assert proc.poll() is None, "1回目の Ctrl+C で止まった"
        assert client.json("api/health")["instance"]
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
        assert stopped in proc.stdout.read()
    finally:
        stop(proc)


def test_unwritable_config_folder_is_reported(tmp_path):
    """設定の場所に書けないときは、理由を出して終わる（終了コード 2）。"""
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    proc = launch("--no-browser", config=blocker / "sub", lang="en_US.UTF-8")
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
    assert "Can't write to the settings folder" in out


@pytest.mark.skipif(not WINDOWS, reason="Windows の Ctrl+Break（POのPCの local_ci で流れる）")
def test_ctrl_break_on_windows_warns_first_then_stops(tmp_path):
    """Windows: 別のプロセスに Ctrl+C は送れないが、Ctrl+Break（SIGBREAK）は送れる。

    未決11 の2回押し（1回目は知らせて止めない、10 秒以内の2回目で止める）を確かめる。

    Ctrl+C の2回押しと、窓を閉じたときは、手で確かめる（報告の手順）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path),
           "LC_ALL": "ja_JP.UTF-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", "--no-browser"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        line = ""
        deadline = time.monotonic() + 5
        while "保存していない変更があります" not in line and time.monotonic() < deadline:
            line = proc.stdout.readline()
        assert "mine.json" in line
        time.sleep(0.3)
        assert proc.poll() is None, "1回目の Ctrl+Break で止まった"
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        assert proc.wait(timeout=10) == 0
    finally:
        stop(proc)


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の SO_EXCLUSIVEADDRUSE と Ctrl+Break（POのPCの local_ci で流れる）")
def test_restart_on_windows_binds_the_same_port_while_a_browser_keeps_connections(tmp_path):
    """Windows: 待っている接続（ブラウザの keep-alive）を開いたまま止めて、すぐ同じポートで起動し直せる。

    SO_EXCLUSIVEADDRUSE のソケットは、受け付けた接続が残っていると同じポートに結べない（Microsoft の文書）。
    止めるときに待っている接続を RST で閉じるので、28471 へ移らない（開いている画面が戻れる）。"""
    import http.client

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path),
           "LC_ALL": "ja_JP.UTF-8"}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def start():
        return subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(port)], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    proc = start()
    conns = []
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        cookie = "; ".join(f"{c.name}={c.value}" for c in client.jar)
        for _ in range(3):  # 応答を読み終えて、つないだままにする
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health", headers={"Cookie": cookie})
            conn.getresponse().read()
            conns.append(conn)
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        proc.wait(timeout=15)
        proc = start()
        url2, lines = read_lines_until_url(proc)
        assert f":{port}/" in url2, ("同じポートで起動し直せなかった", lines)
        assert client.json("api/health")["instance"]  # 前のクッキーのまま戻れる
    finally:
        for conn in conns:
            conn.close()
        stop(proc)
