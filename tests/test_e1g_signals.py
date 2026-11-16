"""E1g の範囲3・4・6・7・8・9・10・12（受け入れ基準7〜11、13）。

- 信号の情報（基準9、10）: すべての回路で、結果の名前ごとに単位と正の向きが返り、
名前が「表示する」と「内部」に
  漏れなく分かれる。テンプレートが決めていない名前（内部とみなす名前）は無い。表示する信号は画面仕様の付録A の
  信号の表（A.2、A.3、A.4）と1対1。テンプレートが決めた情報は、エンジンが回路から作る情報と食い違わない。
- CSV（範囲4、基準7）: コマンドラインの --csv の見出しに単位（画面と同じ形）。注意は番号つきでデータの後ろ。
- テンプレートの注意（基準7）: C_esr を省いた降圧で、
Result.notes・コマンドラインの表示・CSV に番号つきで出る。
- 表示に使う値（基準11）: 降圧の r_on がテンプレートの表示定義から取れ、計算に使った値と一致する。
- 出典（基準8）: 手入力 と manual が同じ結果になり、書き出すと 手入力。
- 層A の欄（基準13）: targets と missing は予約で、読まない（結果を変えない）。
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Spec, expand
from circuit_sim.__main__ import main as cli_main
from circuit_sim.signals import KIND_UNIT, describe_signals
from circuit_sim.templates import TEMPLATE_DISPLAY

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "docs" / "design" / "screens-v0.1.md"
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"


def buck(fidelity="switching", esr=None, load_step=False, ref_step=False, t_end=0.3e-3):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    if esr is not None:
        d["plant"]["C_esr"] = {"value": esr, "unit": "ohm", "source": "手入力"}
    if load_step:
        d["plant"]["R_load_step"] = {"value": 3, "unit": "ohm"}
        d["plant"]["t_load_step"] = {"value": 0.1, "unit": "ms"}
    if ref_step:
        d["control"]["Vref_step"] = {"value": 10, "unit": "V"}
        d["control"]["t_ref_step"] = {"value": 0.15, "unit": "ms"}
    return expand(Spec.model_validate(d))


def t0(template, **plant):
    return expand(Spec.model_validate({"template": template, "plant": {
        k: {"value": v, "unit": u} for k, (v, u) in plant.items()}}))


RC = dict(Vin=(5, "V"), R=(1e3, "ohm"), C=(1e-6, "F"))
RLC = dict(Vin=(10, "V"), R=(1, "ohm"), L=(1e-3, "H"), C=(10e-6, "F"))

ALL_CIRCUITS = {"rc_lowpass": lambda: t0("rc_lowpass", **RC), "rlc_series": lambda: t0("rlc_series", **RLC)}
COMBINATIONS = itertools.product(("switching", "average"), (None, 0.0, 0.02), (False, True), (False, True))
for f, esr, ls, rs in COMBINATIONS:
    ALL_CIRCUITS[f"buck_{f}_esr{esr}_load{ls}_ref{rs}"] = (
        lambda f=f, esr=esr, ls=ls, rs=rs: buck(f, esr, ls, rs))


@pytest.mark.parametrize("case", list(ALL_CIRCUITS))
def test_every_result_name_is_either_shown_or_internal_and_declared_by_the_template(case):
    ex = ALL_CIRCUITS[case]()
    res = ex.run()
    names = set(res.names)
    assert set(res.signal_info) == names
    display, internal = set(res.display_names), set(res.internal_names)
    assert display | internal == names and not (display & internal)
    declared = {s.name for s in ex.signals}
    assert names - declared == set(), "テンプレートが決めていない名前（内部とみなされる）"
    gates = {c.gate: [c.id] for c in ex.netlist.components if c.type == "S"}
    engine = describe_signals(ex.netlist, res.names, gates)
    for name, info in res.signal_info.items():
        assert info.unit == KIND_UNIT[info.kind] and info.unit in ("V", "A", "")
        e = engine[name]
        assert (info.kind, info.unit, info.node, info.element, info.positive, info.command, info.gate) == (
            e.kind, e.unit, e.node, e.element, e.positive, e.command, e.gate), name
        if info.kind == "element_current":
            assert info.positive == tuple(ex.netlist.component(info.element).nodes)
        assert len(res[name]) == len(res.t)


def table_rows(section_title: str, end: str) -> list[list[str]]:
    text = SCREENS.read_text(encoding="utf-8").split(section_title)[1].split(end)[0]
    table = text.split("**信号。**")[1].split("\n\n")[1]
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in table.splitlines()
            if line.startswith("|") and not line.startswith("|---") and "番号（色）" not in line]


@pytest.mark.parametrize("template, section, end, plant", [
    ("rc_lowpass", "### A.2 RCローパス", "### A.3", RC),
    ("rlc_series", "### A.3 直列RLC", "### A.4", RLC),
])
def test_rc_and_rlc_signals_match_appendix_a_one_to_one(template, section, end, plant):
    """名前と並び、単位、既定で表示するか。正の向きは説明の「a から out へ」「V1 の中を ＋ から −」と同じ。"""
    rows = table_rows(section, end)
    ex = t0(template, **plant)
    res = ex.run()
    assert res.display_names == [r[1].strip("`") for r in rows]
    for name, r in zip(res.display_names, rows, strict=True):
        info = res.signal_info[name]
        assert info.unit == r[2] and info.shown == (r[3] == "する") and not info.internal
        m = re.search(r"(\w+) から (\w+) へ流れる向きが正", r[4])
        if m:
            assert info.positive == m.groups()
        elif info.kind == "element_current":
            assert "の中を ＋ から − へ流れる向きが正" in r[4] and info.positive == ("in", "0")
    assert res.internal_names == []
    assert [s.name for s in TEMPLATE_DISPLAY[template].signals[None]] == res.display_names


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_buck_display_signals_match_a4_and_the_display_definition(fidelity):
    rows = table_rows("### A.4 降圧コンバータ", "### A.5")
    column = {"switching": 4, "average": 5}[fidelity]
    expected = [r[1].strip("`") for r in rows if r[column] == "出る"]
    for esr, ls, rs in itertools.product((None, 0.02), (False, True), (False, True)):
        res = buck(fidelity, esr, ls, rs).run()
        assert res.display_names == expected
    assert [s.name for s in TEMPLATE_DISPLAY["buck_voltage_mode"].signals[fidelity]] == expected


def test_buck_internal_signals_match_the_a4_internal_table():
    """付録A.4 の「内部の信号」の表に挙げた名前と、結果の内部の信号が同じ（i(Rleg) は結果に無い）。"""
    text = SCREENS.read_text(encoding="utf-8").split("| 内部の信号 | モデル | 出さない理由 |")[1]
    text = text.split("\n\n")[0]
    first = [line.split("|")[1] for line in text.splitlines()
             if line.strip().startswith("|") and "---" not in line]
    listed = set(re.findall(r"`([a-z]+\([A-Za-z0-9_]+\))`", "\n".join(first)))
    found = set()
    for f, esr, ls in itertools.product(("switching", "average"), (None, 0.02), (False, True)):
        found |= set(buck(f, esr, ls).run().internal_names)
    assert found == listed - {"i(Rleg)"}


# ------------------------------------------------------------------ CSV とテンプレートの注意


def write_spec(tmp_path, d) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return path


def test_template_note_appears_in_notes_cli_and_csv_with_its_code(tmp_path, capsys):
    """基準7: C_esr を省いた降圧（同梱の例）。"""
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "switching", "t_end": 0.2e-3}
    ex = expand(Spec.model_validate(d))
    res = ex.run()
    assert [n.code for n in res.notes].count("ESR_OMITTED") == 1
    assert res.notes[: len(ex.notes)] == ex.notes                    # 展開の注意が先頭に入る
    csv_path = tmp_path / "out.csv"
    assert cli_main([str(write_spec(tmp_path, d)), "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^  \[ESR_OMITTED\] 注意: C_esr（出力コンデンサの等価直列抵抗）の値が無い", out, re.M)
    assert out.count("注意:") == len(res.notes)                      # 展開の注意を二重に出さない
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t [s],v(out) [V],i(L1) [A]"
    notes = [line for line in lines if line.startswith("#")]
    assert notes == [f"# [{n.code}] 注意: {n}" for n in res.notes]
    assert any("[ESR_OMITTED]" in line for line in notes)
    assert all(not line.startswith("#") for line in lines[: len(res.t) + 1])     # 注意はデータの後ろ
    data = np.loadtxt(csv_path, delimiter=",", skiprows=1)            # # の行は読み飛ばせる
    assert data.shape == (len(res.t), 3) and np.array_equal(data[:, 1], res["v(out)"])
    raw = csv_path.read_bytes()                                       # 行の終わりは、注意の行も同じ（CR LF）
    assert raw.endswith(b"\r\n") and raw.count(b"\n") == raw.count(b"\r\n") == len(lines)


def test_csv_header_units_for_dimensionless_and_time(tmp_path):
    res = buck().run()
    path = tmp_path / "all.csv"
    res.to_csv(path, ["v(out)", "i(L1)", "cmd(duty)", "gate(q1)"], units=True)
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "t [s],v(out) [V],i(L1) [A],cmd(duty) [1],gate(q1) [1]")
    res.to_csv(path, ["v(out)"])                                     # 既定は単位なし（E0 の形のまま）
    assert path.read_text(encoding="utf-8").splitlines()[0] == "t,v(out)"


def test_rc_cli_csv_has_units_and_no_notes_when_there_are_none(tmp_path, capsys):
    plant = {k: {"value": v, "unit": u, "source": "手入力"} for k, (v, u) in RC.items()}
    d = {"template": "rc_lowpass", "plant": plant}
    csv_path = tmp_path / "rc.csv"
    assert cli_main([str(write_spec(tmp_path, d)), "--csv", str(csv_path)]) == 0
    assert "注意" not in capsys.readouterr().out
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t [s],v(out) [V],i(V1) [A]" and not any(line.startswith("#") for line in lines)


# ------------------------------------------------------------------ 表示に使う値


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_on_resistance_from_the_display_definition_matches_the_netlist(fidelity):
    """基準11: r_on は仕様の値によらず表示定義から取れる。

    計算に使った S1・S2（平均モデルでは Rleg）の値と同じ。"""
    r_on = TEMPLATE_DISPLAY["buck_voltage_mode"].value("r_on")
    assert r_on.unit == "ohm" and set(r_on.elements) == {"S1", "S2", "Rleg"}
    net = buck(fidelity).netlist
    used = [c.r_on for c in net.components if c.id in ("S1", "S2")] + [
        c.value for c in net.components if c.id == "Rleg"]
    assert used and all(v == r_on.value for v in used)


def test_display_definition_has_models_and_their_defaults():
    d = TEMPLATE_DISPLAY["buck_voltage_mode"]
    assert d.fidelities == ("switching", "average") and d.default_fidelity == "switching"
    assert [i.key for i in d.items][:3] == ["Vin", "L", "C"]
    assert TEMPLATE_DISPLAY["rc_lowpass"].fidelities == () and TEMPLATE_DISPLAY["rc_lowpass"].values == ()


# ------------------------------------------------------------------ 層A の欄


def rc_spec(source=None, **extra):
    extra_source = {"source": source} if source is not None else {}
    d = {"template": "rc_lowpass", "plant": {k: {"value": v, "unit": u, **extra_source}
                                            for k, (v, u) in RC.items()}}
    d.update(extra)
    return Spec.model_validate(d)


@pytest.mark.parametrize("written", ["manual", " Manual ", "MANUAL"])
def test_manual_and_tenyuryoku_are_the_same_and_saved_as_tenyuryoku(written):
    """基準8: manual と 手入力 は同じに扱う（計算の結果も同じ）。書き出すと 手入力。"""
    a, b = rc_spec("手入力"), rc_spec(written)
    assert a == b
    assert all(q.source == "手入力" for q in b.plant.values())
    assert json.loads(b.model_dump_json())["plant"]["R"]["source"] == "手入力"
    ra, rb = expand(a).run(), expand(b).run()
    assert np.array_equal(ra.x, rb.x) and [str(n) for n in ra.notes] == [str(n) for n in rb.notes]


def test_other_sources_are_kept_as_written():
    s = rc_spec("p.3 表2")
    assert s.plant["R"].source == "p.3 表2" and rc_spec(None).plant["R"].source is None
    assert rc_spec("manually typed").plant["R"].source == "manually typed"


def test_targets_and_missing_are_reserved_and_not_read():
    """基準13: targets（設計目標）と missing は予約。中身があっても結果は変わらず、注意も出ない。

    不足は実際の値から判断する（missing に名前があっても値があれば計算し、missing が空でも
    値が無ければ不足）。"""
    base = expand(rc_spec("手入力")).run()
    other = expand(rc_spec("手入力", targets={"overshoot": 5.0}, missing=["R", "C"])).run()
    assert np.array_equal(base.x, other.x) and [str(n) for n in base.notes] == [str(n) for n in other.notes]
    d = {"template": "rc_lowpass", "missing": [], "plant": {"Vin": {"value": 5, "unit": "V"},
                                                          "R": {"value": None, "unit": "ohm"}}}
    with pytest.raises(ValueError) as info:
        expand(Spec.model_validate(d))
    assert info.value.code == "MISSING_VALUES" and info.value.names == ["R", "C"]
    assert "予約" in Spec.__doc__ and "targets" in Spec.__doc__ and "missing" in Spec.__doc__
