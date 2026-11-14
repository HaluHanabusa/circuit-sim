#!/usr/bin/env python3
"""開発環境の確認。自分のPCで実行して、開発の道具が動く環境かどうかを確かめる。

  python scripts/doctor.py               OS、Python、git、claude の有無と版、scripts/check.py の結果
  python scripts/doctor.py --no-check    scripts/check.py（数分かかる）を省く
  python scripts/doctor.py --headless    加えて、本物の claude -p を最小の指示で1回だけ実行し、
                                         ハーネス（scripts/cycle.py run）が前提にしている出力の形かを確かめる

--headless はClaudeの利用枠を少し使うので、付けたときだけ実行する。実行の前に、何をするかを表示する。

結果は画面と .cycle/doctor-result.json に出す。書き出すのは、形（項目の有無と型）、真偽、版の番号だけ。
認証情報、環境変数の中身、claude の応答の本文は、画面にもファイルにも書き出さない。
終了コードは、必要なもの（Python の版、git、確認、頼まれたヘッドレスの確認）に問題が無ければ 0。
標準ライブラリだけで動く。
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cycle  # noqa: E402  ハーネスと同じ組み立て、同じ読み方を使う

RESULT_FILE = ".cycle/doctor-result.json"
MIN_PYTHON = (3, 11)
VERSION_TIMEOUT = 60

# ヘッドレスの確認で渡す、最小の指示
PROBE_PROMPT = (
    "これは開発環境の確認です。ファイルを読んだり書いたりせず、道具も使わないでください。"
    "verdict を pass、summary を ok、criteria と issues を空の一覧にした結果だけを返してください。"
)
PROBE_ROLE = "code-reviewer"
PROBE_TOOLS = "Read"

# 設定されていると、ヘッドレス実行が利用枠ではなくAPIの料金になる環境変数（中身は見ない。有無だけ）
BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# 値まで記録してよいもの。版の表示（数字、英字、空白、かっこ）と、種類を表す語（英小文字と _ だけ）
VERSION_TEXT = re.compile(r"[A-Za-z0-9_.+\-() ]{1,60}")
WORD = re.compile(r"[a-z][a-z_]{0,39}")
KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
MAX_KEYS = 40

# ハーネスが読む項目（cycle.headless_verdict と cmd_run）と、形を知っておきたい項目
FIELDS = (
    "type", "subtype", "is_error", "session_id", "result", "structured_output",
    "total_cost_usd", "num_turns", "duration_ms",
)
NUMBER_FIELDS = ("total_cost_usd", "num_turns", "duration_ms")
WORD_FIELDS = ("type", "subtype")


# ------------------------------------------------------------------ 環境

def os_info() -> dict:
    return {
        "system": platform.system(), "release": platform.release(),
        "version": platform.version(), "machine": platform.machine(),
    }


def python_info() -> dict:
    return {
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "implementation": platform.python_implementation(),
        "supported": sys.version_info[:2] >= MIN_PYTHON,
    }


def encoding_info() -> dict:
    """文字コード。画面（端末）が UTF-8 でないと、表せない文字は置き換えて表示される。"""
    return {
        "stdout": getattr(sys.stdout, "encoding", None),
        "locale": locale.getencoding(),
        "filesystem": sys.getfilesystemencoding(),
        "utf8_mode": bool(sys.flags.utf8_mode),
    }


def command_info(name: str) -> dict:
    """コマンドの有無と版。版は --version の1行目を、版の表示らしい文字のときだけ記録する。"""
    found = cycle.find_command(name)
    if found is None:
        return {"found": False}
    info = {"found": True, "file": Path(found).name, "runs": False, "version": None}
    try:
        proc = cycle.run_captured([found, "--version"], Path.cwd(), VERSION_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return info
    first = (proc.stdout.strip().splitlines() or [""])[0].strip()
    info["runs"] = proc.returncode == 0
    info["version"] = first if VERSION_TEXT.fullmatch(first) else None
    return info


def check_info(root: Path, skip: bool) -> dict:
    """scripts/check.py の結果。出力は記録せず、そのまま画面に流す。"""
    if skip:
        return {"ran": False, "reason": "skipped"}
    if not (root / cycle.CHECK_SCRIPT).is_file():
        return {"ran": False, "reason": "missing"}
    print(f"{cycle.CHECK_SCRIPT} を実行します（ruff と pytest。数分かかることがあります）", flush=True)
    started = time.monotonic()
    try:
        code = subprocess.run([sys.executable, cycle.CHECK_SCRIPT], cwd=root).returncode
    except OSError:
        return {"ran": False, "reason": "cannot_start"}
    seconds = round(time.monotonic() - started, 1)
    return {"ran": True, "returncode": code, "passed": code == 0, "seconds": seconds}


# ------------------------------------------------------------------ ヘッドレスの出力の形

def kind(value: object) -> str:
    """JSON の型の名前。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "other"


