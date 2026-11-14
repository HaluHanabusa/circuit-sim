"""W1（Windowsで動くこと）の受け入れ試験（検証担当が追加）。

実装者の試験（tests/test_harness.py、tests/test_dev_tools.py、tests/test_console_encoding.py）が
確かめていない点を、別の経路で確かめる。Linux と Windows の自動テストの両方で、同じ試験が走る。
実測値と測り方は `docs/verification/W1-verifier.md` にある。

- 基準2: ハーネスの試験のうち、このOSでスキップされるものが、決まった数件だけであること。
- 基準3: UTF-8 でない端末でも落ちず、置き換えた後も数値と単位の意味が変わらないこと。
- 基準4: `scripts/doctor.py` が claude を起動する回数と引数、記録に残る「形」、残らない「中身」。
- 基準5: W1 で変わった箇所のうち、実装者の試験では壊しても落ちなかった分岐。

期待値の作り方:
- 偽の claude と一時リポジトリは、このファイルの中で作る（実装者の試験の部品は使わない）。
- 「形」の期待値は、偽の claude に返させた出力から、このファイルの中で決める。
- 単位の期待値は、接頭語と単位記号の表をこのファイルの中に書く（製品の表は使わない）。

時間の許容の根拠:
- 打ち切りの後に戻るまでの猶予（GRACE）。Linux は止める操作が一瞬なので 3 秒。Windows は taskkill の
  起動を含むので 10 秒（実装者の試験は、1 秒の打ち切りに対して 15 秒まで許している）。

製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
CYCLE = ROOT / "scripts" / "cycle.py"
DOCTOR = ROOT / "scripts" / "doctor.py"
WINDOWS = os.name == "nt"
GRACE = 10.0 if WINDOWS else 3.0

# 利用者の git の設定の代わりに読ませる、空のファイル
EMPTY_GIT_CONFIG = Path(tempfile.gettempdir()) / "circuit-sim-w1-acceptance-empty.gitconfig"
EMPTY_GIT_CONFIG.write_text("", encoding="utf-8")

# 設定されていると doctor.py --headless が実行を断る環境変数（試験の外の設定に左右されないよう、外す）
BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def product_bug(severity: str, *lines: str, condition: bool = True) -> pytest.MarkDecorator:
    """製品側の不具合で落ちる試験に付ける印。直ったら（XPASS で失敗するので）印を外す。"""
    return pytest.mark.xfail(condition, strict=True, reason=f"製品の不具合({severity}): " + "".join(lines))


def base_env(**extra: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in BILLING_VARS
    }
    env.update({
        "GIT_AUTHOR_NAME": "w1", "GIT_AUTHOR_EMAIL": "w1@example.invalid",
        "GIT_COMMITTER_NAME": "w1", "GIT_COMMITTER_EMAIL": "w1@example.invalid",
        "GIT_CONFIG_GLOBAL": str(EMPTY_GIT_CONFIG), "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra)
    return env


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"w1_acceptance_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 偽の claude

# 起動のたびに、受け取った引数を1行ずつ書き足す（何回、どの引数で起動されたかを、後で全部見るため）。
# --version には版を返す。それ以外は conf.json に従う:
#   child: 出力の口を受け継いだ子プロセスを残す（値のファイルが置かれるまで、最長 40 秒いる）
#   sleep: 待つ秒数 / exit: 終了コード。出力は out.bin と err.bin のバイト列をそのまま出す
FAKE_CLAUDE = """\
import json
import pathlib
import subprocess
import sys
import time

here = pathlib.Path(__file__).resolve().parent
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    sys.stdout.write("9.9.9 (fake)\\n")
    sys.exit(0)
conf = json.loads((here / "conf.json").read_text(encoding="utf-8"))
if conf.get("child"):
    waiter = (
        "import pathlib, sys, time\\n"
        "end = time.monotonic() + 40\\n"
        "while time.monotonic() < end and not pathlib.Path(sys.argv[1]).exists():\\n"
        "    time.sleep(0.05)\\n"
    )
    subprocess.Popen([sys.executable, "-c", waiter, conf["child"]], stdout=sys.stdout, stderr=sys.stderr)
    (here / "child-started").touch()
