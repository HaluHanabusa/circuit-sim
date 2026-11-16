"""E1g の範囲1・13（受け入れ基準1・2）: 注意とエラーは番号と差し込む値で返り、文面は日英の辞書から作る。

- 辞書: すべての番号に日本語と英語があり、差し込む値の名前が両方でそろう。
- 番号の無い文が出ないこと: (a) ソースを読んで、エンジン・テンプレート・制御ブロックに、文字列をそのまま
  渡す raise や notes.append が無い。(b) いろいろな仕様と回路を実際に計算し、
  出た注意とエラーがすべて番号つき。
- 画面のお知らせ（付録B）との対応表: 表の N-… がすべて付録B にあり、基本設計書 10.4 の表がこの表と同じ。
"""
from __future__ import annotations

import ast
import json
import pickle
import re
import string
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import (
    DC,
    CircuitError,
    CodedError,
    Command,
    Component,
    Message,
    MissingValueError,
    Netlist,
    Pwm,
    Sine,
    Spec,
    Step,
    StepLimitError,
    expand,
    simulate,
)
from circuit_sim.messages import (
    LANGS,
    MESSAGES,
    SCREEN_NOTICES,
    Coded,
    CodedKeyError,
    Names,
    SwitchState,
    Value,
    message_of,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "circuit_sim"
EXAMPLES = ROOT / "examples"
SCREENS = ROOT / "docs" / "design" / "screens-v0.1.md"
BASIC = ROOT / "docs" / "design" / "basic-design.md"

# 番号の無い文を出してはいけないモジュール（エンジン、テンプレート、制御ブロック）。
# 初期ゲインの設計（tuning/）は E1g の範囲外: GainDesignError は理由の番号（reason）を持ち、文は日本語だけ
# （画面に出すのは E2 から）。messages.py は辞書そのもの（辞書の使い方の誤りの文だけを持つ）。
CHECKED = [p for p in PACKAGE.rglob("*.py")
           if "tuning" not in p.parts and "app" not in p.parts
           and p.name not in ("messages.py", "__main__.py")]


def fields(template: str) -> set[str]:
    return {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(template) if f}


# ------------------------------------------------------------------ 辞書


def test_every_code_has_japanese_and_english_with_the_same_fields():
    for code, texts in MESSAGES.items():
        assert len(texts) == len(LANGS), code
        assert all(t.strip() for t in texts), code
        assert fields(texts[0]) == fields(texts[1]), code
        assert re.fullmatch(r"[A-Z][A-Za-z0-9_]*", code), code


def test_english_uses_the_glossary_terms():
    """英語の文は glossary の英語にそろえる（11章: time step、stop time、integer multiple。数え方の step と
    負荷の load step を取り違えない）。日本語の文に、直す前の言葉（9章）が残っていない。"""
    english = " ".join(t[1] for t in MESSAGES.values())
    assert "whole multiple" not in english and "base unit" not in english
    japanese = "".join(t[0] for t in MESSAGES.values())
    for old in ("行き場", "折り返して", "計算時間", "落ち着かない", "段差", "周期の頭", "出どころ",
                "交差周波数", "極の半径", "搬送波", "モデルの精度", "基本単位", "接頭辞", "積分の飽和"):
        assert old not in japanese, old


def codes_used_in_source() -> set[str]:
    """ソースの中で、番号として使っている文字列。

    Message("…") や CodedError("…") の最初の引数と、DIM_ の組み立て。"""
    used = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in MESSAGES:
                used.add(node.value)
    used |= {k for k in MESSAGES if k.startswith("DIM_")}          # f"DIM_{dimension}" で引く
    return used


def test_every_code_in_the_dictionary_is_used_and_every_used_code_exists():
    used = codes_used_in_source()
    assert set(MESSAGES) - used == set()
    literal = set()
    for path in CHECKED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in ("Message", "CodedError", "CircuitError", "UnitError", "DivergenceError",
                            "CodedTypeError", "CodedKeyError", "SimulationCancelled"):
                    literal.add(node.args[0].value)
    assert literal and literal <= set(MESSAGES), literal - set(MESSAGES)


# ------------------------------------------------------------------ 番号の無い文が無いこと（ソース）


def _has_text(node: ast.AST) -> bool:
    """文字列の定数、f 文字列、それらを + でつないだもの。"""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.BinOp):
        return _has_text(node.left) or _has_text(node.right)
    return False


