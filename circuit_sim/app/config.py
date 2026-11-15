"""端末の中の設定の場所（D-050、D-051）。合言葉（token）と画面の好み（settings.json）を置く。

場所:
  Windows   %APPDATA%\\circuit-sim\\
  ほか      $XDG_CONFIG_HOME/circuit-sim/（無ければ ~/.config/circuit-sim/）
  CIRCUIT_SIM_CONFIG_DIR を指定すると、その場所（試験は一時フォルダに向ける。利用者の本物の設定に触れない）

合言葉:
  同じPCの別の利用者が、アプリ本体（127.0.0.1 の番号）に要求を送って作業の状態を読み書きするのを防ぐ。
  起動のたびには変えない（起動し直したとき、開いている画面がそのまま再接続できるように。画面仕様 1.3）。
  本人だけが読める権限で書く（POSIX は 0600。Windows は %APPDATA% の権限を受け継ぐ。
  本人・SYSTEM・管理者だけ）。
  権限が広い、持ち主が違う、形が違う合言葉は、漏れたものとみなして作り直す。

画面の好み:
  ブラウザの保存（localStorage）は番号（ポート）ごとに分かれるので、番号が変わっても戻るように、ここにも書く。
  受け付ける名前と値は PREFS に決めたものだけ（知らない名前、形の違う値は書かない）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

CONFIG_ENV = "CIRCUIT_SIM_CONFIG_DIR"
APP_DIR_NAME = "circuit-sim"
TOKEN_FILE = "token"
SETTINGS_FILE = "settings.json"
TOKEN_FORM = re.compile(r"[A-Za-z0-9_-]{43,128}")
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


def ensure_dir(path: Path) -> Path:
    """設定の場所を作る。POSIX では本人だけが入れる権限（0700）にする。"""
    path.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o700)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """本人だけが読める権限で、置き換える形で書く（途中で止まっても、壊れたファイルを残さない）。"""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)  # mkstemp は 0600 で作る
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if not WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _private_enough(path: Path) -> bool:
    """POSIX: 本人の持ち物で、ほかの人が読み書きできない。Windows は場所の権限に任せる。"""
    if WINDOWS:
        return True
    st = path.stat()
    return st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077


def load_token(directory: Path) -> str:
    """合言葉を読む。無い・形が違う・権限が広いときは作り直して書く。"""
    ensure_dir(directory)
    path = directory / TOKEN_FILE
    try:
        if _private_enough(path):
            token = path.read_text(encoding="ascii").strip()
            if TOKEN_FORM.fullmatch(token):
                return token
    except (OSError, UnicodeDecodeError):
        pass
    token = secrets.token_urlsafe(32)  # 43 文字
    _write_private(path, (token + "\n").encode("ascii"))
    return token


# ------------------------------------------------------------------ 画面の好み

def _choice(*allowed: str) -> Callable[[str], bool]:
    return lambda value: value in allowed


def _size(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,5}", value)) and int(value) <= 10000


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
    """設定ファイル（settings.json）。読めない・形の違う中身は、無いものとして扱う（画面は既定で開く）。"""

    def __init__(self, directory: Path) -> None:
        self.path = directory / SETTINGS_FILE
        self.lock = threading.Lock()

    def read(self) -> dict[str, str]:
        with self.lock:
            return self._read()

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if valid_pref(k, v)}

    def update(self, changes: Mapping[str, str | None]) -> dict[str, str]:
        """changes の値が None の名前は消す。changes は確かめてから渡す（valid_pref）。"""
        with self.lock:
            current = self._read()
            for name, value in changes.items():
                if value is None:
                    current.pop(name, None)
                else:
                    current[name] = value
            ensure_dir(self.path.parent)
            text = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            _write_private(self.path, text.encode("utf-8"))
            return current
