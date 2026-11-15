"""試験の全体に掛かる設定。

1. 試験の中から、このリポジトリの試験がもう一度実行されること（入れ子）を止める。
   確認スクリプト（scripts/check.py）や環境の確認（scripts/doctor.py）を起動する試験で、起動する場所の指定が
   壊れると、本物の scripts/check.py → pytest → 同じ試験 → … と続いて、終わらなくなるため。
   試験を集めるだけの起動（--collect-only）は、実行しないので止めない。

2. 試験から、本物の claude に行き着く経路を断つ（遮断と見張り役）。
   試験は、偽の claude を PATH の先頭に置いて動かす。偽が飛ばされたり（実行できない一時フォルダ、Windows の
   拡張子の扱い）、起動に失敗したり（シェルは PATH の探索を続ける）すると、PATH の後ろの本物に届いていた。
   - 遮断: 試験のセッションの始めに、PATH から、claude として起動されうるファイル（claude、claude.exe、
     claude.cmd、claude.bat、claude.com、claude.ps1 と、PATHEXT の拡張子。大文字と小文字は区別しない）の
     ある場所と、相対パスの項目（実行した場所によって指す先が変わる）を取り除く。中を確かめられない場所も
     取り除く。子プロセスは、この PATH を引き継ぐ。
   - 見張り役: 取り除いた後の PATH の末尾に、見張り役の claude を置く。起動されたら記録を残して 0 でない
     終了コードで終わる。試験のセッションの終わりに、一度でも起動されていたら、全体を失敗にする
     （終了コード SENTINEL_EXIT_CODE。どの試験の間に起動されたかを表示する）。
   - 取り除いた場所にしか git が無い場合（Homebrew の bin など）は、git へのリンクを別の場所に置いて残す。
     リンクを作れない Windows では、試験を始めずに止める。
   - セッションの始めに、取り除いた場所（理由つき）と、リンクで残した道具を表示する（原因を追えるように）。

3. Windows で、OSのエラー画面（「サポートされていない 16 ビット アプリケーション」など。人が OK を押すまで
   止まる）を出させない。試験のプロセスに設定し、起動する子プロセスに引き継がれる。scripts/cycle.py の
   quiet_error_dialogs と同じ設定（この conftest は別の場所に写して使われるので、ここに持つ）。

4. アプリの設定の場所（合言葉と画面の好み。D-050、D-051）を、試験のセッションの一時フォルダに向ける
   （CIRCUIT_SIM_CONFIG_DIR）。試験から起動するアプリ本体が、利用者の本物の設定に触れないように。
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

RUNNING = "CIRCUIT_SIM_PYTEST_RUNNING"
NESTED_EXIT_CODE = 70

WINDOWS = os.name == "nt"
# 見張り役のある場所、claude の無い PATH（見張り役を含まない）を、子プロセスに知らせる環境変数
SENTINEL_VAR = "CIRCUIT_SIM_CLAUDE_SENTINEL"
SAFE_PATH_VAR = "CIRCUIT_SIM_SAFE_PATH"
# 遮断で PATH から外した場所（絶対パスのもの）。外した場所にしか無い道具を、PATH に戻さずに絶対パスで呼ぶため
# （tests/test_app_frontend.py の node。Homebrew や npm -g では node と claude が同じ場所にある）
REMOVED_PATH_VAR = "CIRCUIT_SIM_GUARD_REMOVED"
SENTINEL_EXIT_CODE = 71         # 見張り役が起動された試験のセッションの終了コード
SENTINEL_RETURNCODE = 97        # 見張り役そのものの終了コード
SENTINEL_LOG = "started.log"
# claude として起動されうる拡張子（PATHEXT に無くても数える。npm は claude、claude.cmd、claude.ps1 を置く）
CLAUDE_SUFFIXES = ("", ".exe", ".cmd", ".bat", ".com", ".ps1")
# 取り除いた場所にしか無ければ、リンクで残す道具（試験が名前で起動するもの。Python は sys.executable）
KEPT_TOOLS = ("git",)
# アプリの設定の場所（circuit_sim/app/config.py の CONFIG_ENV）
CONFIG_VAR = "CIRCUIT_SIM_CONFIG_DIR"


# Windows の SetErrorMode の印（scripts/cycle.py の QUIET_ERROR_MODE と同じ）。起動できない実行ファイルや
# 見つからない DLL の画面、異常終了の画面、ファイルを開けない画面を出さない
QUIET_ERROR_MODE = 0x0001 | 0x0002 | 0x8000


class GuardError(Exception):
    """遮断を組めない（取り除いた場所にしか無い道具を、残せない）。"""


def quiet_error_dialogs() -> None:
    """Windows で、OSのエラー画面を出させずに、エラーとして返させる。起動する子プロセスにも引き継がれる。"""
    if not WINDOWS:
        return
    import ctypes  # Windows でだけ読む（ctypes の無い Python の作りでも、ほかのOSで動くように）

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.SetErrorMode(kernel32.GetErrorMode() | QUIET_ERROR_MODE)


def claude_names(pathext: str | None) -> frozenset[str]:
    """claude として起動されうるファイルの名前（小文字）。PATHEXT の区切りは、どのOSでも ;。"""
    suffixes = {*CLAUDE_SUFFIXES, *(item.lower() for item in (pathext or "").split(";") if item)}
    return frozenset("claude" + suffix for suffix in suffixes)


def claude_files(entry: str, names: frozenset[str]) -> list[str] | None:
    """その場所にある、claude として起動されうるファイルの名前。中を確かめられなければ None。"""
    try:
        found = os.listdir(entry)
    except OSError:
        return None
    return sorted(name for name in found if name.lower() in names)


def removal_reason(entry: str, names: frozenset[str]) -> str | None:
    """PATH の項目を取り除く理由。残してよければ None。中を確かめられない場所は、claude があるものとする。"""
    if not os.path.isabs(entry):
        return "相対パスの項目" if entry else "空の項目"
    found = claude_files(entry, names)
    if found is None:
        return "中を確かめられない場所"
    return f"{'、'.join(found)} がある" if found else None


def without_claude(path: str, pathext: str | None) -> list[str]:
    """PATH の項目のうち、残してよいもの（絶対パスで、claude として起動されうるファイルが無い場所）。"""
    names = claude_names(pathext)
    return [entry for entry in path.split(os.pathsep) if removal_reason(entry, names) is None]


def shown_place(path: str) -> str:
    """表示する場所。利用者のホームの下は ~ から書く（確認の記録に残っても、利用者名が出ないように）。"""
    try:
        return str(Path("~") / Path(path).relative_to(Path.home()))
    except (ValueError, RuntimeError):      # ホームの下ではない、ホームを決められない
        return path


def guard_report(path: str, pathext: str | None, tools: Path) -> list[str]:
    """遮断で取り除いた PATH の項目（理由つき）と、リンクで残した道具（tools の中）を、表示の行にする。"""
    names = claude_names(pathext)
    removed = [(entry, removal_reason(entry, names)) for entry in path.split(os.pathsep)]
    lines = [f"  PATH から外した場所: {shown_place(entry)}（{why}）" for entry, why in removed if why]
    links = sorted(tools.iterdir()) if tools.is_dir() else []
    lines += [f"  リンクで残した道具: {link.name} → {shown_place(os.readlink(link))}" for link in links]
    return [
        "試験の遮断（本物の claude を起動しないため。tests/conftest.py）:",
        *(lines or ["  PATH から外した場所: なし"]),
    ]


def keep_lost_tools(before: list[str], kept: list[str], place: Path) -> list[str]:
    """取り除いた場所にしか無い道具（KEPT_TOOLS）を、place に置いたリンクで残す。足す PATH の項目を返す。"""
    lost = {}
    for tool in KEPT_TOOLS:
        found = shutil.which(tool, path=os.pathsep.join(before))
        if found and not shutil.which(tool, path=os.pathsep.join(kept)):
            lost[tool] = found
    if not lost:
        return []
    if WINDOWS:
        places = "、".join(str(Path(found).parent) for found in lost.values())
        raise GuardError(
            f"{'、'.join(lost)} が、claude と同じ場所（{places}）にしか無い。"
            "その場所を PATH から外すと見つからなくなるので、試験を始めない。"
            "別の場所に入れてから実行する"
        )
    place.mkdir(parents=True, exist_ok=True)
    for tool, found in lost.items():
        os.symlink(found, place / tool)
    return [str(place)]


# 見張り役。起動されたら、記録の1行（試験の名前が分かれば、その名前と、実行した場所）を足して、失敗で終わる
SENTINEL_SH = """\
#!/bin/sh
# 試験の見張り役（tests/conftest.py）。本物の claude の代わりに、PATH の末尾に置いてある
printf '%s\\t%s\\n' "${{PYTEST_CURRENT_TEST-}}" "$(pwd)" >> {log}
exit {code}
"""
# Windows の起動用のファイル。記録の場所は、このファイルの場所から求める（場所の文字を書き込まない）
SENTINEL_CMD = """\
@echo off
rem 試験の見張り役（tests/conftest.py）。本物の claude の代わりに、PATH の末尾に置いてある
>>"%~dp0{log}" echo claude-sentinel
exit /b {code}
"""


def install_sentinel(directory: Path, windows: bool = WINDOWS) -> Path:
    """見張り役の claude を directory に置き、記録のファイルの場所を返す。

    Windows では claude.cmd と claude.bat（PATHEXT から .CMD が外れても当たる）。.exe は標準ライブラリでは
    作れない。それ以外では、実行できる claude（/bin/sh のスクリプト。Python の場所に左右されない）。
    """
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / SENTINEL_LOG
    if windows:
        body = SENTINEL_CMD.format(log=SENTINEL_LOG, code=SENTINEL_RETURNCODE).replace("\n", "\r\n")
        for name in ("claude.cmd", "claude.bat"):
            (directory / name).write_bytes(body.encode("ascii", errors="replace"))
    else:
        command = directory / "claude"
        body = SENTINEL_SH.format(log=shlex.quote(str(log)), code=SENTINEL_RETURNCODE)
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    return log


def guard_path(path: str, pathext: str | None, place: Path, windows: bool = WINDOWS) -> tuple[str, str]:
    """（claude の無い PATH、その末尾に見張り役を置いた PATH）を組む。見張り役と道具のリンクは place の中。"""
    before = [entry for entry in path.split(os.pathsep) if os.path.isabs(entry)]
    kept = without_claude(path, pathext)
    safe = os.pathsep.join([*kept, *keep_lost_tools(before, kept, place / "tools")])
    sentinel = place / "sentinel"
    install_sentinel(sentinel, windows)
    return safe, os.pathsep.join(filter(None, [safe, str(sentinel)]))


def sentinel_calls(log: Path) -> list[str]:
    try:
        return log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


# このセッションが組んだ遮断（並列実行の子では組まない。子は、親の組んだ PATH を引き継ぐ）
GUARD: dict = {}


def pytest_configure(config: pytest.Config) -> None:
    # 並列実行の子でも設定する（OSのエラー画面は、画面を出させたプロセスを止める）
    quiet_error_dialogs()


def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config
    if hasattr(config, "workerinput"):      # 並列実行の子（pytest-xdist）は、同じ1回の実行の一部
        return
    if os.environ.get(RUNNING) and not config.option.collectonly:
        pytest.exit(
            "試験の中から、このリポジトリの試験がもう一度実行された（入れ子）。終わらなくなるので止める",
            returncode=NESTED_EXIT_CODE,
        )
    place = Path(tempfile.mkdtemp(prefix="circuit-sim-guard-"))
    path, pathext = os.environ.get("PATH", ""), os.environ.get("PATHEXT")
    try:
        safe, guarded = guard_path(path, pathext, place)
    except GuardError as exc:
        shutil.rmtree(place, ignore_errors=True)
        pytest.exit(str(exc), returncode=SENTINEL_EXIT_CODE)
    # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
    print("\n".join(guard_report(path, pathext, place / "tools")), file=sys.stderr, flush=True)
    GUARD.update(place=place, log=place / "sentinel" / SENTINEL_LOG, tests=[])
    os.environ["PATH"] = guarded            # ここから起動するプロセスに引き継がれる
    os.environ[SAFE_PATH_VAR] = safe
    kept = set(safe.split(os.pathsep))
    os.environ[REMOVED_PATH_VAR] = os.pathsep.join(
        entry for entry in path.split(os.pathsep) if os.path.isabs(entry) and entry not in kept
    )
    os.environ[SENTINEL_VAR] = str(place / "sentinel")
    os.environ[RUNNING] = "1"
    os.environ[CONFIG_VAR] = str(place / "app-config")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """見張り役が、どの試験の間に起動されたかを控える。"""
    seen = len(sentinel_calls(GUARD["log"])) if GUARD else 0
    yield
    if GUARD and len(sentinel_calls(GUARD["log"])) > seen:
        GUARD["tests"].append(item.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not GUARD:
        return
    calls = sentinel_calls(GUARD["log"])
    if calls:
        session.exitstatus = SENTINEL_EXIT_CODE
        tests = GUARD["tests"] or ["（試験の外。どの試験か分からない）"]
        # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
        print(
            f"\n失敗: 見張り役の claude が {len(calls)} 回起動された（本物の claude なら、起動されていた）。"
            "偽の claude を置く試験の PATH の組み立てを直す。\n"
            + "".join(f"  起動した試験: {name}\n" for name in tests)
            + "".join(f"  記録: {line}\n" for line in calls[:20]),
            file=sys.stderr, flush=True,
        )
    shutil.rmtree(GUARD["place"], ignore_errors=True)
