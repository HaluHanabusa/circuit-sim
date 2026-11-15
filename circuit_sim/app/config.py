"""端末の中の設定の場所（D-050、D-051、D-053）。合言葉（token）と画面の好み（settings.json）を置く。

場所:
  Windows   %APPDATA%\\circuit-sim\\
  ほか      $XDG_CONFIG_HOME/circuit-sim/（無ければ ~/.config/circuit-sim/）
  CIRCUIT_SIM_CONFIG_DIR を指定すると、その場所（試験は一時フォルダに向ける。利用者の本物の設定に触れない）

合言葉:
  同じPCの別の利用者が、アプリ本体（127.0.0.1 の番号）に要求を送って作業の状態を読み書きするのを防ぐ。
  起動のたびには変えない（起動し直したとき、開いている画面がそのまま再接続できるように。画面仕様 1.3）。
  本人だけが読める権限で書く。
    POSIX    場所は 0700、ファイルは 0600
    Windows  場所とファイルに、本人と SYSTEM だけの権限（ACL）を明示して付け、上の階層から受け継がない。
             %APPDATA% の既定の権限（本人・SYSTEM・管理者）に任せると、CIRCUIT_SIM_CONFIG_DIR やフォルダの
             リダイレクトで、ほかの利用者が読める場所に置かれたときに守れないため
  権限が広い、持ち主が違う、形が違う合言葉は、漏れたものとみなして作り直す。
  初めての起動で2つ同時に立ち上げても合言葉が1つになるように、ファイルは「無ければ作る」（O_EXCL）で作り、
  先を越されたら、相手が書き終えるのを待って読み直す。

画面の好み:
  ブラウザの保存（localStorage）は番号（ポート）ごとに分かれるので、番号が変わっても戻るように、ここにも書く。
  受け付ける名前と値は PREFS に決めたものだけ（知らない名前、形の違う値は書かない）。
  読めない・壊れた設定ファイルは、既定（空）として読み、on_broken で知らせ、次に書くときに元のファイルを
  settings.json.broken に控えてから書き直す。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

CONFIG_ENV = "CIRCUIT_SIM_CONFIG_DIR"
APP_DIR_NAME = "circuit-sim"
TOKEN_FILE = "token"
SETTINGS_FILE = "settings.json"
BROKEN_SUFFIX = ".broken"
# 好みは数百バイト。これより大きいものは壊れているとみなす（読み込みで固まらない）
SETTINGS_MAX_BYTES = 64 * 1024
TOKEN_FORM = re.compile(r"[A-Za-z0-9_-]{43,128}")
TOKEN_WAIT_SECONDS = 2.0  # 先を越されたとき、相手が書き終えるのを待つ時間
WINDOWS = os.name == "nt"


def config_dir(
    env: Mapping[str, str] | None = None, windows: bool = WINDOWS, home: Path | None = None
) -> Path:
    """設定の場所。ファイルは作らない（決めるだけ）。"""
    env = os.environ if env is None else env
    override = env.get(CONFIG_ENV)
    if override:
        return Path(override)
    home = Path.home() if home is None else home
    if windows:
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / APP_DIR_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    # XDG の決まり: 相対パスの指定は無視する
    base = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"
    return base / APP_DIR_NAME


# ------------------------------------------------------------------ Windows の権限（ACL）

# 本人と SYSTEM のほかに、持っていても漏れとみなさない相手（管理者は、どのファイルも持ち主になって読める）
WINDOWS_TRUSTED_ALIASES = frozenset({"SY", "BA", "S-1-5-18", "S-1-5-32-544"})
# DACL の項目の種類（SDDL）。許可は、条件つき（XA、ZA）とオブジェクト型（OA）も含む。これら以外の種類は
# 読み方を決めていないので、本人だけとはみなさない
SDDL_ALLOW = frozenset({"A", "OA", "XA", "ZA"})
SDDL_DENY = frozenset({"D", "OD", "XD"})
_DACL_FLAGS = re.compile(r"NO_ACCESS_CONTROL|AI|AR|P")


def sddl_dacl(sddl: str) -> tuple[set[str], list[tuple[str, str, str, str]]] | None:
    """SDDL の DACL を（フラグ、項目の並び）にする。項目は（種類、フラグ、権利、相手）。

    条件つきの項目の後ろの条件（入れ子の括弧）は読み飛ばす。DACL が無い・読めない形なら None。"""
    if "D:" not in sddl:
        return None
    rest = sddl.split("D:", 1)[1].strip()
    i = rest.find("(")
    head = rest if i < 0 else rest[:i]
    if re.search(r"[OGS]:", head):  # 項目の無い DACL の後ろに、ほかの部分が続く
        head = re.split(r"[OGS]:", head, maxsplit=1)[0]
        i = -1
    flags = set(_DACL_FLAGS.findall(head))
    if _DACL_FLAGS.sub("", head).strip():
        return None
    aces: list[tuple[str, str, str, str]] = []
    while 0 <= i < len(rest) and rest[i] == "(":
        depth, j = 0, i
        while j < len(rest):
            depth += {"(": 1, ")": -1}.get(rest[j], 0)
            if depth == 0:
                break
            j += 1
        if depth != 0:
            return None
        fields = rest[i + 1:j].split(";", 6)
        if len(fields) < 6 or not fields[0] or not fields[5]:
            return None
        aces.append((fields[0], fields[1], fields[2], fields[5]))
        i = j + 1
    return flags, aces


def sddl_trustees(sddl: str) -> set[str] | None:
    """SDDL の DACL で、許可の項目が効く相手（SID か略号）。

    継承するだけの項目（IO）も数える（場所なら、中に作るファイルに効く）。DACL が無い（誰でも使える）、
    読めない形、知らない種類の項目があるなら None（本人だけとはみなさない）。"""
    dacl = sddl_dacl(sddl)
    if dacl is None or "NO_ACCESS_CONTROL" in dacl[0]:
        return None
    trustees: set[str] = set()
    for kind, _flags, _rights, who in dacl[1]:
        if kind in SDDL_ALLOW:
            trustees.add(who)
        elif kind not in SDDL_DENY:
            return None
    return trustees


def user_aliases(user_sid: str) -> set[str]:
    """SDDL で本人を表す書き方。組み込みの Administrator は、略号 LA で出る。"""
    return {user_sid, "LA"} if user_sid.endswith("-500") else {user_sid}


def folder_dacl_is_private(sddl: str, user_sid: str) -> bool:
    """場所の DACL が、期待する形と完全に一致する。

    期待する形: 上から受け継がず（P）、本人と SYSTEM だけが、中に作るものまで使える（OICI、FA）。"""
    dacl = sddl_dacl(sddl)
    if dacl is None:
        return False
    flags, aces = dacl
    if "P" not in flags or "NO_ACCESS_CONTROL" in flags or len(aces) != 2:
        return False
    return any(set(aces) == {("A", "OICI", "FA", me), ("A", "OICI", "FA", "SY")}
               for me in user_aliases(user_sid))


class _Win32:
    """Windows の権限を読み書きする（advapi32。ctypes だけ。Windows でだけ作る）。"""

    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SDDL_REVISION_1 = 1
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        a = self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        p = ctypes.c_void_p
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.LocalFree.argtypes = [p]
        k.LocalFree.restype = p
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [p, ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(p), ctypes.POINTER(wintypes.ULONG)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        a.GetSecurityDescriptorDacl.argtypes = [p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(p),
                                                ctypes.POINTER(wintypes.BOOL)]
        a.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        a.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, p, p, p, p]
        a.SetNamedSecurityInfoW.restype = wintypes.DWORD
        a.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(p),
                                            ctypes.POINTER(p), ctypes.POINTER(p), ctypes.POINTER(p),
                                            ctypes.POINTER(p)]
        a.GetNamedSecurityInfoW.restype = wintypes.DWORD
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG)]
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self.wintypes = wintypes
        self._sid: str | None = None

    def _fail(self, code: int | None = None) -> OSError:
        return self.ctypes.WinError(self.ctypes.get_last_error() if code is None else code)

    def user_sid(self) -> str:
        """この利用者の SID（S-1-5-21-…）。"""
        if self._sid is not None:
            return self._sid
        ctypes, wt = self.ctypes, self.wintypes
        token = wt.HANDLE()
        if not self.advapi32.OpenProcessToken(self.kernel32.GetCurrentProcess(), self.TOKEN_QUERY,
                                              ctypes.byref(token)):
            raise self._fail()
        try:
            size = wt.DWORD(0)
            self.advapi32.GetTokenInformation(token, self.TOKEN_USER, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(token, self.TOKEN_USER, buf, size, ctypes.byref(size)):
                raise self._fail()
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents  # TOKEN_USER.User.Sid
            text = wt.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
                raise self._fail()
            try:
                self._sid = text.value
            finally:
                self.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.kernel32.CloseHandle(token)
        return self._sid

    def protect(self, path: Path, folder: bool) -> None:
        """本人と SYSTEM だけが使える権限を明示して付け、上の階層から受け継がない（D:P）。

        場所（folder）には、中に作るファイルへ受け継がせる印（OICI）を付ける。"""
        ctypes, wt = self.ctypes, self.wintypes
        inherit = "OICI" if folder else ""
        sddl = f"D:P(A;{inherit};FA;;;{self.user_sid()})(A;{inherit};FA;;;SY)"
        sd = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, self.SDDL_REVISION_1, ctypes.byref(sd), None):
            raise self._fail()
        try:
            present, defaulted, dacl = wt.BOOL(), wt.BOOL(), ctypes.c_void_p()
            if not self.advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                                           ctypes.byref(defaulted)):
                raise self._fail()
            info = self.DACL_SECURITY_INFORMATION | self.PROTECTED_DACL_SECURITY_INFORMATION
            code = self.advapi32.SetNamedSecurityInfoW(str(path), self.SE_FILE_OBJECT, info, None, None, dacl,
                                                       None)
            if code:
                raise self._fail(code)
        finally:
            self.kernel32.LocalFree(sd)

    def sddl(self, path: Path) -> str:
        """path の DACL（SDDL の文字列）。"""
        ctypes, wt = self.ctypes, self.wintypes
        sd, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        code = self.advapi32.GetNamedSecurityInfoW(str(path), self.SE_FILE_OBJECT,
                                                   self.DACL_SECURITY_INFORMATION, None, None,
                                                   ctypes.byref(dacl), None, ctypes.byref(sd))
        if code:
            raise self._fail(code)
        try:
            text = wt.LPWSTR()
            if not self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    sd, self.SDDL_REVISION_1, self.DACL_SECURITY_INFORMATION, ctypes.byref(text), None):
                raise self._fail()
            try:
                return text.value or ""
            finally:
                self.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.kernel32.LocalFree(sd)

    def private(self, path: Path) -> bool:
        """本人・SYSTEM・管理者のほかに、使える相手がいない（ファイル）。"""
        trustees = sddl_trustees(self.sddl(path))
        return trustees is not None and trustees <= WINDOWS_TRUSTED_ALIASES | user_aliases(self.user_sid())

    def private_folder(self, path: Path) -> bool:
        """場所の DACL が、protect(folder=True) で付ける形と完全に一致する。"""
        return folder_dacl_is_private(self.sddl(path), self.user_sid())


_win32: _Win32 | None = None


def win32() -> _Win32:
    global _win32
    if _win32 is None:
        _win32 = _Win32()
    return _win32


# ------------------------------------------------------------------ 場所とファイル


def ensure_dir(path: Path) -> Path:
    """設定の場所を作り、本人だけが入れる権限にする（POSIX は 0700、Windows は本人と SYSTEM の ACL）。"""
    path.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        # 本人と SYSTEM だけで、上から受け継がない形と完全に一致しなければ付け直す（知らない種類の項目、
        # 継承するだけの項目があっても付け直す）
        if not win32().private_folder(path):
            win32().protect(path, folder=True)
    else:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o700)
    return path


def _protect_file(path: Path) -> None:
    if WINDOWS:
        win32().protect(path, folder=False)
    else:
        os.chmod(path, 0o600)


def _write_private(path: Path, data: bytes) -> None:
    """本人だけが読める権限で、置き換える形で書く（途中で止まっても、壊れたファイルを残さない）。"""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)  # mkstemp は 0600 で作る
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if not WINDOWS:
            os.chmod(tmp, 0o600)  # Windows は、場所の権限（本人と SYSTEM）を受け継ぐ
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_private(path: Path, data: bytes) -> bool:
    """path が無ければ、本人だけが読める権限で作って data を書く（O_EXCL）。

    先を越されて、もうあれば False。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    _protect_file(path)
    return True