def test_no_raise_or_note_with_plain_text_in_engine_and_templates():
    """raise X("文") と notes.append("文") が無い（文は番号と差し込む値から辞書で作る）。

    失敗すべきときに失敗すること: 同じ確かめを、
    番号つきにする前の形の小さなソースに当てて見つかることも確かめる。
    """
    bad = []
    for path in CHECKED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            call = None
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                call = node.exc
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("append", "insert", "extend")
                  and getattr(node.func.value, "id", "") == "notes"):
                call = node
            if call is not None and any(_has_text(a) for a in call.args):
                name = getattr(call.func, "id", getattr(call.func, "attr", ""))
                if name not in ("Message", "CodedError", "CircuitError", "UnitError", "DivergenceError",
                                "CodedTypeError", "CodedKeyError", "SimulationCancelled",
                                "MissingValueError"):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert bad == []
    # 確かめそのものが働くこと（番号つきにする前の形）
    old = ast.parse('raise ValueError(f"t_end が h より短い {x}")\nnotes.append("文")')
    assert _has_text(old.body[0].exc.args[0]) and _has_text(old.body[1].value.args[0])


# ------------------------------------------------------------------ 番号の無い文が無いこと（実際に計算して）


def spec(template, plant=None, control=None, simulation=None, **extra):
    d = {"template": template, "plant": {k: {"value": v, "unit": u} for k, (v, u) in (plant or {}).items()}}
    if control is not None:
        d["control"] = {k: {"value": v, "unit": u} for k, (v, u) in control.items()}
    if simulation is not None:
        d["simulation"] = simulation
    d.update(extra)
    return Spec.model_validate(d)


def buck_dict(**changes):
    d = json.loads((EXAMPLES / "buck_voltage_mode.json").read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "switching", "t_end": 0.4e-3}
    for key, value in changes.items():
        section, name = key.split("__")
        if value is None:
            d[section].pop(name, None)
        else:
            d[section][name] = {"value": value[0], "unit": value[1]}
    return d


def rlc(r, ind, cap, **kw):
    return expand(spec("rlc_series", {"Vin": (10, "V"), "R": r, "L": ind, "C": cap}, **kw))


NOTE_CASES = {
    "rc_換算と使わない項目": lambda: expand(spec(
        "rc_lowpass", {"Vin": (5, "V"), "R": (1, "kohm"), "C": (1, "uF"), "X": (1, "V")},
        simulation={"fidelity": "average"})),
    "rlc_減衰が遅い": lambda: rlc((0.001, "ohm"), (1, "mH"), (10, "uF")),
    "rlc_位相のずれ": lambda: rlc((0.01, "ohm"), (1, "mH"), (10, "uF"), simulation={"t_end": 0.5, "h": 2e-5}),
    "rlc_過減衰": lambda: rlc((1, "kohm"), (1, "uH"), (1, "mF")),
    "rlc_指定の t_end": lambda: rlc((0.1, "ohm"), (1, "mH"), (10, "uF"), simulation={"t_end": 2.0}),
    "buck_例": lambda: expand(Spec.model_validate(buck_dict())),
    "buck_平均_ステップ_範囲の外": lambda: expand(Spec.model_validate({
        **buck_dict(plant__R_load_step=(3, "ohm"), plant__t_load_step=(0.2003, "ms"),
                    control__Vref_step=(30, "V"), control__t_ref_step=(1, "s"), control__Tf=None),
        "simulation": {"fidelity": "average", "t_end": 0.4e-3}})),
    "buck_版0.1": lambda: expand(Spec.model_validate({
        "template": "buck_voltage_mode", "plant": buck_dict()["plant"],
        "control": {"f_sw": 1e5, "f_ctrl": 1e5, "Vref": 12, "Kp": 0.0117, "Ki": 147, "Kd": 0, "d_min": 0,
                    "d_max": 0.9},
        "simulation": {"t_end": 0.2e-3}})),
}

