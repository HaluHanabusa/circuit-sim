"""E1g の独立検証（基準7〜11、13）: 信号の情報、内部の信号、表示に使う値、出典、層A の欄、注意の行き先。

期待値は、画面仕様（docs/design/screens-v0.1.md）の付録A の表を、この試験が自分で読んで作る（実装の定数を
写さない）。文書の形が変わって表を読めなくなったら、読めないこと自体で落ちる（黙って通らない）。

  基準9   すべての回路で、結果の信号ごとに単位と正の向きが返る。降圧は付録A.4 の信号の表と 1 対 1。
  基準10  結果の名前が「表示する」と「内部」に漏れなく分かれる。表示する信号は付録A の表と 1 対 1。
  基準11  r_on が表示定義から取れ、計算に使った値と同じ（波形から逆算した値とも合う）。
  基準8   出典の 手入力 と manual は同じ結果で、書き出すと 手入力。
  基準13  層A の missing と targets は予約（読まない）。説明が、仕様の説明と基本設計書にある。
  基準7   C_esr を省いた降圧の注意が、Result.notes とコマンドラインの表示に番号つきで出る
          （CSV と注意のファイルは、D-075 の形を確かめる test_e1g_review_targets.py）。
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import re
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Spec, expand, simulate
from circuit_sim.__main__ import main as cli_main
from circuit_sim.templates import TEMPLATE_DISPLAY, TEMPLATE_ITEMS

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

ROOT = Path(__file__).resolve().parents[2]
q = hp.q
UNIT = {"V": "V", "A": "A", "なし": ""}

# ------------------------------------------------------------------ 画面仕様の表を読む


def section(start: str, end: str) -> str:
    doc = (ROOT / "docs" / "design" / "screens-v0.1.md").read_text(encoding="utf-8")
    a = doc.index(start)
    return doc[a:doc.index(end, a + 1)]


def table_rows(text: str, header_cell: str) -> list[list[str]]:
    """見出しの最初の欄が header_cell の表の、データの行（欄の並び）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"| {header_cell} |"):
            rows = []
            for row in lines[i + 2:]:                              # 見出しと区切りの次から
                if not row.strip().startswith("|"):
                    break
                rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
            return rows
    raise AssertionError(f"表「{header_cell}」が見つからない（文書の形が変わったら、この読み方を直す）")


def signal_table(text: str):
    """信号の表 → [(名前、単位、既定で表示、欄の残り)]。"""
    after = text[text.index("**信号。**"):]
    rows = table_rows(after, "番号（色）")
    assert rows, "信号の表が空"
    return [(r[1].strip("`"), UNIT[r[2]], r[3] == "する", r[4:]) for r in rows]


A2 = lambda: section("### A.2 ", "### A.3 ")       # noqa: E731
A3 = lambda: section("### A.3 ", "### A.4 ")       # noqa: E731
A4 = lambda: section("### A.4 ", "### A.5 ")       # noqa: E731


def buck_tables():
    rows = signal_table(A4())
    shown = {
        "switching": [(n, u, s) for n, u, s, rest in rows if rest[0] == "出る"],
        "average": [(n, u, s) for n, u, s, rest in rows if rest[1] == "出る"],
    }
    internal = {}
    for row in table_rows(A4(), "内部の信号"):
        for name in re.findall(r"`([^`]+)`", row[0]):
            internal[name] = row[1]                                # スイッチング / 平均 / 両方
    return shown, internal


def test_the_tables_were_read():
    """読み方の確かめ: 付録A.4 の表から、名前と数が期待どおりに取れている。"""
    shown, internal = buck_tables()
    assert [n for n, _, _ in shown["switching"]] == [
        "v(out)", "i(L1)", "v(sw)", "cmd(duty)", "gate(q1)", "gate(q2)", "v(in)", "i(V1)"]
    assert [n for n, _, _ in shown["average"]] == ["v(out)", "i(L1)", "v(sw)", "cmd(duty)", "i(Vleg)"]
    assert set(internal) == {"on(S1)", "on(S2)", "v(ld)", "gate(q_load)", "cmd(load)", "on(S3)", "v(cap)",
                             "v(sw_avg)", "i(Rleg)"}
    assert len(signal_table(A2())) == 3 and len(signal_table(A3())) == 5


