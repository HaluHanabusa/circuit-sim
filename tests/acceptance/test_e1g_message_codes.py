"""E1g の独立検証（基準1・2、範囲13）: 注意とエラーの番号、日英の辞書、用語の是正。

確かめること:
  1. ソースを構文木で読む。辞書（messages.py）の外に、日本語を含む文字列（説明の文を除く）が増えていない。
     組み込みの例外に文字列を渡して raise していない。notes.append に渡すのは Message だけ。
  2. 辞書: 全部の番号に日英の文があり、差し込む値の名前が日英で同じ。英語の文に日本語が残っていない。
  3. 動かす: エラーを 80 通りあまり、注意を 20 通りあまり実際に出し、どれも番号つきで、英語の文にでき、
     画面に渡す形（JSON）にできる。別のプロセスへ渡せる（pickle）。
  4. 用語の是正（glossary 9章）: 直す前の言葉が日本語の辞書に残っていない。直した言葉が、その文に入っている。
  5. 画面のお知らせとの対応（付録B）: 対応表の N-… が付録B にあり、実行のお知らせの差し込む値がエンジンの
     params にある。

番号の対象の外（basic-design.md 10.3 と同じ扱い。この試験でも対象の外として数える）:
  - 初期ゲインの設計（tuning/initial.py。理由の番号 reason を持つ。辞書にするのは E2）
  - pydantic が自分で作る検証の文（欄の型、必須の欄など。画面は N-OPEN-SCHEMA の schema.* で見せる）
"""
from __future__ import annotations

import ast
import importlib.util
import json
import math
import pickle
import re
import string
from pathlib import Path

import pytest

from circuit_sim import DC, Command, Component, Netlist, Pwm, Sine, Spec, Step, expand, simulate
from circuit_sim import messages as msg
from circuit_sim.control import Pid, PidController
from circuit_sim.control.blocks import Limiter
from circuit_sim.control.schedule import Schedule, ScheduleController
from circuit_sim.templates.legs import HalfBridgeLeg
from circuit_sim.units import parse_unit, to_si

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "circuit_sim"
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
NOT_ENGINE = {"app", "ingest", "ai", "__pycache__"}        # エンジンでもテンプレートでもない場所
q = hp.q

# ------------------------------------------------------------------ 1. ソースを読む


def engine_files() -> list[Path]:
    return [p for p in sorted(PKG.rglob("*.py")) if not (set(p.relative_to(PKG).parts) & NOT_ENGINE)]


def docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def japanese_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstrings(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if JAPANESE.search(node.value):
                out.append((node.lineno, node.value))
    return out


# 辞書の外に置いてよい日本語の文字列（文面ではなく、データや、コマンドの使い方の説明）
ALLOWED_LITERALS = {
    "ir.py": {"記載なし", "手入力"},                          # 出典の決まった書き方（データ）
    # argparse の使い方の説明（先頭の一致で見る）
    "__main__.py": {"仕様JSON（層A）", "波形をCSVに書き出す"},
}
OUT_OF_SCOPE_FILES = {"tuning/initial.py"}                            # 番号の対象の外（上の説明）


def test_no_japanese_text_outside_the_dictionary():
    assert len(engine_files()) >= 20                                  # 読む場所を取り違えていない
    extra = []
    for path in engine_files():
        rel = path.relative_to(PKG).as_posix()
        if rel == "messages.py" or rel in OUT_OF_SCOPE_FILES:
            continue
        allowed = ALLOWED_LITERALS.get(rel, set())
        for line, text in japanese_literals(path):
            if not any(text.startswith(a) for a in allowed):
                extra.append(f"{rel}:{line}: {text[:40]}")
    assert extra == [], extra


def test_the_scan_finds_japanese_text_where_it_exists():
    """上の試験の前提: 読み方が、日本語の文字列を本当に見つける（辞書と、対象の外のファイルで確かめる）。"""
    assert len(japanese_literals(PKG / "messages.py")) > 150
    assert len(japanese_literals(PKG / "tuning" / "initial.py")) > 30


BUILTIN_ERRORS = {"ValueError", "TypeError", "KeyError", "RuntimeError", "Exception", "ArithmeticError",
                  "AssertionError", "NotImplementedError", "LookupError", "IndexError", "OSError"}


def test_no_builtin_exception_is_raised_with_a_bare_text_and_notes_take_only_messages():
    bad = []
    for path in engine_files():
        rel = path.relative_to(PKG).as_posix()
        if rel in OUT_OF_SCOPE_FILES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                name = getattr(node.exc.func, "id", getattr(node.exc.func, "attr", None))
                if name in BUILTIN_ERRORS:
                    bad.append(f"{rel}:{node.lineno}: raise {name}(...)")
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "append":
                target = node.func.value
                if isinstance(target, ast.Name) and target.id == "notes":
                    arg = node.args[0]
                    if not (isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "Message"):
                        bad.append(f"{rel}:{node.lineno}: notes.append({ast.unparse(arg)[:40]})")
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "warn":
                bad.append(f"{rel}:{node.lineno}: warn(...)")
    # 辞書そのものの使い方の誤り（知らない言語、辞書に無い番号）だけは、組み込みの例外でよい
    # （利用者に届かない）
    allowed = [b for b in bad if b.startswith("messages.py:")]
    assert len(allowed) <= 2 and [b for b in bad if b not in allowed] == [], bad


# ------------------------------------------------------------------ 2. 辞書

def placeholders(text: str) -> list[str]:
    names = {field.split(".")[0].split("[")[0] for _, field, _, _ in string.Formatter().parse(text) if field}
    return sorted(names)


def test_every_code_has_japanese_and_english_with_the_same_placeholders():
    assert len(msg.MESSAGES) >= 150
    for code, pair in msg.MESSAGES.items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*(_[a-z_]+)?", code), code      # DIM_<次元> だけ小文字が続く
        assert isinstance(pair, tuple) and len(pair) == 2, code
        ja, en = pair
        assert ja.strip() and en.strip(), code
        assert placeholders(ja) == placeholders(en), (code, placeholders(ja), placeholders(en))
        assert not JAPANESE.search(en), (code, en)


def test_message_carries_code_params_both_languages_and_a_json_form():
    note = msg.Message("STEP_TIME_ROUNDED", id="V1", t=msg.Value(1.2345e-3, "s"),
                       t_grid=msg.Value(1.23e-3, "s"))
    assert isinstance(note, str) and str(note) == note.text("ja") and note.code == "STEP_TIME_ROUNDED"
    assert "V1" in note.text("en") and note.text("en") != note.text("ja")
    d = json.loads(json.dumps(note.to_dict()))
    assert d == {"code": "STEP_TIME_ROUNDED", "params": {
        "id": "V1", "t": {"value": 1.2345e-3, "unit": "s"}, "t_grid": {"value": 1.23e-3, "unit": "s"}}}
    with pytest.raises(KeyError):
        msg.Message("NO_SUCH_CODE")
    with pytest.raises(ValueError):
        note.text("fr")


# ------------------------------------------------------------------ 3. 動かす

def comp(cid, typ, a, b, value=None, **kw):
    return Component(id=cid, type=typ, nodes=(a, b), value=value, **kw)


def rc(waveform=None):
    return hp.rc(waveform)


def cmd_rc():
    return hp.rc(Command(command="u", gain=1.0))


class Returns:
    """決まった値を返すだけの制御器（指令の数や値の誤りを起こすのに使う）。"""

    period, inputs, outputs = 1e-3, (), ("u",)

    def __init__(self, value):
        self.value = value

    def reset(self):
        return [0.0]

    def step(self, t, measured):
        return self.value


def t0(template, plant, sim=None):
    return hp.t0_spec(template, plant, sim)


RLC = hp.RLC_PLANT
G1 = [Pwm(name="g", frequency=1e5, duty=0.5)]
SCHED = ScheduleController(Schedule(0.0), "u", 1e-3)

ERRORS = {
    # --- 計算の入口
    "arg_t_end_zero": lambda: simulate(rc(), 0.0, 1e-5),
    "arg_h_nan": lambda: simulate(rc(), 1e-3, math.nan),
    "arg_damping_negative": lambda: simulate(rc(), 1e-3, 1e-5, damping_steps=-1),
    "arg_h_missing": lambda: simulate(rc(), 1e-3),
    "arg_steps_per_period_without_gates": lambda: simulate(rc(), 1e-3, 1e-5, steps_per_period=10),
    "arg_command_without_controller": lambda: simulate(cmd_rc(), 1e-3, 1e-5),
    "arg_t_end_shorter_than_h": lambda: simulate(rc(), 1e-6, 1e-5),
    "steps_over_limit": lambda: simulate(rc(), 1.0, 1e-9),
    "steps_over_max_steps": lambda: simulate(rc(), 1.0, 1e-5, max_steps=1000),
    "memory_over_limit": lambda: simulate(hp.ladder(14), 1.0, 1e-7),
    # --- 解けない回路
    "circuit_floating": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), comp("R1", "R", "a", "0", 1.0),
        comp("C1", "C", "b", "c", 1e-6), comp("R2", "R", "b", "c", 1.0)), 1e-3, 1e-5),
    "circuit_voltage_loop": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), hp.vsrc("V2", "a", "0", DC(value=2.0))), 1e-3, 1e-5),
    "circuit_inductors_in_series": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), comp("L1", "L", "a", "b", 1e-3, ic=1.0),
        comp("L2", "L", "b", "0", 1e-3)), 1e-3, 1e-5),
    "circuit_unsolvable_in_a_switching_state": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), comp("S1", "S", "a", "b", gate="g", r_on=1e-3, r_off=1e9),
        comp("L1", "L", "b", "c", 1e-3), comp("L2", "L", "c", "0", 1e-3, ic=1.0)), 1e-3,
        gates=[Pwm(name="g", frequency=1e4, duty=0.5)]),
    # --- ゲート信号と制御器
    "gate_duplicate": lambda: simulate(hp.buck_diode(), 1e-4, gates=G1 + G1),
    "gate_missing": lambda: simulate(hp.buck_diode(), 1e-4, gates=[Pwm(name="x", frequency=1e5, duty=0.5)]),
    "gate_no_h": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), comp("D1", "D", "a", "b"),
        comp("R1", "R", "b", "0", 1.0)), 1e-3),
    "gate_steps_per_period_one": lambda: simulate(hp.buck_diode(), 1e-4, gates=G1, steps_per_period=1),
    "gate_h_and_steps_per_period": lambda: simulate(
        hp.buck_diode(), 1e-4, 1e-7, gates=G1, steps_per_period=100),
    "gate_period_not_multiple": lambda: simulate(hp.buck_diode(), 1e-4, 3e-7, gates=G1),
    "gate_period_one_step": lambda: simulate(hp.buck_diode(), 1e-4, 1e-5, gates=G1),
    "gate_duty_command_unknown": lambda: simulate(
        hp.buck_diode(), 1e-4, gates=[Pwm(name="g", frequency=1e5, duty="zz")]),
    "controller_wrong_shape": lambda: simulate(hp.buck_diode(), 1e-4, gates=G1, controllers=[object()]),
    "controller_period_not_multiple": lambda: simulate(
        cmd_rc(), 1e-2, 1e-5, controllers=[ScheduleController(Schedule(0.0), "u", 1.5e-5)]),
    "controller_input_unknown": lambda: simulate(cmd_rc(), 1e-2, 1e-5, controllers=[
        PidController(Pid(1.0, 0.0, period=1e-3), "v(nope)", "u", 1.0)]),
    "controller_command_duplicate": lambda: simulate(cmd_rc(), 1e-2, 1e-5, controllers=[SCHED, SCHED]),
    "source_command_unknown": lambda: simulate(
        cmd_rc(), 1e-2, 1e-5, controllers=[ScheduleController(Schedule(0.0), "w", 1e-3)]),
    "controller_output_count": lambda: simulate(cmd_rc(), 1e-2, 1e-5, controllers=[Returns([1.0, 2.0])]),
    "controller_output_nan": lambda: simulate(cmd_rc(), 1e-2, 1e-5, controllers=[Returns([math.nan])]),
    "diverged": lambda: simulate(hp.rc(Command(command="u", gain=10.0)), 0.2, 1e-5,
                                 controllers=[hp.Feedback(0.3)]),
    # --- 結果の取り出し、表示定義
    "result_no_signal": lambda: simulate(rc(), 1e-3, 1e-5)["v(zz)"],
    "result_no_current": lambda: simulate(rc(), 1e-3, 1e-5).i("C1"),
    "netlist_no_component": lambda: rc().component("zz"),
    # --- ネットリスト（pydantic の検証の中で出す。元のエラーは message_of で取り出せる）
    "component_same_nodes": lambda: comp("R1", "R", "a", "a", 1.0),
    "component_value_zero": lambda: comp("R1", "R", "a", "b", 0.0),
    "component_value_missing": lambda: comp("R1", "R", "a", "b"),
    "component_waveform_on_resistor": lambda: Component(
        id="R1", type="R", nodes=("a", "b"), value=1.0, waveform=DC(value=1.0)),
    "source_without_waveform": lambda: comp("V1", "V", "a", "b"),
    "source_with_value": lambda: Component(
        id="V1", type="V", nodes=("a", "b"), value=1.0, waveform=DC(value=1.0)),
    "r_on_on_resistor": lambda: comp("R1", "R", "a", "b", 1.0, r_on=1.0),
    "gate_on_resistor": lambda: comp("R1", "R", "a", "b", 1.0, gate="g"),
    "ic_on_resistor": lambda: comp("R1", "R", "a", "b", 1.0, ic=1.0),
    "switch_with_value": lambda: comp("S1", "S", "a", "b", 1.0, gate="g"),
    "switch_r_negative": lambda: comp("S1", "S", "a", "b", gate="g", r_on=-1.0),
    "switch_r_order": lambda: comp("S1", "S", "a", "b", gate="g", r_on=10.0, r_off=1.0),
    "switch_without_gate": lambda: comp("S1", "S", "a", "b"),
    "command_limits_order": lambda: Command(command="u", gain=1.0, lower=1.0, upper=0.0),
    "netlist_duplicate_ids": lambda: Netlist(components=[comp("R1", "R", "a", "0", 1.0)] * 2),
    "netlist_no_ground": lambda: hp.net(comp("R1", "R", "a", "b", 1.0), comp("R2", "R", "a", "b", 1.0)),
    "netlist_dangling_node": lambda: hp.net(comp("R1", "R", "a", "0", 1.0), comp("R2", "R", "a", "b", 1.0)),
    # --- PWM、制御ブロック、レグ
    "pwm_frequency_inf": lambda: Pwm(name="g", frequency=math.inf, duty=0.5),
    "pwm_complement_same_name": lambda: Pwm(name="g", frequency=1e3, duty=0.5, complement="g"),
    "pwm_dead_time": lambda: Pwm(name="g", frequency=1e3, duty=0.5, dead_time=1e-6),
    "pwm_duty_empty_name": lambda: Pwm(name="g", frequency=1e3, duty=""),
    "pwm_duty_out_of_range": lambda: Pwm(name="g", frequency=1e3, duty=1.5),
    "limiter_order": lambda: Limiter(1.0, 0.0),
    "pid_gain_nan": lambda: Pid(math.nan, 0.0, period=1e-3),
    "pid_period_zero": lambda: Pid(1.0, 0.0, period=0.0),
    "pid_filter_negative": lambda: Pid(1.0, 0.0, period=1e-3, t_filter=-1.0),
    "pid_anti_windup_unknown": lambda: Pid(1.0, 0.0, period=1e-3, anti_windup="zz"),
    "pid_derivative_on_unknown": lambda: Pid(1.0, 0.0, period=1e-3, derivative_on="zz"),
    "pid_measurement_missing": lambda: Pid(1.0, 0.0, period=1e-3, derivative_on="measurement").update(1.0),
    "schedule_nan": lambda: Schedule(math.nan),
    "schedule_order": lambda: Schedule(0.0, ((2.0, 1.0), (1.0, 2.0))),
    "leg_fidelity_unknown": lambda: HalfBridgeLeg("p", "0", "o", 0.5, 1e4, 10.0).expand("ideal"),
    # --- 単位
    "unit_unreadable": lambda: parse_unit("furlong"),
    "unit_unreadable_with_expectation": lambda: to_si(1.0, "zz", "voltage"),
    "unit_wrong_dimension": lambda: to_si(1.0, "mH", "voltage"),
    "unit_wrong_dimension_exact": lambda: to_si(1.0, "V", "per_voltage"),
    # --- テンプレート
    "template_unknown": lambda: expand(Spec.model_validate({"template": "zz", "plant": {}})),
    "missing_values_t0": lambda: expand(t0("rlc_series", {"Vin": q(10, "V"), "R": q(None, "ohm")})),
    "units_unreadable_t0": lambda: expand(t0("rlc_series", {**RLC, "R": q(5, "zz")})),
    "units_wrong_t0": lambda: expand(t0("rlc_series", {**RLC, "R": q(5, "mH")})),
    "units_both_t0": lambda: expand(t0("rlc_series", {**RLC, "R": q(5, "mH"), "L": q(1, "zz")})),
    "values_not_positive_t0": lambda: expand(t0("rlc_series", {**RLC, "R": q(-5, "ohm")})),
    "missing_values_buck": lambda: expand(hp.buck_spec(drop=("L", "Kp"))),
    "missing_pair_buck": lambda: expand(hp.buck_spec(plant={"R_load_step": q(3, "ohm")})),
    "units_buck": lambda: expand(hp.buck_spec(plant={"L": q(47, "uF")}, control={"Kp": q(0.01, "V")})),
    "values_out_of_range_buck": lambda: expand(
        hp.buck_spec(plant={"L": q(-47, "uH")}, control={"Kd": q(-1, "s/V")})),
    "duty_limits_order": lambda: expand(hp.buck_spec(control={"d_min": q(0.9, "1"), "d_max": q(0.1, "1")})),
    "f_sw_not_multiple": lambda: expand(hp.buck_spec(control={"f_ctrl": q(30, "kHz")})),
    "load_step_same": lambda: expand(
        hp.buck_spec(plant={"R_load_step": q(6, "ohm"), "t_load_step": q(1, "ms")})),
    "load_step_too_small": lambda: expand(
        hp.buck_spec(plant={"R_load_step": q(0.0005, "ohm"), "t_load_step": q(1, "ms")})),
    "legacy_control_not_number": lambda: Spec.model_validate({
        "schema_version": "0.1", "template": "buck_voltage_mode", "plant": {}, "control": {"Kp": "abc"}}),
}


