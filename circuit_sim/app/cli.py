"""起動コマンド circuit-sim。

  circuit-sim                 127.0.0.1 のポート 28470 でアプリ本体を起動し、既定のブラウザで作業画面を開く。
                              使われていれば 28471〜28479 を順に試し、どれも使えなければ空いているポートを使う
                              （D-050。ポートを決めておくと、ブラウザが覚えた画面の状態が次の起動でも当たる）
  circuit-sim --port 8765     ポートを決めて起動する（使えなければ、理由を出して終わる）
  circuit-sim --no-browser    ブラウザを開かない（開く場所の URL を表示する）

ブラウザに渡す URL（/?k=…）には、合言葉ではなく、一度だけ・5 分だけ使える起動の番号を載せる。アプリ本体は
番号を合言葉のクッキーと交換し、番号の無い URL へ移す（D-051、D-053）。ブラウザの起動の引数
（/proc/<pid>/cmdline）や履歴から番号が読まれても、使い終わっているか期限が切れている。
端末に出す URL も同じ番号。
新しい URL は、端末で Enter を押すと出る（端末やパイプから読めないときは、起動し直すと出る）。
合言葉と画面の好みは、端末の中の設定の場所に置く（config.py）。

止めるときは Ctrl+C。保存していない変更があるときは、1回目の Ctrl+C では止めずに知らせ、10 秒以内に
もう一度押すと止める（画面仕様 14章の未決11。画面からは、アプリ本体を止める操作を防げないため）。
端末の窓を閉じたときや、OS から止められたとき（SIGTERM）は、知らせずに止まる。

端末に出す文言は、OS の言語で日本語か英語にする（LC_ALL、LC_MESSAGES、LANG の順。無ければ Windows の
表示言語。ja で始まれば日本語）。

終了コード: 0 = 止めた、2 = 起動できなかった（ポートが使えない、設定の場所に書けない、など）。
"""
from __future__ import annotations

import argparse
import errno
import os
import signal
import socket
import stat
import struct
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping

import uvicorn

from .config import Settings, config_dir, load_token
from .server import LAUNCH_SECONDS, TOKEN_PARAM, Launches, Workspace, create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 28470
FALLBACK_PORTS = tuple(range(28471, 28480))
CONFIRM_SECONDS = 10.0
WINDOWS = os.name == "nt"

IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048}
DENIED = {errno.EACCES, getattr(errno, "WSAEACCES", 10013), 10013}
# 止めるときに、待っている接続を RST で閉じる（SO_LINGER をオン・0 秒）。struct linger は Windows では
# u_short 2つ、ほかは int 2つ
LINGER_RESET = struct.pack("HH" if WINDOWS else "ii", 1, 0)