SIM_CASES = {
    "正弦波の点が少ない_ステップの丸め_終了時刻の丸め": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=Sine(amplitude=1, frequency=3e4)),
        Component(id="R1", type="R", nodes=("a", "b"), value=1.0),
        Component(id="C1", type="C", nodes=("b", "0"), value=1e-6),
        Component(id="V2", type="V", nodes=("c", "0"), waveform=Step(final=1, t_step=1.5e-5)),
        Component(id="R2", type="R", nodes=("c", "d"), value=1.0),
        Component(id="C2", type="C", nodes=("d", "0"), value=1e-6)]), 1.05e-4, 1e-5),
    "刻みが粗い_初期値の食い違い": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1)),
        Component(id="R1", type="R", nodes=("a", "b"), value=1.0),
        Component(id="C1", type="C", nodes=("b", "0"), value=1e-9, ic=0.5),
        Component(id="C2", type="C", nodes=("b", "0"), value=1e-9, ic=0.2)]), 1e-3, 1e-5),
    "減衰なし": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=Step(final=1)),
        Component(id="R1", type="R", nodes=("a", "b"), value=1.0),
        Component(id="C1", type="C", nodes=("b", "0"), value=1e-9)]), 1e-3, 1e-5, damping_steps=0),
    "振動の点が少ない": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=Step(final=1)),
        Component(id="L1", type="L", nodes=("a", "b"), value=1e-6),
        Component(id="C1", type="C", nodes=("b", "0"), value=1e-6),
        Component(id="R1", type="R", nodes=("b", "0"), value=1e3)]), 1e-4, 2e-6),
    "電流の遮断": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=10)),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q"),
        Component(id="L1", type="L", nodes=("sw", "out"), value=1e-3),
        Component(id="R1", type="R", nodes=("out", "0"), value=1.0)]), 2e-4,
        gates=[Pwm(name="q", frequency=1e4, duty=0.5)]),
}

ERROR_CASES = {
    "不足": lambda: expand(spec("rlc_series", {"Vin": (10, "V"), "R": (None, "ohm")})),
    "組の片方": lambda: expand(Spec.model_validate(buck_dict(plant__R_load_step=(3, "ohm")))),
    "単位": lambda: expand(spec("rc_lowpass", {"Vin": (5, "V"), "R": (1, "kH"), "C": (1, "uX")})),
    "非正": lambda: expand(spec("rc_lowpass", {"Vin": (5, "V"), "R": (0, "ohm"), "C": (1, "uF")})),
    "範囲の外": lambda: expand(Spec.model_validate(buck_dict(control__Kp=(-1, "1/V")))),
    "デューティ": lambda: expand(Spec.model_validate(buck_dict(control__d_min=(0.95, "1")))),
    "f_sw": lambda: expand(Spec.model_validate(buck_dict(control__f_ctrl=(30, "kHz")))),
    "負荷ステップが同じ": lambda: expand(Spec.model_validate(buck_dict(plant__R_load_step=(6, "ohm"),
                                                                      plant__t_load_step=(1, "ms")))),
    "負荷ステップが小さすぎ": lambda: expand(Spec.model_validate(buck_dict(plant__R_load_step=(0.0009, "ohm"),
                                                                          plant__t_load_step=(1, "ms")))),
    "テンプレート": lambda: expand(spec("nothing", {})),
    "上限": lambda: rlc((10, "kohm"), (1, "mH"), (10, "uF"), simulation={"h": 1e-12}).run(),
    "記憶量": lambda: simulate(Netlist(components=[
        Component(id=f"R{j}", type="R", nodes=(f"n{j}", f"n{j + 1}" if j < 59 else "0"), value=1.0)
        for j in range(60)] + [Component(id="V1", type="V", nodes=("n0", "0"), waveform=DC(value=1))]),
        1.0, 1e-7),
    "解けない": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1)),
        Component(id="V2", type="V", nodes=("a", "0"), waveform=DC(value=2))]), 1e-3, 1e-5),
    "制御器の形": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1)),
        Component(id="R1", type="R", nodes=("a", "0"), value=1.0)]), 1e-3, 1e-5, controllers=[object()]),
    "ゲートが無い": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1)),
        Component(id="S1", type="S", nodes=("a", "b"), gate="q"),
        Component(id="R1", type="R", nodes=("b", "0"), value=1.0)]), 1e-3, 1e-5),
    "指令の電源に制御器が無い": lambda: simulate(Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=Command(command="u", gain=1)),
        Component(id="R1", type="R", nodes=("a", "0"), value=1.0)]), 1e-3, 1e-5),
    "ネットリストの形": lambda: Netlist(components=[
        Component(id="R1", type="R", nodes=("a", "a"), value=1.0)]),
    "版0.1の数でない値": lambda: Spec.model_validate({"schema_version": "0.1", "template": "rc_lowpass",
                                                      "plant": {}, "control": {"Kp": "x"}}),
}