def raised(name) -> BaseException:
    try:
        ERRORS[name]()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError(f"{name}: エラーにならなかった")


def coded_error(exc: BaseException):
    """番号つきのエラー（pydantic に包まれていれば、中のエラー）。"""
    if isinstance(exc, msg.Coded):
        return exc
    for item in exc.errors():
        inner = (item.get("ctx") or {}).get("error")
        if isinstance(inner, msg.Coded):
            return inner
    return None


@pytest.mark.parametrize("name", list(ERRORS))
def test_every_error_is_coded_bilingual_and_serializable(name):
    exc = raised(name)
    err = coded_error(exc)
    assert err is not None, f"番号の無いエラー {type(exc).__name__}: {exc}"
    assert msg.message_of(exc) is err.message
    assert err.code in msg.MESSAGES and err.message.code in msg.MESSAGES
    ja, en = err.text("ja"), err.text("en")
    assert ja == str(err) and JAPANESE.search(ja), ja
    assert en and not JAPANESE.search(en) and "{" not in en and "{" not in ja, en
    d = err.to_dict()
    assert json.loads(json.dumps(d, allow_nan=False))["code"] == err.code
    back = pickle.loads(pickle.dumps(err))                           # 別のプロセスへ渡せる
    assert type(back) is type(err) and back.to_dict() == d and str(back) == str(err)