time.sleep(conf.get("sleep", 0))
sys.stdout.buffer.write((here / "out.bin").read_bytes())
sys.stderr.buffer.write((here / "err.bin").read_bytes())
sys.exit(conf.get("exit", 0))
"""


def install_fake_claude(
    base: Path, output: object, *, exit_code: int = 0, stderr: str = "", sleep: float = 0, child: str = "",
) -> Path:
    """偽の claude を base/bin に置いて、その場所を返す。output は JSON にして（bytes はそのまま）出す。"""
    fake = base / "bin"
    fake.mkdir(exist_ok=True)
    if isinstance(output, bytes):
        data = output
    else:
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        data = text.encode("utf-8")
    (fake / "out.bin").write_bytes(data)
    (fake / "err.bin").write_bytes(stderr.encode("utf-8"))
    conf = {"exit": exit_code, "sleep": sleep, "child": child}
    (fake / "conf.json").write_text(json.dumps(conf), encoding="utf-8")
    if WINDOWS:     # npm で入れた CLI と同じ形（claude.cmd が実体を呼ぶ）
        script = fake / "claude-script.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        (fake / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="oem")
    else:
        command = fake / "claude"
        command.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        command.chmod(0o755)
    return fake


def with_claude(fake: Path, **extra: str) -> dict[str, str]:
    """偽の claude を PATH の先頭に置いた環境変数（本物が後ろにあっても、先に見つかるのは偽のほう）。"""
    return base_env(PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", **extra)


def claude_calls(fake: Path) -> list[list[str]]:
    """偽の claude が受け取った引数を、起動された順に全部。"""
    log = fake / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


PASS = {"verdict": "pass", "summary": "ok", "criteria": [], "issues": []}


def good(structured: object = PASS, **top: object) -> dict:
    """ハーネスが前提にしている形の出力。"""
    return {
        "type": "result", "subtype": "success", "is_error": False, "session_id": "sess-1",
        "result": "done", "structured_output": structured, "total_cost_usd": 0.01, "num_turns": 1, **top,
    }


# ---------------------------------------------------------------- 一時リポジトリと cycle.py

SPEC = "---\nid: U1\nowner: solver-engineer\n---\n\n# U1: 受け入れ試験用の作業単位\n"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=base_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(base: Path) -> Path:
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "plan").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text("print('checked')\n", encoding="utf-8")
    (root / "docs" / "plan" / "U1-demo.md").write_text(SPEC, encoding="utf-8")
    (root / ".gitignore").write_text(".cycle/\n", encoding="utf-8")
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def cycle(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """cycle.py をコマンドラインから実行する（別のプロセス。試験の中で関数を呼ぶ形にはしない）。"""
    return subprocess.run(
        [sys.executable, str(CYCLE), *args], capture_output=True, text=True, encoding="utf-8",
        env={**(env or base_env()), "CYCLE_ROOT": str(root)}, stdin=subprocess.DEVNULL, timeout=120,
    )


def started(base: Path) -> Path:
    """start と implement の記録まで済ませたリポジトリ。"""
    root = make_repo(base)
    assert cycle(root, "start", "U1").returncode == 0
    (base / "wt" / "U1" / "product.py").write_text("x = 2\n", encoding="utf-8")
    result = cycle(root, "record", "U1", "implement", "--verdict", "pass", "--by", "impl-1")
    assert result.returncode == 0, result.stderr
    return root


def state(root: Path) -> dict:
    return json.loads((root / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))


# ================================================================ 基準2: スキップされる試験

# 試験を集めるときに、スキップの印（skip、skipif）と xfail の印を調べて書き出す、pytest の差し込み
CENSUS_PLUGIN = """\
import json
import os