def _private_enough(path: Path) -> bool:
    """本人の持ち物で、ほかの人が読み書きできない（Windows は ACL で確かめる）。"""
    if WINDOWS:
        return win32().private(path)
    st = path.stat()
    return st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077


def _read_token(path: Path) -> str | None:
    """形も権限も正しい合言葉なら、それ。無い・読めない・形が違う・権限が広いなら None。"""
    try:
        if _private_enough(path):
            token = path.read_text(encoding="ascii").strip()
            if TOKEN_FORM.fullmatch(token):
                return token
    except (OSError, UnicodeDecodeError):
        pass
    return None


def load_token(directory: Path) -> str:
    """合言葉を読む。無ければ作る。形が違う・権限が広いときは作り直して書く。"""
    ensure_dir(directory)
    path = directory / TOKEN_FILE
    token = _read_token(path)
    if token is not None:
        return token
    new = secrets.token_urlsafe(32)  # 43 文字
    data = (new + "\n").encode("ascii")
    if create_private(path, data):
        return new
    # もうある。同時に起動したほかの circuit-sim が、作ったところで書いている途中（空で新しい）なら、
    # 書き終えるのを待って読み直す
    deadline = time.monotonic() + TOKEN_WAIT_SECONDS
    while _being_written(path) and time.monotonic() < deadline:
        time.sleep(0.02)
    token = _read_token(path)
    if token is not None:
        return token
    # 形が違う・権限が広い: 漏れた・壊れたものとみなして作り直す
    _write_private(path, data)
    _protect_file(path)
    return new


