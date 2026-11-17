"""A0 の独立検証（verifier、6回目）その2: Windows のパイプを「待たない読み方」で読む部分の確かめ。

記録は docs/verification/A0-verifier-6.md。対象は circuit_sim/app/cli.py の watch_enter と windows_pipe_ready
（80da2ea..9f594c5）。Windows の実機（PO の PC）で確かめられている事実は1つ: 標準入力のパイプを読んで
待っている間に、標準入力を差し替えずに起動した子の Python は、始まれない（待たない対照は始まる）。
だから守ることは「アプリ本体が Enter を待っている間、パイプに読み取りの待ちを残さない」。

ここで確かめること:
1. PeekNamedPipe の呼び方（どの OS でも流れる）。kernel32 の代わりに、本物の C の関数（ctypes が作る、
   C から呼べる関数）を置き、製品が決めた引数の型で、ctypes に実際に引数を運ばせる。運ばれた値を見る:
   ハンドルが 64 ビットのまま届く、バッファと「読んだ量」と「残り」は NULL、大きさは 0、量を受け取る先は
   5 番目。失敗の理由は ctypes.get_last_error()（WinDLL に use_last_error=True を付けたときだけ正しい）。
   呼び方の出どころは Microsoft の文書 PeekNamedPipe（引数の並び、NULL でよい引数、成功は 0 でない値）と
   CPython 3.13.2 の Modules/_winapi.c（同じ API を PeekNamedPipe(handle, NULL, 0, NULL, &navail, &nleft)
   と呼ぶ）。
2. 読み方の選び方: Windows のパイプは待たない読み方、端末と Windows でない OS は待つ読み方。
   Windows でない OS では、製品の分かれ目（cli.WINDOWS）を真にし、PeekNamedPipe の所だけを POSIX の
   身代わり（FIONREAD と POLLHUP）にして、製品の選び方をそのまま通す。Windows では、本物が流れる。
3. 回り方: 届いていない間は読みに行かない、届いた分だけ読む（1 行が分かれて届く、CR LF、CR だけ、
   複数の行、1024 バイトより多い）、Enter への反応の速さ、止める合図、書く側が閉じたとき、調べられない
   入力（1回で待つ読み方に戻る。回り続けない）。
4. アプリ本体ごと（POSIX だけ。Windows の分かれ目を真にした起動）: Enter を待っている間、標準入力に
   読み取りの待ちが1度も無い。対照（調べる用意ができない）では、待つ読み方に戻って、待ちが残る。
5. Windows でだけ流れるもの（PO の PC で最初に流れる。落ちたときに原因が分かるよう、調べた値を添える）:
   本物のパイプでの答えが、標準ライブラリの _winapi.PeekNamedPipe と合う。書く側が閉じたら None。
   NUL とファイルは、パイプとして調べられない（OSError）。製品の読み方で待っている間に、子が始まれる
   （対照: 読んで待つと始まれない）。

身代わりが Windows の約束と同じと言える範囲: 「届いた量を、読まずに・待たずに答える」「届いた分が残って
いる間は、書く側が閉じていても量を答える」「空で、書く側が閉じていたら終わり」。違う所（Windows では
失敗の番号で終わりを知る、ハンドルを共有する別のプロセスが読んでいると PeekNamedPipe も待つ）は、
身代わりでは確かめられない。
"""
from __future__ import annotations

import ctypes
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import types
from ctypes import wintypes
from pathlib import Path

import pytest

from circuit_sim.app import cli

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"
WAIT = 30.0     # 待つ上限（合否の基準ではない）


# ------------------------------------------------------------ 1. PeekNamedPipe の呼び方

# 64 ビットの Python では、32 ビットに収まらないハンドルの値を使う（型の指定が無い・誤っていると、
# 上の桁が落ちる）。本物のハンドルも、型は void*（Microsoft の文書 Windows Data Types の HANDLE）
BIG_HANDLE = 0x1234_5678_9ABC if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x1234_5678