# 端末の文言（画面の辞書とは別。画面の外に出るものだけ）。「ポート」は --port と同じ言葉
MESSAGES: dict[str, tuple[str, str]] = {
    "description": (
        "circuit-sim の作業画面を起動します。",
        "Start the circuit-sim workspace in your browser.",
    ),
    "usage": ("使い方: ", "usage: "),
    "options": ("オプション", "options"),
    "help": ("この説明を出して終わる", "show this help and exit"),
    "port.help": (
        "待ち受けるポート（省くと 28470。使えなければ 28471〜28479、それも使えなければ空いているポート）",
        "port to listen on (default 28470; if it can't be used, 28471-28479, then any free port)",
    ),
    "no_browser.help": (
        "ブラウザを開かない（開く場所の URL を表示する）",
        "don't open a browser (print the URL to open instead)",
    ),
    "error": ("エラー", "error"),
    "arg.unknown": (
        "知らないオプションです: {option}。使えるオプションは circuit-sim --help で確かめてください。",
        "Unknown option: {option}. See circuit-sim --help for the options.",
    ),
    "arg.missing": (
        "{option} の後ろに値を指定してください。使い方は circuit-sim --help で確かめてください。",
        "{option} needs a value. See circuit-sim --help for how to use it.",
    ),
    "arg.unexpected": (
        "{option} には値を付けられません。使い方は circuit-sim --help で確かめてください。",
        "{option} doesn't take a value. See circuit-sim --help for how to use it.",
    ),
    "arg.other": (
        "引数が正しくありません（{detail}）。使い方は circuit-sim --help で確かめてください。",
        "The arguments aren't right ({detail}). See circuit-sim --help for how to use it.",
    ),
    "port.range": (
        "--port には 0〜65535 のポートを指定してください（指定: {port}）。",
        "--port must be a port from 0 to 65535 (given: {port}).",
    ),
    "port.in_use": (
        "ポート {port} は、ほかのプログラムが使っています。別のポートを --port で指定するか、"
        "--port を付けずに起動してください（使えるポートを探して起動します）。",
        "Port {port} is being used by another program. Choose another port with --port, "
        "or start without --port (a port that can be used is chosen).",
    ),
    "port.denied": (
        "ポート {port} は使えません（OS が使用を許していません）。ほかのプログラムがこのポートを専用で"
        "使っているか、Windows では OS が予約しているポートの範囲に入っていることがあります"
        "（使っているプログラムは netstat -ano、予約の範囲は "
        "netsh interface ipv4 show excludedportrange protocol=tcp で確かめられます）。"
        "別のポートを --port で指定するか、--port を付けずに起動してください。",
        "Port {port} can't be used (the OS doesn't allow it). Another program may be using it exclusively, "
        "or on Windows it may be in a port range reserved by the system (check the programs with: "
        "netstat -ano; the reserved ranges with: netsh interface ipv4 show excludedportrange protocol=tcp). "
        "Choose another port with --port, or start without --port.",
    ),
    "port.other": (
        "ポート {port} で待ち受けられませんでした（{reason}）。別のポートを --port で指定するか、"
        "--port を付けずに起動してください。",
        "Couldn't listen on port {port} ({reason}). "
        "Choose another port with --port, or start without --port.",
    ),
    "port.none": (
        "待ち受けるポートを用意できませんでした（{reason}）。"
        "セキュリティソフトなどが、このPCの中の通信（127.0.0.1）を止めていないか確かめてください。",
        "Couldn't open any port to listen on ({reason}). "
        "Check that security software isn't blocking connections within this computer (127.0.0.1).",
    ),
    "port.fallback": (
        "ポート {wanted} が使えないため、ポート {port} で起動します。",
        "Port {wanted} can't be used, so port {port} is used instead.",
    ),
    "config.failed": (
        "設定の場所（{path}）に書き込めないため、起動できません（{reason}）。"
        "その場所に書き込めるか（フォルダの権限、空き容量、同じ名前のファイルが無いか）を確かめてください。",
        "Can't write to the settings folder ({path}), so the app can't start ({reason}). "
        "Check that the folder can be written to (permissions, free space, no file with the same name).",
    ),
    "settings.broken": (
        "設定ファイル（{path}）を読めなかったため、画面の好み（言語、テーマ、仕切りの位置）は既定で開きます"
        "（{reason}）。好みを変えると、元のファイルを {backup} に控えてから書き直します。",
        "Couldn't read the settings file ({path}), so the display preferences (language, theme, panel sizes) "
        "start from the defaults ({reason}). When a preference changes, the old file is kept as {backup} "
        "before the new one is written.",
    ),
    "started": ("circuit-sim を起動しました: {url}", "circuit-sim is running: {url}"),
    "url.note": (
        "この URL は1回だけ、{minutes} 分以内に使えます。ほかの人に見せないでください。",
        "This URL works once, within {minutes} minutes. Don't share it.",
    ),
    "url.renew.enter": (
        "画面を開き直すときや、URL の期限が切れたときは、ここで Enter を押すと新しい URL を出します。",
        "To open the workspace again, or if the URL has expired, press Enter here for a new URL.",
    ),
    "url.renew.restart": (
        "画面を開き直すときや、URL の期限が切れたときは、circuit-sim を起動し直してください"
        "（新しい URL を出します）。",
        "To open the workspace again, or if the URL has expired, start circuit-sim again for a new URL.",
    ),
    "url.new": (
        "新しい URL（1回だけ、{minutes} 分以内に使えます）: {url}",
        "New URL (works once, within {minutes} minutes): {url}",
    ),
    "url.stale.enter": (
        "使い終わったか期限が切れた URL で、画面が開かれました。"
        "ここで Enter を押すと、新しい URL を出します。",
        "The workspace was opened from a URL that was already used or has expired. "
        "Press Enter here for a new URL.",
    ),
    "url.stale.restart": (
        "使い終わったか期限が切れた URL で、画面が開かれました。circuit-sim を起動し直すと、新しい URL を"
        "出します。",
        "The workspace was opened from a URL that was already used or has expired. "
        "Start circuit-sim again for a new URL.",
    ),
    "how_to_stop": ("止めるには、この画面で Ctrl+C を押してください。", "To stop it, press Ctrl+C here."),
    "browser.failed": (
        "ブラウザを開けませんでした。ブラウザで次の場所を開いてください: {url}",
        "Couldn't open a browser. Open this address in your browser: {url}",
    ),
    "unsaved": (
        "保存していない変更があります（{file}）。ブラウザの画面で保存してから止めてください。\n"
        "保存せずに止めるには、{seconds} 秒以内にもう一度 Ctrl+C を押してください。",
        "There are unsaved changes ({file}). Save them in the browser before stopping.\n"
        "To stop without saving, press Ctrl+C again within {seconds} seconds.",
    ),
    "stopped": ("circuit-sim を止めました。", "circuit-sim has stopped."),
}


