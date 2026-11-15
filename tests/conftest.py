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
   - claude があるので取り除いた場所（Linux の /usr/bin、Homebrew の bin など）は、claude 以外の中身への
     リンクを並べた場所に置き換える（同じ場所にある git、bash などが消えないように）。
     リンクを作れない Windows では、取り除くだけにし、そこにしか git が無ければ、試験を始めずに止める。
   - セッションの始めに、取り除いた場所（理由つき）と、リンクで残した道具を表示する（原因を追えるように）。
   - 見張り役を起動できない（一時フォルダが実行不可）か、遮断を組めない（Windows で git が claude と同じ場所に
     しか無い）なら、理由を表示して、試験を始めずに止める（終了コード GUARD_EXIT_CODE。見張り役が起動された
     ときの SENTINEL_EXIT_CODE とは分ける）。
   - 呼び出し元の環境にある、見張り役に記録を残させない印（SENTINEL_PROBE_VAR）は、セッションの始めに消す。

3. Windows で、OSのエラー画面（「サポートされていない 16 ビット アプリケーション」など。人が OK を押すまで
   止まる）を出させない。試験のプロセスに設定し、起動する子プロセスに引き継がれる。scripts/cycle.py の
   quiet_error_dialogs と同じ設定（この conftest は別の場所に写して使われるので、ここに持つ）。

4. 無視に設定されて引き継いだ終了の合図（nohup の SIGHUP など）を、受け取って何もしない扱いに置き換える
   （試験が起動するプロセスでは既定の扱いに戻り、合図で止める試験が、nohup の下でも同じに動く）。
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
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
SENTINEL_EXIT_CODE = 71         # 見張り役が起動された試験のセッションの終了コード
# 遮断を組めない、見張り役を起動できない（試験を始めなかった）セッションの終了コード。71 と分ける
# （1か所ずつ壊す確認は 71 を「壊れたことに試験が気づいた」と数える。始められなかった実行を、そう数えない）
GUARD_EXIT_CODE = 72
SENTINEL_RETURNCODE = 97        # 見張り役そのものの終了コード
SENTINEL_LOG = "started.log"
# 見張り役を、記録を残さずに起動できるかだけを確かめるときの印（セッションの始めに1回）。
# 呼び出し元の環境にあると、見張り役が記録を残さずに 0 で終わるので、セッションの始めに消す
SENTINEL_PROBE_VAR = "CIRCUIT_SIM_SENTINEL_PROBE"
# claude として起動されうる拡張子（PATHEXT に無くても数える。npm は claude、claude.cmd、claude.ps1 を置く）
CLAUDE_SUFFIXES = ("", ".exe", ".cmd", ".bat", ".com", ".ps1")
# 取り除いた場所にしか無ければ、リンクで残す道具（試験が名前で起動するもの。Python は sys.executable）
KEPT_TOOLS = ("git",)


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
    for shadow in sorted(tools.iterdir()) if tools.is_dir() else []:
        links = sorted(link.name for link in shadow.iterdir())
        origin = shown_place(str(Path(os.readlink(shadow / links[0])).parent)) if links else "?"
        more = f" ほか {len(links) - 8} 個" if len(links) > 8 else ""
        lines.append(f"  リンクで残した道具（{origin} の claude 以外）: {'、'.join(links[:8])}{more}")
    return [
        "試験の遮断（本物の claude を起動しないため。tests/conftest.py）:",
        *(lines or ["  PATH から外した場所: なし"]),
    ]


def link_all_but_claude(entry: str, names: frozenset[str], shadow: Path) -> None:
    """entry の中身のうち、claude として起動されうるもの以外へのリンクを、shadow に並べる。"""
    shadow.mkdir(parents=True, exist_ok=True)
    for name in sorted(os.listdir(entry)):
        if name.lower() not in names:
            os.symlink(os.path.join(entry, name), shadow / name)


