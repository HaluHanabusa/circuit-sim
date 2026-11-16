"""起動コマンド circuit-sim。

  circuit-sim                 127.0.0.1 のポート 28470 でアプリ本体を起動し、既定のブラウザで作業画面を開く。
                              使われていれば 28471〜28479 を順に試し、どれも使えなければ空いているポートを使う
                              （D-050。ポートを決めておくと、ブラウザが覚えた画面の状態が次の起動でも当たる）
  circuit-sim --port 8765     ポートを決めて起動する（使えなければ、理由を出して終わる）
  circuit-sim --no-browser    ブラウザを開かない（開く場所の URL を表示する）

起動の URL（/?k=…）には、合言葉ではなく、一度だけ・5 分だけ使える起動の番号を載せる。アプリ本体は
番号を合言葉のクッキーと交換し、番号の無い URL へ移す（D-051、D-053）。
ブラウザには URL を渡さず、その URL へ移るだけの転送用の HTML（設定の場所に、本人だけが読める権限で書く）の
場所を渡す。ブラウザの起動の引数（/proc/<pid>/cmdline、ps）は同じ PC のほかの利用者にも見えるので、番号も
載せない（載せると、ブラウザより先に使われて合言葉を取られうる）。HTML は、番号が使われた・期限が切れた・
止めたときに消す。端末に出す URL（本人にしか見えない）は番号つき。
新しい URL は、端末で Enter を押すと出る（端末やパイプから読めないときは、起動し直すと出る）。
合言葉と画面の好みは、端末の中の設定の場所に置く（config.py）。

起動し直すとき: 設定の場所に前回の起動の記録（ポートとプロセス ID。config.py の last-run.json）を残し、
前回のポートを最初に試す（開いている画面は前回のポートを見ているので、同じポートに戻れば再接続できる）。
前回のプロセスがもう無く、そのポートで待ち受けているものも無いのに結べないとき（前回の接続の後始末が OS に
残っている）は、RESTART_WAIT_SECONDS まで結び直してみる。それでも駄目なら次のポートへ移る。

止めるときは Ctrl+C。保存していない変更があるときは、1回目の Ctrl+C では止めずに知らせ、10 秒以内に
もう一度押すと止める（画面仕様 14章の未決11。画面からは、アプリ本体を止める操作を防げないため）。
Windows の Ctrl+Break も、Ctrl+C と同じに扱う（知らせ方も、終了コードも。break_as_interrupt）。
OS から止められたとき（POSIX の SIGTERM）は、知らせずに止める。
Windows で端末の窓を閉じたとき（ログオフ、シャットダウンも）は、C ランタイムが Ctrl+Break と同じ合図
（SIGBREAK）として渡すので、Ctrl+Break と同じ扱いに入るが、OS がすぐにプロセスを終わらせる。保存して
いない変更の知らせを人が読む時間は無く、後始末まで進むとは限らない（実機では、まだ確かめていない）。

端末の Enter は、起動の直後から読む（ブラウザを開く呼び出しが戻るのを待たない）。Windows で標準入力が
パイプのときは、待たない読み方をする（watch_enter。届いている量を調べて、届いた分だけ読む）。
パイプを読んで待っていると、同じパイプを標準入力に受け継いだプログラム（環境変数 BROWSER のコマンドなど）が
始まれないため（実機で確かめた。docs/design/app-design.md の 8.3）。

端末に出す文言は、OS の言語で日本語か英語にする（LC_ALL、LC_MESSAGES、LANG の順。無ければ Windows の
表示言語。ja で始まれば日本語）。

終了コード: 0 = 止めた（Ctrl+C、Windows の Ctrl+Break）、2 = 起動できなかった（ポートが使えない、設定の
場所に書けない、など）。SIGTERM で止められたときは、後始末の後に合図の既定の動きで終わる（POSIX では -15）。
Windows での動きの根拠は docs/design/app-design.md の 8章。
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import html
import json
import os
import secrets
import signal
import socket
import stat
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import uvicorn

from .config import (
    Settings,
    config_dir,
    create_private,
    ensure_dir,
    load_token,
    read_last_run,
    write_last_run,
)
from .server import LAUNCH_SECONDS, TOKEN_PARAM, Launches, Workspace, create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 28470
FALLBACK_PORTS = tuple(range(28471, 28480))
CONFIRM_SECONDS = 10.0
BROWSER_CHECK_SECONDS = 15.0  # ブラウザを開いてから、画面が開かなかったと案内するまで
LAUNCH_PAGE_PREFIX = "open-"
WINDOWS = os.name == "nt"

IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048}
DENIED = {errno.EACCES, getattr(errno, "WSAEACCES", 10013), 10013}
# 起動し直すとき、前回のポートを結び直してみる時間の上限。開いている画面は 2 秒ごとにアプリ本体を確かめ
# （HEALTH_INTERVAL_MS）、応答を 2 秒待つ（HEALTH_TIMEOUT_MS）。そのとき、ブラウザは前回のプロセスが閉じた
# 待ちの接続（keep-alive）を使おうとして閉じ直すので、OS に残る前回の接続の後始末は、確かめ1回分（約 4 秒）の
# うちに終わる。少し足して 5 秒
RESTART_WAIT_SECONDS = 5.0
RESTART_RETRY_SECONDS = 0.2

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
    "arg.file": (
        "circuit-sim は、起動するときにファイルを受け取りません（{file}）。"
        "仕様JSONは、起動した後に画面の「開く」で開いてください。",
        "circuit-sim doesn't take a file when it starts ({file}). "
        "Open the spec JSON with Open in the workspace after it starts.",
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
    "browser.check": (
        "ブラウザで作業画面が開かないときは、ブラウザで次の場所を開いてください: {url}",
        "If the workspace didn't open in your browser, open this address in it: {url}",
    ),
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

    使えなければ OSError。

    Windows では、どの設定も付けない。
    - SO_REUSEADDR は、使用中のポートを奪えてしまうので付けない。
    - SO_EXCLUSIVEADDRUSE も付けない。Windows Server 2003 からは、決まったアドレス（127.0.0.1）に何も付けずに
      結んだポートを、ほかの利用者が SO_REUSEADDR で奪うことはできない（WSAEACCES。Microsoft の文書
      「Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE」の表）。SO_EXCLUSIVEADDRUSE が足すのは、同じ利用者の
      ほかのプログラムからの守りだけで、それは合言葉のファイルも読める相手なので守りにならない。逆に、この設定の
      ソケットは、受け付けた接続が残っている間は閉じた後も同じポートに結べない（同じ文書）ので、起動し直すと
      別のポートへ移り、開いている画面が戻れない。
    POSIX は SO_REUSEADDR（止めた直後の TIME_WAIT の残りがあっても、同じポートで起動し直せるように。
    POSIX のこの設定は、使用中のポートは奪えない）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def pid_alive(pid: int, windows: bool = WINDOWS) -> bool:
    """プロセス pid が動いているか。分からないときは動いているとみなす（待たずに次のポートへ移る）。"""
    if pid <= 0:
        return False
    if windows:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: あるが、ほかの利用者のもの
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # ほかの利用者のプロセス（PermissionError）など
    return True


def port_listening(port: int) -> bool:
    """127.0.0.1 のポート port で、何かが待ち受けているか（つながるか）。"""
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def port_problem(exc: OSError) -> str:
    """待ち受けられなかった理由: in_use（使用中）、denied（権限・OS の予約の範囲）、other。"""
    codes = {exc.errno, getattr(exc, "winerror", None)}
    if codes & IN_USE:
        return "in_use"
    if codes & DENIED:
        return "denied"
    return "other"


def bind_any(wanted: int | None, binder: Callable[[int], socket.socket] = bind,
             previous: tuple[int, int] | None = None, alive: Callable[[int], bool] = pid_alive,
             listening: Callable[[int], bool] = port_listening, wait: float = RESTART_WAIT_SECONDS,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
             ) -> tuple[socket.socket | None, list[tuple[int, OSError]]]:
    """待ち受けのソケットを作る。（ソケット、試して使えなかったポートと理由の並び）。

    wanted が None なら、前回のポート（previous =（ポート、プロセス ID））→ 28470 → 28471〜28479 → 空いている
    ポートの順に試す。決めてあれば、そのポートだけ。
    前回のポートが使用中でも、前回のプロセスがもう無く、そのポートで待ち受けているものも無ければ（OS に前回の
    接続の後始末が残っているだけ）、wait 秒まで結び直してみる。"""
    candidates = [wanted] if wanted is not None else [DEFAULT_PORT, *FALLBACK_PORTS, 0]
    if wanted is None and previous is not None and previous[0] > 0:
        candidates = [previous[0], *[c for c in candidates if c != previous[0]]]
    failures: list[tuple[int, OSError]] = []
    for port in candidates:
        try:
            return binder(port), failures
        except OSError as exc:
            last = exc
        if (previous is not None and port == previous[0] and port_problem(last) == "in_use"
                and not alive(previous[1]) and not listening(port)):
            deadline = clock() + wait
            while clock() < deadline:
                sleep(RESTART_RETRY_SECONDS)
                try:
                    return binder(port), failures
                except OSError as exc:
                    last = exc
                    if port_problem(exc) != "in_use" or listening(port):
                        break
        failures.append((port, last))
    return None, failures


def bind_failure_text(tr: Texts, wanted: int | None, failures: list[tuple[int, OSError]]) -> str:
    port, exc = failures[-1]
    reason = exc.strerror or str(exc)
    if wanted is None:
        return tr("port.none", reason=reason)
    return tr(f"port.{port_problem(exc)}", port=port, reason=reason)


class GuardedServer(uvicorn.Server):
    """保存していない変更があるとき、1回目の Ctrl+C では止めずに知らせる uvicorn のサーバー。

    止めるときは uvicorn の手順のまま: 待ち受けを閉じ、応答の途中のものは応答を終えてから、どの接続もふつうに
    FIN で閉じる（RST で送りの待ちを捨てない）。送りの待ちのデータは、相手が読む間は送り続ける。ただし、
    止める合図から timeout_graceful_shutdown（main では 2 秒）たっても閉じ終わらない接続は、そこで打ち切る
    （読むのがそれより遅い相手には、応答の終わりが届かないことがある）。その前に on_shutdown を呼ぶ。"""

    def __init__(self, config: uvicorn.Config, workspace: Workspace, say: Callable[[str], None],
                 tr: Texts | None = None, on_shutdown: Callable[[], None] | None = None) -> None:
        super().__init__(config)
        self.on_shutdown = on_shutdown
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
        if self.on_shutdown is not None:
            # SIGTERM・窓を閉じたときは、止めた後に uvicorn が合図を出し直してプロセスが終わり、main の後始末
            # まで進まないので、ここでする（転送用の HTML を消す）
            self.on_shutdown()
        await super().shutdown(sockets=sockets)


@contextlib.contextmanager
def break_as_interrupt(windows: bool) -> Iterator[None]:
    """Windows: Ctrl+Break（SIGBREAK）で止めた後も、Ctrl+C と同じに、main の後始末と「止めました」まで進める。

    uvicorn は、止め終えた後に、受け取った合図の扱いを元に戻してから、同じ合図を出し直す
    （uvicorn.Server.capture_signals の signal.raise_signal）。SIGBREAK の元の扱いは「既定」で、Windows では、
    既定の扱いの SIGBREAK を出すと、その場で終了コード 3 で終わる（実機で確かめた:
    signal.raise_signal(signal.SIGBREAK) の後の終了コードは 3。docs/design/app-design.md の 8.1）。
    そのままだと、「止めました」を出さずに終了コード 3 で終わる（実機で、直す前にそうなった）。
    この中では、元の扱いを Ctrl+C の既定と同じ（KeyboardInterrupt を出す）にしておく。出し直された合図は
    KeyboardInterrupt として呼んだ側に届き、Ctrl+C と同じ終わり方になる。出るときに、扱いを戻す。
    直した後の動きは、Windows の経路を身代わりの合図で流す試験で確かめた。実機では、まだ確かめていない。

    合図の扱いを決められるのは主スレッドだけなので、ほかのスレッドから呼ばれたときは何もしない
    （uvicorn も、主スレッドでなければ合図を受けない）。Windows でないときも、何もしない。"""
    sig = getattr(signal, "SIGBREAK", None)
    if not windows or sig is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(sig, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(sig, previous)


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


# Windows のパイプに何も届いていないとき、次に調べるまで眠る時間。Enter を押してから新しい URL が出るまでの
# 遅れは、長くてもこの時間（設計の原則 5「操作への反応は 0.1 秒以内」に収まる長さ）。調べるのは 1 秒に
# 20 回で、1回は OS への問い合わせ1つなので、届かない間も CPU をほとんど使わない
PIPE_POLL_SECONDS = 0.05
# PeekNamedPipe の失敗のうち、「書く側が閉じた（入力の終わり）」を表すエラーの番号（Microsoft の文書
# System Error Codes: ERROR_BROKEN_PIPE 109 "The pipe has been ended."、ERROR_PIPE_NOT_CONNECTED 233
# "No process is on the other end of the pipe."）。CPython も、109 をパイプの終わりとして扱う
# （Lib/multiprocessing/connection.py）
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_NOT_CONNECTED = 233
PIPE_ENDED = frozenset({ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED})


def windows_pipe_ready(fd: int) -> Callable[[], int | None]:
    """Windows: パイプ fd に届いているバイト数を、読まずに・待たずに調べる関数を作る。

    作った関数は、届いているバイト数（届いていなければ 0）を返す。書く側が閉じていれば None。パイプとして
    調べられない入力なら OSError。

    調べ方は PeekNamedPipe(ハンドル, NULL, 0, NULL, &届いている量, NULL)（Microsoft の文書 PeekNamedPipe:
    名前付きパイプにも、無名パイプの読む側にも使える。バッファを渡さなければ読まない。成功は 0 でない値、
    失敗は 0 で、理由は GetLastError）。CPython の _winapi.PeekNamedPipe も、同じ形で呼ぶ。

    なぜ待たない読み方が要るか: Windows の同期のハンドルは、操作を1つずつしか通さない。アプリ本体が
    標準入力のパイプを読んで待っている間は、そのパイプを標準入力に受け継いだプログラム（標準入力を
    差し替えずに起動されるもの。webbrowser がコマンドで起動するブラウザなど）が、起動のときに標準入力を
    調べる所で止まる（実機で確かめた。docs/design/app-design.md の 8.3）。調べるだけなら、待ちは残らない。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                       wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    available = wintypes.DWORD(0)

    def ready() -> int | None:
        if kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            return available.value
        error = ctypes.get_last_error()
        if error in PIPE_ENDED:
            return None
        raise OSError(errno.EINVAL, f"PeekNamedPipe failed (Windows error {error})")

    return ready