def _being_written(path: Path) -> bool:
    """O_EXCL で作られた直後で、まだ中身が無い（ほかのプロセスが書いている途中）。"""
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size == 0 and time.time() - st.st_mtime < TOKEN_WAIT_SECONDS


# ------------------------------------------------------------------ 画面の好み

def _choice(*allowed: str) -> Callable[[str], bool]:
    return lambda value: value in allowed


def _size(value: str) -> bool:
    # [0-9] だけ（\d は全角や他の文字の数字も通す）
    return bool(re.fullmatch(r"[0-9]{1,5}", value)) and int(value) <= 10000


# 名前は画面の prefs.ts と同じ（localStorage の名前から circuit-sim. を外したもの）
PREFS: dict[str, Callable[[str], bool]] = {
    "lang": _choice("ja", "en"),
    "theme": _choice("light", "dark"),
    "layout.specWidth": _size,
    "layout.schemHeight": _size,
    "layout.specCollapsed": _choice("0", "1"),
    "layout.schemCollapsed": _choice("0", "1"),
    "layout.noticesOpen": _choice("0", "1"),
}


def valid_pref(name: object, value: object) -> bool:
    return isinstance(name, str) and name in PREFS and isinstance(value, str) and PREFS[name](value)


class Settings:
    """設定ファイル（settings.json）。読めない・壊れた中身は、無いものとして扱う（画面は既定で開く）。

    on_broken(path, reason, backup) は、壊れたファイルを見つけたとき、同じ中身につき1回呼ぶ。"""

    def __init__(self, directory: Path,
                 on_broken: Callable[[Path, str, Path], None] | None = None) -> None:
        self.path = directory / SETTINGS_FILE
        self.backup = directory / (SETTINGS_FILE + BROKEN_SUFFIX)
        self.lock = threading.Lock()
        self.on_broken = on_broken
        self._reported: tuple[int, int] | None = None
        self._broken = False

    def read(self) -> dict[str, str]:
        with self.lock:
            return self._read()

    def _read(self) -> dict[str, str]:
        self._broken = False
        try:
            st = self.path.stat()
            if st.st_size > SETTINGS_MAX_BYTES:
                raise ValueError(f"too large ({st.st_size} bytes)")
            data = json.loads(self.path.read_bytes().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError, MemoryError) as exc:
            # 壊れている（JSON でない、入れ子が深すぎる、大きすぎる、読めない）: 既定で開き、知らせる
            self._broken = True
            self._report(exc)
            return {}
        return {k: v for k, v in data.items() if valid_pref(k, v)}

    def _report(self, exc: BaseException) -> None:
        if self.on_broken is None:
            return
        try:
            st = self.path.stat()
            seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            seen = (0, 0)
        if seen == self._reported:
            return
        self._reported = seen
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        if isinstance(exc, RecursionError):
            reason = "nesting too deep"
        self.on_broken(self.path, reason, self.backup)

    def update(self, changes: Mapping[str, str | None]) -> dict[str, str]:
        """changes の値が None の名前は消す。changes は確かめてから渡す（valid_pref）。

        書けなければ OSError。"""
        with self.lock:
            current = self._read()
            for name, value in changes.items():
                if value is None:
                    current.pop(name, None)
                else:
                    current[name] = value
            ensure_dir(self.path.parent)
            if self._broken and self.path.exists():
                os.replace(self.path, self.backup)  # 上書きする前に、壊れた元のファイルを控える
            text = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            _write_private(self.path, text.encode("utf-8"))
            self._broken = False
            return current