def test_error_classes_keep_their_builtin_families():
    """既存の呼び出し側の except が効き続ける。

    値の誤りは ValueError、名前で引けないのは KeyError、型は TypeError。"""
    for name in ("arg_t_end_zero", "steps_over_limit", "circuit_voltage_loop", "unit_unreadable",
                 "missing_values_buck", "diverged", "template_unknown", "pid_period_zero"):
        assert isinstance(raised(name), ValueError), name
    for name in ("result_no_signal", "result_no_current", "netlist_no_component"):
        assert isinstance(raised(name), KeyError), name
    assert isinstance(raised("controller_wrong_shape"), TypeError)
    assert raised("missing_values_buck").names == ["L", "Kp"]
    assert raised("missing_pair_buck").pairs == {"t_load_step": "R_load_step"}


NOTES = {
    "rc_units_unused_fidelity": lambda: expand(t0(
        "rc_lowpass", {**hp.RC_PLANT, "X": q(1, "V")}, {"fidelity": "average"})).run(),
    "rlc_slow_decay": lambda: expand(t0("rlc_series", {**RLC, "R": q(0.01, "ohm")})).run(),
    "rlc_stiff": lambda: expand(t0("rlc_series", {**RLC, "R": q(10, "kohm")})).run(),
    "rlc_user_t_end_coarse": lambda: expand(
        t0("rlc_series", {**RLC, "R": q(1, "ohm")}, {"t_end": 1.0})).run(),
    "rlc_phase_drift": lambda: expand(
        t0("rlc_series", {**RLC, "R": q(0.5, "ohm")}, {"h": 2e-5, "t_end": 0.02})).run(),
    "buck_switching": lambda: expand(hp.buck_spec(sim={"t_end": 3e-4})).run(),
    "buck_average_everything": lambda: expand(hp.buck_spec(
        "average", plant={"R_load_step": q(3, "ohm"), "t_load_step": q(1.003, "ms")},
        control={"Tf": q(None, "us"), "Vref": q(23, "V"), "Vref_step": q(1, "V"), "t_ref_step": q(5, "ms"),
                 "d_min": q(5, "%")}, sim={"t_end": 2e-3})).run(),
    "buck_legacy_control": lambda: expand(Spec.model_validate({
        "schema_version": "0.1", "template": "buck_voltage_mode",
        "plant": {"Vin": q(24, "V"), "L": q(47, "uH"), "C": q(100, "uF"), "R_load": q(6, "ohm"),
                  "Z": q(1, "V")},
        "control": {"f_sw": 1e5, "f_ctrl": 1e5, "Vref": 12, "Kp": 0.0117, "Ki": 147, "Kd": 2.74e-6,
                    "d_min": 0, "d_max": 0.9, "junk": 3},
        "simulation": {"fidelity": "average", "t_end": 3e-4}})).run(),
    "buck_legacy_control_inferred": lambda: expand(Spec.model_validate({
        "template": "buck_voltage_mode",
        "plant": {"Vin": q(24, "V"), "L": q(47, "uH"), "C": q(100, "uF"), "R_load": q(6, "ohm")},
        "control": {"f_sw": 1e5, "f_ctrl": 1e5, "Vref": 12, "Kp": 0.0117, "Ki": 147, "Kd": 2.74e-6,
                    "d_min": 0, "d_max": 0.9},
        "simulation": {"fidelity": "average", "t_end": 3e-4}})).run(),
    "sine_undersampled": lambda: simulate(rc(Sine(amplitude=1.0, frequency=1e4)), 5e-3, 5e-5),
    "step_time_rounded": lambda: simulate(rc(Step(final=1.0, t_step=1.2345e-3)), 5e-3, 1e-5),
    "t_end_rounded": lambda: simulate(rc(), 1.05e-3, 1e-4),
    "step_coarse": lambda: simulate(rc(), 5e-2, 5e-3),
    "step_coarse_undamped": lambda: simulate(rc(), 5e-2, 5e-3, damping_steps=0),
    "step_coarse_ringing": lambda: simulate(hp.rlc(0.1), 5e-2, 2e-4),
    "step_coarse_over_limit": lambda: simulate(hp.rectifier(), 0.02, 1e-5),
    "initial_value_adjusted": lambda: simulate(hp.net(
        hp.vsrc("V1", "a", "0", DC(value=1.0)), comp("R1", "R", "a", "b", 1.0),
        comp("C1", "C", "b", "0", 1e-6, ic=1.0), comp("C2", "C", "b", "0", 1e-6, ic=2.0)), 1e-3, 1e-5),
    "inductor_current_interrupted": lambda: simulate(hp.net(
        hp.vsrc("V1", "in", "0", DC(value=24.0)), comp("S1", "S", "in", "sw", gate="g"),
        comp("L1", "L", "sw", "out", 47e-6), comp("C1", "C", "out", "0", 100e-6),
        comp("R1", "R", "out", "0", 6.0)), 1e-4, gates=G1),
}
# その回路で必ず出るはずの番号（出なければ、回路が注意を出していない = 確かめていない）
EXPECTED_CODES = {
    "rc_units_unused_fidelity": {"UNIT_CONVERTED", "UNUSED_ITEMS", "FIDELITY_IGNORED"},
    "rlc_slow_decay": {"DEFAULT_T_END_SHORTENED"},
    "rlc_stiff": {"DEFAULT_H_STIFF", "STEP_COARSE_OVER_LIMIT"},
    "rlc_user_t_end_coarse": {"DEFAULT_H_COARSENED"},
    "rlc_phase_drift": {"PHASE_DRIFT"},
    "buck_switching": {"UNIT_CONVERTED", "ESR_OMITTED"},
    "buck_average_everything": {"AVERAGE_MODEL_USED", "ESR_OMITTED", "TF_DEFAULTED", "LOAD_STEP_SNAPPED",
                                "REFERENCE_OUT_OF_RANGE", "STEP_AFTER_END", "UNIT_CONVERTED_RATIO"},
    "buck_legacy_control": {"SPEC_LEGACY_CONTROL", "UNUSED_ITEMS"},
    "buck_legacy_control_inferred": {"SPEC_LEGACY_CONTROL_INFERRED"},
    "sine_undersampled": {"SINE_UNDERSAMPLED"},
    "step_time_rounded": {"STEP_TIME_ROUNDED"},
    "t_end_rounded": {"T_END_ROUNDED"},
    "step_coarse": {"STEP_COARSE"},
    "step_coarse_undamped": {"STEP_COARSE_UNDAMPED"},
    "step_coarse_ringing": {"STEP_COARSE_RINGING"},
    "step_coarse_over_limit": {"STEP_COARSE_OVER_LIMIT"},
    "initial_value_adjusted": {"INITIAL_VALUE_ADJUSTED"},
    "inductor_current_interrupted": {"INDUCTOR_CURRENT_INTERRUPTED"},
}


