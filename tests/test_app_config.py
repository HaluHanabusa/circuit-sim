"""設定の場所と合言葉（circuit_sim/app/config.py。D-050、D-051）の試験。

試験の間、設定の場所は一時フォルダに向いている（tests/conftest.py の CIRCUIT_SIM_CONFIG_DIR）。
Windows でだけ動く試験は、POの PC の local_ci で流れる（合言葉のファイルの権限、%APPDATA% の場所）。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath

import pytest

from circuit_sim.app.config import (
    CONFIG_ENV,
    LAST_RUN_FILE,
    SETTINGS_FILE,
    TOKEN_FILE,
    TOKEN_FORM,
    Settings,
    config_dir,
    folder_dacl_is_private,
    load_token,
    read_last_run,
    sddl_trustees,
    valid_pref,
    write_last_run,
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


ROOT = Path(__file__).resolve().parents[1]
# config.py は標準ライブラリだけを使うので、パッケージ全体を読まずに、ファイルから読む（起動を速く）
RACE = """
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("config", sys.argv[3])
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
Path(sys.argv[4]).write_text("ready", encoding="utf-8")
go = Path(sys.argv[2])
while not go.exists():
    time.sleep(0.0005)
print(config.load_token(Path(sys.argv[1])))
"""
CONFIG_PY = ROOT / "circuit_sim" / "app" / "config.py"


def test_first_starts_at_the_same_time_share_one_key(tmp_path):
    """初めての起動で同時に立ち上げても、合言葉は1つ（O_EXCL で作り、先を越されたら読み直す）。

    8 つのプロセスを、全員が合図を待つところまで進めてから、一斉に load_token させる。3 回くり返す。"""
    script = tmp_path / "race.py"
    script.write_text(RACE, encoding="utf-8")
    for round_ in range(3):
        folder, go = tmp_path / f"c{round_}", tmp_path / f"go{round_}"
        ready = [tmp_path / f"ready{round_}-{i}" for i in range(8)]
        procs = [subprocess.Popen([sys.executable, "-I", str(script), str(folder), str(go), str(CONFIG_PY),
                                   str(r)], stdout=subprocess.PIPE, text=True) for r in ready]
        deadline = time.monotonic() + 30
        while not all(r.exists() for r in ready):
            assert time.monotonic() < deadline and all(p.poll() is None for p in procs)
            time.sleep(0.01)
        go.write_text("go", encoding="utf-8")
        tokens = {proc.communicate(timeout=30)[0].strip() for proc in procs}
        assert all(proc.returncode == 0 for proc in procs)
        assert len(tokens) == 1, tokens
        assert tokens == {(folder / TOKEN_FILE).read_text(encoding="ascii").strip()}


def test_threads_starting_together_share_one_key(tmp_path):
    """同じプロセスの中で同時に呼んでも、合言葉は1つ。"""
    barrier = threading.Barrier(16)
    results: list[str] = []

    def run():
        barrier.wait()
        results.append(load_token(tmp_path / "c"))

    threads = [threading.Thread(target=run) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 16 and len(set(results)) == 1


@pytest.mark.parametrize("content", [
    "", "{", "[]", '{"port": "1", "pid": 2}', '{"port": 0, "pid": 2}', '{"port": 70000, "pid": 2}',
    '{"port": 1, "pid": -1}', '{"port": true, "pid": 2}', '{"pid": 2}',
])
def test_broken_last_run_record_is_ignored(tmp_path, content):
    (tmp_path / LAST_RUN_FILE).write_text(content, encoding="utf-8")
    assert read_last_run(tmp_path) is None


def test_last_run_record_round_trips_privately(tmp_path):
    assert read_last_run(tmp_path / "c") is None
    write_last_run(tmp_path / "c", 28471, 4242)
    assert read_last_run(tmp_path / "c") == (28471, 4242)
    if not WINDOWS:
        assert stat.S_IMODE((tmp_path / "c" / LAST_RUN_FILE).stat().st_mode) == 0o600


# ------------------------------------------------------------------ 画面の好み


@pytest.mark.parametrize("value", ["360", "0", "10000"])
def test_sizes_are_ascii_digits(value):
    assert valid_pref("layout.specWidth", value)


@pytest.mark.parametrize("value", ["３６０", "٣٦٠", "३६०", "36０", "", "10001", "-1", "1e3", " 360", "360 "])
def test_sizes_with_other_digits_or_forms_are_refused(value):
    """\\d は全角や他の文字の数字（٣٦٠、३६०）も通すので、[0-9] だけにした（レビュー2回目の軽5）。"""
    assert not valid_pref("layout.specWidth", value)


@pytest.mark.parametrize(("content", "why"), [
    # 64 KiB（大きすぎるとみなす境目）より小さいまま、入れ子を深くする
    ("[" * 30000 + "]" * 30000, "nesting too deep"),
    ('{"a":' * 10000 + "1" + "}" * 10000, "nesting too deep"),
    ("{not json", "Expecting"),
    ('["a list"]', "not an object"),
    ('{"theme": "dark"' + " " * (70 * 1024) + "}", "too large"),
    (b"\xff\xfe{}", "codec"),
], ids=["deep-array", "deep-object", "not-json", "list", "too-large", "not-utf8"])
def test_broken_settings_are_read_as_defaults_reported_and_kept(tmp_path, content, why):
    """壊れた設定ファイル（深い入れ子、JSON でない、大きすぎる）は既定で読み、1回だけ知らせ、

    上書きする前に settings.json.broken へ控える（レビュー2回目の軽3）。"""
    path = tmp_path / SETTINGS_FILE
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    reports = []
    settings = Settings(tmp_path, on_broken=lambda p, reason, backup: reports.append((p, reason, backup)))
    assert settings.read() == {}
    assert settings.read() == {}
    assert len(reports) == 1 and reports[0][0] == path and why in reports[0][1], reports
    assert settings.update({"theme": "dark"}) == {"theme": "dark"}
    assert (tmp_path / (SETTINGS_FILE + ".broken")).read_bytes() == data
    assert json.loads(path.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert settings.read() == {"theme": "dark"} and len(reports) == 1


def test_settings_that_cannot_be_written_raise_oserror(tmp_path):
    """書けないとき（ここでは、設定ファイルと控えの場所がどちらも中身のあるフォルダ）は OSError を出す。

    アプリ本体はそれを 500 と理由にして返す（画面は送り直しを止め、お知らせで知らせる）。"""
    (tmp_path / SETTINGS_FILE).mkdir()
    (tmp_path / (SETTINGS_FILE + ".broken")).mkdir()
    (tmp_path / (SETTINGS_FILE + ".broken") / "x").write_text("x", encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.read() == {}
    with pytest.raises(OSError):
        settings.update({"theme": "dark"})


# ------------------------------------------------------------------ Windows の権限（ACL）


ME = "S-1-5-21-1-2-3-1001"


@pytest.mark.parametrize(("sddl", "expected"), [
    (f"D:PAI(A;;FA;;;{ME})(A;;FA;;;SY)", {ME, "SY"}),
    ("D:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;S-1-5-21-9)", {"SY", "BA", "S-1-5-21-9"}),
    ("D:(A;;0x1200a9;;;WD)(D;;FA;;;BG)", {"WD"}),  # 拒否（D）は数えない
    # 継承するだけ（IO）も数える: 場所なら、中に作るファイルに効く（レビュー3回目の軽）
    (f"D:P(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)(A;OICIIO;FR;;;WD)", {ME, "SY", "WD"}),
    # 条件つきの許可（XA）も許可として数える。後ろの条件の入れ子の括弧も読める
    (f"D:P(A;;FA;;;{ME})(XA;;FA;;;WD;(@User.Project Any_of {{1, 2}}))", {ME, "WD"}),
    (f"D:P(A;;FA;;;{ME})(OA;;FA;bf967aba-0de6-11d0-a285-00aa003049e2;;WD)", {ME, "WD"}),
    (f"D:P(A;;FA;;;{ME})(ZZ;;FA;;;SY)", None),  # 知らない種類: 本人だけとはみなさない
    (f"D:P(A;;FA;;;{ME})(A;;FA)", None),  # 読めない形
    (f"D:P(A;;FA;;;{ME}", None),  # 括弧が閉じていない
    ("D:NO_ACCESS_CONTROL", None),  # DACL が無い: 誰でも使える
    ("O:BAG:SYD:(A;;FA;;;AU)S:(AU;SA;FA;;;WD)", {"AU"}),
    ("D:P", set()),
], ids=lambda v: v if isinstance(v, str) else repr(v))
def test_sddl_trustees(sddl, expected):
    assert sddl_trustees(sddl) == expected


@pytest.mark.parametrize(("sddl", "user", "ok"), [
    (f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)", ME, True),
    (f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;{ME})", ME, True),  # 並びは問わない
    ("D:PAI(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)", "S-1-5-21-1-2-3-500", True),  # 組み込みの Administrator
    (f"D:AI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)", ME, False),  # 上から受け継ぐ（P が無い）
    (f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)(A;OICIIO;FR;;;WD)", ME, False),  # 継承するだけの Everyone
    (f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)(XA;;FA;;;WD;(@User.x == 1))", ME, False),  # 条件つきの許可
    (f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;SY)(D;;FA;;;WD)", ME, False),  # 知らない形は付け直す
    (f"D:PAI(A;OICI;FA;;;{ME})(A;OICI;FA;;;BA)", ME, False),
    (f"D:PAI(A;;FA;;;{ME})(A;;FA;;;SY)", ME, False),  # 中に作るものへ受け継がせない
    (f"D:PAI(A;OICI;FR;;;{ME})(A;OICI;FA;;;SY)", ME, False),
    ("D:PAI(A;OICI;FA;;;S-1-5-21-9)(A;OICI;FA;;;SY)", ME, False),  # ほかの利用者
    ("D:NO_ACCESS_CONTROL", ME, False),
])
def test_folder_dacl_must_match_exactly(sddl, user, ok):
    """場所の DACL は、付ける形と完全に一致するかで見る。違えば付け直す（レビュー3回目の軽）。"""
    assert folder_dacl_is_private(sddl, user) is ok


def windows_user_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         errors="replace", check=True).stdout
    return out.strip().split(",")[-1].strip('"')


def saved_sddl(path: Path, tmp: Path) -> str:
    saved = tmp / f"acl-{path.name}.txt"
    subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, check=True)
    return saved.read_bytes().decode("utf-16-le", errors="replace")


@pytest.mark.skipif(not WINDOWS, reason="Windows の権限（ACL）。POのPCの local_ci で流れる")
def test_token_file_and_folder_get_an_explicit_private_acl_on_windows(tmp_path):
    """場所と合言葉のファイルに、本人と SYSTEM だけの権限を明示して付け、上から受け継がない（D:P）。

    上の階層に Everyone の読み取りを足しておいても、合言葉のファイルには届かない（icacls で確かめる）。"""
    parent = tmp_path / "shared"
    parent.mkdir()
    subprocess.run(["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)R"], capture_output=True, check=True)
    folder = parent / "circuit-sim"
    load_token(folder)
    me = windows_user_sid()
    for target in (folder, folder / TOKEN_FILE):
        sddl = saved_sddl(target, tmp_path)
        assert "D:P" in sddl, (target, sddl)
        trustees = sddl_trustees(sddl[sddl.index("D:"):].strip())
        assert trustees == {me, "SY"}, (target, sddl)
    out = subprocess.run(["icacls", str(folder / TOKEN_FILE)], capture_output=True, text=True,
                         errors="replace", check=True).stdout
    assert "Everyone" not in out and "(I)" not in out, out


@pytest.mark.skipif(not WINDOWS, reason="Windows の権限（ACL）。POのPCの local_ci で流れる")
def test_inherit_only_entry_on_the_folder_is_removed_on_windows(tmp_path):
    """場所に「中に作るものだけに効く」Everyone の項目（IO）を足されても、次の起動で付け直す。

    新しく作る合言葉・設定・転送用の HTML に Everyone が受け継がれない。"""
    folder = tmp_path / "circuit-sim"
    load_token(folder)
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-1-0:(OI)(CI)(IO)(R)"], capture_output=True,
                   check=True)
    assert "WD" in saved_sddl(folder, tmp_path) or "S-1-1-0" in saved_sddl(folder, tmp_path)
    load_token(folder)
    sddl = saved_sddl(folder, tmp_path)
    assert folder_dacl_is_private(sddl[sddl.index("D:"):].strip(), windows_user_sid()), sddl
    (folder / "new.txt").write_text("x", encoding="utf-8")
    new = saved_sddl(folder / "new.txt", tmp_path)
    assert sddl_trustees(new[new.index("D:"):].strip()) == {windows_user_sid(), "SY"}, new


@pytest.mark.skipif(not WINDOWS, reason="Windows の権限（ACL）。POのPCの local_ci で流れる")
def test_launch_page_is_private_on_windows(tmp_path):
    """ブラウザに渡す転送用の HTML（中に起動の番号がある）も、本人と SYSTEM だけが読める（D:P）。"""
    from circuit_sim.app.cli import LaunchPages

    parent = tmp_path / "shared"
    parent.mkdir()
    subprocess.run(["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)R"], capture_output=True, check=True)
    folder = parent / "circuit-sim"
    load_token(folder)
    page = LaunchPages(folder).write("n", "http://127.0.0.1:28470/?k=n")
    sddl = saved_sddl(page, tmp_path)
    assert "D:P" in sddl, sddl
    assert sddl_trustees(sddl[sddl.index("D:"):].strip()) == {windows_user_sid(), "SY"}, sddl


@pytest.mark.skipif(not WINDOWS, reason="Windows の権限（ACL）。POのPCの local_ci で流れる")
def test_token_readable_by_everyone_is_replaced_on_windows(tmp_path):
    """Everyone が読めた合言葉は、漏れたものとみなして作り直し、権限も直す。"""
    first = load_token(tmp_path / "c")
    path = tmp_path / "c" / TOKEN_FILE
    subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], capture_output=True, check=True)
    second = load_token(tmp_path / "c")
    assert second != first
    sddl = saved_sddl(path, tmp_path)
    assert sddl_trustees(sddl[sddl.index("D:"):].strip()) == {windows_user_sid(), "SY"}, sddl


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