# ------------------------------------------------------------------ 基準9・10: RC、RLC

@pytest.mark.parametrize("template,table,plant", [
    ("rc_lowpass", A2, hp.RC_PLANT), ("rlc_series", A3, hp.RLC_PLANT)])
def test_basic_templates_match_the_signal_tables(template, table, plant):
    expected = [(n, u, s) for n, u, s, _ in signal_table(table())]
    display = TEMPLATE_DISPLAY[template]
    assert [(s.name, s.unit, s.shown) for s in display.signals[None]] == expected
    assert display.fidelities == () and display.default_fidelity is None       # モデルは選べない
    res = expand(hp.t0_spec(template, plant)).run()
    info = res.signal_info
    assert list(info) == [n for n, _, _ in expected]                           # 並びも表の順
    assert set(info) == set(res.names)
    assert [(n, info[n].unit, info[n].shown) for n in res.display_names] == expected
    assert res.internal_names == []
    for name, s in info.items():
        assert res.unit_of(name) == s.unit
        if s.kind == "element_current":
            assert s.positive == tuple(res.netlist.component(s.element).nodes)


def test_basic_templates_current_directions_agree_with_the_waveforms():
    """向きの説明（付録A.2、A.3）どおりの符号: i(V1) は電源の中を ＋ から − へが正（送り出すと負）、
    i(L1) は a から out へが正。"""
    rc = expand(hp.t0_spec("rc_lowpass", hp.RC_PLANT)).run()
    assert rc.signal_info["i(V1)"].positive == ("in", "0")
    assert rc["i(V1)"][1:50].max() < 0                        # 充電の間、電源は電流を送り出している
    rlc = expand(hp.t0_spec("rlc_series", hp.RLC_PLANT)).run()
    assert rlc.signal_info["i(L1)"].positive == ("a", "out")
    assert rlc["i(L1)"][1:50].min() > 0
    assert np.abs(rlc["i(V1)"] + rlc["i(L1)"]).max() < 1e-9   # 直列なので、電源の電流は符号が逆


# ------------------------------------------------------------------ 基準9・10: 降圧（付録A.4）

def test_buck_display_definition_matches_the_signal_table():
    shown, _ = buck_tables()
    display = TEMPLATE_DISPLAY["buck_voltage_mode"]
    assert display.fidelities == ("switching", "average") and display.default_fidelity == "switching"
    for fidelity in ("switching", "average"):
        assert [(s.name, s.unit, s.shown) for s in display.signals[fidelity]] == shown[fidelity]
        assert not any(s.internal for s in display.signals[fidelity])


COMBOS = list(itertools.product(("switching", "average"), (None, 0.0, 0.02), (None, 3.0, 12.0),
                                (False, True), (True, False)))


@pytest.mark.parametrize("fidelity,esr,load,ref_step,with_tf", COMBOS)
def test_buck_result_names_split_into_shown_and_internal_exactly_as_the_tables(
        fidelity, esr, load, ref_step, with_tf):
    """モデル × C_esr（無し、0、あり）× 負荷ステップ（無し、重く、軽く）× 指令値のステップ × Tf。72 通り。"""
    shown, internal_table = buck_tables()
    plant, control = {}, {}
    if esr is not None:
        plant["C_esr"] = q(esr, "ohm")
    if load is not None:
        plant.update(R_load_step=q(load, "ohm"), t_load_step=q(0.2, "ms"))
    if ref_step:
        control.update(Vref_step=q(10, "V"), t_ref_step=q(0.1, "ms"))
    if not with_tf:
        control["Tf"] = q(None, "s")
    ex = expand(hp.buck_spec(fidelity, plant=plant, control=control, sim={"t_end": 4e-4}))
    res = ex.run()
    info = res.signal_info
    # 漏れなく、重なりなく分かれる
    assert set(info) == set(res.names) and len(info) == len(res.names)
    assert set(res.display_names) | set(res.internal_names) == set(res.names)
    assert not set(res.display_names) & set(res.internal_names)
    # 表示する信号: 表の「出る」と、名前・単位・既定の表示・並びが同じ
    assert [(n, info[n].unit, info[n].shown) for n in res.display_names] == shown[fidelity]
    # 内部の信号: 内部の信号の表から、この組み合わせで出るはずのもの
    expected = {"on(S1)", "on(S2)"} if fidelity == "switching" else {"v(sw_avg)"}
    if load is not None:
        expected |= {"v(ld)", "gate(q_load)", "cmd(load)", "on(S3)"}
    if esr:
        expected |= {"v(cap)"}
    assert set(res.internal_names) == expected
    assert expected <= set(internal_table)
    for name in expected:
        model = internal_table[name]
        assert model == "両方" or model == {"switching": "スイッチング", "average": "平均"}[fidelity], name
        assert info[name].shown is False and info[name].internal is True
    # 単位と向き
    for name, s in info.items():
        assert s.unit == {"node_voltage": "V", "element_current": "A"}.get(s.kind, ""), name
        assert res.unit_of(name) == s.unit
        if s.kind == "element_current":
            assert s.positive == tuple(res.netlist.component(s.element).nodes), name
    # 展開の時点の情報（計算の前に画面が使う）も、結果の名前と同じ集まり
    assert {s.name for s in ex.signals} == set(res.names)