def collect_notes(obj) -> list:
    return list(obj.notes) + (list(obj.run().notes) if hasattr(obj, "run") else [])


@pytest.mark.parametrize("case", list(NOTE_CASES) + list(SIM_CASES))
def test_every_note_has_a_code_and_both_languages(case):
    made = NOTE_CASES[case]() if case in NOTE_CASES else SIM_CASES[case]()
    notes = collect_notes(made)
    assert notes, "この例では注意が出るはず"
    for n in notes:
        assert isinstance(n, Message) and n.code in MESSAGES, n
        assert n.text("ja") == str(n) and n.text("en") and n.text("en") != n.text("ja")
        json.dumps(n.to_dict(), ensure_ascii=False)                     # 画面に渡せる形


def test_the_note_cases_cover_the_note_codes():
    """上の例で出る注意の番号が、注意の番号のほとんどを覆っている（漏れた番号を一覧にする）。"""
    seen = set()
    for case in list(NOTE_CASES) + list(SIM_CASES):
        made = NOTE_CASES[case]() if case in NOTE_CASES else SIM_CASES[case]()
        seen |= {n.code for n in collect_notes(made)}
    expected = {
        "UNIT_CONVERTED", "UNUSED_ITEMS", "FIDELITY_IGNORED", "DEFAULT_T_END_SHORTENED", "PHASE_DRIFT",
        "DEFAULT_H_STIFF", "DEFAULT_H_COARSENED", "ESR_OMITTED", "AVERAGE_MODEL_USED", "TF_DEFAULTED",
        "LOAD_STEP_SNAPPED", "REFERENCE_OUT_OF_RANGE", "STEP_AFTER_END", "SPEC_LEGACY_CONTROL_INFERRED",
        "SINE_UNDERSAMPLED", "STEP_TIME_ROUNDED", "T_END_ROUNDED", "STEP_COARSE", "INITIAL_VALUE_ADJUSTED",
        "STEP_COARSE_UNDAMPED", "STEP_COARSE_RINGING", "INDUCTOR_CURRENT_INTERRUPTED",
    }
    assert expected - seen == set()


@pytest.mark.parametrize("case", list(ERROR_CASES))
def test_every_error_has_a_code_and_both_languages(case):
    with pytest.raises(Exception) as info:
        ERROR_CASES[case]()
    message = message_of(info.value)
    assert message is not None, repr(info.value)
    assert message.code in MESSAGES or message.code == "WITH_API_HINT"
    assert message.text("en")
    if isinstance(info.value, Coded):
        assert info.value.code in MESSAGES and str(info.value) == str(info.value.message)
        json.dumps(info.value.to_dict(), ensure_ascii=False)


def test_missing_values_keep_names_and_pairs():
    with pytest.raises(MissingValueError) as info:
        expand(Spec.model_validate(buck_dict(plant__R_load_step=(3, "ohm"), plant__L=None)))
    err = info.value
    assert err.names == ["L", "t_load_step"] and err.pairs == {"t_load_step": "R_load_step"}
    assert err.code == "MISSING_VALUES" and err.to_dict()["params"]["pairs"] == {"t_load_step": "R_load_step"}
    assert str(err) == "値が入っていないパラメータ: ['L', 't_load_step']"


def test_step_limit_error_keeps_the_reason_without_the_api_hint():
    with pytest.raises(StepLimitError) as info:
        simulate(Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1)),
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0)]), 1.0, 1e-9)
    err = info.value
    assert err.code == "STEPS_OVER_LIMIT" and str(err).startswith(err.without_api_hint + "。")
    assert "max_steps" in str(err) and "max_steps" not in err.without_api_hint
    p = err.to_dict()["params"]
    assert p["limit"] == 20_000_000 and p["t_max"] == {"value": 0.02, "unit": "s"}
    assert p["h_min"]["value"] == pytest.approx(5e-8)


