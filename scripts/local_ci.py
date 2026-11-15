#!/usr/bin/env python3
"""手元の確認。GitHub上の自動テストに頼らず、自分のPCで確認（scripts/check.py）を走らせて、結果を記録する。

  python scripts/local_ci.py                 リモート（origin）の最新を取得し、origin/main を、まっさらな
                                             場所に取り出して確認する（作業中のフォルダの内容に左右されない）
  python scripts/local_ci.py --ref <名前>    確かめるコミットを指定する（ブランチ、タグ、コミットID）
  python scripts/local_ci.py --no-fetch      リモートから取得せずに、手元にあるコミットを確かめる
  python scripts/local_ci.py --current       取り出さずに、いまの作業フォルダの内容（未コミットの変更を
                                             含む）で確認する
  python scripts/local_ci.py --force         同じ内容の結果が既にあっても、やり直す

結果は .cycle/ci/ に、1回の実行につき1つのファイルで書く: どのコミットとツリーを、どのOSと Python で確かめ、
通過・失敗・スキップが何件で、合否がどうだったか。失敗した試験の名前。
認証情報、環境変数の中身、試験の出力の本文は、結果のファイルに書かない（出力は、画面に流すだけ）。
同じ内容を、同じ環境で確かめた結果が既にあれば、実行せずにそれを示す。

合格にするのは、確認が終了コード 0 で終わり、確かめた内容が実行の前後で対象のツリーと同じで、
試験ごとの結果（1件以上の通過、失敗とエラーが 0）を読めたときだけ。

終了コード: 0 = 合格、1 = 不合格（確認が失敗した、結果を得られなかった）、2 = 確認を始められなかった。
標準ライブラリだけで動く。OSで違う箇所は、scripts/cycle.py の「OSの違い」の節のものを使う。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cycle  # noqa: E402  ツリーIDの求め方、git の呼び方、OSの違いは、ハーネスと同じものを使う
import doctor  # noqa: E402  OS、Python、文字コードの調べ方は、環境の確認と同じものを使う

CI_DIR = ".cycle/ci"
REMOTE = "origin"
DEFAULT_REF = "origin/main"
FETCH_TIMEOUT = 300

# 取り出す場所は、リポジトリの外（OSの一時フォルダ）に作る。理由は docs/design/harness.md の8章。
WORK_PREFIX = "circuit-sim-ci-"

VERDICT_TEXT = {"pass": "合格", "fail": "不合格", "error": "結果を得られなかった（不合格として扱う）"}
REASON_TEXT = {
    "ok": "確認が通った",
    "tests_failed": "失敗した試験がある",
    "check_failed": "確認が失敗した。試験の失敗は報告されていない（ruff の指摘か、試験を始められなかった）",
    "content_mismatch": "確認を始める時点の内容が、対象のツリーと一致しなかった",
    "content_changed": "確認の実行中に、内容が変わった",
    "no_test_results": "確認は終了コード 0 で終わったが、試験ごとの結果を読めなかった",
    "test_results_disagree": "確認は終了コード 0 で終わったが、試験ごとの結果が合格と合わない",
    "timeout": "時間内に終わらなかった",
}


# ------------------------------------------------------------------ 対象

def environment() -> dict:
    return {"os": doctor.os_info(), "python": doctor.python_info(), "encoding": doctor.encoding_info()}


def fetch(root: Path) -> None:
    """リモートの最新を取得する。取得できなければ、古い内容を「最新」として確かめないよう、そこで止める。

    人が見ていない実行で、認証の入力を待って止まり続けないようにする（端末での入力を求めさせず、時間で打ち切る）。
    git の出力は表示しない（リモートのURLに、認証情報が入っていることがある）。
    """
    cmd = ["git", "-C", str(root), "fetch", "--quiet", REMOTE]
    env = {**cycle.git_env(), "GIT_TERMINAL_PROMPT": "0"}
    try:
        code = cycle.run_captured(cmd, root, FETCH_TIMEOUT, env).returncode
    except subprocess.TimeoutExpired:
        code = None
    if code != 0:
        why = "時間内に終わらなかった" if code is None else f"終了コード {code}"
        raise cycle.CycleError(
            f"リモート（{REMOTE}）の最新を取得できなかった（{why}）。"
            f"原因は git fetch {REMOTE} を実行して確かめる。"
            "手元にあるコミットを確かめるなら、--no-fetch を付ける"
        )


def commit_target(root: Path, ref: str) -> dict:
    """取り出して確かめる対象（ref の指すコミット）。"""
    commit = cycle.run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()
    if not cycle.is_tree_id(commit):        # 読めなければ、何も出力されない
        raise cycle.CycleError(
            f"{ref} を、このリポジトリのコミットとして読めない"
            "（--ref には、ブランチ、タグ、コミットIDを渡す）"
        )
    tree = cycle.git(root, "rev-parse", f"{commit}^{{tree}}")
    full = cycle.run_git(root, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", ref).stdout.strip()
    branch = next((full[len(top):] for top in ("refs/heads/", "refs/remotes/") if full.startswith(top)), None)
    return {"mode": "checkout", "ref": ref, "commit": commit, "tree": tree, "branch": branch, "dirty": False}


def current_target(root: Path) -> dict:
    """いまの作業フォルダの内容（未コミットの変更を含む）を、対象にする。"""
    content = cycle.content_info(root)
    if content["commit"] is None:
        raise cycle.CycleError(f"{root} は、コミットのある gitの作業ツリー（の最上位）ではない")
    return {"mode": "current", "ref": None, **content}


# ------------------------------------------------------------------ 既にある結果

def ci_dir(root: Path) -> Path:
    return root / CI_DIR


def identity(result: dict) -> tuple:
    """同じ確認かどうかを決めるもの: 取り出したか、どのコミットとツリーか、どのOSと Python か。"""
    return (
        result.get("mode"), result["commit"], result["tree"],
        result["os"]["system"], result["python"]["version"],
    )


def earlier_result(root: Path, wanted: dict) -> tuple[Path, dict] | None:
    """同じ内容を、同じ環境で確かめた結果が既にあれば、そのうち最も新しいもの。

    読めないファイル、形の違うファイル、結果を得られなかった実行（error）の記録は、結果として扱わない。
    """
    # ファイルの名前の先頭が時刻なので、名前の逆順が、新しい順になる
    for path in sorted(ci_dir(root).glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if cycle.ci_result_problem(data) or data["verdict"] == "error":
            continue
        if identity(data) == identity(wanted):
            return path, data
    return None


# ------------------------------------------------------------------ 確認の実行

def junit_options(junit: Path) -> str:
    """pytest に、試験ごとの結果をファイル（JUnit の形）へ書かせるオプション。環境変数 PYTEST_ADDOPTS で渡す。

    scripts/check.py を変えずに、どのコミットの確認からでも、件数と失敗した試験の名前を受け取れる。
    pytest は、この環境変数を、どのOSでもシェルと同じ決まりで分ける。Windows のパスの区切り（\\）は
    そこで消えるので、/ 区切りにして引用する（空白や日本語を含む場所でも、そのまま届く）。
    xunit1 の形にするのは、試験のファイル名（file）が入るため。
    """
    return f"--junitxml={shlex.quote(junit.as_posix())} -o junit_family=xunit1"


def node_id(case: ElementTree.Element) -> str:
    """試験の名前（pytest のノードID）。JUnit の項目（file、classname、name）から組み立てる。"""
    name, classname = case.get("name", ""), case.get("classname", "")
    file = (case.get("file") or "").replace("\\", "/")
    if not file:                            # 試験を集める段階のエラーなど、ファイル名の無い項目
        return "::".join(part for part in (classname, name) if part)
    module = file.removesuffix(".py").replace("/", ".")
    classes = classname[len(module) + 1:].split(".") if classname.startswith(module + ".") else []
    return "::".join([file, *classes, name])


def read_junit(junit: Path) -> tuple[dict | None, list[str]]:
    """試験ごとの結果のファイルから、件数と、失敗した試験の名前を読む。読めなければ（None、空の一覧）。

    読み出すのは、件数と名前だけ。失敗の文面や、試験の出力は読み出さない。
    """
    try:
        root = ElementTree.parse(junit).getroot()
    except (OSError, ElementTree.ParseError):
        return None, []
    counts = dict.fromkeys(cycle.CI_COUNTS, 0)
    failed = []
    for case in root.iter("testcase"):
        kinds = {child.tag for child in case}
        if "failure" in kinds or "error" in kinds:
            counts["failed" if "failure" in kinds else "errors"] += 1
            failed.append(node_id(case))
        else:
            counts["skipped" if "skipped" in kinds else "passed"] += 1
        counts["total"] += 1
    return counts, failed


def judge(code: int, tests: dict | None, failed: list[str], unchanged: bool) -> tuple[str, str]:
    """確認の結果から（合否、理由）を決める。合格にするのは、どこにも欠けや食い違いが無いときだけ。"""
    if code != 0:
        return "fail", "tests_failed" if failed else "check_failed"
    if not unchanged:
        return "error", "content_changed"
    if tests is None:
        return "error", "no_test_results"
    if tests["failed"] or tests["errors"] or tests["passed"] < 1:
        return "error", "test_results_disagree"
    return "pass", "ok"


def run_streaming(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> int:
    """コマンドを実行して、終了コードを返す。出力は受け取らず、そのまま画面に流す（どこにも記録しない）。

    呼び出し元の標準入力は渡さない（人が見ていない実行で、入力を待って止まり続けないように）。
    時間切れと中断では、起動したプロセス（Windows では、その先のプロセスも）を止めてから、例外を送出する。
    """
    with subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL) as proc:
        try:
            return proc.wait(timeout=timeout)
        except BaseException:               # 時間切れ、中断（Ctrl-C、終了の合図）
            cycle.kill_tree(proc)
            raise


def changed_paths(place: Path, before: str, after: str) -> list[str]:
    """2つのツリーの間で違うファイルの名前（結果に残すのは、先頭の20個まで）。"""
    return cycle.git_paths(place, "diff", "--no-renames", "--name-only", "-z", before, after)[:20]


def run_check(place: Path, tree: str, junit: Path, timeout: float) -> dict:
    """place で scripts/check.py を実行して、結果（終了コード、試験の件数、失敗した試験、合否と理由）を返す。

    tree は、確かめる対象のツリーID。place の内容がそれと違えば、確認を実行しない。
    """
    check = {"script": cycle.CHECK_SCRIPT, "returncode": None, "seconds": 0.0, "timed_out": False}
    outcome = {"check": check, "tests": None, "failed": [], "changed": []}
    before = cycle.tree_id(place)
    if before != tree:
        changed = changed_paths(place, tree, before)
        return {**outcome, "changed": changed, "verdict": "error", "reason": "content_mismatch"}
    addopts = " ".join(filter(None, [os.environ.get("PYTEST_ADDOPTS"), junit_options(junit)]))
    print(f"{cycle.CHECK_SCRIPT} を実行します（ruff と pytest。数分かかることがあります）", flush=True)
    started = time.monotonic()
    try:
        code = run_streaming(
            [sys.executable, cycle.CHECK_SCRIPT], place, {**os.environ, "PYTEST_ADDOPTS": addopts}, timeout,
        )
    except subprocess.TimeoutExpired:
        code = None
    check = {**check, "returncode": code, "seconds": round(time.monotonic() - started, 1)}
    if code is None:
        check["timed_out"] = True
        return {**outcome, "check": check, "verdict": "error", "reason": "timeout"}
    tests, failed = read_junit(junit)
    after = cycle.tree_id(place)
    verdict, reason = judge(code, tests, failed, after == before)
    return {
        "check": check, "tests": tests, "failed": failed, "changed": changed_paths(place, before, after),
        "verdict": verdict, "reason": reason,
    }


def clean_up(root: Path, base: Path, checkout: Path | None) -> bool:
    """作業用の場所を片付ける。片付いたら True を返す（片付けられなくても、例外にはしない）。"""
    remove = ("worktree", "remove", "--force", str(checkout))
    removed = checkout is None or cycle.run_git(root, *remove).returncode == 0
    try:
        cycle.retry_while_busy(lambda: shutil.rmtree(base))
    except OSError:
        pass
    if not removed:
        # git が作業ツリーを消せなかった（Windows で、中のファイルが使われていた、など）。
        # フォルダを消した後なら、登録だけを外せる
        cycle.run_git(root, *remove)
    return not base.exists()


def check_target(root: Path, target: dict, timeout: float) -> dict:
    """対象の内容で確認を実行する。取り出す場合は、まっさらな場所に取り出し、終わったら（失敗しても）片付ける。

    取り出すのは git の作業ツリー（コミットにある内容だけ）。作業中のフォルダの未コミットの変更、
    無視されているファイル（.venv など）は、取り出した場所には無い。
    """
    base = cycle.real_path(tempfile.mkdtemp(prefix=WORK_PREFIX))
    checkout = base / "checkout" if target["mode"] == "checkout" else None
    try:
        if checkout is not None:
            cycle.git(root, "worktree", "add", "--detach", str(checkout), target["commit"])
        return run_check(checkout or root, target["tree"], base / "junit.xml", timeout)
    finally:
        if not clean_up(root, base, checkout):
            print(f"注意: 作業用の場所を片付けられなかった。消してよい: {base}", file=sys.stderr)


# ------------------------------------------------------------------ 結果

def write_result(root: Path, result: dict) -> Path:
    """結果を、1回の実行につき1つのファイルに書く。途中まで書いたファイルを読ませないよう、別名で書いてから置く。"""
    name = f"{cycle.stamp()}-{result['os']['system'].lower()}-{result['commit'][:10]}.json"
    path = ci_dir(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    partial.write_text(text, encoding="utf-8", newline="\n")
    try:
        cycle.retry_while_busy(lambda: os.replace(partial, path))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise cycle.CycleError(f"結果を {path} に書けない: {exc}") from exc
    return path


def report(root: Path, path: Path, result: dict) -> list[str]:
    asked = f"{result['ref']} の" if result["ref"] else "いまの作業フォルダの内容。"
    dirty = "、未コミットの変更あり" if result["dirty"] else ""
    system, python, check = result["os"], result["python"], result["check"]
    lines = [
        f"結果: {VERDICT_TEXT[result['verdict']]}（{REASON_TEXT[result['reason']]}）",
        f"  対象  : {asked}コミット {result['commit'][:10]}、ツリー {result['tree'][:10]}{dirty}",
        f"  環境  : {system['system']} {system['release']}、Python {python['version']}、"
        f"既定の文字コード {result['encoding']['locale']}",
    ]
    tests = result["tests"]
    if tests is not None:
        lines.append(
            f"  試験  : 通過 {tests['passed']}、失敗 {tests['failed']}、エラー {tests['errors']}、"
            f"スキップ {tests['skipped']}（{check['seconds']} 秒）"
        )
    lines += [f"  失敗  : {name}" for name in result["failed"][:20]]
    lines += [f"  違い  : {name}" for name in result["changed"]]
    lines.append(f"  記録  : {path.relative_to(root).as_posix()}")
    return lines


def run(args: argparse.Namespace) -> int:
    root = cycle.repo_root()
    if args.current:
        target = current_target(root)
    else:
        if not args.no_fetch:
            fetch(root)
        target = commit_target(root, args.ref or DEFAULT_REF)
    wanted = {**target, **environment()}
    earlier = None if args.force else earlier_result(root, wanted)
    if earlier is not None:
        path, result = earlier
        print("同じ内容を、同じ環境で確かめた結果が既にあるので、実行しなかった（やり直すなら --force）")
    else:
        with cycle.termination_as_exception():
            outcome = check_target(root, target, args.timeout)
        result = {"schema": cycle.CI_SCHEMA, "kind": cycle.CI_KIND, "at": cycle.now(), **wanted, **outcome}
        path = write_result(root, result)
    print("\n".join(report(root, path, result)))
    return 0 if result["verdict"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="local_ci.py", description="手元の確認（確認を走らせて、結果を記録する）",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--ref", help=f"確かめるコミット（ブランチ、タグ、コミットID）。既定は {DEFAULT_REF}")
    which.add_argument(
        "--current", action="store_true",
        help="取り出さずに、いまの作業フォルダの内容（未コミットの変更を含む）で確認する",
    )
    parser.add_argument("--no-fetch", action="store_true", help="リモートから取得しない")
    parser.add_argument("--force", action="store_true", help="同じ内容の結果が既にあっても、やり直す")
    parser.add_argument("--timeout", type=int, default=3600, help="確認を打ち切るまでの秒数")
    cycle.make_output_safe()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (cycle.CycleError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