def key_names(data: object) -> list[str]:
    """項目の名前の一覧（名前らしい形のものだけ、数を限って）。値は含めない。"""
    if not isinstance(data, dict):
        return []
    return sorted(key for key in data if isinstance(key, str) and KEY_NAME.fullmatch(key))[:MAX_KEYS]


def last_result(items: list) -> dict | None:
    """一覧の中の、結果のオブジェクト（type が result の最後のもの。無ければ最後のオブジェクト）。"""
    objects = [item for item in items if isinstance(item, dict)]
    results = [item for item in objects if item.get("type") == "result"]
    return (results or objects or [None])[-1]


def result_object(stdout: str) -> tuple[str, dict | None]:
    """出力の形と、結果の入ったオブジェクト（見つかれば）。

    ハーネスが前提にしているのは json_object（JSON のオブジェクトが1つ）。
    """
    text = stdout.strip()
    if not text:
        return "empty", None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return "not_json", None
        return "json_lines", last_result(items)
    if isinstance(data, dict):
        return "json_object", data
    if isinstance(data, list):
        return "json_array", last_result(data)
    return "json_other", None


def describe_fields(data: dict) -> dict:
    """ハーネスが読む項目の有無と型。値まで記録するのは、真偽、数、種類を表す語だけ。"""
    fields: dict[str, dict] = {}
    for name in FIELDS:
        if name not in data:
            fields[name] = {"present": False}
            continue
        value = data[name]
        entry: dict[str, object] = {"present": True, "kind": kind(value)}
        if name == "is_error" and isinstance(value, bool):
            entry["value"] = value
        elif name in NUMBER_FIELDS and kind(value) == "number":
            entry["value"] = value
        elif name in WORD_FIELDS and isinstance(value, str) and WORD.fullmatch(value):
            entry["value"] = value
        elif name == "session_id":
            entry["empty"] = not value
        elif name == "structured_output":
            entry["keys"] = key_names(value)
            entry["problem"] = cycle.schema_problem(value)
            if not entry["problem"]:
                entry["verdict"] = value["verdict"]
                entry["criteria"] = len(value["criteria"])
                entry["issues"] = len(value["issues"])
        fields[name] = entry
    return fields


def describe_messages(text: str, options: list[str]) -> dict:
    """エラーの文面から分かること。文面そのものは記録しない。

    rejected_options は、「知らない」と言われたオプション（こちらが渡した名前に限る）。
    mentions_login は、サインインや認証に触れているか。
    """
    lowered = text.lower()
    rejected = [
        option for option in options
        if re.search(
            rf"(unknown|invalid|unrecognized|unexpected)[^\n]*(?<![\w-]){re.escape(option.lower())}(?![\w-])",
            lowered,
        )
    ]
    login = re.search(r"log ?in|sign ?in|authenticat|api key|credential|unauthorized", lowered)
    return {
        "characters": len(text), "lines": len(text.splitlines()),
        "rejected_options": rejected, "mentions_login": login is not None,
    }


def describe_output(returncode: int, stdout: str, stderr: str, options: list[str]) -> dict:
    """ヘッドレス実行の結果の形。unexpected は、ハーネスの前提と違っていた点（空なら前提どおり）。"""
    form, data = result_object(stdout)
    fields = describe_fields(data) if data is not None else {}
    verdict = cycle.headless_verdict(returncode, stdout)[0]
    structured = fields.get("structured_output", {})
    expected = {
        "returncode": returncode == 0,
        "stdout_form": form == "json_object",
        "is_error": fields.get("is_error", {}).get("value") is False,
        "subtype": fields.get("subtype", {}).get("value") == "success",
        "structured_output": structured.get("problem") == "",
        "session_id": fields.get("session_id", {}) == {"present": True, "kind": "string", "empty": False},
        "harness_verdict": verdict == "pass",
    }
    # 出力が JSON でないときは、そこにエラーの文面が出ていることがある
    messages = stderr if data is not None else "\n".join(part for part in (stdout, stderr) if part)
    return {
        "returncode": returncode,
        "stdout": {"form": form, "characters": len(stdout)},
        "messages": describe_messages(messages, options),
        "keys": key_names(data),
        "fields": fields,
        "harness_verdict": verdict,
        "unexpected": [name for name, ok in expected.items() if not ok],
    }