@pytest.fixture(scope="module")
def noted():
    return {name: run() for name, run in NOTES.items()}


@pytest.mark.parametrize("name", list(NOTES))
def test_every_note_is_coded_bilingual_and_serializable(name, noted):
    notes = noted[name].notes
    assert EXPECTED_CODES[name] <= {n.code for n in notes}, [n.code for n in notes]
    for note in notes:
        assert isinstance(note, msg.Message) and note.code in msg.MESSAGES
        ja, en = note.text("ja"), note.text("en")
        assert ja == str(note) and "{" not in ja and "{" not in en
        assert en and not JAPANESE.search(en), en
        json.dumps(note.to_dict(), allow_nan=False)
        assert pickle.loads(pickle.dumps(note)).to_dict() == note.to_dict()


# ------------------------------------------------------------------ 4. 用語の是正（glossary 9章）

OLD_WORDS = ["行き場", "段差", "出どころ", "周期の頭", "計算時間", "落ち着かない", "搬送波", "モデルの精度",
             "極の半径", "植物", "基本単位", "折り返して"]


def test_old_words_are_gone_from_the_japanese_dictionary():
    for code, (ja, _) in msg.MESSAGES.items():
        for word in OLD_WORDS:
            assert word not in ja, (code, word)
        if "回路状態" in ja:
            assert "スイッチング状態（回路状態" in ja, code           # 併記（D-063）