def test_a_name_the_template_did_not_declare_is_treated_as_internal():
    """テンプレートが決め忘れた名前は、内部の信号として扱う（黙って画面に足さない。画面仕様 付録A.4）。"""
    ex = expand(hp.buck_spec("average", sim={"t_end": 2e-4}))
    ex.signals = [s for s in ex.signals if s.name not in ("v(sw)", "v(sw_avg)")]
    res = ex.run()
    assert set(res.signal_info) == set(res.names)
    for name in ("v(sw)", "v(sw_avg)"):
        assert res.signal_info[name].internal and not res.signal_info[name].shown
        assert name in res.internal_names and name not in res.display_names
        assert res.unit_of(name) == "V"                           # 単位は、決め忘れても返る
    assert res.display_names == ["v(out)", "i(L1)", "cmd(duty)", "i(Vleg)"]


def test_buck_current_directions_agree_with_the_waveforms():
    sw = expand(hp.buck_spec("switching", sim={"t_end": 3e-3})).run()
    assert sw.signal_info["i(L1)"].positive == ("sw", "out") and sw["i(L1)"][-2000:].mean() > 1.5
    assert sw.signal_info["i(V1)"].positive == ("in", "0") and sw["i(V1)"][-2000:].mean() < -0.5
    avg = expand(hp.buck_spec("average", sim={"t_end": 3e-3})).run()
    assert avg.signal_info["i(Vleg)"].positive == ("sw_avg", "0")
    assert np.abs(avg["i(Vleg)"] + avg["i(L1)"]).max() < 1e-9          # 付録A.4: −i(L1) と同じ


def test_signals_of_a_circuit_built_by_hand_all_have_unit_and_direction():
    """テンプレートを通さない回路（エンジンの口）でも、結果の名前の全部に情報がある。"""
    res = simulate(hp.rectifier(), 2e-3, 1e-5)
    assert set(res.signal_info) == set(res.names) and len(res.names) > 15
    kinds = {s.kind for s in res.signal_info.values()}
    assert kinds == {"node_voltage", "element_current", "conduction"}
    for name, s in res.signal_info.items():
        assert s.unit == {"node_voltage": "V", "element_current": "A"}.get(s.kind, "")
        if s.kind == "element_current":
            assert s.positive == tuple(res.netlist.component(s.element).nodes)
        if s.kind == "conduction":
            assert res.netlist.component(s.element).type == "D" and set(np.unique(res[name])) <= {0.0, 1.0}
    res = simulate(hp.buck_diode(), 1e-4, gates=hp.gate())
    gate = res.signal_info["gate(g)"]
    assert gate.kind == "gate" and gate.unit == "" and gate.element == "S1" and not gate.internal


