"""起動コマンド circuit-sim。

  circuit-sim                 127.0.0.1 の空いている番号でアプリ本体を起動し、既定のブラウザで作業画面を開く
  circuit-sim --port 8765     番号を決めて起動する（使われていれば、理由を出して終わる）
  circuit-sim --no-browser    ブラウザを開かない（開く場所の URL を表示する）

止めるときは Ctrl+C。保存していない変更があるときは、1回目の Ctrl+C では止めずに知らせ、10 秒以内に
もう一度押すと止める（画面仕様 14章の未決11。画面からは、アプリ本体を止める操作を防げないため）。
端末の窓を閉じたときや、OS から止められたとき（SIGTERM）は、知らせずに止まる。

終了コード: 0 = 止めた、2 = 起動できなかった（番号が使われている、など）。
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable

import uvicorn

from .server import Workspace, create_app

HOST = "127.0.0.1"
CONFIRM_SECONDS = 10.0
WINDOWS = os.name == "nt"


def bind(port: int) -> socket.socket:
    """127.0.0.1 の番号 port（0 なら空いている番号）に、待ち受けのソケットを作る。使われていれば OSError。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not WINDOWS:
        # 止めた直後に同じ番号で起動し直せるように（Windows のこの設定は、使用中の番号を奪えてしまうので付けない）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


class GuardedServer(uvicorn.Server):
    """保存していない変更があるとき、1回目の Ctrl+C では止めずに知らせる uvicorn のサーバー。"""

    def __init__(self, config: uvicorn.Config, workspace: Workspace, say: Callable[[str], None]) -> None:
        super().__init__(config)
        self.workspace = workspace
        self.say = say
        self.warned_at: float | None = None

    def handle_exit(self, sig: int, frame) -> None:
        unsaved = self.workspace.unsaved()
        interactive = sig == signal.SIGINT or (WINDOWS and sig == getattr(signal, "SIGBREAK", None))
        now = time.monotonic()
        recently = self.warned_at is not None and now - self.warned_at <= CONFIRM_SECONDS
        if interactive and unsaved is not None and not recently and not self.should_exit:
            self.warned_at = now
            self.say(
                f"保存していない変更があります（{unsaved}）。ブラウザの画面で保存してから止めてください。\n"
                f"保存せずに止めるには、{int(CONFIRM_SECONDS)} 秒以内にもう一度 Ctrl+C を押してください。"
            )
            return
        super().handle_exit(sig, frame)


def open_when_ready(server: uvicorn.Server, url: str, say: Callable[[str], None]) -> None:
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
        say(f"ブラウザを開けませんでした。ブラウザで次の場所を開いてください: {url}")


def say(text: str) -> None:
    print(text, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="circuit-sim", description="circuit-sim の作業画面を起動する")
    parser.add_argument("--port", type=int, default=0, help="待ち受ける番号（省くと空いている番号を使う）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print(f"--port には 0〜65535 の番号を指定してください（指定された値: {args.port}）。", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):  # UTF-8 でない端末（日本語 Windows の cp932 など）でも落ちない
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    try:
        sock = bind(args.port)
    except OSError:
        print(
            f"番号 {args.port} は、ほかのプログラムが使っています。"
            "別の番号を --port で指定するか、--port を付けずに起動してください（空いている番号を使います）。",
            file=sys.stderr,
        )
        return 2
    port = sock.getsockname()[1]
    url = f"http://{HOST}:{port}/"

    workspace = Workspace()
    app = create_app(workspace=workspace)
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off", timeout_graceful_shutdown=2)
    server = GuardedServer(config, workspace, say)

    say(f"circuit-sim を起動しました: {url}")
    say("止めるには、この画面で Ctrl+C を押してください。")
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(server, url, say), daemon=True).start()
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    say("circuit-sim を止めました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