def test_corrected_words_appear_in_the_messages_that_were_corrected(noted):
    def text(name, code):
        return next(str(n) for n in noted[name].notes if n.code == code)

    assert "還流経路が無くなり" in text("inductor_current_interrupted", "INDUCTOR_CURRENT_INTERRUPTED")
    assert "エイリアシング（折り返し）" in text("sine_undersampled", "SINE_UNDERSAMPLED")
    assert "終了時刻 t_end" in text("t_end_rounded", "T_END_ROUNDED")
    assert "この終了時刻では" in text("rlc_stiff", "STEP_COARSE_OVER_LIMIT")
    assert "既定の終了時刻" in text("rlc_slow_decay", "DEFAULT_T_END_SHORTENED")
    assert "負荷ステップの時刻" in text("buck_average_everything", "LOAD_STEP_SNAPPED")
    assert "スイッチング周期の先頭" in text("buck_average_everything", "LOAD_STEP_SNAPPED")
    assert "ステップは波形に現れない" in text("buck_average_everything", "STEP_AFTER_END")
    assert "出典は「記載なし」" in text("buck_legacy_control", "SPEC_LEGACY_CONTROL")
    assert "終了時刻 t_end を短くする" in str(raised("steps_over_limit"))
    assert "負荷ステップ" in str(raised("load_step_same"))
    assert "負荷ステップ" in str(raised("load_step_too_small"))
    assert "未対応のモデルの種類" in str(raised("leg_fidelity_unknown"))
    in_state = str(raised("circuit_unsolvable_in_a_switching_state"))
    assert in_state.startswith("スイッチング状態（回路状態。S1=オン）で")
    # 英語の文は、用語集の英語（time step、stop time、load step、freewheeling path、aliasing）
    en = {n.code: n.text("en") for name in noted for n in noted[name].notes}
    assert "freewheeling path" in en["INDUCTOR_CURRENT_INTERRUPTED"] and "Aliasing" in en["SINE_UNDERSAMPLED"]
    assert "stop time" in en["T_END_ROUNDED"] and "time step" in en["T_END_ROUNDED"]
    assert "load step" in en["LOAD_STEP_SNAPPED"].lower()


