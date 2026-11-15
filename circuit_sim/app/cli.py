"""起動コマンド circuit-sim。

  circuit-sim                 127.0.0.1 のポート 28470 でアプリ本体を起動し、既定のブラウザで作業画面を開く。
                              使われていれば 28471〜28479 を順に試し、どれも使えなければ空いているポートを使う
                              （D-050。ポートを決めておくと、ブラウザが覚えた画面の状態が次の起動でも当たる）
  circuit-sim --port 8765     ポートを決めて起動する（使えなければ、理由を出して終わる）
  circuit-sim --no-browser    ブラウザを開かない（開く場所の URL を表示する）

ブラウザに渡す URL には合言葉が入る（/?k=…）。画面はそれをクッキーにして、合言葉の無い URL へ移る（D-051）。
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
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping

import uvicorn

from .config import Settings, config_dir, load_token
from .server import TOKEN_PARAM, Workspace, create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 28470
FALLBACK_PORTS = tuple(range(28471, 28480))
CONFIRM_SECONDS = 10.0
WINDOWS = os.name == "nt"

IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048}
DENIED = {errno.EACCES, getattr(errno, "WSAEACCES", 10013), 10013}

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
        "ポート {port} は使えません（OS が使用を許していません。Windows では、OS が予約している"
        "ポートの範囲に入っていることがあります。"
        "netsh interface ipv4 show excludedportrange protocol=tcp で確かめられます）。"
        "別のポートを --port で指定するか、--port を付けずに起動してください。",
        "Port {port} can't be used (the OS doesn't allow it; on Windows it may be in a port range reserved "
        "by the system - check with: netsh interface ipv4 show excludedportrange protocol=tcp). "
        "Choose another port with --port, or start without --port.",
    ),
    "port.other": (
        "ポート {port} で待ち受けられませんでした（{reason}）。別のポートを --port で指定するか、"
        "--port を付けずに起動してください。",
        "Couldn't listen on port {port} ({reason}). "
        "Choose another port with --port, or start without --port.",
    ),
    "port.none": (
        "待ち受けるポートを用意できませんでした（{reason}）。",
        "Couldn't open any port to listen on ({reason}).",
    ),
    "port.fallback": (
        "ポート {wanted} が使えないため、ポート {port} で起動します。",
        "Port {wanted} can't be used, so port {port} is used instead.",
    ),
    "config.failed": (
        "設定の場所（{path}）に書き込めないため、起動できません（{reason}）。",
        "Can't write to the settings folder ({path}), so the app can't start ({reason}).",
    ),
    "started": ("circuit-sim を起動しました: {url}", "circuit-sim is running: {url}"),
    "url.note": (
        "作業画面はこの URL から開きます。URL に合言葉が入っているので、ほかの人に見せないでください。",
        "Open the workspace from this URL. It contains a key, so don't share it.",
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
            # ほかのプログラム（ほかの利用者を含む）に、同じポートを横取りさせない
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


class GuardedServer(uvicorn.Server):
    """保存していない変更があるとき、1回目の Ctrl+C では止めずに知らせる uvicorn のサーバー。"""

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
                         formatter_class=Formatter)
        self.tr = tr
        self._optionals.title = tr("options")
        self.add_argument("-h", "--help", action="help", help=tr("help"))
        self.add_argument("--port", type=self.port, default=None, help=tr("port.help"))
        self.add_argument("--no-browser", action="store_true", help=tr("no_browser.help"))

    def port(self, text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(self.tr("port.range", port=text)) from None
        if not 0 <= value <= 65535:
            raise argparse.ArgumentTypeError(self.tr("port.range", port=text))
        return value

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: {self.tr('error')}: {message}\n")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # UTF-8 でない端末（日本語 Windows の cp932 など）でも落ちない
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    tr = Texts(terminal_lang())
    args = Parser(tr).parse_args(argv)

    directory = config_dir()
    try:
        token = load_token(directory)
        settings = Settings(directory)
    except OSError as exc:
        print(tr("config.failed", path=directory, reason=exc.strerror or exc), file=sys.stderr)
        return 2

    sock, failures = bind_any(args.port)
    if sock is None:
        print(bind_failure_text(tr, args.port, failures), file=sys.stderr)
        return 2
    port = sock.getsockname()[1]
    base = f"http://{HOST}:{port}/"
    url = f"{base}?{TOKEN_PARAM}={token}"

    workspace = Workspace()
    app = create_app(token=token, settings=settings, workspace=workspace)
    config = uvicorn.Config(
        app, log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=2
    )
    server = GuardedServer(config, workspace, say, tr)

    if failures:
        say(tr("port.fallback", wanted=failures[0][0], port=port))
    say(tr("started", url=url))
    say(tr("url.note"))
    say(tr("how_to_stop"))
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
