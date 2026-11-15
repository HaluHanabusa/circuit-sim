#!/usr/bin/env python3
"""開発環境の確認。自分のPCで実行して、開発の道具が動く環境かどうかを確かめる。

  python scripts/doctor.py               OS、Python、git、claude の有無と版、確かめた内容（コミットIDと
                                         ツリーID）、scripts/check.py の結果
  python scripts/doctor.py --no-check    scripts/check.py（数分かかる）を省く
  python scripts/doctor.py --headless    加えて、本物の claude -p を最小の指示で、ゲートで使う役割ごとに
                                         1回ずつ実行し、ハーネス（scripts/cycle.py run）が前提にしている
                                         出力の形かを確かめる（--role で役割を絞れる。確認は省く。
                                         --check で流す）

--headless はClaudeの利用枠を少し使うので、付けたときだけ実行する。実行の前に、何をするかを表示する。
失敗したときは、原因の種類（サインインしていない、利用枠の上限、モデルを使えない、など）を、文面を記録せずに
分類して出す。

結果は画面と .cycle/doctor-result.json に出す。書き出すのは、形（項目の有無と型）、真偽、版の番号、
確かめた内容の git のID（コミット、ツリー）とブランチ名だけ。
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

# ヘッドレスの確認で渡す、最小の指示（独立検証の合格には基準が1つ以上要るので、基準を1つ返させる）
PROBE_PROMPT = (
    "これは開発環境の確認です。ファイルを読んだり書いたりせず、道具も使わないでください。"
    "verdict を pass、summary を ok、criteria を name が probe、measured が ok、pass が true の項目1つだけの"
    "一覧、issues を空の一覧にした結果だけを返してください。"
)
# 確認の実行に許す道具（読む道具だけ。許可の扱いと禁じる道具は、その役割を使うゲートのとおり）
PROBE_TOOLS = "Read"
# 役割の定義の場所。確認系のゲートの担当（cycle.GATE_AGENT）のほかの役割は、実装の担当として確かめる
AGENTS_DIR = ".claude/agents"

# 設定されていると、ヘッドレス実行が利用枠ではなくAPIの料金になる環境変数（中身は見ない。有無だけ）
BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# 値まで記録してよいもの。版の表示（数字、英字、空白、かっこ）と、種類を表す語（英小文字と _ だけ）
VERSION_TEXT = re.compile(r"[A-Za-z0-9_.+\-() ]{1,60}")
WORD = re.compile(r"[a-z][a-z_]{0,39}")

# ハーネスが読む項目（cycle.headless_verdict と cmd_run）と、形を知っておきたい項目
FIELDS = (
    "type", "subtype", "is_error", "session_id", "result", "structured_output",
    "total_cost_usd", "num_turns", "duration_ms", "stop_reason", "terminal_reason",
)
NUMBER_FIELDS = ("total_cost_usd", "num_turns", "duration_ms")
# 失敗の切り分けに使う、種類を表す語（値まで記録する）
WORD_FIELDS = ("type", "subtype", "stop_reason", "terminal_reason")

# 失敗の原因の種類。応答（result）とエラーの文面を、この順に照らして、最初に当たったものにする。
# 文面そのものは記録しない（種類の名前だけ）。
FAILURE_KINDS = (
    ("usage_limit", (
        r"usage limit|rate limit|limit reached|quota|too many requests|credit balance|out of credits"
    )),
    ("model_unavailable", (
        r"model[^\n]{0,120}(not found|not available|unavailable|not exist|not supported|access|"
        r"not allowed|invalid)|(invalid|unknown) model"
    )),
    ("not_logged_in", r"not logged in|log ?in|sign ?in|authenticat|unauthori[sz]ed|api key|credential|oauth"),
    ("budget_exceeded", r"budget"),
)
FAILURE_TEXT = {
    "usage_limit": "Claudeの利用枠の上限に達している（時間をおいてやり直す）",
    "model_unavailable": "指定のモデルを使えない（プランか、モデルの指定を確かめる）",
    "not_logged_in": "サインインしていない（claude を起動して /login する）",
    "budget_exceeded": "1回の上限額（--budget）に達した",
    "other": "原因の種類を分類できなかった",
}

# 結果に名前を残してよい項目。ここに無い名前は、個数だけを記録する
# （項目の名前の位置に秘密のような文字列が入っていても、結果には残さない）
KNOWN_KEYS = frozenset({
    *FIELDS, "duration_api_ms", "usage", "modelUsage", "permission_denials", "uuid", "errors",
})
KNOWN_RESULT_KEYS = frozenset(cycle.VERDICT_SCHEMA["properties"])


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


def command_info(name: str, root: Path) -> dict:
    """コマンドの有無と版。版は --version の1行目を、版の表示らしい文字のときだけ記録する。

    実行した場所と本体（root）の中にあるものは、見つけても使わない（版を聞くための起動もしない）。
    """
    found = cycle.find_command(name, (root,))
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
        # 呼び出し元の pytest の環境変数（PYTEST_ADDOPTS など）は渡さない（一部の試験だけの合格にしない）
        env = cycle.check_env(dict(os.environ))
        code = subprocess.run([sys.executable, cycle.CHECK_SCRIPT], cwd=root, env=env).returncode
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


def key_names(data: object, known: frozenset[str]) -> tuple[list[str], int]:
    """項目の名前のうち、あらかじめ決めた一覧（known）にあるものと、それ以外の個数。値は含めない。"""
    keys = list(data) if isinstance(data, dict) else []
    return sorted(key for key in keys if key in known), sum(1 for key in keys if key not in known)


def result_as_json(data: dict) -> dict | None:
    """structured_output が無いとき、result（文字列）が JSON として読めるか。読めた中身は記録しない。

    ハーネスが読むのは structured_output だけ。本物の CLI が、結果を result の側に JSON の文字列で返すなら、
    ハーネスの読み方を直す必要がある。その判断の材料にする。当てはまらない場合は None。
    """
    text = data.get("result")
    if isinstance(data.get("structured_output"), dict) or not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"readable": False}
    return {"readable": True, "kind": kind(parsed), "fits_schema": cycle.schema_problem(parsed) == ""}


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
            entry["keys"], entry["other_keys"] = key_names(value, KNOWN_RESULT_KEYS)
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


def failure_kind(text: str) -> str:
    """失敗の文面（応答とエラーの出力）から、原因の種類の名前を決める（FAILURE_KINDS）。文面は残さない。"""
    lowered = text.lower()
    for name, pattern in FAILURE_KINDS:
        if re.search(pattern, lowered):
            return name
    return "other"


def describe_output(returncode: int, stdout: str, stderr: str, options: list[str], gate: str) -> dict:
    """ヘッドレス実行の結果の形。unexpected は、ハーネスの前提と違っていた点（空なら前提どおり）。

    failure は、ハーネスの判定が合格でないときの、原因の種類（FAILURE_KINDS。合格なら None）。
    """
    form, data = result_object(stdout)
    fields = describe_fields(data) if data is not None else {}
    verdict = cycle.headless_verdict(returncode, stdout, gate)[0]
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
    keys, other_keys = key_names(data, KNOWN_KEYS)
    answer = data.get("result") if data is not None and isinstance(data.get("result"), str) else ""
    failure = None if verdict == "pass" else failure_kind(answer + "\n" + messages)
    return {
        "returncode": returncode,
        "stdout": {"form": form, "characters": len(stdout)},
        "messages": describe_messages(messages, options),
        "keys": keys,
        "other_keys": other_keys,
        "fields": fields,
        "result_as_json": result_as_json(data) if data is not None else None,
        "harness_verdict": verdict,
        "failure": failure,
        "unexpected": [name for name, ok in expected.items() if not ok],
    }


def gate_roles(root: Path, only: list[str] | None = None) -> list[tuple[str, str]]:
    """確かめる役割と、それを使うゲート: 確認系のゲートの担当と、.claude/agents/ のほかの役割（実装の担当）。

    先頭は code-reviewer（レビュー）。only を渡すと、その名前の役割だけ。渡さず、役割の定義の場所が無い
    リポジトリでは、code-reviewer だけ。
    """
    checkers = [(role, gate) for gate, role in cycle.GATE_AGENT.items()]
    agents = root / AGENTS_DIR
    others = sorted(path.stem for path in agents.glob("*.md")) if agents.is_dir() else []
    roles = checkers + [(name, "implement") for name in others if name not in cycle.GATE_AGENT.values()]
    if only:
        return [(role, gate) for role, gate in roles if role in only]
    return roles if agents.is_dir() else checkers[:1]


def restricts_tools(root: Path, role: str) -> bool:
    """役割の定義の見出しに、使える道具の一覧（tools:）があるか。あると、構造化された結果が返らない（D-030）。"""
    path = root / AGENTS_DIR / f"{role}.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.partition(":")[0].strip() == "tools":
            return True
    return False


def headless_info(
    root: Path, budget: float, timeout: int, allow_api_billing: bool, only: list[str] | None = None,
) -> dict:
    """本物の claude -p を、ハーネスと同じ組み立てで、ゲートで使う役割ごとに1回ずつ実行し、形を調べる"""
    roles = gate_roles(root, only)
    unknown = sorted(set(only or ()) - {role for role, _ in roles})
    if unknown:
        print(f"ヘッドレスの確認は実行しませんでした。知らない役割: {'、'.join(unknown)}")
        return {"ran": False, "reason": "unknown_role"}
    billing = [name for name in BILLING_VARS if os.environ.get(name)]
    if billing and not allow_api_billing:
        print(
            f"ヘッドレスの確認は実行しませんでした。環境変数 {'、'.join(billing)} が設定されていて、"
            "このまま実行すると、Claudeの利用枠ではなくAPIの料金になります。"
            "変数を外すか、料金が掛かってよければ --allow-api-billing を付けてください"
        )
        return {"ran": False, "reason": "api_key_set"}
    try:
        first = cycle.claude_command(PROBE_PROMPT, roles[0][0], budget, roles[0][1], PROBE_TOOLS)
        cycle.launchable(first, (root,))
    except cycle.CommandNotFound:
        return {"ran": False, "reason": "claude_not_found"}
    except cycle.CycleError:
        return {"ran": False, "reason": "cannot_pass_arguments"}

    print("ヘッドレスの確認（--headless）。これから次のことをします。")
    print(f"  - claude -p を、役割ごとに1回ずつ、{len(roles)} 回実行します（役割 "
          f"{'、'.join(role for role, _ in roles)}。上限は1回 {budget} 米ドル、最長 {timeout} 秒）")
    print(f"  - 指示: {PROBE_PROMPT}")
    print("  - Claudeの利用枠を少し使います（プランでサインインしている場合。対話と同じ枠です）")
    print("  - 記録するのは、出力の形（項目の有無と型）と真偽だけです。応答の本文と認証情報は記録しません")
    probes = []
    for role, gate in roles:
        cmd = cycle.claude_command(PROBE_PROMPT, role, budget, gate, PROBE_TOOLS)
        restricted = restricts_tools(root, role)
        if restricted:
            print(f"  - 注意: 役割 {role} の定義に、使える道具の一覧（tools:）がある。"
                  "構造化された結果が返らない"
                  "ことがある（D-030）。使えない道具（disallowedTools:）で絞る")
        print(f"  - 実行するコマンド（{role}）: {cycle.shell_line(root, cmd)}", flush=True)
        probes.append({
            "role": role, "gate": gate, "restricts_tools": restricted, **probe(root, cmd, timeout, gate),
        })
    # 先頭の役割の結果を、そのまま上の段に置く（1つの役割だけを確かめる場合の形は、H2 より前と同じ）。
    # ほかの役割の結果は others に並べ、前提と違う点は「役割: 項目」の形で、上の段の unexpected に足す
    first, *others = probes
    return {
        **first, "others": others,
        "unexpected": [*first["unexpected"], *(f"{item['role']}: {name}" for item in others
                                              for name in item["unexpected"])],
    }


def probe(root: Path, cmd: list[str], timeout: int, gate: str) -> dict:
    """1つの役割で claude -p を1回実行して、出力の形を調べる。判定は、その役割を使うゲート（gate）のもの。"""
    options = cmd[1::2]
    try:
        launch = cycle.launchable(cmd, (root,))
    except cycle.CycleError:
        return {"ran": False, "reason": "cannot_pass_arguments", "unexpected": ["cannot_pass_arguments"]}
    info: dict[str, object] = {"ran": True, "launcher": Path(launch[0]).name, "options": options}
    started = time.monotonic()
    try:
        proc = cycle.run_captured(launch, root, timeout)
    except subprocess.TimeoutExpired:
        return {**info, "timed_out": True, "unexpected": ["timed_out"]}
    except OSError:
        return {
            "ran": False, "reason": "cannot_start", "launcher": info["launcher"],
            "unexpected": ["cannot_start"],
        }
    info["timed_out"] = False
    info["seconds"] = round(time.monotonic() - started, 1)
    return {**info, **describe_output(proc.returncode, proc.stdout, proc.stderr, options, gate)}


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


def content_line(content: dict) -> str:
    if content["commit"] is None:
        return "分からない（gitの作業ツリーの最上位ではないか、コミットがまだ無い）"
    dirty = "、未コミットの変更あり" if content["dirty"] else ""
    branch = content["branch"] or "ブランチから切り離されている"
    return f"コミット {content['commit'][:10]}、ツリー {content['tree'][:10]}（{branch}{dirty}）"


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


NOT_RUN = {
    "not_requested": "実行していない（--headless で、役割ごとに claude -p を1回ずつ実行して、形を確かめる）",
    "api_key_set": "実行していない（APIキーの環境変数が設定されている）",
    "unknown_role": "実行していない（--role に、知らない役割がある）",
    "claude_not_found": "実行できない（claude コマンドが見つからない）",
    "cannot_pass_arguments": "実行できない（claude の起動用のファイルに、指示をそのまま渡せない）",
    "cannot_start": "実行できない（claude を起動できない）",
}


def headless_lines(headless: dict) -> list[str]:
    if "others" not in headless:            # どの役割も実行していない
        return [NOT_RUN[headless["reason"]]]
    first = {**headless, "unexpected": [name for name in headless["unexpected"] if ": " not in name]}
    lines = []
    for item in [first, *headless["others"]]:
        first, *rest = probe_lines(item)
        lines += [f"{item['role']}: {first}", *(f"  {line}" for line in rest)]
    return lines


def probe_lines(headless: dict) -> list[str]:
    """1つの役割の確認の結果の表示。"""
    restricted = headless["restricts_tools"]
    notes = ["役割の定義に tools: がある（D-030。disallowedTools: で絞る）"] if restricted else []
    if not headless["ran"]:
        return [NOT_RUN[headless["reason"]], *notes]
    if headless["timed_out"]:
        return ["時間内に終わらなかった（--timeout で延ばせる）", *notes]
    if not headless["unexpected"]:
        return ["ハーネスの前提どおりの形で返ってきた", *notes]
    fields = headless["fields"]
    present = [name for name in FIELDS if fields.get(name, {}).get("present")]
    words = [f"{name} {fields[name]['value']}" for name in WORD_FIELDS[1:] if "value" in fields.get(name, {})]
    lines = [
        f"ハーネスの前提と違う点がある: {'、'.join(headless['unexpected'])}",
        f"原因の種類: {FAILURE_TEXT.get(headless['failure'], '—')}",
        f"終了コード {headless['returncode']}、出力の形 {headless['stdout']['form']}、"
        f"ハーネスの判定 {headless['harness_verdict']}{''.join(f'、{word}' for word in words)}",
        f"あった項目: {'、'.join(present) or 'なし'}",
        *notes,
    ]
    as_json = headless["result_as_json"]
    if as_json is not None:
        fits = "決まった形に合う" if as_json.get("fits_schema") else "決まった形には合わない"
        readable = f"JSON として読める（{fits}）" if as_json["readable"] else "JSON として読めない"
        lines.append(f"structured_output が無い。result の文字列は {readable}")
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
        f"  内容        : {content_line(result['content'])}",
        f"  確認        : {check_line(result['check'])}",
        f"  ヘッドレス  : {first}",
        *(f"                {line}" for line in rest),
        "結果: 問題なし" if result["ok"] else f"結果: 問題あり（{'、'.join(result['problems'])}）",
    ]


def main(argv: list[str] | None = None) -> int:
    cycle.quiet_error_dialogs()     # Windows で、OSのエラー画面で止まらないように（起動するものに引き継ぐ）
    cycle.no_current_directory_search()
    parser = argparse.ArgumentParser(prog="doctor.py", description="開発環境の確認")
    parser.add_argument("--no-check", action="store_true", help="scripts/check.py を実行しない")
    parser.add_argument(
        "--check", action="store_true", help="--headless のときも scripts/check.py を実行する（既定は省く）",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="本物の claude -p を、ゲートで使う役割ごとに1回ずつ実行して、出力の形を確かめる"
        "（Claudeの利用枠を少し使う）",
    )
    parser.add_argument("--role", action="append", help="ヘッドレスの確認をする役割（くり返せる）")
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
        "git": command_info("git", root),
        "claude": {
            **command_info("claude", root),
            "api_key_env_set": any(os.environ.get(name) for name in BILLING_VARS),
        },
        # 確かめた内容（確認を始める前の時点）。ツリーIDは、ハーネス（cycle.py）と同じ求め方
        "content": cycle.content_info(root),
        # --headless のときは、確認（数分かかる）を省くのが既定（ヘッドレスの失敗をすぐ切り分けるため）
        "check": check_info(root, args.no_check or (args.headless and not args.check)),
        "headless": {"ran": False, "reason": "not_requested"},
    }
    if args.headless:
        result["headless"] = headless_info(root, args.budget, args.timeout, args.allow_api_billing, args.role)
    result["problems"] = problems(result, args.headless)
    result["ok"] = not result["problems"]

    path = root / RESULT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("\n".join(report(result)))
    print(f"記録: {RESULT_FILE}（形、真偽、git のIDだけ。認証情報と応答の本文は入っていない）")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