def test_gain_design_texts_use_the_corrected_words_and_say_how_to_fix():
    """初期ゲインの設計の文（番号の対象の外）も、用語は直っている（glossary 9章の 8・9）。範囲の外の文に、
    原因と直し方がある（範囲14）。"""
    from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

    source = (PKG / "tuning" / "initial.py").read_text(encoding="utf-8")
    literals = [text for _, text in japanese_literals(PKG / "tuning" / "initial.py")]
    assert not any("極の半径" in t for t in literals)
    assert all("ゲイン交差周波数" in t for t in literals if "交差周波数" in t)
    assert "閉ループ極の絶対値の最大" in source
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(24.0, 47e-6, 100e-6, 1e-5, 1e5)        # Q = R·√(C/L) = 1.5e-5（下限 1e-3）
    text = str(info.value)
    assert info.value.reason == "invalid_argument"
    assert "負荷の Q" in text and "現在 1.46e-05" in text             # 何が、いくつで外れたか
    assert "ので" in text and "r_load を大きくする" in text           # 原因と直し方
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(24.0, 0.1, 0.1, 1e3, 1e12)             # f_ctrl/f0 = 6.3e11（上限 1e11）
    text = str(info.value)
    assert info.value.reason == "invalid_argument"
    assert "f_ctrl/f0" in text and "現在 6.28e+11" in text
    assert "ので" in text and "f_ctrl を下げる" in text