class FakeKernel32:
    """kernel32 の身代わり。PeekNamedPipe は、ctypes が実際に呼ぶ C の関数（中身は Python）。

    製品は、この関数に引数の型（argtypes）と戻り値の型（restype）を決めてから呼ぶ。関数そのものは、型を
    何も決めていない形で渡す（本物の DLL から取り出した関数と同じ。決めなければ、整数は C の int で
    運ばれる）。
    届いた引数は、C の側の型（ポインターは番地、大きさは DWORD）で受け取って控える。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.available: int | None = 0      # None なら失敗（戻り値 0）
        proto = ctypes.CFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        self._keep = proto(self._peek)      # C から呼べる関数（持ち続けないと、消える）
        address = ctypes.cast(self._keep, ctypes.c_void_p).value
        self.PeekNamedPipe = ctypes.CFUNCTYPE(ctypes.c_int)(address)    # 型を決めていない関数

    def _peek(self, handle, buffer, size, read, available, left) -> int:
        self.calls.append({"handle": handle, "buffer": buffer, "size": size, "read": read,
                           "available": available, "left": left})
        if self.available is None:
            return 0
        if available:
            wintypes.DWORD.from_address(available).value = self.available
        if left:
            wintypes.DWORD.from_address(left).value = 0
        return 1


def test_peek_named_pipe_gets_its_arguments_as_microsoft_documents(monkeypatch):
    """windows_pipe_ready が、PeekNamedPipe を文書どおりに呼び、答えと失敗の理由を正しく読む。

    BOOL PeekNamedPipe(HANDLE hNamedPipe, LPVOID lpBuffer, DWORD nBufferSize, LPDWORD lpBytesRead,
                       LPDWORD lpTotalBytesAvail, LPDWORD lpBytesLeftThisMessage)
    - 読まずに量だけ調べる: lpBuffer は NULL、nBufferSize は 0、lpBytesRead は NULL
    - 量は 5 番目（lpTotalBytesAvail）の先に書かれる。6 番目は NULL でも、受け取る先を渡してもよい
    - 成功は 0 でない値。失敗は 0 で、理由は GetLastError
    失敗の理由を ctypes で確かに読むには、WinDLL(..., use_last_error=True) と ctypes.get_last_error() を
    組にする（Python の文書 ctypes の use_last_error）。付けないと、get_last_error() は前の値のまま。
    番号は Microsoft の文書 System Error Codes: 109 ERROR_BROKEN_PIPE、233 ERROR_PIPE_NOT_CONNECTED は
    「書く側が閉じた」。1 ERROR_INVALID_FUNCTION、5 ERROR_ACCESS_DENIED、6 ERROR_INVALID_HANDLE、
    232 ERROR_NO_DATA は、そう扱わない（パイプとして調べられない）。"""
    kernel32 = FakeKernel32()
    loaded: list[tuple] = []
    last_error = {"value": 0, "asked": 0}

    def win_dll(name, *args, **kwargs):
        loaded.append((name, args, kwargs))
        return kernel32

    def get_last_error() -> int:
        last_error["asked"] += 1
        return last_error["value"]

    monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", get_last_error, raising=False)
    fake_msvcrt = types.SimpleNamespace(get_osfhandle=lambda fd: BIG_HANDLE + fd)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    ready = cli.windows_pipe_ready(3)
    assert len(loaded) == 1, loaded
    name, args, kwargs = loaded[0]
    assert str(name).lower() in ("kernel32", "kernel32.dll"), f"PeekNamedPipe は kernel32 にある: {name!r}"
    assert kwargs.get("use_last_error") is True and not args, (
        f"use_last_error=True が無いと、失敗の理由（GetLastError）を読めない: {args!r} {kwargs!r}"
    )

    assert ready() == 0, "届いていないときは 0"
    kernel32.available = 5
    assert ready() == 5
    kernel32.available = 70_000     # パイプの入れ物より多くても、そのまま答える
    assert ready() == 70_000
    assert last_error["asked"] == 0, "成功したのに、失敗の理由を読んだ"
    assert len(kernel32.calls) == 3
    for call in kernel32.calls:
        assert call["handle"] == BIG_HANDLE + 3, (
            f"ハンドルが、そのまま届いていない: 届いた値 {call['handle']!r}、渡した値 {BIG_HANDLE + 3:#x}"
            "（型の指定が無い・誤っていると、64 ビットの Windows で上の桁が落ちる）"
        )
        assert call["buffer"] is None and call["size"] == 0 and call["read"] is None, (
            f"読まずに調べるときは、バッファは NULL、大きさは 0、読んだ量の先も NULL: {call}"
        )
        assert call["available"], "量を受け取る先（5 番目の引数）が NULL"
    kernel32.available = None
    for ended in (109, 233):
        last_error["value"] = ended
        assert ready() is None, f"Windows のエラー {ended} は、書く側が閉じた（入力の終わり）"
    for other in (1, 5, 6, 232):
        last_error["value"] = other
        with pytest.raises(OSError):
            ready()
    kernel32.available = 2
    assert ready() == 2, "失敗の後も、次の答えを正しく読む"


# ------------------------------------------------------------ 2・3. 読み方の選び方と、回り方


def posix_arrived(fd: int) -> int:
    """POSIX: パイプ fd に届いているバイト数（読まない、待たない）。"""
    import array
    import fcntl
    import termios

    count = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, count)
    return count[0]


def posix_stand_in(fd: int):
    """windows_pipe_ready の身代わり（POSIX）。約束は同じ: 届いた量、届いていなければ 0、空で書く側が
    閉じていたら None。

    先に「書く側が閉じたか」を見てから、量を見る（閉じた後に届くものは無いので、閉じていて量が 0 なら、
    確かに終わり）。逆の順だと、量を見た後・閉じたかを見る前に「書いて、閉じる」が挟まったとき、届いた分を
    残したまま終わりと答えてしまう（レビュー 6回目の軽微1 が、実装者の身代わりで指摘した競合）。"""
    import select

    def ready() -> int | None:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        closed = any(event & select.POLLHUP for _, event in poller.poll(0))
        count = posix_arrived(fd)
        if count:
            return count
        return None if closed else 0

    return ready


def arrived(fd: int) -> int:
    """この OS で、パイプ fd に届いているバイト数。製品の調べ方とは別の道で調べる（Windows は、標準
    ライブラリの _winapi.PeekNamedPipe）。"""
    if not WINDOWS:
        return posix_arrived(fd)
    import _winapi
    import msvcrt

    return _winapi.PeekNamedPipe(msvcrt.get_osfhandle(fd), 0)[0]


# 読むスレッドが回り続ける後退が入ったときの歯止め: 調べた回数がこれを超えたら、そのスレッドを終わらせる
# （試験は、回数や Enter の数で落ちる。回り続けるスレッドを、後の試験まで残さない）。眠りが 0.05 秒なら、
# 1つの試験で調べるのは数百回まで
RUNAWAY_CHECKS = 20_000


class Polled:
    """製品が Windows のパイプのために用意した調べ方（windows_pipe_ready が作る関数）の見張り。

    made は用意した fd、checks は調べた回数、answers は 0 のほかの答え（同じ答えが続いたら1つ）、
    after_end は「書く側が閉じた」（None）と答えた後に、また調べられた回数。
    Windows では本物を包む。ほかの OS では身代わりを包む。"""

    def __init__(self, real) -> None:
        self.real = real
        self.made: list[int] = []
        self.checks = 0
        self.answers: list = []
        self.ended = False
        self.after_end = 0

    def __call__(self, fd: int):
        self.made.append(fd)
        inner = self.real(fd)

        def ready():
            self.checks += 1
            if self.ended:
                self.after_end += 1
            if self.checks > RUNAWAY_CHECKS:
                raise SystemExit("読むスレッドが回り続けている（歯止め）")
            answer = inner()
            if answer != 0 and (not self.answers or self.answers[-1] != answer):
                self.answers.append(answer)
            self.ended = self.ended or answer is None
            return answer

        return ready


@pytest.fixture
def polled(monkeypatch):
    """製品を「Windows のパイプ」の道に入れる。Windows では何も偽らない。ほかの OS では、cli.WINDOWS を
    真にし、PeekNamedPipe の所だけを身代わりにする（読み方を選ぶのは、製品のコードのまま）。"""
    watch = Polled(cli.windows_pipe_ready if WINDOWS else posix_stand_in)
    if not WINDOWS:
        monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "windows_pipe_ready", watch)
    yield watch
    assert watch.checks <= RUNAWAY_CHECKS, "読むスレッドが、眠らずに調べ続けた（歯止めで止めた）"
    assert watch.after_end == 0, f"書く側が閉じた後も、{watch.after_end} 回調べた"


class Reads:
    """os.read の見張り: fd を読みに行くたびに、その時点で届いていた量と、頼んだ大きさ、読めた量を控える。"""

    def __init__(self, monkeypatch, fd: int) -> None:
        self.log: list[tuple[int, int, int]] = []
        real_read = os.read

        def watched(target, size):
            if target != fd:
                return real_read(target, size)
            before = arrived(fd)
            data = real_read(target, size)
            self.log.append((before, size, len(data)))
            return data

        monkeypatch.setattr(os, "read", watched)


class Enters:
    """Enter を受けた回数（待つときは、上限つき）。"""

    def __init__(self) -> None:
        self.count = 0
        self.changed = threading.Condition()

    def __call__(self) -> None:
        with self.changed:
            self.count += 1
            self.changed.notify_all()

    def wait_for(self, count: int, timeout: float = 10.0) -> bool:
        with self.changed:
            return self.changed.wait_for(lambda: self.count >= count, timeout)


def close_all(*fds: int | None) -> None:
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def test_a_windows_pipe_is_polled_and_never_read_before_something_arrives(polled, monkeypatch):
    """Windows のパイプ: 届いている量を調べてから読む。届いていない間は、読みに行かない（待ちを残さない）。

    届いた分だけ読む。1 行が2回に分かれて届いても Enter は1回。CR LF は1回。CR だけでも Enter（22d2fdc と
    同じ）。1回の読み取りに複数の行が入っていても1回（22d2fdc と同じ）。1024 バイトより多く届いたら、
    何回かに分けて全部読み、終わりの Enter を見落とさない。
    止める合図で、眠りからすぐ抜けて終わる（1 秒は上限。眠りは PIPE_POLL_SECONDS）。"""
    r, w = os.pipe()
    reads = Reads(monkeypatch, r)
    enters = Enters()
    stop = threading.Event()
    thread = None
    try:
        started = time.monotonic()
        thread = cli.watch_enter(enters, "pipe", fd=r, stop=stop)
        assert polled.made == [r], "Windows のパイプなのに、待たない読み方を用意していない"
        time.sleep(0.5)     # 何も届かない間
        idle_checks, idle = polled.checks, time.monotonic() - started
        assert reads.log == [], f"届いていないのに、読みに行った（そこで待つ）: {reads.log}"
        assert enters.count == 0
        # 調べるのは、眠り（PIPE_POLL_SECONDS）おき。上限は、実際にたった時間から決める
        # （回り続けると、桁が違う）
        assert 2 <= idle_checks <= idle / cli.PIPE_POLL_SECONDS + 3, (
            f"{idle:.2f} 秒の間に調べた回数が {idle_checks}（眠りは {cli.PIPE_POLL_SECONDS} 秒のはず）"
        )
        sent = 0
        steps = [(b"ab", 0), (b"c\n", 1), (b"\r\n", 2), (b"\r", 3), (b"x\ny\nz\n", 4),
                 (b"x" * 2999 + b"\n", 5)]
        for data, wanted in steps:
            os.write(w, data)
            sent += len(data)
            if wanted > enters.count:
                assert enters.wait_for(wanted), f"{data[:8]!r}… を送っても、Enter を受けない"
            deadline = time.monotonic() + 5
            while sum(got for _, _, got in reads.log) < sent:     # 送った分を読み終えるまで
                assert time.monotonic() < deadline, f"届いた分を読み終えない: {reads.log}"
                time.sleep(0.01)
            time.sleep(0.15)
            assert enters.count == wanted, f"{data[:8]!r}… の後の Enter の回数: {enters.count}"
        assert all(0 < size <= before and got == size for before, size, got in reads.log), (
            "届いた量より多く読もうとした、または届いていないのに読みに行った"
            f"（届いた量、頼んだ量、読めた量）: {reads.log}"
        )
        assert max(size for _, size, _ in reads.log) <= 1024
        assert sum(got for _, _, got in reads.log) == sent
        stop.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive(), "止める合図の後、1 秒たっても読むスレッドが終わらない"
    finally:
        stop.set()
        close_all(w)
        if thread is not None:
            thread.join(timeout=5)
        close_all(r)


def test_enter_on_a_windows_pipe_is_answered_within_a_tenth_of_a_second_or_so(polled):
    """待たない読み方でも、Enter への反応は遅れない。

    眠りの長さ（PIPE_POLL_SECONDS）が、Enter から反応までの遅れの上限。設計の原則 5 は「操作への反応は
    0.1 秒以内」（docs/design/app-design.md 4章）なので、眠りは 0.1 秒以下。実測の上限 0.5 秒は、混んだ
    機械でのスレッドの切り替えを見込んだ値（眠りが 1 秒に延びるような後退とは見分けられる）。"""
    assert 0 < cli.PIPE_POLL_SECONDS <= 0.1
    r, w = os.pipe()
    enters = Enters()
    stop = threading.Event()
    thread = None
    try:
        thread = cli.watch_enter(enters, "pipe", fd=r, stop=stop)
        worst = 0.0
        for nth in range(1, 6):
            time.sleep(0.13)    # 眠りの途中に届くように、ずらす
            sent_at = time.monotonic()
            os.write(w, b"\n")
            assert enters.wait_for(nth), "Enter を受けない"
            worst = max(worst, time.monotonic() - sent_at)
        assert worst < 0.5, f"Enter から反応まで {worst:.2f} 秒"
    finally:
        stop.set()
        close_all(w)
        if thread is not None:
            thread.join(timeout=5)
        close_all(r)
    assert not thread.is_alive()


def test_polling_ends_at_the_end_of_input_after_reading_what_had_arrived(polled, monkeypatch):
    """書く側が閉じたら（入力の終わり）、先に届いていた分を読んでから、読むのをやめる。

    止める合図は要らない。終わった後は、調べ続けない（回り続けない）。空になった後に読みに行かない。"""
    r, w = os.pipe()
    reads = Reads(monkeypatch, r)
    enters = Enters()
    thread = None
    try:
        thread = cli.watch_enter(enters, "pipe", fd=r)
        time.sleep(0.2)
        assert thread.is_alive()
        os.write(w, b"last line\n")
        os.close(w)
        w = None
        thread.join(timeout=5)
        assert not thread.is_alive(), "書く側が閉じても、読むスレッドが終わらない"
        assert enters.count == 1, "閉じる前に届いていた Enter を、読まずに終わった"
        assert [got for _, _, got in reads.log] == [10], (
            f"読み取り（届いた量、頼んだ量、読めた量）: {reads.log}"
        )
        assert polled.answers[-1] is None, f"終わりの答えで終わっていない: {polled.answers}"
        assert polled.after_end == 0, f"終わりの答えの後も、{polled.after_end} 回調べた（回り続ける）"
        checks = polled.checks
        time.sleep(0.3)
        assert polled.checks == checks, "終わった後も、調べ続けている"
    finally:
        close_all(w)
        if thread is not None:
            thread.join(timeout=5)
        close_all(r)


@pytest.mark.parametrize("failing", ["check", "prepare-oserror", "prepare-attributeerror"])
def test_input_that_cannot_be_peeked_falls_back_to_the_waiting_read_once(monkeypatch, failing):
    """パイプとして調べられない入力は、待つ読み方に戻す。1回で戻り、調べ続けない。Enter は読める。

    - check: 調べるたびに失敗する（PeekNamedPipe が、書く側が閉じた、のほかの理由で失敗）
    - prepare-oserror、prepare-attributeerror: 調べる用意そのものができない（ハンドルを取れない、API が無い）
    Enter が読めなくなる（スレッドが黙って終わる）より、止まり方が残っても Enter が読めるほうを選ぶ、という
    製品の決めごと（app-design.md 8.3）どおりかを見る。入力の終わりで、スレッドは終わる。"""
    asked = {"count": 0}

    def factory(fd: int):
        if failing == "prepare-oserror":
            raise OSError(9, "Bad file descriptor")
        if failing == "prepare-attributeerror":
            raise AttributeError("function 'PeekNamedPipe' not found")

        def ready():
            asked["count"] += 1
            if asked["count"] > RUNAWAY_CHECKS:
                raise SystemExit("読むスレッドが回り続けている（歯止め）")
            raise OSError(22, "PeekNamedPipe failed (Windows error 1)")

        return ready

    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "windows_pipe_ready", factory)
    r, w = os.pipe()
    enters = Enters()
    thread = None
    try:
        thread = cli.watch_enter(enters, "pipe", fd=r)
        time.sleep(0.3)
        assert thread.is_alive(), "調べられない入力で、読むスレッドが終わった（Enter が読めなくなる）"
        assert asked["count"] == (1 if failing == "check" else 0), f"調べ続けている: {asked['count']} 回"
        os.write(w, b"\n")
        assert enters.wait_for(1), "待つ読み方に戻っていない（Enter を受けない）"
        os.write(w, b"\n")
        assert enters.wait_for(2)
        assert asked["count"] == (1 if failing == "check" else 0)
    finally:
        close_all(w)
        if thread is not None:
            thread.join(timeout=5)
        close_all(r)
    assert not thread.is_alive(), "入力の終わりで、読むスレッドが終わらない"


@pytest.mark.skipif(WINDOWS, reason="製品の分かれ目（cli.WINDOWS）を差し替えて流す。Windows では本物が流れる")
@pytest.mark.parametrize(("windows", "source"), [(True, "tty"), (False, "pipe")],
                         ids=["windows-console", "posix-pipe"])
def test_only_a_windows_pipe_is_polled(monkeypatch, windows, source):
    """待たない読み方にするのは、Windows のパイプだけ。Windows の端末（コンソール）と、Windows でない OS の
    パイプは、待つ読み方のまま（PeekNamedPipe の用意をしない）。Enter は読める。

    端末の身代わりにパイプを使う（読み方の選び方だけを見る）。"""
    made = []

    def factory(fd: int):
        made.append(fd)
        return lambda: 0

    monkeypatch.setattr(cli, "WINDOWS", windows)
    monkeypatch.setattr(cli, "windows_pipe_ready", factory)
    r, w = os.pipe()
    enters = Enters()
    stop = threading.Event()
    thread = None
    try:
        thread = cli.watch_enter(enters, source, fd=r, stop=stop)
        os.write(w, b"\n")
        assert enters.wait_for(1), "Enter を受けない"
        assert made == [], "待つ読み方のはずの入力に、待たない読み方を用意した"
    finally:
        stop.set()
        close_all(w)    # 入力の終わり（Windows の端末の道は、0.2 秒おきに読み直し、止める合図で終わる）
        if thread is not None:
            thread.join(timeout=5)
        close_all(r)
    assert not thread.is_alive()


# ------------------------------------------------------------ 4. アプリ本体ごと（Windows の分かれ目で）


class Lines:
    """起動したプロセスの出力を、別のスレッドで読む。待つときは、上限つき。"""

    def __init__(self, command: list[str], env: dict[str, str], stdin=subprocess.PIPE) -> None:
        self.seen: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace")
            self.seen.append(line)
            self.q.put(line)
        self.q.put(None)

    @property
    def out(self) -> str:
        return "".join(self.seen)

    def wait_line(self, wanted: str, timeout: float = WAIT) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.q.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                self.q.put(None)
                return None
            if wanted in line:
                return line

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=20)
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path / "config"), "LC_ALL": "ja_JP.UTF-8",
            "PYTHONIOENCODING": "utf-8", **extra}


# アプリ本体を、Windows の分かれ目を真にして起動する入口（POSIX で流す）。製品のコードは変えず、起動の前に:
# - cli.WINDOWS を真にする
# - cli.windows_pipe_ready を、POSIX の身代わりにする（A0V6_CANNOT_PREPARE があれば、用意できない）
# - os.read を見張る: 標準入力（fd 0）を読みに行くたびに、その時点で届いていた量を A0V6_LOG に控える
#   （届いた量が 0 の read は、そこで待つ = パイプに読み取りの待ちが残る）
AS_WINDOWS_ENTRY = r'''
import array, fcntl, os, select, sys, termios

from circuit_sim.app import cli

LOG = os.environ["A0V6_LOG"]


def log(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def arrived(fd):
    count = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, count)
    return count[0]


def stand_in(fd):
    log(f"prepared fd={fd}")
    if os.environ.get("A0V6_CANNOT_PREPARE"):
        raise OSError(9, "stand-in: cannot prepare")
    state = {"checks": 0}

    def ready():
        state["checks"] += 1
        if state["checks"] == 1:
            log("checked")
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        closed = any(event & select.POLLHUP for _, event in poller.poll(0))  # 先に見る（量は、その後）
        count = arrived(fd)
        if count:
            return count
        if closed:
            log("closed")
            return None
        return 0

    return ready


real_read = os.read


def watched_read(fd, size):
    if fd == 0:
        log(f"read arrived={arrived(0)} size={size}")
    return real_read(fd, size)


os.read = watched_read
cli.WINDOWS = True
cli.windows_pipe_ready = stand_in
raise SystemExit(cli.main(sys.argv[1:]))
'''

NEW_URL = "新しい URL（1回だけ"


def log_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


@pytest.mark.skipif(WINDOWS, reason="Windows の分かれ目を、POSIX で流す入口。Windows では、本物が流れる")
@pytest.mark.parametrize("can_prepare", [True, False], ids=["polling", "control-cannot-prepare"])
def test_app_waiting_for_enter_leaves_no_read_pending_on_a_windows_pipe(tmp_path, can_prepare):
    """アプリ本体（main）ごと: Windows で標準入力がパイプのとき、Enter を待っている間、標準入力に読み取りの
    待ちが1度も無い（届いた量が 0 のときに読みに行かない）。Enter は、起動の直後から効く。

    これが、PO の PC で見つかった止まり方（読んで待っている間は、標準入力を受け継ぐ子が始まれない）を
    防ぐ条件。対照（control-cannot-prepare）: 調べる用意ができないと、待つ読み方に戻り、届いた量が 0 の
    読み取りが控えに残る（この見張りが、待ちを見つけられることの確かめ。Enter は読める）。"""
    log = tmp_path / "reads.log"
    env = child_env(tmp_path, A0V6_LOG=str(log), **({} if can_prepare else {"A0V6_CANNOT_PREPARE": "1"}))
    app = Lines([sys.executable, "-c", AS_WINDOWS_ENTRY, "--no-browser", "--port", str(free_port())], env)
    try:
        assert app.wait_line("circuit-sim を起動しました") is not None, app.out
        time.sleep(0.7)     # Enter を待っている間
        for _ in range(2):
            app.proc.stdin.write(b"\n")
            app.proc.stdin.flush()
            assert app.wait_line(NEW_URL, timeout=10) is not None, "Enter で新しい URL が出ない: " + app.out
        time.sleep(0.3)
        lines = log_lines(log)
        reads = [line for line in lines if line.startswith("read ")]
        assert lines.count("prepared fd=0") == 1, lines
        if can_prepare:
            assert "checked" in lines, lines
            assert len(reads) == 2 and all(line == "read arrived=1 size=1" for line in reads), (
                f"届いていないのに読みに行った、または届いた量より多く読もうとした: {reads}"
            )
            app.proc.stdin.close()      # 入力の終わり: 読みに行かずに、読むのをやめる
            deadline = time.monotonic() + 5
            while "closed" not in log_lines(log):
                assert time.monotonic() < deadline, log_lines(log)
                time.sleep(0.05)
            time.sleep(0.3)
            assert [line for line in log_lines(log) if line.startswith("read ")] == reads
            assert app.proc.poll() is None, "入力が終わっただけで、アプリ本体が止まった: " + app.out
        else:
            assert "checked" not in lines, lines
            assert "read arrived=0 size=1024" in reads, f"対照なのに、待つ読み取りが見つからない: {reads}"
    finally:
        app.stop()


# ------------------------------------------------------------ 5. Windows の本物（PO の PC で最初に流れる）


def windows_facts(fd: int) -> str:
    """落ちたときに添える、Windows の側の事実（調べられなかった項目は、その理由）。"""
    facts = [f"Python {sys.version.split()[0]}", f"fd {fd}"]
    try:
        import _winapi
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        facts.append(f"handle {handle:#x}")
        try:
            facts.append(f"GetFileType {_winapi.GetFileType(handle)}（1 ディスク、2 文字、3 パイプ）")
        except OSError as exc:
            facts.append(f"GetFileType が失敗（winerror {getattr(exc, 'winerror', None)}）")
        try:
            facts.append(f"_winapi.PeekNamedPipe {_winapi.PeekNamedPipe(handle, 0)}（届いた量、残り）")
        except OSError as exc:
            facts.append(f"_winapi.PeekNamedPipe が失敗（winerror {getattr(exc, 'winerror', None)}）")
    except Exception as exc:  # noqa: BLE001  調べられなかった理由を、そのまま添える
        facts.append(f"調べられなかった: {type(exc).__name__}: {exc}")
    return "［" + "、".join(facts) + "］"


def answer_of(ready) -> str:
    """ready() の答えを、落ちたときに読める形で（例外も）。"""
    try:
        return repr(ready())
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


@pytest.mark.skipif(not WINDOWS, reason="Windows の本物の PeekNamedPipe（POのPCで流れる）")
def test_windows_real_pipe_answers_match_the_standard_library_and_end_with_none(record_property):
    """Windows の本物のパイプ: 製品の答え（ctypes で呼ぶ PeekNamedPipe）が、標準ライブラリの
    _winapi.PeekNamedPipe の答えと合う。読めば減る。書く側が閉じても、届いた分が残っている間は量を答え、
    空になったら None（入力の終わり）。

    送る量は、パイプの入れ物（os.pipe は既定の大きさ。4096 バイト）に必ず収まる量だけ（収まらないと、
    読む側のいないこの試験は、書く所で待ち続ける）。"""
    r, w = os.pipe()
    try:
        ready = cli.windows_pipe_ready(r)
        steps = []
        for label, action, wanted in [
            ("空", lambda: None, 0),
            ("3 バイト送った", lambda: os.write(w, b"abc"), 3),
            ("2 バイト読んだ", lambda: os.read(r, 2), 1),
            ("100 バイト送った", lambda: os.write(w, b"x" * 100), 101),
        ]:
            action()
            got, independent = answer_of(ready), arrived(r)
            steps.append(f"{label}: 製品 {got}、_winapi {independent}")
            assert got == repr(wanted) and independent == wanted, " / ".join(steps) + windows_facts(r)
        os.close(w)
        w = None
        got = answer_of(ready)
        steps.append(f"書く側を閉じた（101 バイト残っている）: 製品 {got}")
        assert got == "101", " / ".join(steps) + windows_facts(r)
        assert len(os.read(r, 4096)) == 101
        got = answer_of(ready)
        steps.append(f"読み切った: 製品 {got}")
        record_property("windows_pipe_ready_answers", " / ".join(steps))
        assert got == "None", (
            "書く側が閉じて空になったパイプで、入力の終わり（None）にならない。" + " / ".join(steps)
            + windows_facts(r)
            + "（_winapi の winerror が 109・233 のほかなら、製品の PIPE_ENDED に無い番号）"
        )
    finally:
        close_all(w, r)


@pytest.mark.skipif(not WINDOWS, reason="Windows の入力の見分け（POのPCで流れる）")
def test_windows_inputs_are_told_apart(tmp_path, record_property):
    """Windows: パイプは "pipe"（待たない読み方で調べられる）。NUL とファイルは、Enter を読まない（None）。

    製品が頼っているのは、この見分け（NUL とファイルは、待たない読み方まで進まない）。NUL とファイルを
    PeekNamedPipe で調べたときの答え（「書く側が閉じた」ではない失敗 = OSError の見込み）は、合否に使わず、
    記録に残す（パイプとして調べられない入力の実例。ソケットなど、パイプと見分けられる別の入力の参考）。"""
    found = []
    r, w = os.pipe()
    try:
        source, answer = cli.enter_source(r), answer_of(cli.windows_pipe_ready(r))
        found.append(f"パイプ: enter_source {source!r}、調べた答え {answer}")
        assert source == "pipe" and answer == "0", found[-1] + windows_facts(r)
    finally:
        close_all(w, r)
    path = tmp_path / "input.txt"
    path.write_bytes(b"\n\n")
    for label, target in (("NUL", os.devnull), ("ファイル", path)):
        with open(target, "rb") as f:
            fd = f.fileno()
            source, answer = cli.enter_source(fd), answer_of(cli.windows_pipe_ready(fd))
            found.append(f"{label}: enter_source {source!r}、調べた答え {answer}")
            record_property("windows_inputs", " / ".join(found))
            assert source is None, found[-1] + windows_facts(fd)


# 「標準入力（パイプ）の Enter を待っている間に、標準入力を差し替えずに起動した子が、始まれるか」を見る入口
# （PO の PC で確かめた形と同じ）。最初の引数で、待ち方を選ぶ:
#   product         製品の読み方（cli.enter_source と cli.watch_enter）で待つ
#   blocking-read   読んで待つ（直す前の読み方。対照）
# 出すもの（1 行ずつ）: source=…（製品の見分け）、peek=…（Windows だけ。製品の調べ方の、最初の答え）、
# child=started か child=HANG（子の python -c pass が 15 秒のうちに終わったか）、enter=yes か enter=no
CHILD_ENTRY = r'''
import os, subprocess, sys, threading, time

from circuit_sim.app import cli

mode = sys.argv[1]
got = threading.Event()
source = cli.enter_source(0)
print("source=" + str(source), flush=True)
if os.name == "nt":
    try:
        print("peek=" + repr(cli.windows_pipe_ready(0)()), flush=True)
    except Exception as exc:
        print("peek=" + type(exc).__name__ + ": " + str(exc), flush=True)
if mode == "product":
    cli.watch_enter(got.set, source or "pipe")
else:
    def read_and_wait():
        while os.read(0, 1024):
            got.set()

    threading.Thread(target=read_and_wait, daemon=True).start()
time.sleep(1.0)     # 待ちに入るまで
child = subprocess.Popen([sys.executable, "-c", "pass"])    # 標準入力を差し替えない（webbrowser と同じ）
try:
    child.wait(timeout=15)
    print("child=started", flush=True)
except subprocess.TimeoutExpired:
    print("child=HANG", flush=True)
    child.kill()
    child.wait(timeout=15)
print("enter=" + ("yes" if got.wait(20) else "no"), flush=True)
'''


def child_while_waiting(tmp_path: Path, mode: str) -> tuple[str, str, str]:
    """（子が始まれたか、その後に送った Enter を受けたか、出力の全部）。"""
    run = Lines([sys.executable, "-c", CHILD_ENTRY, mode], child_env(tmp_path))
    try:
        child = run.wait_line("child=", timeout=60)
        enter = None
        if child is not None:
            try:
                run.proc.stdin.write(b"\n")
                run.proc.stdin.flush()
            except OSError:
                pass    # 入口が先に終わった（出力を添えて、呼んだ側が落とす）
            enter = run.wait_line("enter=", timeout=60)
        return (child or "").strip(), (enter or "").strip(), run.out
    finally:
        run.stop()


def test_a_child_that_inherits_the_input_can_start_while_the_product_waits_for_enter(tmp_path):
    """製品の読み方で標準入力（パイプ）の Enter を待っている間も、標準入力を受け継ぐ子は始まれる。

    Windows で意味を持つ（PO の PC では、読んで待っていると、子は 5 秒たっても終わらなかった）。Enter も、
    その後に読める。Windows でない OS では、どちらの読み方でも始まれる（通るのが当たり前の確かめ）。"""
    child, enter, out = child_while_waiting(tmp_path, "product")
    assert "source=pipe" in out, "製品が、標準入力をパイプと見分けていない: " + out
    assert child == "child=started", (
        "製品の読み方で待っている間に起動した子が、15 秒たっても終わらない（パイプに読み取りの待ちが"
        "残っている。peek= の行が数でなければ、待たない読み方を用意できていない）: " + out
    )
    assert enter == "enter=yes", "子を起動した後、Enter を読めない: " + out


@pytest.mark.skipif(not WINDOWS, reason="Windows の、読んで待つと子が始まれないことの対照（POのPCで流れる）")
def test_control_a_waiting_read_keeps_the_child_from_starting_on_windows(tmp_path, record_property):
    """対照: 読んで待つ読み方（直す前）だと、Windows では、標準入力を受け継ぐ子が始まれない。

    上の試験が、読み方の違いを見分けられることの確かめ。この PC で対照が成り立たないとき（読んで待って
    いても子が始まる）は、上の試験は読み方の違いを見分けていないので、スキップにして、そう記録する。"""
    child, _enter, out = child_while_waiting(tmp_path, "blocking-read")
    record_property("読んで待つ間に起動した子", child)
    if child != "child=HANG":
        pytest.skip("読んで待つ読み方でも、子が始まれた（この環境では、対照が成り立たない）: " + out.strip())