def headless_info(root: Path, budget: float, timeout: int, allow_api_billing: bool) -> dict:
    """本物の claude -p を、ハーネスと同じ組み立てで1回だけ実行して、出力の形を調べる。"""
    cmd = cycle.claude_command(PROBE_PROMPT, PROBE_ROLE, budget, PROBE_TOOLS)
    options = cmd[1::2]
    billing = [name for name in BILLING_VARS if os.environ.get(name)]
    if billing and not allow_api_billing:
        print(
            f"ヘッドレスの確認は実行しませんでした。環境変数 {'、'.join(billing)} が設定されていて、"
            "このまま実行すると、Claudeの利用枠ではなくAPIの料金になります。"
            "変数を外すか、料金が掛かってよければ --allow-api-billing を付けてください"
        )
        return {"ran": False, "reason": "api_key_set"}
    if cycle.find_command(cmd[0]) is None:
        return {"ran": False, "reason": "claude_not_found"}
    try:
        launch = cycle.launchable(cmd)
    except cycle.CycleError:
        return {"ran": False, "reason": "cannot_pass_arguments"}

    print("ヘッドレスの確認（--headless）。これから次のことをします。")
    print(f"  - claude -p を1回だけ実行します（役割 {PROBE_ROLE}、使える道具は {PROBE_TOOLS} だけ、"
          f"上限 {budget} 米ドル、最長 {timeout} 秒）")
    print(f"  - 指示: {PROBE_PROMPT}")
    print("  - Claudeの利用枠を少し使います（プランでサインインしている場合。対話と同じ枠です）")
    print("  - 記録するのは、出力の形（項目の有無と型）と真偽だけです。応答の本文と認証情報は記録しません")
    print(f"  - 実行するコマンド: {cycle.shell_line(root, cmd)}", flush=True)

    info: dict[str, object] = {"ran": True, "launcher": Path(launch[0]).name, "options": options}
    started = time.monotonic()
    try:
        proc = cycle.run_captured(launch, root, timeout)
    except subprocess.TimeoutExpired:
        return {**info, "timed_out": True, "unexpected": ["timed_out"]}
    except OSError:
        return {"ran": False, "reason": "cannot_start", "launcher": info["launcher"]}
    info["timed_out"] = False
    info["seconds"] = round(time.monotonic() - started, 1)
    return {**info, **describe_output(proc.returncode, proc.stdout, proc.stderr, options)}


# ------------------------------------------------------------------ まとめ

def problems(result: dict, headless_requested: bool) -> list[str]:
    """必要なもののうち、問題があるもの。claude が無いこと自体は問題にしない（セッション内で動かせる）。"""
    found = []
    if not result["python"]["supported"]:
        found.append("python")
    if not result["git"].get("runs"):
        found.append("git")
    check = result["check"]
    if not (check.get("passed") or check.get("reason") == "skipped"):
        found.append("check")
    headless = result["headless"]
    if headless_requested and (not headless["ran"] or headless["unexpected"]):
        found.append("headless")
    return found


def command_line(info: dict, missing: str) -> str:
    if not info["found"]:
        return missing
    if not info["runs"]:
        return f"{info['file']} はあるが、--version を実行できない"
    return f"{info['version'] or '版の表示を読めない'}（{info['file']}）"


def check_line(check: dict) -> str:
    if check["ran"]:
        verdict = "合格" if check["passed"] else f"不合格（終了コード {check['returncode']}）"
        return f"{verdict}、{check['seconds']} 秒"
    reasons = {
        "skipped": "実行していない（--no-check）",
        "missing": f"{cycle.CHECK_SCRIPT} が無い",
        "cannot_start": f"{cycle.CHECK_SCRIPT} を起動できない",
    }
    return reasons[check["reason"]]