def test_item_table_matches_the_template_items():
    """付録A.4 の項目の表（位置、単位、必須、組の相手）と、エンジンの項目の定義。"""
    si = {"V": "V", "H": "H", "F": "F", "Ω": "ohm", "s": "s", "Hz": "Hz", "1/V": "1/V", "1/(V·s)": "1/(V·s)",
          "s/V": "s/V"}
    expected = []
    for row in table_rows(A4()[A4().index("**項目。**"):], "群"):
        position = row[1].strip("`")
        if position.startswith("simulation."):
            continue
        part, key = position.split(".")
        unit = "1" if row[3].startswith("なし") else si[row[3]]
        together = tuple(re.findall(r"`([^`]+)` と組", row[4]))
        expected.append((key, part, unit, row[4].startswith("必須"), together))
    assert len(expected) == 18
    items = TEMPLATE_ITEMS["buck_voltage_mode"]
    got = [(it.key, it.section, it.si_unit, it.required, it.together) for it in items]
    assert got == expected
    assert TEMPLATE_DISPLAY["buck_voltage_mode"].items == TEMPLATE_ITEMS["buck_voltage_mode"]


# ------------------------------------------------------------------ 基準11: 表示に使う値 r_on

def test_r_on_from_the_display_definition_is_the_value_used_in_the_computation():
    display = TEMPLATE_DISPLAY["buck_voltage_mode"]
    r_on = display.value("r_on")
    assert r_on.unit == "ohm" and r_on.elements == ("S1", "S2", "Rleg") and r_on.value > 0
    switching = expand(hp.buck_spec("switching", sim={"t_end": 2e-3}))
    average = expand(hp.buck_spec("average", sim={"t_end": 2e-3}))
    assert switching.netlist.component("S1").r_on == switching.netlist.component("S2").r_on == r_on.value
    assert average.netlist.component("Rleg").value == r_on.value
    # 波形から逆算する（素子の値を読まずに）。平均: v(sw_avg) − v(sw) = r_on·i(L1)
    res = average.run()
    use = np.abs(res["i(L1)"]) > 0.5
    from_average = np.median((res["v(sw_avg)"][use] - res["v(sw)"][use]) / res["i(L1)"][use])
    # スイッチング: S1 がオンの間、v(in) − v(sw) = r_on·i(S1)（切り替えの点を除く）
    res = switching.run()
    on = (res["on(S1)"] == 1) & (np.abs(res["i(L1)"]) > 0.5)
    on[1:] &= res["on(S1)"][:-1] == 1
    on[0] = False
    from_switching = np.median((res["v(in)"][on] - res["v(sw)"][on]) / res.i("S1")[on])
    assert from_average == pytest.approx(r_on.value, rel=1e-6)
    assert from_switching == pytest.approx(r_on.value, rel=1e-6)


def test_r_on_is_available_without_a_complete_spec_and_unknown_keys_are_coded_errors():
    with pytest.raises(ValueError):
        expand(hp.buck_spec(drop=("L",)))                    # 値が足りない仕様は展開できない
    assert TEMPLATE_DISPLAY["buck_voltage_mode"].value("r_on").value == 1e-3     # それでも表示定義から取れる
    with pytest.raises(KeyError) as info:
        TEMPLATE_DISPLAY["buck_voltage_mode"].value("r_off")
    assert info.value.code == "DISPLAY_NO_VALUE" and "r_on" in info.value.text("en")


# ------------------------------------------------------------------ 基準8: 出典

def with_source(source):
    d = hp.buck_dict("average", sim={"t_end": 5e-4})
    for part in ("plant", "control"):
        for key in d[part]:
            d[part][key]["source"] = source
    return Spec.model_validate_json(json.dumps(d))


def sources(spec_dict) -> set:
    return {spec_dict[part][key]["source"] for part in ("plant", "control") for key in spec_dict[part]}


@pytest.mark.parametrize("written", ["manual", "Manual", " MANUAL ", "手入力"])
def test_manual_is_read_as_te_nyuryoku_and_written_back_that_way(written):
    reference = hp.signature(expand(with_source("手入力")).run())
    spec = with_source(written)
    assert hp.signature(expand(spec).run()) == reference
    assert sources(json.loads(spec.model_dump_json())) == {"手入力"}
    assert sources(spec.model_dump()) == {"手入力"}
    assert spec.plant["L"].source == "手入力"


