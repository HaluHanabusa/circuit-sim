"""受け入れ試験の部品: アプリ本体を「偽のブラウザだけ」で起動する（本物のブラウザへ進ませない）。

なぜ要るか: Python の webbrowser は、環境変数 BROWSER の候補を、OS の既定の候補の「前」に足すだけで、既定の
候補は残す（CPython の Lib/webbrowser.py の register_standard_browsers）。open は候補を前から順に試し、
偽を返した候補（起動できない、0 でない終了コード）の次へ進む（同じファイルの open と GenericBrowser.open）。
BROWSER に偽のブラウザを置くだけでは、偽が失敗したときに、OS の既定のブラウザ（Windows は os.startfile、
macOS は osascript、Linux は xdg-open など）が、生きている起動の番号を持つ転送用の HTML を開く。
試験が、利用者の本物のブラウザで、生きている起動の番号を開くのは許されない。

守りは二重（どちらも、アプリ本体を始める前に、同じプロセスの中で仕掛ける。製品のコードは変えない）:
1. 候補の先頭に「BROWSER の偽だけを試し、結果がどうでも『開いた』と答えて終わる」候補を登録する
   （webbrowser.register の preferred。文書にある使い方）。次の候補（本物）は試されない。
2. Python の監査の仕組み（sys.addaudithook。PEP 578）で、webbrowser が偽のほかのものを起動しようとしたら
   （os.startfile、webbrowser の中からの subprocess.Popen と os.system）、起動させずに例外で止める。
   1 が効かなくなっても（webbrowser の作りが変わる、など）、本物は開かない。

起きたことは、記録のファイル（環境変数 NOTES_VAR。conftest.py が試験ごとに決める）に1行ずつ残す:
  fake-failed <開く場所>     偽のブラウザが失敗した（起動できない、0 でない終了コード、例外）
  real-route <経路>          本物のブラウザを開く経路が呼ばれた（止めた）
  unguarded <開く場所>       この部品を通さずに、ブラウザを開く起動をした試験がある（SENTINEL が呼ばれた）
conftest.py は、real-route と unguarded の行が残った試験を失敗にする。

実装者の試験（tests/test_app_cli.py の BROWSER_GUARD）と考え方は同じだが、受け入れ試験は実装者の試験の
部品に頼らないので、ここに自前で持つ。
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

# 記録のファイルの場所を渡す環境変数
NOTES_VAR = "A0_FAKE_BROWSER_NOTES"
# 見張りそのものの試験のための入口: アプリ本体を始めずに、この場所を webbrowser.open で開いて結果を出す
PROBE_VAR = "A0_FAKE_BROWSER_PROBE"
# 見張りそのものの試験のための対照: "off" なら、守りの 1 を外す（偽が失敗すると、次の候補へ進む）
STOP_VAR = "A0_FAKE_BROWSER_STOP"
# 見張りそのものの試験のための入口（Windows）: アプリ本体を始めずに、この場所を os.startfile で開いてみる
# （守りの 2 が、Windows の本物の経路を止めることの確かめ。無い場所を渡す）
STARTFILE_VAR = "A0_FAKE_BROWSER_PROBE_STARTFILE"

# python -c で流す入口。アプリ本体（python -m circuit_sim.app と同じ）を、守りを仕掛けてから始める
GUARD_ENTRY = r'''
import os, runpy, shlex, subprocess, sys, threading, webbrowser

NOTES = os.environ["A0_FAKE_BROWSER_NOTES"]
FAKE = os.environ["BROWSER"]
FAKE_ARGV = shlex.split(FAKE)[:-1]      # 偽のブラウザのコマンド（最後の %s を除く。webbrowser と同じ分け方）
STOP = os.environ.get("A0_FAKE_BROWSER_STOP") != "off"
WEBBROWSER_FILE = os.path.normcase(os.path.abspath(webbrowser.__file__))
state = threading.local()


def note(kind, text):
    with open(NOTES, "a", encoding="utf-8") as f:
        f.write(kind + "\t" + " ".join(str(text).split()) + "\n")


class RealBrowserBlocked(RuntimeError):
    """試験の中で、本物のブラウザを開く経路が呼ばれた（開かせない）。"""


class OnlyTheFake:
    """BROWSER の偽だけを試す。結果がどうでも「開いた」と答えて、次の候補（本物）へ進ませない。"""

    def open(self, url, new=0, autoraise=True):
        ok = False
        state.in_fake = True
        try:
            ok = bool(webbrowser.get(FAKE).open(url, new, autoraise))
        except Exception as exc:
            note("fake-failed", f"{url} ({type(exc).__name__}: {exc})")
            ok = None
        finally:
            state.in_fake = False
        if ok is False:
            note("fake-failed", url)
        return True if STOP else bool(ok)


def is_fake(args):
    """起動されるコマンドが、偽のブラウザか（Windows では、1つの文字列になってから監査に届く）。"""
    if isinstance(args, str):
        return args.startswith(subprocess.list2cmdline(FAKE_ARGV) + " ")
    return [str(arg) for arg in list(args)[:len(FAKE_ARGV)]] == FAKE_ARGV


def from_webbrowser():
    frame = sys._getframe(2)
    while frame is not None:
        if os.path.normcase(os.path.abspath(frame.f_code.co_filename)) == WEBBROWSER_FILE:
            return True
        frame = frame.f_back
    return False


def tripwire(event, args):
    if event == "os.startfile" or event == "os.startfile/2":
        route = "os.startfile " + str(args[0])
    elif event in ("subprocess.Popen", "os.system"):
        if getattr(state, "in_fake", False) or not from_webbrowser():
            return
        if event == "subprocess.Popen" and is_fake(args[1]):
            return      # 守りの 1 を外した対照で、webbrowser が BROWSER の候補（偽）を直接試すとき
        route = event + " " + str(args[1] if event == "subprocess.Popen" else args[0])
    else:
        return
    note("real-route", route)
    raise RealBrowserBlocked(route)


# 先に登録する（登録のときに、webbrowser が OS の既定の候補を調べる。Linux では xdg-settings を呼ぶ）
webbrowser.register("a0-acceptance-only-the-fake", None, OnlyTheFake(), preferred=True)
sys.addaudithook(tripwire)

probe = os.environ.get("A0_FAKE_BROWSER_PROBE")
startfile = os.environ.get("A0_FAKE_BROWSER_PROBE_STARTFILE")
if startfile:
    try:
        os.startfile(startfile)
        print("opened", flush=True)
    except RealBrowserBlocked as exc:
        print("blocked:", exc, flush=True)
    except (OSError, AttributeError) as exc:
        print("not blocked:", type(exc).__name__, exc, flush=True)
elif probe:
    try:
        print("opened:", webbrowser.open(probe), flush=True)
    except RealBrowserBlocked as exc:
        print("blocked:", exc, flush=True)
else:
    runpy.run_module("circuit_sim.app", run_name="__main__", alter_sys=True)
'''

# この部品を通さずに、ブラウザを開く起動をした試験を見つける見張り役（conftest.py が、試験の間
# BROWSER に置く）。
# 呼ばれたら、記録に unguarded の行を足して、0 で終わる（webbrowser は、ここで終わりにする）
SENTINEL = (
    "import os, sys\n"
    f"with open(os.environ[{NOTES_VAR!r}], 'a', encoding='utf-8') as f:\n"
    "    f.write('unguarded\\t' + ' '.join(sys.argv[1:]) + '\\n')\n"
)


def command(script: Path) -> str:
    """BROWSER に置く、偽のブラウザのコマンド（%s に開く場所が入る）。

    webbrowser は、%s を含む指定を shlex（POSIX の決まり）で分けるので、\\ は / に直し（Windows の API は
    どちらも受ける）、空白を含む場所は引用符で包む。"""
    parts = (sys.executable, str(script))
    return " ".join(shlex.quote(part.replace("\\", "/")) for part in parts) + " %s"


def app_argv(*args: str) -> list[str]:
    """偽のブラウザだけでアプリ本体を起動するコマンド（python -m circuit_sim.app <args> の代わり）。

    環境変数には、BROWSER（command の値）と NOTES_VAR（conftest.py が試験ごとに置く）が要る。"""
    return [sys.executable, "-c", GUARD_ENTRY, *args]


def read_notes(path: Path | str | None = None) -> list[tuple[str, str]]:
    """記録の行を（種類、中身）の並びで返す。path を省くと、いまの試験の記録（NOTES_VAR）。"""
    target = Path(path if path is not None else os.environ[NOTES_VAR])
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [tuple(line.split("\t", 1)) for line in text.splitlines() if "\t" in line]  # type: ignore[misc]


def noted(kind: str, path: Path | str | None = None) -> list[str]:
    return [text for found, text in read_notes(path) if found == kind]


def clear_notes(path: Path | str | None = None) -> None:
    """記録を消す（見張りそのものの試験が、わざと残した行を片付けるときだけ使う）。"""
    Path(path if path is not None else os.environ[NOTES_VAR]).unlink(missing_ok=True)


def problem(path: Path | str | None = None) -> str:
    """偽のブラウザが失敗した、本物の経路が呼ばれた、のどちらかがあれば、その説明（無ければ空の文字列）。

    「偽のブラウザが呼ばれない」で落とすときに添える（原因が、偽の失敗なのか、開く呼び出しが戻らないのか）。"""
    found = [f"{kind}: {text}" for kind, text in read_notes(path)]
    return "（偽のブラウザの記録: " + "、".join(found) + "）" if found else ""