def headless_lines(headless: dict) -> list[str]:
    reasons = {
        "not_requested": "実行していない（--headless で、claude -p を1回だけ実行して出力の形を確かめる）",
        "api_key_set": "実行していない（APIキーの環境変数が設定されている）",
        "claude_not_found": "実行できない（claude コマンドが見つからない）",
        "cannot_pass_arguments": "実行できない（claude の起動用のファイルに、指示をそのまま渡せない）",
        "cannot_start": "実行できない（claude を起動できない）",
    }
    if not headless["ran"]:
        return [reasons[headless["reason"]]]
    if headless["timed_out"]:
        return ["時間内に終わらなかった（--timeout で延ばせる）"]
    if not headless["unexpected"]:
        return ["ハーネスの前提どおりの形で返ってきた"]
    fields = headless["fields"]
    present = [name for name in FIELDS if fields.get(name, {}).get("present")]
    lines = [
        f"ハーネスの前提と違う点がある: {'、'.join(headless['unexpected'])}",
        f"終了コード {headless['returncode']}、出力の形 {headless['stdout']['form']}、"
        f"ハーネスの判定 {headless['harness_verdict']}",
        f"あった項目: {'、'.join(present) or 'なし'}",
    ]
    messages = headless["messages"]
    if messages["rejected_options"]:
        lines.append(f"CLI が知らないと言ったオプション: {'、'.join(messages['rejected_options'])}")
    if messages["mentions_login"]:
        lines.append("エラーの文面が、サインインや認証に触れている（claude を起動して、状態を確かめる）")
    lines.append(
        "くわしい原因は、上の「実行するコマンド」を端末（Windows ではコマンド プロンプト）で実行して確かめる"
        "（その出力は記録されない）"
    )
    return lines


def report(result: dict) -> list[str]:
    system, python, encoding = result["os"], result["python"], result["encoding"]
    supported = "対応している版" if python["supported"] else f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要"
    claude = command_line(
        result["claude"], "見つからない（ヘッドレスの実行はできない。セッション内で動かして記録する）",
    )
    if result["claude"]["api_key_env_set"]:
        claude += "。APIキーの環境変数が設定されている（ヘッドレス実行はAPIの料金になる）"
    first, *rest = headless_lines(result["headless"])
    return [
        "環境の確認",
        f"  OS          : {system['system']} {system['release']}（{system['version']}、{system['machine']}）",
        f"  Python      : {python['version']}（{supported}）",
        f"  文字コード  : 画面 {encoding['stdout']}、既定 {encoding['locale']}、"
        f"ファイル名 {encoding['filesystem']}",
        f"  git         : {command_line(result['git'], '見つからない（git を入れて PATH を通す）')}",
        f"  claude      : {claude}",
        f"  確認        : {check_line(result['check'])}",
        f"  ヘッドレス  : {first}",
        *(f"                {line}" for line in rest),
        "結果: 問題なし" if result["ok"] else f"結果: 問題あり（{'、'.join(result['problems'])}）",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doctor.py", description="開発環境の確認")
    parser.add_argument("--no-check", action="store_true", help="scripts/check.py を実行しない")
    parser.add_argument(
        "--headless", action="store_true",
        help="本物の claude -p を1回だけ実行して、出力の形を確かめる（Claudeの利用枠を少し使う）",
    )
    parser.add_argument("--budget", type=float, default=1.0, help="ヘッドレスの確認の上限額（米ドル）")
    parser.add_argument("--timeout", type=int, default=300, help="ヘッドレスの確認を打ち切るまでの秒数")
    parser.add_argument(
        "--allow-api-billing", action="store_true",
        help="APIキーの環境変数が設定されていても、ヘッドレスの確認を実行する（APIの料金になる）",
    )
    cycle.make_output_safe()
    args = parser.parse_args(argv)
    root = cycle.repo_root()

    result: dict[str, object] = {
        "schema": 1,
        "at": cycle.now(),
        "os": os_info(),
        "python": python_info(),
        "encoding": encoding_info(),
        "git": command_info("git"),
        "claude": {
            **command_info("claude"),
            "api_key_env_set": any(os.environ.get(name) for name in BILLING_VARS),
        },
        "check": check_info(root, args.no_check),
        "headless": {"ran": False, "reason": "not_requested"},
    }
    if args.headless:
        result["headless"] = headless_info(root, args.budget, args.timeout, args.allow_api_billing)
    result["problems"] = problems(result, args.headless)
    result["ok"] = not result["problems"]

    path = root / RESULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("\n".join(report(result)))
    print(f"記録: {RESULT_FILE}（形と真偽だけ。認証情報と応答の本文は入っていない）")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