def check_lost_tools(before: list[str], kept: list[str]) -> None:
    """取り除いた場所にしか無い道具（KEPT_TOOLS）があれば、遮断を組まずに止める。Windows 用"""
    lost = {}
    for tool in KEPT_TOOLS:
        found = shutil.which(tool, path=os.pathsep.join(before))
        if found and not shutil.which(tool, path=os.pathsep.join(kept)):
            lost[tool] = found
    if lost:
        places = "、".join(str(Path(found).parent) for found in lost.values())
        raise GuardError(
            f"{'、'.join(lost)} が、claude と同じ場所（{places}）にしか無い。"
            "その場所を PATH から外すと見つからなくなるので、試験を始めない。"
            "別の場所に入れてから実行する"
        )


# 見張り役。起動されたら、記録の1行（試験の名前が分かれば、その名前と、実行した場所）を足して、失敗で終わる
SENTINEL_SH = """\
#!/bin/sh
# 試験の見張り役（tests/conftest.py）。本物の claude の代わりに、PATH の末尾に置いてある
[ -n "${{{probe}-}}" ] && exit 0
printf '%s\\t%s\\n' "${{PYTEST_CURRENT_TEST-}}" "$(pwd)" >> {log}
exit {code}
"""
# Windows の起動用のファイル。記録の場所は、このファイルの場所から求める（場所の文字を書き込まない）
SENTINEL_CMD = """\
@echo off
rem 試験の見張り役（tests/conftest.py）。本物の claude の代わりに、PATH の末尾に置いてある
if defined {probe} exit /b 0
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
        body = SENTINEL_CMD.format(log=SENTINEL_LOG, code=SENTINEL_RETURNCODE, probe=SENTINEL_PROBE_VAR)
        body = body.replace("\n", "\r\n")
        for name in ("claude.cmd", "claude.bat"):
            (directory / name).write_bytes(body.encode("ascii", errors="replace"))
    else:
        command = directory / "claude"
        quoted = shlex.quote(str(log))
        body = SENTINEL_SH.format(log=quoted, code=SENTINEL_RETURNCODE, probe=SENTINEL_PROBE_VAR)
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    return log


def guard_path(path: str, pathext: str | None, place: Path, windows: bool = WINDOWS) -> tuple[str, str]:
    """（claude の無い PATH、その末尾に見張り役を置いた PATH）を組む。見張り役と道具のリンクは place の中。

    claude があるので取り除いた場所は、リンクを作れるOSでは、claude 以外の中身へのリンクを並べた場所
    （place/tools/<番号>）に、同じ順番のまま置き換える。
    """
    names = claude_names(pathext)
    entries = path.split(os.pathsep)
    kept = []
    for index, entry in enumerate(entries):
        if removal_reason(entry, names) is None:
            kept.append(entry)
        elif not windows and os.path.isabs(entry) and claude_files(entry, names):
            shadow = place / "tools" / str(index)
            link_all_but_claude(entry, names, shadow)
            kept.append(str(shadow))
    if windows:
        check_lost_tools([entry for entry in entries if os.path.isabs(entry)], kept)
    safe = os.pathsep.join(kept)
    sentinel = place / "sentinel"
    install_sentinel(sentinel, windows)
    return safe, os.pathsep.join(filter(None, [safe, str(sentinel)]))


def sentinel_starts(directory: Path) -> bool:
    """見張り役を起動できるか（記録は残さない）。一時フォルダが実行不可だと、起動できない。"""
    command = directory / ("claude.cmd" if WINDOWS else "claude")
    try:
        proc = subprocess.run(
            [str(command)], env={**os.environ, SENTINEL_PROBE_VAR: "1"}, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def called_tests(recorded: list[str], calls: list[str]) -> list[str]:
    """見張り役を起動した試験の名前。並列実行（pytest-xdist）の子で起動されると、このセッションは控えられないので、
    見張り役の記録（PYTEST_CURRENT_TEST）から取る。"""
    if recorded:
        return recorded
    named = sorted({line.split("\t")[0].rsplit(" (", 1)[0] for line in calls if line.split("\t")[0]})
    return [f"{name}（見張り役の記録から）" for name in named] or ["（試験の外。どの試験か分からない）"]


def sentinel_calls(log: Path) -> list[str]:
    try:
        return log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


# このセッションが組んだ遮断（並列実行の子では組まない。子は、親の組んだ PATH を引き継ぐ）
GUARD: dict = {}


# 試験が送る、または試験の中で起こす終了の合図
STOP_SIGNAL_NAMES = ("SIGINT", "SIGTERM", "SIGHUP")


def leave_alone(signum: int, frame: object) -> None:
    """受け取って何もしない扱い（無視と同じく、試験のセッションは続く）。"""


def catch_ignored_stop_signals() -> list[str]:
    """無視に設定されて引き継いだ終了の合図（nohup の SIGHUP、ジョブ制御の無いシェルの & の SIGINT）を、
    受け取って何もしない扱い（leave_alone）に置き換える。置き換えた合図の名前を返す。Linux / macOS だけ。

    無視のままだと、試験が起動するプロセスにも無視が引き継がれ、ハーネスは無視の合図を差し替えない（決まりの
    とおり）ので、合図で止める試験が、止まらないプロセスを待ち続けて落ちる。試験のプロセスの中の枠
    （termination_as_exception）も、合図を例外に変えない。Python の関数の扱いは、起動するプロセスには
    引き継がれない（exec で既定の扱いに戻る）。試験のセッションそのものは、置き換えた後も合図で止まらない。
    """
    if WINDOWS:
        return []
    replaced = []
    for name in STOP_SIGNAL_NAMES:
        sig = getattr(signal, name)
        if signal.getsignal(sig) is signal.SIG_IGN:
            signal.signal(sig, leave_alone)
            replaced.append(name)
    return replaced


def pytest_configure(config: pytest.Config) -> None:
    # 並列実行の子でも設定する（OSのエラー画面は、画面を出させたプロセスを止める）
    quiet_error_dialogs()
    catch_ignored_stop_signals()


def pytest_sessionstart(session: pytest.Session) -> None:
    # 見張り役に記録を残させない印は、セッションの始めの確かめ（sentinel_starts）だけが、その起動に付ける。
    # 呼び出し元から引き継いだものは、試験と、試験が起動するプロセスに渡さない
    os.environ.pop(SENTINEL_PROBE_VAR, None)
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
        pytest.exit(str(exc), returncode=GUARD_EXIT_CODE)
    if not sentinel_starts(place / "sentinel"):
        shutil.rmtree(place, ignore_errors=True)
        pytest.exit(
            f"見張り役の claude を、一時フォルダ（{shown_place(str(place))}）で起動できない"
            "（実行できない場所の可能性）。見張り役が動かないと、本物の claude に届く試験を"
            "見つけられないので、"
            "試験を始めない。"
            "環境変数 TMPDIR（Windows では TEMP）を、実行できる場所に変えて流す",
            returncode=GUARD_EXIT_CODE,
        )
    # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
    print("\n".join(guard_report(path, pathext, place / "tools")), file=sys.stderr, flush=True)
    GUARD.update(place=place, log=place / "sentinel" / SENTINEL_LOG, tests=[])
    os.environ["PATH"] = guarded            # ここから起動するプロセスに引き継がれる
    os.environ[SAFE_PATH_VAR] = safe
    os.environ[SENTINEL_VAR] = str(place / "sentinel")
    os.environ[RUNNING] = "1"


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
        tests = called_tests(GUARD["tests"], calls)
        # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
        print(
            f"\n失敗: 見張り役の claude が {len(calls)} 回起動された（本物の claude なら、起動されていた）。"
            "偽の claude を置く試験の PATH の組み立てを直す。\n"
            + "".join(f"  起動した試験: {name}\n" for name in tests)
            + "".join(f"  記録: {line}\n" for line in calls[:20]),
            file=sys.stderr, flush=True,
        )
    shutil.rmtree(GUARD["place"], ignore_errors=True)