def terminal_lang(env: Mapping[str, str] | None = None, windows: bool = WINDOWS) -> str:
    """端末の文言の言語。ja で始まる言語の設定なら日本語、それ以外は英語。"""
    env = os.environ if env is None else env
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name)
        if value:
            return "ja" if value.lower().startswith("ja") else "en"
    if windows:
        try:
            import ctypes  # Windows でだけ読む

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ja" if langid & 0x3FF == 0x11 else "en"  # 0x11 = LANG_JAPANESE
        except (AttributeError, OSError):
            pass
    return "en"


class Texts:
    def __init__(self, lang: str) -> None:
        self.index = 0 if lang == "ja" else 1

    def __call__(self, key: str, **params: object) -> str:
        return MESSAGES[key][self.index].format(**params)


def bind(port: int) -> socket.socket:
    """127.0.0.1 のポート port（0 なら空いているポート）に、待ち受けのソケットを作る。

    使えなければ OSError。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if WINDOWS:
            # ほかのプログラム（ほかの利用者を含む）に、同じポートを横取りさせない（Microsoft の勧め）。
            # この設定のソケットは、受け付けた接続が残っていると、閉じた後も同じポートに結べない。
            # 止めるときに待っている接続を RST で閉じて（GuardedServer.shutdown）、起動し直したときに
            # 同じポートで結べるようにする
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            # 止めた直後に同じポートで起動し直せるように
            # （Windows のこの設定は、使用中のポートを奪えてしまうので付けない）
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def port_problem(exc: OSError) -> str:
    """待ち受けられなかった理由: in_use（使用中）、denied（権限・OS の予約の範囲）、other。"""
    codes = {exc.errno, getattr(exc, "winerror", None)}
    if codes & IN_USE:
        return "in_use"
    if codes & DENIED:
        return "denied"
    return "other"


def bind_any(wanted: int | None, binder: Callable[[int], socket.socket] = bind
             ) -> tuple[socket.socket | None, list[tuple[int, OSError]]]:
    """wanted が None なら 28470 → 28471〜28479 → 空いているポートの順に試す。

    決めてあれば、そのポートだけ。"""
    candidates = [wanted] if wanted is not None else [DEFAULT_PORT, *FALLBACK_PORTS, 0]
    failures: list[tuple[int, OSError]] = []
    for port in candidates:
        try:
            return binder(port), failures
        except OSError as exc:
            failures.append((port, exc))
    return None, failures


def bind_failure_text(tr: Texts, wanted: int | None, failures: list[tuple[int, OSError]]) -> str:
    port, exc = failures[-1]
    reason = exc.strerror or str(exc)
    if wanted is None:
        return tr("port.none", reason=reason)
    return tr(f"port.{port_problem(exc)}", port=port, reason=reason)


def reset_idle_connections(connections: Iterable[object]) -> int:
    """応答の途中でない接続に、閉じたら RST を送る印（SO_LINGER 0 秒）を付ける。付けた数を返す。

    FIN で閉じると、ブラウザが自分の側を閉じるまで接続が残り（FIN_WAIT_2、その後の TIME_WAIT）、
    Windows の SO_EXCLUSIVEADDRUSE のソケットは、その間、同じポートに結べない（起動し直すと別のポートへ
    移り、開いている画面が戻れない）。待っている接続（keep-alive）には送りかけのデータが無いので、
    RST で失うものは無い。応答の途中のものは、uvicorn が応答を終えてから閉じる。"""
    count = 0
    for connection in list(connections):
        cycle = getattr(connection, "cycle", None)
        if cycle is not None and not getattr(cycle, "response_complete", True):
            continue
        transport = getattr(connection, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is None:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_RESET)
            count += 1
        except OSError:
            pass
    return count


class GuardedServer(uvicorn.Server):
    """保存していない変更があるとき、1回目の Ctrl+C では止めずに知らせる uvicorn のサーバー。

    止めるときは、待っている接続に「閉じたら RST」の印を付けてから、uvicorn の手順
    （待ち受けを閉じ、接続を閉じる）で止める（reset_idle_connections）。"""

    def __init__(self, config: uvicorn.Config, workspace: Workspace, say: Callable[[str], None],
                 tr: Texts | None = None) -> None:
        super().__init__(config)
        self.workspace = workspace
        self.say = say
        self.tr = tr or Texts(terminal_lang())
        self.warned_at: float | None = None

    def handle_exit(self, sig: int, frame) -> None:
        unsaved = self.workspace.unsaved()
        interactive = sig == signal.SIGINT or (WINDOWS and sig == getattr(signal, "SIGBREAK", None))
        now = time.monotonic()
        recently = self.warned_at is not None and now - self.warned_at <= CONFIRM_SECONDS
        if interactive and unsaved is not None and not recently and not self.should_exit:
            self.warned_at = now
            self.say(self.tr("unsaved", file=unsaved, seconds=int(CONFIRM_SECONDS)))
            return
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        reset_idle_connections(self.server_state.connections)
        await super().shutdown(sockets=sockets)


def launch_url(base: str, number: str) -> str:
    """ブラウザで開く URL。載せるのは起動の番号（合言葉ではない）。"""
    return f"{base}?{TOKEN_PARAM}={number}"


def windows_console(fd: int) -> bool:
    """Windows: fd が本物の端末（コンソール）か。

    os.isatty は NUL（/dev/null に当たるもの）でも True を返すので、GetConsoleMode が通るかで見分ける。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(fd), ctypes.byref(mode)))


