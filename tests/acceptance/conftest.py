"""受け入れ試験（tests/acceptance/）の全体に掛かる見張り。

1. 試験が起動したプロセスを、残さない（中身と、見分け方の決まりは acceptance_watch.py）。
   - 試験ごと: その試験が起動したプロセスが、試験の終わり（ほかの後始末の後）に動いていたら、止めて、
     その試験を失敗にする（「起動に失敗して終わるはず」が成功した、途中で例外で抜けた、待ち時間が切れた）。
   - モジュールごと: モジュールの間だけ動かすフィクスチャ（アプリ本体、ブラウザ）が起動したものが、
     モジュールの終わりに動いていたら、止めて、失敗にする。準備の途中で失敗したフィクスチャが起動した
     ものは、その場で止める（準備に失敗したフィクスチャは、後始末を呼ばれない）。
   - 実行の全体: 最初の受け入れ試験の前に、動いているアプリ本体らしいプロセスを知らせる（止めない）。
     実行の終わりに、この実行から生まれたアプリ本体らしいプロセスが残っていたら、実行の全体を失敗にする
     （終了コード LEFTOVER_EXIT_CODE。受け入れ試験のほかの試験が残したものも、ここで見つかる）。
   モジュールの間だけ動かすフィクスチャが、試験の最中に新しいプロセスを起動して次の試験へ持ち越す形
   （準備の外での起動）は、「その試験が残した」と数える。持ち越すものは、フィクスチャの準備の中で起動する。

2. 試験から、本物のブラウザを開かせない（部品は a0_fake_browser.py）。
   - 受け入れ試験の間、環境変数 BROWSER に見張り役を置く。ブラウザを開く起動（--no-browser 無し）を、
     a0_fake_browser を通さずにした試験があると、本物のブラウザの代わりに見張り役が呼ばれ、記録が残る。
   - 試験ごとの記録（A0_FAKE_BROWSER_NOTES）に、本物を開く経路が呼ばれた（real-route）、見張り役が
     呼ばれた（unguarded）の行が残っていたら、その試験を失敗にする。

3. conftest という名前を、tests/conftest.py に戻す（pytest_collectstart の説明）。
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

import a0_fake_browser as fake
import acceptance_watch as watch
import pytest

TRACKER = watch.Tracker()
# この実行の見張りの状態（最初の受け入れ試験で始める）
SESSION: dict = {}


def say(text: str) -> None:
    # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
    print(text, file=sys.stderr, flush=True)


def process_lines(processes: list[watch.Process]) -> str:
    return "".join(f"  PID {p.pid}: {watch.shown(p.command)}\n" for p in processes[:20])


def browser_problems(notes: Path) -> list[str]:
    """記録に残った、試験として許されない行（本物を開く経路、部品を通さない起動）。"""
    lines = [
        f"本物のブラウザを開く経路が呼ばれた（止めた）: {text}" for text in fake.noted("real-route", notes)
    ]
    lines += [
        f"a0_fake_browser を通さずに、ブラウザを開く起動をした（見張り役が呼ばれた）: {text}"
        for text in fake.noted("unguarded", notes)
    ]
    return lines


def pytest_collectstart(collector: pytest.Collector) -> None:
    """Python の中での conftest という名前を、tests/conftest.py に戻す。

    pytest は、パッケージでない場所の conftest.py を、どれも conftest という名前で読み込み、後から読んだ
    ものを sys.modules["conftest"] に残す。tests/test_claude_guard.py と tests/test_dev_tools.py は、
    import conftest で tests/conftest.py を引いているので、このファイルを置いただけで、そちらがこのファイルを
    引いてしまう。
    このファイルを名前で引く試験は無い（pytest は、読み込んだものを名前に頼らずに持っている）。"""
    mine = sys.modules.get("conftest")
    if mine is None or Path(getattr(mine, "__file__", "") or "").resolve() != Path(__file__).resolve():
        return
    above = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in collector.config.pluginmanager.get_plugins():
        file = getattr(plugin, "__file__", None)
        if file and Path(file).resolve() == above:
            sys.modules["conftest"] = plugin
            return


@pytest.fixture(scope="session", autouse=True)
def acceptance_run_is_marked():
    """この実行の印を環境変数に置き、始める前から動いているアプリ本体らしいプロセスを知らせる。"""
    if SESSION:
        yield
        return
    token = f"{os.getpid()}-{secrets.token_hex(6)}"
    place = Path(tempfile.mkdtemp(prefix="circuit-sim-acceptance-"))
    (place / "sentinel_browser.py").write_text(fake.SENTINEL, encoding="utf-8")
    SESSION.update(token=token, since=time.time(), previous=os.environ.get(watch.MARK_VAR), place=place,
                   count=0)
    os.environ[watch.MARK_VAR] = token      # ここから起動するプロセスが引き継ぐ
    already = watch.running_apps()
    if already:
        say(
            f"\n受け入れ試験の見張り: 試験を始める前から、アプリ本体らしいプロセスが {len(already)} 個"
            "動いている（この実行のものではない。前の試験の残りなら止める。自分で動かしているものなら、"
            "そのままでよい）。\n" + process_lines(already)
        )
    yield
    # 終わりの確かめは pytest_sessionfinish（最後に走った試験の後始末の失敗にしない）


@pytest.fixture(scope="module", autouse=True)
def acceptance_module_watch(acceptance_run_is_marked):
    """モジュールの間、起動したプロセスを控え、BROWSER に見張り役を置く。

    モジュールの終わりに動いているプロセスは、止めて失敗にする。"""
    place: Path = SESSION["place"]
    notes = place / "module.notes"
    saved = {name: os.environ.get(name) for name in ("BROWSER", fake.NOTES_VAR)}
    os.environ["BROWSER"] = fake.command(place / "sentinel_browser.py")
    os.environ[fake.NOTES_VAR] = str(notes)
    TRACKER.install()
    mark = TRACKER.count()
    yield
    TRACKER.uninstall()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    left = TRACKER.reap(TRACKER.since(mark))
    problems = browser_problems(notes)
    fake.clear_notes(notes)
    if left:
        pytest.fail(
            "このモジュールの試験が起動したプロセスが、モジュールの終わりにも動いていた（止めた）:\n"
            + "".join(f"  {line}\n" for line in left),
            pytrace=False,
        )
    if problems:
        pytest.fail("このモジュールのフィクスチャで:\n" + "".join(f"  {line}\n" for line in problems),
                    pytrace=False)


@pytest.fixture(autouse=True)
def no_process_left_behind(request, acceptance_module_watch):
    """その試験が起動したプロセスが、試験の終わり（ほかの後始末の後）に動いていたら、止めて失敗にする。

    本物のブラウザを開く経路が呼ばれた試験、a0_fake_browser を通さずにブラウザを開く起動をした試験も、
    失敗にする。"""
    SESSION["count"] += 1
    notes: Path = SESSION["place"] / f"test-{SESSION['count']}.notes"
    module_notes = os.environ.get(fake.NOTES_VAR)
    os.environ[fake.NOTES_VAR] = str(notes)
    TRACKER.where = request.node.nodeid
    mark = TRACKER.count()
    yield
    left = TRACKER.reap(TRACKER.since(mark, owner="test"))
    TRACKER.where = ""
    if module_notes is None:
        os.environ.pop(fake.NOTES_VAR, None)
    else:
        os.environ[fake.NOTES_VAR] = module_notes
    problems = browser_problems(notes)
    fake.clear_notes(notes)
    if left:
        pytest.fail(
            "この試験が起動したプロセスが、試験の終わりにも動いていた（止めた）。試験の中で必ず止める"
            "（try/finally か、フィクスチャの後始末）:\n" + "".join(f"  {line}\n" for line in left),
            pytrace=False,
        )
    if problems:
        pytest.fail("この試験で:\n" + "".join(f"  {line}\n" for line in problems), pytrace=False)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """モジュールの間だけ動かすフィクスチャ（function より広いもの）の準備で起動したプロセスは、その
    フィクスチャの持ち物（試験の終わりには止めない）。準備が失敗したら、起動したものを、その場で止める。"""
    wide = fixturedef.scope != "function" and TRACKER.installed > 0
    mark = TRACKER.count()
    where = TRACKER.where
    if wide:
        TRACKER.scoped += 1
        TRACKER.where = f"フィクスチャ {fixturedef.argname}（{request.node.nodeid}）"
    try:
        outcome = yield
    finally:
        if wide:
            TRACKER.scoped -= 1
            TRACKER.where = where
    if wide and outcome.excinfo is not None:
        TRACKER.reap(TRACKER.since(mark), grace=0.0)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not SESSION:
        return
    state = dict(SESSION)
    SESSION.clear()
    try:
        # 控えたもので、まだ動いているもの（モジュールの終わりで止めているので、ふつうは無い）
        tracked = TRACKER.reap(list(TRACKER.started))
        left = watch.leftover_apps(state["token"], set(TRACKER.pids), state["since"])
    finally:
        if state["previous"] is None:
            os.environ.pop(watch.MARK_VAR, None)
        else:
            os.environ[watch.MARK_VAR] = state["previous"]
        shutil.rmtree(state["place"], ignore_errors=True)
    if not left.checked:
        say("\n受け入れ試験の見張り: 実行の終わりに、残ったプロセスを調べられなかった（プロセスの一覧を"
            "作れない）。アプリ本体が残っていないかは、手で確かめる。")
    if not (tracked or left.any()):
        if left.checked:
            say(f"\n受け入れ試験の見張り: 受け入れ試験が起動したプロセスは {len(TRACKER.pids)} 個。"
                "実行の終わりに、この実行から生まれたアプリ本体は残っていない。")
        return
    session.exitstatus = watch.LEFTOVER_EXIT_CODE
    text = "\n失敗: 試験が起動したプロセスが、実行の終わりにも動いていた。残した試験の後始末を直す。\n"
    text += "".join(f"  止めた: {line}\n" for line in tracked)
    if left.killed:
        text += "  止めた（この実行から生まれたアプリ本体）:\n" + process_lines(left.killed)
    if left.reported:
        stop = "taskkill /PID <番号> /F" if watch.WINDOWS else "kill <番号>"
        text += ("  止めていない（たぶんこの実行のアプリ本体。親がもう無く、確かめきれない。"
                 f"この実行のものなら {stop} で止める）:\n" + process_lines(left.reported))
    say(text)