def pytest_collection_modifyitems(items):
    rows = []
    for item in items:
        skipped = []
        for mark in item.iter_markers("skipif"):
            if mark.args and mark.args[0]:
                skipped.append(mark.kwargs.get("reason", ""))
        for mark in item.iter_markers("skip"):
            skipped.append(mark.kwargs.get("reason", "") or "skip")
        rows.append({
            "name": item.name, "skipped": skipped,
            "xfail": any(True for _ in item.iter_markers("xfail")),
        })
    with open(os.environ["W1_CENSUS_OUT"], "w", encoding="utf-8") as out:
        json.dump(rows, out)
"""

STOP_TEST = "test_merge_is_undone_when_interrupted_during_check_on_main"
# このOSに無い機能そのものを試すので、スキップしてよい試験（仕様の基準2）。これ以外が増えたら落とす
SKIPPED_ON_WINDOWS = {f"{STOP_TEST}[SIGINT]", f"{STOP_TEST}[SIGTERM]", f"{STOP_TEST}[SIGHUP]"}
SKIPPED_ELSEWHERE = {
    f"{STOP_TEST}[CTRL_BREAK_EVENT]",
    "test_record_is_written_while_another_program_has_the_state_file_open",
}


@pytest.fixture(scope="module")
def harness_census(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    base = tmp_path_factory.mktemp("census")
    (base / "w1_census_plugin.py").write_text(CENSUS_PLUGIN, encoding="utf-8")
    out = base / "census.json"
    env = base_env(W1_CENSUS_OUT=str(out), PYTHONPATH=str(base))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_harness.py", "--collect-only", "-q",
         "-p", "w1_census_plugin", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_harness_tests_skipped_on_this_os_are_only_the_known_few(harness_census):
    """ゲートの決まりの試験のうち、このOSでスキップされるのは、OSに無い機能そのものを試す数件だけ。"""
    skipped = {row["name"] for row in harness_census if row["skipped"]}
    assert skipped == (SKIPPED_ON_WINDOWS if WINDOWS else SKIPPED_ELSEWHERE)
    # 残りは、このOSで実行される。件数が少なすぎれば、集め方が壊れている
    assert len(harness_census) - len(skipped) >= 150


def test_each_way_of_stopping_a_merge_is_tested_on_some_os(harness_census):
    """止め方ごとの試験が、どちらかのOSで必ず実行される（両方でスキップされる止め方が無い）。"""
    names = {row["name"] for row in harness_census}
    assert SKIPPED_ON_WINDOWS | SKIPPED_ELSEWHERE <= names
    assert not SKIPPED_ON_WINDOWS & SKIPPED_ELSEWHERE
    # Ctrl-C と強制終了は、どのOSでもスキップしない試験が確かめる
    always = {
        "test_merge_is_undone_when_interrupted_from_the_keyboard",
        "test_merge_killed_during_check_is_undone_by_the_next_merge",
    }
    assert always <= {row["name"] for row in harness_census if not row["skipped"]}


def test_harness_tests_do_not_skip_or_expect_failure_by_themselves(harness_census):
    """印（skipif）のほかに、試験の中からスキップしたり、失敗を見込んだりしていない。"""
    assert [row["name"] for row in harness_census if row["xfail"]] == []
    source = (ROOT / "tests" / "test_harness.py").read_text(encoding="utf-8")
    for call in ("pytest.skip(", "pytest.xfail(", "importorskip("):
        assert call not in source, call


# ================================================================ 基準3: UTF-8 でない端末

EXAMPLE = "examples/rlc_series.json"
PREFIXED = "tests/data/rc_lowpass_prefixed_units.json"
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?")
ENCODINGS = ["cp932", "cp1252", "ascii"]        # 日本語 Windows、英語 Windows、日本語の無い端末

# 接頭語（10 の指数）と単位記号。製品の表（circuit_sim.units）とは別に、ここに書く
PREFIXES = {"p": -12, "n": -9, "u": -6, "\u00b5": -6, "\u03bc": -6, "m": -3, "": 0, "k": 3, "M": 6}
SYMBOLS = {"ohm": "ohm", "\u03a9": "ohm", "\u2126": "ohm", "F": "F", "H": "H", "V": "V"}


@functools.cache
def run_cli(spec: str, encoding: str) -> tuple[int, str, str]:
    """端末の文字コードを encoding にして実行する。返すのは（終了コード、出力、エラー出力）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "circuit_sim", spec], cwd=ROOT, capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": encoding},
    )
    out, err = (data.decode(encoding).replace("\r\n", "\n") for data in (proc.stdout, proc.stderr))
    return proc.returncode, out, err         # Windows では画面への改行が CRLF になるので、LF に直して返す