def test_errors_and_notes_survive_pickle():
    """別のプロセスで計算して結果やエラーを戻せる（画面のサーバーが計算を別に走らせるときのため）。"""
    for err in [CodedError("TOO_MANY_DEVICES", n=70, max=62), MissingValueError(["R"], {"R": "C"}),
                CircuitError(Message("CIRCUIT_UNSOLVABLE")), CodedKeyError("RESULT_NO_SIGNAL", name="x",
                                                                          names=Names(["a"]))]:
        err.extra = 1
        back = pickle.loads(pickle.dumps(err))
        assert type(back) is type(err) and str(back) == str(err) and back.code == err.code
        assert back.extra == 1 and back.to_dict() == err.to_dict()
    res = expand(spec("rc_lowpass", {"Vin": (5, "V"), "R": (1, "kohm"), "C": (1, "uF")})).run()
    note = res.notes[0]
    back = pickle.loads(pickle.dumps(note))
    assert back == note and back.code == note.code and back.text("en") == note.text("en")


def test_switch_state_and_units_are_written_per_language():
    state = SwitchState(("S1", "D1"), 0b01)
    assert state.text("ja") == "S1=オン, D1=オフ" and state.text("en") == "S1=on, D1=off"
    m = Message("INITIAL_VALUE_ADJUSTED", id="C1", ic=Value(0.5, "V"), actual=Value(0.35, "V"))
    assert "ic=0.5 V" in m and "0.35 V" in m and m.to_dict()["params"]["ic"] == {"value": 0.5, "unit": "V"}


# ------------------------------------------------------------------ 画面のお知らせとの対応


def appendix_b_codes() -> set[str]:
    text = SCREENS.read_text(encoding="utf-8").split("## 付録B お知らせの一覧")[1].split("## 付録C")[0]
    return set(re.findall(r"^\| (N-[A-Z-]+) \|", text, flags=re.M))


def test_screen_notices_exist_in_appendix_b():
    known = appendix_b_codes()
    assert {"N-RUN-DIVERGED", "N-RUN-NOTE-STEP", "N-MISSING"} <= known
    for code, notices in SCREEN_NOTICES.items():
        assert code in MESSAGES
        assert set(notices) <= known, code


def test_basic_design_table_matches_the_code_table():
    """基本設計書 10.4 の対応表（番号 → お知らせ）が SCREEN_NOTICES と同じ。"""
    text = BASIC.read_text(encoding="utf-8").split("### 10.4")[1].split("\n## ")[0]
    rows = {}
    for line in text.splitlines():
        m = re.match(r"^\| `([A-Z_]+)` \| (.*?) \|", line)
        if m and m.group(1) in MESSAGES:
            rows[m.group(1)] = tuple(re.findall(r"N-[A-Z-]+", m.group(2)))
    assert rows == SCREEN_NOTICES


def test_render_note_line_for_cli_and_csv():
    from circuit_sim.simulate import render_note_line
    note = Message("ESR_OMITTED")
    assert render_note_line(note) == f"  [ESR_OMITTED] 注意: {note}"
    english = render_note_line(note, "CSV_NOTE", "en")
    assert english.startswith("# [ESR_OMITTED] Warning: C_esr (equivalent series")
    assert render_note_line(note, "CSV_NOTE") == f"# [ESR_OMITTED] 注意: {note}"


def test_values_and_names_format():
    assert f"{Value(1.5e-6, 's'):.3g}" == "1.5e-06" and f"{Value(2.0, 's', '2.000')}" == "2.000"
    assert f"{Names(['a', 'b'])}" == "a, b" and f"{Names(['a', 'b']):、}" == "a、b"
    assert f"{Names(['a']):repr}" == "['a']"
    assert Value(float("nan"), "V").to_json() == {"value": "nan", "unit": "V"}        # JSON に書ける
    assert json.loads(json.dumps(Value(np.float64(1.25), "A").to_json()))["value"] == 1.25