def enter_source(fd: int = 0, windows: bool = WINDOWS) -> str | None:
    """Enter を読める入力: "tty"（端末の前にいる）、"pipe"、読めなければ None。

    読めないもの: /dev/null、NUL、ファイル、入力が無い（pythonw など）。

    POSIX で端末の後ろ（バックグラウンド）にいるときは読まない（読むと SIGTTIN で止められる）。"""
    try:
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            return "pipe"
        if windows:
            return "tty" if windows_console(fd) else None
        if os.isatty(fd):
            return "tty" if os.tcgetpgrp(fd) == os.getpgrp() else None
        return None
    except (OSError, AttributeError, ImportError, ValueError):
        return None


def watch_enter(on_enter: Callable[[], None], source: str, fd: int = 0) -> threading.Thread:
    """Enter を押すたびに on_enter を呼ぶ、別のスレッド（sys.stdin のロックを持たない os.read で読む）。"""
    if source == "tty" and not WINDOWS:
        # 後でバックグラウンドへ回されても止められず、読み取りが失敗して終わるように
        signal.signal(signal.SIGTTIN, signal.SIG_IGN)

    def loop() -> None:
        while True:
            try:
                data = os.read(fd, 1024)
            except OSError:
                data = None
            if not data:
                if WINDOWS and source == "tty":
                    time.sleep(0.2)  # Windows の端末は、Ctrl+C で読み取りが切れると 0 バイトを返す
                    continue
                return  # 入力の終わり（Ctrl+D、パイプが閉じた、バックグラウンド）
            if b"\n" in data or b"\r" in data:
                on_enter()

    thread = threading.Thread(target=loop, name="circuit-sim-enter", daemon=True)
    thread.start()
    return thread