def read_unit(text: str) -> tuple[int, str] | None:
    """画面に出た単位の表記を、（10 の指数、単位）として読む。読めなければ None。"""
    for symbol, unit in SYMBOLS.items():
        if text.endswith(symbol) and text[: -len(symbol)] in PREFIXES:
            return PREFIXES[text[: -len(symbol)]], unit
    return None


def conversions(out: str) -> dict[str, tuple[float, str, float, str]]:
    """「名前: 値 単位 を 値 単位 に換算した」の行を拾う（日本語が ? に置き換わっていても拾える）。"""
    found = re.findall(r"(\w+): (\S+) (\S+) \S+ (\S+) (\S+) \S+$", out, flags=re.M)
    return {name: (float(value), unit, float(si_value), si) for name, value, unit, si_value, si in found}


def assert_conversions_read_true(out: str, expected: dict[str, tuple[float, str]]) -> None:
    """画面の単位を文字どおりに読んだ大きさが、同じ行の換算後の値と合う（µ が落ちれば 6 桁ずれる）。"""
    shown = conversions(out)
    assert set(shown) == set(expected), out
    for name, (value, unit, si_value, si) in shown.items():
        meaning = read_unit(unit)
        assert meaning is not None, f"{name}: 単位 {unit!r} を読めない"
        exponent, base = meaning
        assert base == si == expected[name][1], (name, unit, si)
        assert value * 10.0 ** exponent == pytest.approx(si_value, rel=1e-9), (name, value, unit, si_value)
        assert si_value == pytest.approx(expected[name][0], rel=1e-9)


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("spec", [EXAMPLE, PREFIXED])
def test_numbers_are_the_same_on_a_terminal_that_is_not_utf8(spec, encoding):
    """表せない文字を置き換えても、数値は1文字も変わらず、行も増減しない。"""
    code, out, err = run_cli(spec, encoding)
    assert code == 0 and err == ""
    code, reference, err = run_cli(spec, "utf-8")
    assert code == 0 and err == ""
    assert NUMBER.findall(out) == NUMBER.findall(reference)
    assert len(NUMBER.findall(out)) >= 9
    assert len(out.splitlines()) == len(reference.splitlines())


@pytest.mark.parametrize("encoding", [*ENCODINGS, "utf-8"])
def test_units_keep_their_meaning_on_a_terminal_that_is_not_utf8(encoding):
    """µ が落ちて無印や m に読めたり、Ω が消えたりしない（同梱の仕様: 1 kΩ と 10 µF）。"""
    code, out, err = run_cli(PREFIXED, encoding)
    assert code == 0 and err == ""
    assert_conversions_read_true(out, {"R": (1000.0, "ohm"), "C": (1e-05, "F")})
    if encoding != "utf-8":         # Ω（U+2126）は、この3つの文字コードのどれにも無い
        assert "1 kohm" in out


# µ と Ω の2通りずつの文字と、取り違えやすい接頭語（m と M）を使った仕様
UNIT_SPECS = {
    "ミリとマイクロ": (
        "rlc_series",
        {"Vin": (5, "V"), "R": (500, "m\u2126"), "L": (10, "\u00b5H"), "C": (10, "\u03bcF")},
        {"R": (0.5, "ohm"), "L": (1e-05, "H"), "C": (1e-05, "F")},
    ),
    "メガとナノ": (
        "rc_lowpass",
        {"Vin": (5, "V"), "R": (2, "M\u03a9"), "C": (10, "nF")},
        {"R": (2e6, "ohm"), "C": (1e-08, "F")},
    ),
}