def watch_enter(on_enter: Callable[[], None], source: str, fd: int = 0,
                stop: threading.Event | None = None,
                ready: Callable[[], int | None] | None = None) -> threading.Thread:
    """Enter を押すたびに on_enter を呼ぶ、別のスレッド（sys.stdin のロックを持たない os.read で読む）。

    読み方は入力で決まる:
    - Windows のパイプ: 待たない読み方。届いている量を調べ（ready。省くと windows_pipe_ready）、届いた分だけ
      読む。届いていなければ PIPE_POLL_SECONDS 眠る（stop が立てば、すぐ抜ける）。書く側が閉じたら終わる。
      読み取りの待ちをパイプに残さないので、アプリ本体が起動するプログラムを止めない
    - 端末（どの OS でも）と、Windows でない OS のパイプ: 待つ読み方（os.read が戻るまで待つ）
    ready を渡すと、どの入力でも待たない読み方にする（試験が、Windows でない OS でこの道を流すため）。
    パイプとして調べられない入力（ready が OSError）は、待つ読み方に戻す。"""
    stop = stop if stop is not None else threading.Event()
    if source == "tty" and not WINDOWS:
        # 後でバックグラウンドへ回されても止められず、読み取りが失敗して終わるように
        signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    if ready is None and WINDOWS and source == "pipe":
        try:
            ready = windows_pipe_ready(fd)
        except (OSError, AttributeError, ImportError, ValueError):
            ready = None  # 調べる用意ができない: 待つ読み方

    def loop() -> None:
        peek = ready
        while not stop.is_set():
            size = 1024
            if peek is not None:
                try:
                    count = peek()
                except OSError:
                    peek = None  # パイプとして調べられない入力: 待つ読み方に戻す
                    continue
                if count is None:
                    return  # 書く側が閉じた（入力の終わり）
                if count == 0:
                    stop.wait(PIPE_POLL_SECONDS)  # 届いていない。短く眠る（止めるときは、すぐ抜ける）
                    continue
                size = min(count, size)  # 届いた分だけ読む（待たない）
            try:
                data = os.read(fd, size)
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