# ------------------------------------------------------------------ 5. 画面のお知らせ（付録B）との対応

def appendix_b() -> dict[str, set[str]]:
    doc = (ROOT / "docs" / "design" / "screens-v0.1.md").read_text(encoding="utf-8")
    start = doc.index("## 付録B")
    text = doc[start:doc.index("## 付録C", start)]
    notices = {}
    for line in text.splitlines():
        found = re.match(r"\| (N-[A-Z0-9-]+) \|", line)
        if found:
            notices[found.group(1)] = set(re.findall(r"\{([a-z_]+)\}", line))
    assert len(notices) >= 30, "付録B の表を読めていない（文書の形が変わったら、この読み方を直す）"
    return notices


def test_screen_notice_table_points_at_notices_that_exist_and_codes_that_exist():
    notices = appendix_b()
    for code, names in msg.SCREEN_NOTICES.items():
        assert code in msg.MESSAGES, code
        for name in names:
            assert name in notices, (code, name)
    # 実行のお知らせ（付録B.4）のうち、エンジンが原因を知っているものは、どれかの番号から届く
    reachable = {n for names in msg.SCREEN_NOTICES.values() for n in names}
    for name in ("N-RUN-CIRCUIT", "N-RUN-SWITCH", "N-RUN-DIVERGED", "N-RUN-STEPS", "N-RUN-MEMORY",
                 "N-RUN-NOTE-STEP", "N-RUN-NOTE-ROUND", "N-STEPS-LIMIT", "N-STEP-COARSE", "N-MISSING",
                 "N-MISSING-PAIR", "N-UNIT-WRONG", "N-UNIT-CONVERTED", "N-UNUSED", "N-OPEN-LEGACY",
                 "N-OPEN-TEMPLATE", "N-REF-RANGE", "N-STEP-AFTER-END", "N-STEP-SNAP", "N-INVALID"):
        assert name in reachable, name


def test_engine_params_carry_what_the_run_notices_insert(noted):
    """付録B の実行のお知らせと、計算の前の判定のお知らせの {…}（実行の番号 {n}、項目の記号と名前を除く）が、
    実際に出したエラーと注意の params にある。時定数の無い注意（正弦波の点数、振動の点数）の {tau} は除く
    （画面はその部分を省いた文にする。付録B.2）。"""
    notices = appendix_b()
    instances = {n.code: n for name in noted for n in noted[name].notes}
    for name in ("steps_over_limit", "memory_over_limit", "circuit_voltage_loop", "diverged"):
        err = raised(name)
        instances[err.code] = err
    checked = 0
    for code, obj in instances.items():
        for notice in msg.SCREEN_NOTICES.get(code, ()):
            if not (notice.startswith("N-RUN-") or notice in (
                    "N-STEPS-LIMIT", "N-STEP-COARSE", "N-REF-RANGE", "N-STEP-AFTER-END", "N-STEP-SNAP")):
                continue                                              # 項目ごとのお知らせは、画面が組み立てる
            need = notices[notice] - {"n", "symbol", "name"}
            if code in ("SINE_UNDERSAMPLED", "STEP_COARSE_RINGING"):
                need -= {"tau"}
            assert need <= set(obj.params), (code, notice, sorted(need - set(obj.params)))
            checked += 1
    assert checked >= 12
    # 記号を差し込むお知らせは、記号も持つ
    for code in ("LOAD_STEP_SNAPPED", "REFERENCE_OUT_OF_RANGE", "STEP_AFTER_END"):
        assert "symbol" in instances[code].params, code