@pytest.mark.parametrize("encoding", ["cp932", "ascii"])
@pytest.mark.parametrize("case", UNIT_SPECS)
def test_prefixes_are_not_confused_after_replacement(tmp_path, case, encoding):
    template, plant, expected = UNIT_SPECS[case]
    spec = {
        "schema_version": "0.1", "template": template, "targets": {}, "missing": [],
        "plant": {
            name: {"value": value, "unit": unit, "source": "手入力"} for name, (value, unit) in plant.items()
        },
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    code, out, err = run_cli(str(path), encoding)
    assert code == 0 and err == "", err
    assert_conversions_read_true(out, expected)


# ================================================================ 基準4: scripts/doctor.py

# 記録にも画面にも出てはいけない目印。形を3通りにする（記号つき、識別子の形、英小文字だけ）。
# 記録してよい値の決まり（版の表示、種類を表す語、項目の名前）に、形だけなら当てはまるものを含める
MARKS = ("SECRET-7f3a", "sk_ant_SECRETTOKEN123", "hunterpassword")
BODY = " / ".join(MARKS)


def leaking(text: str) -> list[str]:
    return [mark for mark in MARKS if mark.lower() in text.lower()]


def secret_output(**top: object) -> dict:
    """応答の本文、セッションのIDなど、値の位置にだけ目印を入れた出力（項目の名前には入れない）。"""
    structured = {
        "verdict": "pass", "summary": BODY, "issues": [{"severity": "minor", "description": BODY}],
        "criteria": [{"name": MARKS[2], "measured": MARKS[1], "pass": True}],
    }
    return good(structured, session_id=MARKS[1], result=MARKS[2], usage={"note": BODY}, uuid=MARKS[2], **top)


def doctor_root(base: Path) -> Path:
    root = base / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text("print('check output')\n", encoding="utf-8")
    return root


def run_doctor(root: Path, fake: Path, *args: str, **extra: str) -> tuple[int, dict, str, str]:
    """doctor.py をコマンドラインから実行する。返すのは（終了コード、結果、結果のファイルの中身、画面）。"""
    env = with_claude(fake, CYCLE_ROOT=str(root), SOME_OTHER_VARIABLE=MARKS[2], **extra)
    proc = subprocess.run(
        [sys.executable, str(DOCTOR), *args], capture_output=True, text=True, encoding="utf-8", env=env,
        stdin=subprocess.DEVNULL, timeout=120,
    )
    path = root / ".cycle" / "doctor-result.json"
    assert path.is_file(), proc.stdout + proc.stderr
    written = path.read_text(encoding="utf-8")
    return proc.returncode, json.loads(written), written, proc.stdout + proc.stderr


def test_doctor_without_headless_asks_claude_only_for_its_version(tmp_path):
    """--headless を付けない実行で claude を起動するのは、版を聞く1回だけ（-p は実行しない）。"""
    root, fake = doctor_root(tmp_path), install_fake_claude(tmp_path, secret_output())
    code, result, written, screen = run_doctor(root, fake)
    assert code == 0, screen
    assert claude_calls(fake) == [["--version"]]
    # 範囲6の項目: OS、Python の版、git、claude の有無と版、scripts/check.py の結果
    assert result["os"]["system"] and result["os"]["release"]
    assert result["python"]["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert result["git"]["found"] is True and result["git"]["version"].startswith("git version")
    assert result["claude"]["found"] is True and result["claude"]["version"] == "9.9.9 (fake)"
    assert result["check"]["ran"] is True and result["check"]["passed"] is True
    assert result["headless"] == {"ran": False, "reason": "not_requested"}
    assert result["ok"] is True and "check output" in screen
    assert leaking(written) == [] and leaking(screen) == []
    # 書き出すのは結果のファイルだけ
    assert sorted(path.name for path in (root / ".cycle").iterdir()) == ["doctor-result.json"]


# 偽の claude に返させる出力と、そこから決まる「形」の期待値。
# （出力、終了コード、標準エラー、待つ秒数、--timeout、doctor の終了コード、記録に残るはずの形）
HEADLESS_SHAPES = {
    "正常": (
        secret_output(), 0, "", 0, None, 0,
        {"returncode": 0, "form": "json_object", "harness_verdict": "pass", "unexpected": []},
    ),
    "is_error が真": (
        secret_output(is_error=True), 0, "", 0, None, 1,
        {"returncode": 0, "form": "json_object", "harness_verdict": "error",
         "unexpected": ["is_error", "harness_verdict"]},
    ),
    "JSON でない": (
        f"Error: {BODY}\n", 0, "", 0, None, 1,
        {"returncode": 0, "form": "not_json", "harness_verdict": "error", "fields": {},
         "unexpected": ["stdout_form", "is_error", "subtype", "structured_output", "session_id",
                        "harness_verdict"]},
    ),
    "終了コードが 0 でない": (
        secret_output(), 3, f"fatal: {BODY}\n", 0, None, 1,
        {"returncode": 3, "form": "json_object", "harness_verdict": "error",
         "unexpected": ["returncode", "harness_verdict"]},
    ),
    "structured_output が無い": (
        {k: v for k, v in secret_output().items() if k != "structured_output"}, 0, "", 0, None, 1,
        {"returncode": 0, "form": "json_object", "harness_verdict": "error",
         "unexpected": ["structured_output", "harness_verdict"]},
    ),
    "時間切れ": (
        secret_output(), 0, "", 30, "2", 1,
        {"timed_out": True, "unexpected": ["timed_out"]},
    ),
}


@pytest.mark.parametrize("case", HEADLESS_SHAPES)
def test_doctor_headless_runs_the_probe_once_and_records_only_the_shape(tmp_path, case):
    output, exit_code, stderr, sleep, timeout, doctor_code, shape = HEADLESS_SHAPES[case]
    root = doctor_root(tmp_path)
    fake = install_fake_claude(tmp_path, output, exit_code=exit_code, stderr=stderr, sleep=sleep)
    args = ["--no-check", "--headless", *(["--timeout", timeout] if timeout else [])]
    code, result, written, screen = run_doctor(root, fake, *args)
    assert code == doctor_code, screen

    # claude の起動は、版を聞く1回と、-p の1回だけ。-p の組み立ては cycle.py run と同じ並び
    calls = claude_calls(fake)
    assert [call[0] for call in calls] == ["--version", "-p"]
    options = dict(zip(calls[1][2::2], calls[1][3::2], strict=True))
    assert options["--agent"] and options["--output-format"] == "json"
    required = json.loads(options["--json-schema"])["required"]
    assert set(required) == {"verdict", "summary", "criteria", "issues"}

    # 記録された形が、偽の claude に返させた出力を正しく表している
    head = result["headless"]
    assert head["ran"] is True
    assert head["unexpected"] == shape["unexpected"]
    assert head["timed_out"] is shape.get("timed_out", False)
    if not head["timed_out"]:
        assert head["returncode"] == shape["returncode"]
        assert head["stdout"]["form"] == shape["form"]
        assert head["harness_verdict"] == shape["harness_verdict"]
        # JSON でない出力は、エラーの文面として数える
        in_stdout = len(output) if shape["form"] == "not_json" else 0
        assert head["messages"]["characters"] == len(stderr) + in_stdout
    if "fields" in shape:
        assert head["fields"] == shape["fields"]
    elif not head["timed_out"]:
        fields = head["fields"]
        assert fields["is_error"]["value"] is output["is_error"]
        assert fields["structured_output"]["present"] is ("structured_output" in output)
        assert fields["session_id"] == {"present": True, "kind": "string", "empty": False}
        assert fields["result"] == {"present": True, "kind": "string"}
    assert result["problems"] == ([] if doctor_code == 0 else ["headless"])

    # 応答の本文、セッションのID、エラーの文面、環境変数の中身は、ファイルにも画面にも出ない
    assert leaking(written) == [] and leaking(screen) == []
    assert sorted(path.name for path in (root / ".cycle").iterdir()) == ["doctor-result.json"]


def test_doctor_probe_uses_the_same_command_as_a_harness_run(tmp_path):
    """doctor.py --headless と cycle.py run review が、同じオプションの並び、同じ結果の形の指定で起動する。"""
    root = started(tmp_path)
    fake = install_fake_claude(tmp_path, good())
    assert cycle(root, "run", "U1", "review", "--budget", "1.0", env=with_claude(fake)).returncode == 0
    args = ("--no-check", "--headless", "--budget", "1.0")
    code, _, _, screen = run_doctor(doctor_root(tmp_path), fake, *args)
    assert code == 0, screen
    harness, version, probe = claude_calls(fake)
    assert version == ["--version"] and harness[0] == probe[0] == "-p"
    assert harness[2::2] == probe[2::2]
    same = ("--output-format", "--json-schema", "--permission-mode", "--max-budget-usd", "--agent")
    harness_options = dict(zip(harness[2::2], harness[3::2], strict=True))
    probe_options = dict(zip(probe[2::2], probe[3::2], strict=True))
    assert {name: probe_options[name] for name in same} == {name: harness_options[name] for name in same}
    assert probe_options["--allowedTools"] == "Read"            # 確認の実行には、読む道具だけを渡す


@pytest.mark.parametrize("variable", BILLING_VARS)
def test_doctor_does_not_run_the_probe_or_show_the_key_when_an_api_key_is_set(tmp_path, variable):
    root, fake = doctor_root(tmp_path), install_fake_claude(tmp_path, good())
    key = {variable: MARKS[1]}
    code, result, written, screen = run_doctor(root, fake, "--no-check", "--headless", **key)
    assert code == 1
    assert claude_calls(fake) == [["--version"]]
    assert result["headless"] == {"ran": False, "reason": "api_key_set"}
    assert result["claude"]["api_key_env_set"] is True
    assert leaking(written) == [] and leaking(screen) == []


# ================================================================ 基準5: 壊しても落ちなかった分岐

def test_cli_output_that_is_not_utf8_is_recorded_as_an_error(tmp_path):
    """claude の出力に UTF-8 でないバイト列があっても、例外で落ちず、error として記録する（決まり6）。"""
    root = started(tmp_path)
    fake = install_fake_claude(tmp_path, b'{"session_id": "s", "structured_output": \xff\xfe}')
    result = cycle(root, "run", "U1", "review", env=with_claude(fake))
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert "review: error" in result.stdout
    assert state(root)["gates"]["review"]["verdict"] == "error"


def test_cli_output_is_read_as_utf8_whatever_the_default_encoding_is(tmp_path):
    """claude の出力（UTF-8）の日本語が、既定の文字コードが UTF-8 でないOS（Windows）でも崩れずに届く。

    Linux の自動テストは既定が UTF-8 なので、この試験が違いを見つけられるのは Windows だけ。
    """
    summary = "要約は日本語で届く（10 µF、1 kΩ）"
    root = started(tmp_path)
    fake = install_fake_claude(tmp_path, good({**PASS, "summary": summary}))
    result = cycle(root, "run", "U1", "review", env=with_claude(fake))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"review: pass（{summary}）" in result.stdout
    raw = (root / state(root)["gates"]["review"]["report"]).read_text(encoding="utf-8")
    assert json.loads(raw)["structured_output"]["summary"] == summary


def test_state_and_result_files_end_lines_with_lf_on_any_os(tmp_path):
    """状態ファイルと doctor.py の結果は、どのOSでも改行が LF（CR を含まない）。

    Windows 以外では、改行を指定しなくても LF になるので、違いを見つけられるのは Windows だけ。
    """
    root = started(tmp_path)
    data = (root / ".cycle" / "state" / "U1.json").read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n") and data.count(b"\n") > 10
    fake = install_fake_claude(tmp_path, good())
    code, _, _, screen = run_doctor(doctor_root(tmp_path), fake, "--no-check")
    assert code == 0, screen
    data = (tmp_path / "root" / ".cycle" / "doctor-result.json").read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n") and data.count(b"\n") > 10


def test_check_that_cannot_be_launched_is_an_error_not_a_pass(tmp_path):
    """確認スクリプトを起動できない（OSError）とき、合格にも例外にもせず、error を返す（決まり4、7）。"""
    module = load_script("cycle")
    root = make_repo(tmp_path)
    real_run = subprocess.run

    def refuse_check(cmd, *args, **kwargs):
        if list(cmd[1:]) == [module.CHECK_SCRIPT]:
            raise OSError(8, "起動できない")
        return real_run(cmd, *args, **kwargs)

    with mock.patch.dict(os.environ, base_env(), clear=True), \
            mock.patch.object(module.subprocess, "run", refuse_check):
        verdict, tree, output = module.run_check(root)
    assert verdict == "error"
    assert re.fullmatch(r"[0-9a-f]{40}", tree)
    assert "scripts/check.py を起動できない" in output


def test_headless_timeout_returns_even_when_the_cli_left_a_child_holding_its_output(tmp_path):
    """--timeout は、claude が子プロセスを残していても効く（人が見ていない実行の、最後の歯止め）。"""
    root = started(tmp_path)
    release = tmp_path / "release"
    fake = install_fake_claude(tmp_path, good(), sleep=60, child=str(release))
    timeout = 3
    proc = subprocess.Popen(
        [sys.executable, str(CYCLE), "run", "U1", "review", "--timeout", str(timeout)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env={**with_claude(fake), "CYCLE_ROOT": str(root)}, stdin=subprocess.DEVNULL,
    )
    began = time.monotonic()
    try:
        try:
            out, err = proc.communicate(timeout=timeout + GRACE)
            returned_in = time.monotonic() - began
        except subprocess.TimeoutExpired:
            returned_in = None
    finally:
        release.touch()                         # 残った子プロセスを終わらせる
        out, err = proc.communicate(timeout=60)
    assert (fake / "child-started").exists()    # 打ち切りの前に、子プロセスは起動していた
    assert returned_in is not None, f"{timeout + GRACE} 秒たっても戻らない（--timeout {timeout}）"
    assert proc.returncode == 1 and "review: error" in out, out + err
    assert state(root)["gates"]["review"]["verdict"] == "error"


@pytest.mark.parametrize("name", [
    "scripts/check.sh", "scripts/check.py", "scripts/cycle.py", "scripts/doctor.py",
    "examples/rlc_series.json", "tests/data/rc_lowpass_prefixed_units.json", "pyproject.toml",
    ".gitattributes",
])
def test_checked_out_files_end_lines_with_lf(name):
    """チェックアウトしたファイルの改行が、どのOSでも LF（Windows の git が CRLF に変えていない）。"""
    data = (ROOT / name).read_bytes()
    assert b"\r" not in data and data.count(b"\n") >= 3


def test_check_sh_only_delegates_to_check_py(tmp_path):
    """scripts/check.sh は scripts/check.py を呼ぶだけで、終了コードをそのまま返す。"""
    text = (ROOT / "scripts" / "check.sh").read_bytes().decode("utf-8")
    commands = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert any(line.startswith("exec ") and line.endswith('/check.py"') for line in commands), commands
    assert "ruff" not in " ".join(commands) and "pytest" not in " ".join(commands)
    assert "\r" not in text                     # 改行が CRLF だと、bash が読めない
    if WINDOWS:
        return      # Windows では bash を前提にしない（python scripts/check.py を使う）。中身の確認まで
    scripts = tmp_path / "project" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "check.sh").write_text(text, encoding="utf-8")
    (scripts / "check.sh").chmod(0o755)
    for code in (0, 7):
        body = f"import sys\nprint('ran check.py')\nsys.exit({code})\n"
        (scripts / "check.py").write_text(body, encoding="utf-8")
        # python という名前で、いまの Python が見つかるようにする
        env = base_env(PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
        proc = subprocess.run(
            [str(scripts / "check.sh")], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
            env=env,
        )
        assert proc.returncode == code and "ran check.py" in proc.stdout, proc.stderr