def launch_page(url: str, theme: str | None = None) -> bytes:
    """転送用の HTML: 起動の URL へ移るだけ（履歴に残さない location.replace と、スクリプトが動かないときの
    meta refresh、どちらも動かないときのリンク）。移るまでの一瞬の地の色は、覚えているテーマに合わせる。"""
    href = html.escape(url, quote=True)
    scheme = theme if theme in ("light", "dark") else "light dark"
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f'<meta name="color-scheme" content="{scheme}"><meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0;url={href}"><title>circuit-sim</title>'
        f"<script>location.replace({json.dumps(url)});</script>"
        f'</head><body><a href="{href}">circuit-sim</a></body></html>\n'
    ).encode()


class LaunchPages:
    """転送用の HTML（設定の場所に、本人だけが読める権限で書く）。番号ごとに1つ。

    番号が使われた・期限が切れた（spent）、止めた（remove_all）ときに消す。落ちて残ったものは、次の起動で
    消す（remove_stale。ほかに動いている circuit-sim のものを消さないよう、期限より古いものだけ）。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.files: dict[str, Path] = {}
        self.lock = threading.Lock()

    def write(self, number: str, url: str, theme: str | None = None) -> Path:
        ensure_dir(self.directory)
        path = self.directory / f"{LAUNCH_PAGE_PREFIX}{secrets.token_hex(8)}.html"
        if not create_private(path, launch_page(url, theme)):
            raise FileExistsError(errno.EEXIST, "already exists", str(path))
        with self.lock:
            self.files[number] = path
        return path

    def spent(self, number: str) -> None:
        with self.lock:
            path = self.files.pop(number, None)
        _remove(path)

    def remove_all(self) -> None:
        with self.lock:
            paths, self.files = list(self.files.values()), {}
        for path in paths:
            _remove(path)

    @staticmethod
    def remove_stale(directory: Path, older_than: float = LAUNCH_SECONDS) -> None:
        now = time.time()
        try:
            found = list(directory.glob(f"{LAUNCH_PAGE_PREFIX}*.html"))
        except OSError:
            return
        for path in found:
            try:
                if now - path.stat().st_mtime > older_than:
                    path.unlink()
            except OSError:
                pass


def _remove(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass  # 消せなくても、中の番号は使い終わっているか期限が切れている


def open_when_ready(server: uvicorn.Server, target: str, url: str, say: Callable[[str], None], tr: Texts,
                    used: Callable[[], bool] | None = None, wait: float = BROWSER_CHECK_SECONDS) -> None:
    """サーバーが受け付けを始めたら、既定のブラウザで target（転送用の HTML の file:// の場所）を開く。

    開けなければ、url（端末で開く起動の URL）を出す。開いたのに wait 秒たっても番号が使われなければ
    （used() が偽。HTML がブラウザ以外で開いた、ブラウザがその場所を読めない、など）、
    url を開くよう案内する。

    webbrowser.open は、コマンドで起動するブラウザ（環境変数 BROWSER の指定など）が終わるまで戻らない
    ことがある（Python が名前を知らないコマンドは、終わりを待つ）。その間も、端末の Enter は効く
    （Enter は別のスレッドが、起動の直後から読んでいる）。"""
    while not server.started and not server.should_exit:
        time.sleep(0.02)
    if server.should_exit:
        return
    try:
        opened = webbrowser.open(target)
    except Exception:  # ブラウザを起動できない環境（画面の無いサーバーなど）でも、アプリ本体は動かし続ける
        opened = False
    if not opened:
        say(tr("browser.failed", url=url))
        return
    if used is None:
        return
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not server.should_exit:
        if used():
            return
        time.sleep(0.1)
    if not used() and not server.should_exit:
        say(tr("browser.check", url=url))


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
            first = extras[0]
            if first.startswith("-"):
                self.fail(self.tr("arg.unknown", option=first))
            # ファイルを渡して起動しようとした（python -m circuit_sim <ファイル> と同じ形で試す人がいる）
            self.fail(self.tr("arg.file", file=first))
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

    sock, failures = bind_any(args.port, previous=read_last_run(directory))
    if sock is None:
        print(bind_failure_text(tr, args.port, failures), file=sys.stderr)
        return 2
    port = sock.getsockname()[1]
    try:
        write_last_run(directory, port, os.getpid())  # 次に起動し直したとき、同じポートを最初に試す
    except OSError:
        pass
    base = f"http://{HOST}:{port}/"
    source = enter_source()
    renew = "enter" if source else "restart"
    minutes = int(LAUNCH_SECONDS // 60)
    pages = LaunchPages(directory)
    LaunchPages.remove_stale(directory)
    launches = Launches(on_stale=lambda: say(tr(f"url.stale.{renew}")), on_spent=pages.spent)
    number = launches.issue()
    url = launch_url(base, number)

    workspace = Workspace()
    app = create_app(token=token, settings=settings, workspace=workspace, launches=launches)
    config = uvicorn.Config(
        app, log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=2
    )
    server = GuardedServer(config, workspace, say, tr, on_shutdown=pages.remove_all)

    if failures:
        say(tr("port.fallback", wanted=failures[0][0], port=port))
    say(tr("started", url=url))
    say(tr("url.note", minutes=minutes))
    say(tr(f"url.renew.{renew}"))
    say(tr("how_to_stop"))
    stop_reading = threading.Event()
    if source:
        def renew_url() -> None:
            say(tr("url.new", url=launch_url(base, launches.issue()), minutes=minutes))

        # Enter は、起動の直後から読む（ブラウザを開く呼び出しが戻るのを待たない）
        watch_enter(renew_url, source, stop=stop_reading)
    if not args.no_browser:
        try:
            # ブラウザには、番号を書いた本人だけが読める HTML の場所を渡す（起動の引数に番号を載せない）
            target = pages.write(number, url, settings.read().get("theme")).as_uri()
        except OSError:
            say(tr("browser.failed", url=url))
        else:
            def used() -> bool:
                return not launches.is_open(number)

            threading.Thread(target=open_when_ready, args=(server, target, url, say, tr, used),
                             daemon=True).start()
            sweeper = threading.Timer(LAUNCH_SECONDS + 1, launches.sweep)  # 期限で HTML を消す
            sweeper.daemon = True
            sweeper.start()
    try:
        with break_as_interrupt(WINDOWS):  # Windows の Ctrl+Break でも、下の KeyboardInterrupt に届く
            server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        stop_reading.set()
        sock.close()
        pages.remove_all()
    say(tr("stopped"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