@pytest.mark.parametrize("written", ["p.3 表2", "manually", "手 入力", "記載なし", None, ""])
def test_other_sources_are_kept_as_written_and_never_change_the_result(written):
    reference = hp.signature(expand(with_source("手入力")).run())
    spec = with_source(written)
    assert sources(json.loads(spec.model_dump_json())) == {written}
    assert hp.signature(expand(spec).run()) == reference


# ------------------------------------------------------------------ 基準13: 層A の欄

@pytest.mark.parametrize("extra", [
    {"targets": {"overshoot": 5.0, "settling_time": 1e-3}},
    {"missing": ["L", "Kp", "no_such_item"]},
    {"targets": {"x": 1e300}, "missing": ["Vin"]},
])
def test_targets_and_missing_are_reserved_and_not_read(extra):
    base = hp.buck_dict("average", sim={"t_end": 5e-4})
    reference = hp.signature(expand(Spec.model_validate(base)).run())
    spec = Spec.model_validate_json(json.dumps({**base, **extra}))
    assert hp.signature(expand(spec).run()) == reference          # 結果も注意も変わらない
    written = json.loads(spec.model_dump_json())
    assert all(written[key] == value for key, value in extra.items())   # 中身は捨てずに持つ


def test_missing_values_are_judged_from_the_values_not_from_the_missing_field():
    d = hp.buck_dict("average")
    d["plant"]["L"]["value"] = None
    d["missing"] = []                                             # 欄は「足りないものは無い」と言っている
    with pytest.raises(ValueError) as info:
        expand(Spec.model_validate(d))
    assert info.value.code == "MISSING_VALUES" and info.value.names == ["L"]


def test_reserved_fields_are_described_in_the_spec_docstring_and_the_basic_design():
    docstring = Spec.__doc__
    design = (ROOT / "docs" / "design" / "basic-design.md").read_text(encoding="utf-8")
    for text in (docstring, design):
        assert "targets" in text and "missing" in text and "予約" in text
    assert "手入力" in design and "manual" in design


# ------------------------------------------------------------------ 基準7: テンプレートの注意の行き先

@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_template_notes_reach_the_result_and_the_command_line_with_codes(fidelity, tmp_path, capsys):
    spec = hp.buck_spec(fidelity, sim={"t_end": 3e-4})            # C_esr を省いた降圧
    ex = expand(spec)
    res = ex.run()
    codes = [n.code for n in res.notes]
    assert "ESR_OMITTED" in codes and "ESR_OMITTED" in [n.code for n in ex.notes]
    assert [n.code for n in ex.notes] == codes[:len(ex.notes)]    # 結果の注意の先頭が、展開の注意
    assert all(a is b for a, b in zip(ex.notes, res.notes, strict=False))
    path = tmp_path / "spec.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    assert cli_main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    shown = re.findall(r"^\s+\[([A-Z0-9_]+)\] 注意: (.+)$", captured.out, flags=re.M)
    assert [code for code, _ in shown] == codes                   # 番号つきで、同じ並び
    assert [text for _, text in shown] == [str(n) for n in res.notes]
    esr = [text for code, text in shown if code == "ESR_OMITTED"]
    assert len(esr) == 1 and "C_esr" in esr[0]
    # 2 回計算しても、展開の注意が重ならない
    assert [n.code for n in ex.run().notes] == codes


def test_csv_header_carries_units_and_dimensionless_is_1(tmp_path):
    """画面と同じ見出し（D-042、画面仕様 8.4）: 単位を角かっこで、無次元は [1]。"""
    res = expand(hp.buck_spec("switching", sim={"t_end": 1e-4})).run()
    res.to_csv(tmp_path / "all.csv", res.display_names, units=True)
    header = (tmp_path / "all.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == ("t [s],v(out) [V],i(L1) [A],v(sw) [V],cmd(duty) [1],gate(q1) [1],gate(q2) [1],"
                      "v(in) [V],i(V1) [A]")
    res.to_csv(tmp_path / "plain.csv")                            # 単位を付けない呼び方は、前と同じ見出し
    assert (tmp_path / "plain.csv").read_text(encoding="utf-8").splitlines()[0].startswith("t,v(")