def open_when_ready(server: uvicorn.Server, url: str, say: Callable[[str], None], tr: Texts) -> None:
    """サーバーが受け付けを始めたら、既定のブラウザで url を開く。"""
    while not server.started and not server.should_exit:
        time.sleep(0.02)
    if server.should_exit:
        return
    try:
        opened = webbrowser.open(url)
    except Exception:  # ブラウザを起動できない環境（画面の無いサーバーなど）でも、アプリ本体は動かし続ける
        opened = False
    if not opened:
        say(tr("browser.failed", url=url))


def say(text: str) -> None:
    print(text, flush=True)


class Parser(argparse.ArgumentParser):
    def __init__(self, tr: Texts) -> None:
        class Formatter(argparse.HelpFormatter):
            def add_usage(self, usage, actions, groups, prefix=None):
                super().add_usage(usage, actions, groups, prefix=tr("usage"))

        super().__init__(prog="circuit-sim", description=tr("description"), add_help=False,
                         formatter_class=Formatter, exit_on_error=False)
        self.tr = tr
        self._optionals.title = tr("options")
        self.own_messages: set[str] = set()
        self.add_argument("-h", "--help", action="help", help=tr("help"))
        self.add_argument("--port", type=self.port, default=None, help=tr("port.help"))
        self.add_argument("--no-browser", action="store_true", help=tr("no_browser.help"))
        self.takes_value = {"--port"}

    def port(self, text: str) -> int:
        message = self.tr("port.range", port=text)
        self.own_messages.add(message)
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(message) from None
        if not 0 <= value <= 65535:
            raise argparse.ArgumentTypeError(message)
        return value

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        """argparse の英語の文を出さない: 誤りの種類を見分けて、言語の表の文にする（改善5）。"""
        try:
            parsed, extras = self.parse_known_args(args, namespace)
        except argparse.ArgumentError as err:
            name = err.argument_name or ""
            if err.message in self.own_messages:
                self.fail(err.message)
            elif name in self.takes_value:
                self.fail(self.tr("arg.missing", option=name))
            else:
                self.fail(self.tr("arg.unexpected", option=name.split("/")[-1]))
        if extras:
            self.fail(self.tr("arg.unknown", option=extras[0]))
        return parsed

    def fail(self, text: str):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: {self.tr('error')}: {text}\n")

    def error(self, message: str):
        # 上で見分けられなかったもの（argparse が自分で error を呼ぶもの）
        self.fail(self.tr("arg.other", detail=message))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # UTF-8 でない端末（日本語 Windows の cp932 など）でも落ちない
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    tr = Texts(terminal_lang())
    args = Parser(tr).parse_args(argv)

    directory = config_dir()

    def broken(path, reason, backup) -> None:
        say(tr("settings.broken", path=path, reason=reason, backup=backup.name))

    try:
        token = load_token(directory)
        settings = Settings(directory, on_broken=broken)
    except OSError as exc:
        print(tr("config.failed", path=directory, reason=exc.strerror or exc), file=sys.stderr)
        return 2

    sock, failures = bind_any(args.port)
    if sock is None:
        print(bind_failure_text(tr, args.port, failures), file=sys.stderr)
        return 2
    port = sock.getsockname()[1]
    base = f"http://{HOST}:{port}/"
    source = enter_source()
    renew = "enter" if source else "restart"
    minutes = int(LAUNCH_SECONDS // 60)
    launches = Launches(on_stale=lambda: say(tr(f"url.stale.{renew}")))
    url = launch_url(base, launches.issue())

    workspace = Workspace()
    app = create_app(token=token, settings=settings, workspace=workspace, launches=launches)
    config = uvicorn.Config(
        app, log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=2
    )
    server = GuardedServer(config, workspace, say, tr)

    if failures:
        say(tr("port.fallback", wanted=failures[0][0], port=port))
    say(tr("started", url=url))
    say(tr("url.note", minutes=minutes))
    say(tr(f"url.renew.{renew}"))
    say(tr("how_to_stop"))
    if source:
        def renew_url() -> None:
            say(tr("url.new", url=launch_url(base, launches.issue()), minutes=minutes))

        watch_enter(renew_url, source)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(server, url, say, tr), daemon=True).start()
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    say(tr("stopped"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
