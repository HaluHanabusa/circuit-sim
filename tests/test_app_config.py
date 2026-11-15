"""設定の場所と合言葉（circuit_sim/app/config.py。D-050、D-051）の試験。

試験の間、設定の場所は一時フォルダに向いている（tests/conftest.py の CIRCUIT_SIM_CONFIG_DIR）。
Windows でだけ動く試験は、POの PC の local_ci で流れる（合言葉のファイルの権限、%APPDATA% の場所）。
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from circuit_sim.app.config import (
    CONFIG_ENV,
    TOKEN_FILE,
    TOKEN_FORM,
    config_dir,
    load_token,
)

WINDOWS = os.name == "nt"


def test_tests_never_touch_the_real_config():
    """試験の間、設定の場所は試験の一時フォルダ（利用者の本物の設定に触れない）。"""
    here = config_dir()
    assert os.environ.get(CONFIG_ENV), "tests/conftest.py が設定の場所を一時フォルダに向けていない"
    assert here == Path(os.environ[CONFIG_ENV])
    assert "circuit-sim-guard-" in str(here)


@pytest.mark.parametrize(("env", "windows", "expected"), [
    ({"APPDATA": r"C:\Users\u\AppData\Roaming"}, True,
     PureWindowsPath(r"C:\Users\u\AppData\Roaming\circuit-sim")),
    ({}, True, None),  # APPDATA が無ければ ホーム\AppData\Roaming
    ({"XDG_CONFIG_HOME": "/x/conf"}, False, Path("/x/conf/circuit-sim")),
    ({"XDG_CONFIG_HOME": "relative/conf"}, False, None),  # 相対パスは無視（XDG の決まり）
    ({}, False, None),
    ({CONFIG_ENV: "/tmp/override", "APPDATA": "C:/a", "XDG_CONFIG_HOME": "/x"}, False, Path("/tmp/override")),
])
def test_config_dir_follows_os_conventions(env, windows, expected, tmp_path):
    got = config_dir(env=env, windows=windows, home=tmp_path)
    if expected is None:
        tail = ("AppData", "Roaming", "circuit-sim") if windows else (".config", "circuit-sim")
        assert got == tmp_path.joinpath(*tail)
    elif isinstance(expected, PureWindowsPath):
        assert PureWindowsPath(str(got)) == expected
    else:
        assert got == expected


def test_token_is_created_once_and_kept(tmp_path):
    """合言葉は起動のたびには変えない（起動し直しても、開いている画面がそのまま再接続できる）。"""
    first = load_token(tmp_path / "c")
    assert TOKEN_FORM.fullmatch(first)
    assert load_token(tmp_path / "c") == first
    assert (tmp_path / "c" / TOKEN_FILE).read_text(encoding="ascii").strip() == first
    assert load_token(tmp_path / "other") != first


@pytest.mark.skipif(WINDOWS, reason="POSIX の権限（Windows は下の icacls の試験）")
def test_token_file_and_folder_are_private_on_posix(tmp_path):
    load_token(tmp_path / "c")
    assert stat.S_IMODE((tmp_path / "c").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "c" / TOKEN_FILE).stat().st_mode) == 0o600


@pytest.mark.skipif(WINDOWS, reason="POSIX の権限")
def test_token_readable_by_others_is_replaced(tmp_path):
    """ほかの人が読めた合言葉は、漏れたものとみなして作り直す。"""
    first = load_token(tmp_path)
    path = tmp_path / TOKEN_FILE
    path.chmod(0o644)
    second = load_token(tmp_path)
    assert second != first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["", "short", "x" * 42, "has space " * 5, "日本語" * 20, "a" * 200])
def test_malformed_token_is_replaced(tmp_path, content):
    path = tmp_path / TOKEN_FILE
    path.write_text(content, encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o600)
    token = load_token(tmp_path)
    assert TOKEN_FORM.fullmatch(token) and token != content.strip()


@pytest.mark.skipif(not WINDOWS, reason="Windows の権限（ACL）。POのPCの local_ci で流れる")
def test_token_file_is_not_readable_by_other_users_on_windows(tmp_path):
    """合言葉のファイルに、Everyone・Users・Authenticated Users の読み取りが付いていない。

    tmp_path は利用者のフォルダの下（%LOCALAPPDATA%\\Temp）にあり、%APPDATA% と同じく
    本人・SYSTEM・管理者だけの権限を受け継ぐ。icacls の表示は SID（*S-1-…）でも名前でも確かめる。"""
    import re

    load_token(tmp_path)
    path = tmp_path / TOKEN_FILE
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, errors="replace",
                         check=True).stdout
    for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert broad not in out, (broad, out)
    # /save は SDDL（D:(A;フラグ;権利;;;SID)）で書く。広い利用者の組は、略号（WD、BU、AU）か SID で出る
    saved = tmp_path / "acl.txt"
    subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, check=True)
    sddl = saved.read_bytes().decode("utf-16-le", errors="replace")
    trustees = {m.group(1) for m in re.finditer(r"\(A;[^;]*;[^;]*;[^;]*;[^;]*;([^)]+)\)", sddl)}
    assert trustees, sddl
    broad = {"WD", "BU", "AU", "S-1-1-0", "S-1-5-32-545", "S-1-5-11"}
    assert not trustees & broad, (trustees, sddl)


@pytest.mark.skipif(not WINDOWS, reason="Windows の既定の場所")
def test_default_config_dir_on_windows_is_appdata():
    env = {k: v for k, v in os.environ.items() if k != CONFIG_ENV}
    assert config_dir(env=env) == Path(os.environ["APPDATA"]) / "circuit-sim"
