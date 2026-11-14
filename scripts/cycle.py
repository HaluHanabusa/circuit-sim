#!/usr/bin/env python3
"""開発サイクルのゲートを、スクリプトで確実に運ぶ。

判断はエージェント（AI）が行い、手順と合否の扱いはこのスクリプト（決定的なコード）が行う。
ゲートの結果は、そのとき見ていた内容（gitのツリー）と一緒に記録する。
検証の後で中身が変わっていたら、統合を拒む。

  scripts/cycle.py start <ID>                    作業ツリーとブランチを用意する
  scripts/cycle.py run <ID> <gate> [--dry-run]   担当エージェントをヘッドレスで起動し、結果を記録する
  scripts/cycle.py snapshot <ID>                 いまの内容のツリーIDを表示する
  scripts/cycle.py record <ID> <gate> --verdict pass|fail --by <名前> [--tree <ID>] [--report <ファイル>]
                                                 セッション内で動かしたエージェントの結果を記録する
  scripts/cycle.py check <ID>                    作業ツリーで scripts/check.sh を実行して記録する
  scripts/cycle.py status [<ID>]                 ゲートの状況を表示する
  scripts/cycle.py merge <ID>                    必要なゲートがすべて合格のときだけ main に取り込む

ゲート: implement（実装）、check（自動確認）、review（レビュー）、verify（独立検証）、ux（UX確認）
標準ライブラリだけで動く。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

AGENT_GATES = ("implement", "review", "verify", "ux")
ALL_GATES = ("implement", "check", "review", "verify", "ux")
DEFAULT_GATES = ["implement", "check", "review", "verify"]
GATE_AGENT = {"review": "code-reviewer", "verify": "verifier", "ux": "ux-designer"}

# レビューや検証の後で足してよい場所（検証担当の受け入れ試験と記録）。
# ここ以外が変わっていたら、そのゲートは古いものとして扱う。
AFTER_GATE_PATHS = ("tests/acceptance/", "docs/verification/")

GATE_TOOLS = {
    "implement": "Read,Edit,Write,Bash,Grep,Glob",
    "review": "Read,Bash,Grep,Glob",
    "verify": "Read,Edit,Write,Bash,Grep,Glob",
    "ux": "Read,Edit,Write,Bash,Grep,Glob",
}

GATE_PROMPT = {
    "implement": (
        "仕様 {spec} を、この作業ツリーの中で実装してください。"
        "受け入れ基準をすべて自分で実測し、scripts/check.sh を通してください。"
        "コミットはしないでください。"
    ),
    "review": (
        "仕様 {spec} に対するこの作業ツリーの変更（git diff main...HEAD と"
        "未コミットの変更）をレビューしてください。"
        "コードは変更しないでください。重大な指摘が1つでもあれば verdict は fail にしてください。"
    ),
    "verify": (
        "仕様 {spec} の受け入れ基準を、実装者とは別の立場で1つずつ実測してください。"
        "製品のコードは変更しないでください。"
        "受け入れ試験は tests/acceptance/ に、記録は docs/verification/ に置いてください。"
        "測れなかった基準が1つでもあれば verdict は fail にしてください。"
    ),
    "ux": (
        "仕様 {spec} で実装された画面を実際に動かし、"
        "画面仕様と docs/design/app-design.md の原則に照らして判定してください。"
        "コードは変更しないでください。記録は docs/verification/ に置いてください。"
    ),
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "summary": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "measured": {"type": "string"},
                    "pass": {"type": "boolean"},
                },
                "required": ["name", "measured", "pass"],
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "description": {"type": "string"},
                },
                "required": ["severity", "description"],
            },
        },
    },
    "required": ["verdict", "summary", "criteria", "issues"],
}


class CycleError(Exception):
    pass


# ------------------------------------------------------------------ 共通

def repo_root() -> Path:
    env = os.environ.get("CYCLE_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[1]


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise CycleError(f"git {' '.join(args)} が失敗: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_spec(root: Path, uid: str) -> Path:
    plan = root / "docs" / "plan"
    found = sorted(plan.glob(f"{uid}-*.md")) + sorted(plan.glob(f"{uid}.md"))
    if len(found) != 1:
        raise CycleError(f"docs/plan/ に {uid} の仕様が1つだけ必要（見つかった数: {len(found)}）")
    return found[0]


def read_header(spec: Path) -> dict[str, str]:
    """仕様ファイル先頭の `---` で囲まれた部分を読む（key: value の行だけ）。"""
    lines = spec.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise CycleError(f"{spec.name}: 先頭に --- で囲んだ見出し（id、owner）が必要")
    header: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()
    else:
        raise CycleError(f"{spec.name}: 見出しが --- で閉じられていない")
    for key in ("id", "owner"):
        if not header.get(key):
            raise CycleError(f"{spec.name}: 見出しに {key} が無い")
    return header


def state_file(root: Path, uid: str) -> Path:
    return root / ".cycle" / "state" / f"{uid}.json"


def load_state(root: Path, uid: str) -> dict:
    path = state_file(root, uid)
    if not path.exists():
        raise CycleError(f"{uid} はまだ始まっていない。先に start を実行する")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(root: Path, state: dict) -> None:
    path = state_file(root, state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_id(worktree: Path) -> str:
    """作業ツリーのいまの内容（未コミットと新規ファイルを含む）を表すID。"""
    git(worktree, "add", "-A")
    return git(worktree, "write-tree")


def required_gates(state: dict) -> list[str]:
    if state.get("gates_required"):
        return list(state["gates_required"])
    gates = list(DEFAULT_GATES)
    if state.get("needs_ux"):
        gates.append("ux")
    return gates


def record(state: dict, gate: str, verdict: str, tree: str, by: str, source: str, report: str = "") -> None:
    entry = {"verdict": verdict, "tree": tree, "by": by, "source": source, "report": report, "at": now()}
    state.setdefault("gates", {})[gate] = entry
    state.setdefault("history", []).append({"gate": gate, **entry})


def runs_dir(root: Path) -> Path:
    path = root / ".cycle" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- コマンド

def cmd_start(args: argparse.Namespace) -> int:
    root = repo_root()
    spec = find_spec(root, args.id)
    header = read_header(spec)
    if header["id"] != args.id:
        raise CycleError(f"{spec.name}: 見出しの id（{header['id']}）がファイル名と合わない")
    worktree = Path(args.worktree).resolve() if args.worktree else root.parent / "wt" / args.id
    branch = args.branch or args.id
    if worktree.exists():
        if git(worktree, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise CycleError(f"{worktree} は既にあるが、gitの作業ツリーではない")
        branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    else:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(root, "worktree", "add", str(worktree), "-b", branch)
    gates = [g.strip() for g in header.get("gates", "").split(",") if g.strip()]
    unknown = [g for g in gates if g not in ALL_GATES]
    if unknown:
        raise CycleError(f"{spec.name}: 知らないゲート {unknown}")
    state = {
        "id": args.id,
        "spec": str(spec.relative_to(root)),
        "owner": header["owner"],
        "needs_ux": header.get("needs_ux", "false").lower() == "true",
        "gates_required": gates,
        "worktree": str(worktree),
        "branch": branch,
        "started_at": now(),
        "gates": {},
        "history": [],
    }
    path = state_file(root, args.id)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        state["gates"], state["history"] = old.get("gates", {}), old.get("history", [])
    save_state(root, state)
    print(f"{args.id}: 作業ツリー {worktree}（ブランチ {branch}）、必要なゲート {required_gates(state)}")
    return 0


def agent_command(state: dict, gate: str, budget: float) -> list[str]:
    role = state["owner"] if gate == "implement" else GATE_AGENT[gate]
    prompt = GATE_PROMPT[gate].format(spec=state["spec"])
    return [
        "claude", "-p", prompt,
        "--agent", role,
        "--output-format", "json",
        "--json-schema", json.dumps(VERDICT_SCHEMA, ensure_ascii=False),
        "--permission-mode", "acceptEdits",
        "--max-budget-usd", str(budget),
        "--allowedTools", GATE_TOOLS[gate],
    ]


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    cmd = agent_command(state, args.gate, args.budget)
    if args.dry_run:
        print(f"cd {shlex.quote(str(worktree))} && {shlex.join(cmd)}")
        return 0

    before = tree_id(worktree)
    log = runs_dir(root) / f"{args.id}-{args.gate}-{now().replace(':', '')}.json"
    try:
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, timeout=args.timeout)
        log.write_text(proc.stdout or proc.stderr, encoding="utf-8")
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except subprocess.TimeoutExpired:
        log.write_text("timeout", encoding="utf-8")
        data = {}
    except json.JSONDecodeError:
        data = {}

    # 構造化された結果が無い実行は、合格として扱わない
    result = data.get("structured_output") if isinstance(data, dict) else None
    ok = isinstance(result, dict) and result.get("verdict") in ("pass", "fail")
    verdict = result["verdict"] if ok else "error"
    # 実装は「作った結果」を、確認系は「見た対象」を記録する
    tree = tree_id(worktree) if args.gate == "implement" else before
    by = "headless-unknown"
    if isinstance(data, dict) and data.get("session_id"):
        by = str(data["session_id"])
    record(state, args.gate, verdict, tree, by, "headless", str(log.relative_to(root)))
    save_state(root, state)
    summary = result.get("summary", "") if isinstance(result, dict) else "構造化された結果が得られなかった"
    cost = data.get("total_cost_usd") if isinstance(data, dict) else None
    note = f" 費用の概算 ${cost}" if cost else ""
    print(f"{args.id} {args.gate}: {verdict}（{summary}）記録 {log.name}{note}")
    return 0 if verdict == "pass" else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    state = load_state(repo_root(), args.id)
    print(tree_id(Path(state["worktree"])))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    tree = args.tree or tree_id(Path(state["worktree"]))
    record(state, args.gate, args.verdict, tree, args.by, "session", args.report or "")
    save_state(root, state)
    print(f"{args.id} {args.gate}: {args.verdict} を記録（対象 {tree[:10]}、担当 {args.by}）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    proc = subprocess.run(["scripts/check.sh"], cwd=worktree, capture_output=True, text=True)
    log = runs_dir(root) / f"{args.id}-check-{now().replace(':', '')}.log"
    log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    verdict = "pass" if proc.returncode == 0 else "fail"
    report = str(log.relative_to(root))
    record(state, "check", verdict, tree_id(worktree), "scripts/check.sh", "script", report)
    save_state(root, state)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    print(f"{args.id} check: {verdict}（{tail[0]}）")
    return 0 if verdict == "pass" else 1


def gate_problems(state: dict, worktree: Path) -> list[str]:
    """統合を妨げる理由の一覧。空なら統合してよい。"""
    current = tree_id(worktree)
    gates = state.get("gates", {})
    problems = []
    for gate in required_gates(state):
        entry = gates.get(gate)
        if entry is None:
            problems.append(f"{gate}: まだ記録が無い")
            continue
        if entry["verdict"] != "pass":
            problems.append(f"{gate}: {entry['verdict']}")
            continue
        if gate == "implement":
            continue
        if gate == "check":
            if entry["tree"] != current:
                problems.append("check: 確認した後で内容が変わっている。check をやり直す")
            continue
        changed = git(worktree, "diff", "--name-only", entry["tree"], current).splitlines()
        stale = [p for p in changed if not p.startswith(AFTER_GATE_PATHS)]
        if stale:
            problems.append(f"{gate}: 確認の後で変わったファイルがある（{', '.join(stale[:5])}）。やり直す")
    implement, verify = gates.get("implement"), gates.get("verify")
    if implement and verify and implement["by"] == verify["by"]:
        problems.append("verify: 実装した本人が検証している。別の担当でやり直す")
    return problems


def cmd_status(args: argparse.Namespace) -> int:
    root = repo_root()
    ids = [args.id] if args.id else sorted(p.stem for p in (root / ".cycle" / "state").glob("*.json"))
    if not ids:
        print("進行中の作業単位は無い")
    for uid in ids:
        state = load_state(root, uid)
        done = "  統合済み" if state.get("merged_at") else ""
        print(f"{uid}（{state['spec']}、担当 {state['owner']}）{done}")
        for gate in required_gates(state):
            entry = state.get("gates", {}).get(gate)
            text = f"{entry['verdict']}  {entry['by']}  {entry['at']}" if entry else "未実施"
            print(f"  {gate:<10} {text}")
        if not state.get("merged_at"):
            problems = gate_problems(state, Path(state["worktree"]))
            print("  統合: " + ("できる" if not problems else "まだできない"))
            for p in problems:
                print(f"    - {p}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    root = repo_root()
    state = load_state(root, args.id)
    worktree = Path(state["worktree"])
    problems = gate_problems(state, worktree)
    if git(worktree, "status", "--porcelain"):
        problems.append("作業ツリーに未コミットの変更がある。コミットしてから統合する")
    if git(root, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        problems.append("本体のチェックアウトが main ではない")
    elif git(root, "status", "--porcelain"):
        problems.append("本体（main）に未コミットの変更がある")
    if problems:
        print(f"{args.id}: 統合できない")
        for p in problems:
            print(f"  - {p}")
        return 1

    gates = state["gates"]
    lines = [f"{g}: {gates[g]['verdict']}（{gates[g]['by']}）" for g in required_gates(state)]
    message = f"{args.id} を統合\n\n仕様: {state['spec']}\n" + "\n".join(lines)
    trailers = os.environ.get("CYCLE_COMMIT_TRAILERS", "").strip()
    if trailers:
        message += "\n\n" + trailers
    before = git(root, "rev-parse", "HEAD")
    git(root, "merge", "--no-ff", state["branch"], "-m", message)
    proc = subprocess.run(["scripts/check.sh"], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        git(root, "reset", "--hard", before)
        print(f"{args.id}: main に取り込んだ状態で check が失敗したので、取り込みを取り消した")
        print((proc.stdout + proc.stderr).strip()[-600:])
        return 1
    state["merged_at"] = now()
    state["merged_commit"] = git(root, "rev-parse", "HEAD")
    save_state(root, state)
    print(f"{args.id}: main に取り込んだ（{state['merged_commit'][:10]}）。push はまだ")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cycle.py", description="開発サイクルのゲートを運ぶ")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="作業ツリーとブランチを用意する")
    p.add_argument("id")
    p.add_argument("--worktree", help="既にある作業ツリーを使う")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("run", help="担当エージェントをヘッドレスで起動する")
    p.add_argument("id")
    p.add_argument("gate", choices=AGENT_GATES)
    p.add_argument("--dry-run", action="store_true", help="実行せず、コマンドだけ表示する")
    p.add_argument("--budget", type=float, default=5.0, help="1回の上限額（米ドル）")
    p.add_argument("--timeout", type=int, default=3600, help="打ち切りまでの秒数")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("snapshot", help="いまの内容のツリーIDを表示する")
    p.add_argument("id")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("record", help="セッション内で動かした結果を記録する")
    p.add_argument("id")
    p.add_argument("gate", choices=AGENT_GATES)
    p.add_argument("--verdict", required=True, choices=["pass", "fail"])
    p.add_argument("--by", required=True, help="担当の名前（実装と検証で別であること）")
    p.add_argument("--tree", help="確認の対象にしたツリーID（省略時はいまの内容）")
    p.add_argument("--report", help="報告を保存したファイル")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("check", help="scripts/check.sh を実行して記録する")
    p.add_argument("id")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="ゲートの状況")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("merge", help="必要なゲートがすべて合格のときだけ main に取り込む")
    p.add_argument("id")
    p.set_defaults(func=cmd_merge)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CycleError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
