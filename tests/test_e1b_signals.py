"""E1b: 降圧コンバータのテンプレートが返す信号の情報が、画面仕様の付録A.4 の信号の表と1対1で合うこと。

エンジンは文言を持たない（決まり8）。返すのは構造化した情報（名前、単位、既定で表示するか、種類、節点、
素子ID、正の向き、指令、ゲート）だけで、説明の文言は画面の辞書（signal.buck_voltage_mode.<信号>.desc）が持つ。
表は docs/design/screens-v0.1.md から読む（表が変われば、この試験が落ちる）。
"""
import json
import re
from pathlib import Path

import pytest

from circuit_sim import Spec, expand
from circuit_sim.templates import SignalInfo

ROOT = Path(__file__).resolve().parents[1]
SCREENS = ROOT / "docs" / "design" / "screens-v0.1.md"
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"
# 表の列: 0 番号、1 名前、2 単位、3 既定で表示、4 スイッチング、5 平均、6 測定点
MODELS = {"switching": 4, "average": 5}


def a4_section() -> str:
    text = SCREENS.read_text(encoding="utf-8")
    return text.split("### A.4 降圧コンバータ")[1].split("### A.5")[0]


def a4_signal_rows() -> list[list[str]]:
    table = a4_section().split("**信号。**")[1].split("\n\n")[1]
    rows = []
    for line in table.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "番号（色）" in line:
            continue
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def expanded(fidelity: str):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": 50e-6}
    return expand(Spec.model_validate(d))


def test_signal_table_is_found():
    rows = a4_signal_rows()
    assert [r[1] for r in rows] == [
        "`v(out)`", "`i(L1)`", "`v(sw)`", "`cmd(duty)`", "`gate(q1)`", "`gate(q2)`", "`v(in)`", "`i(V1)`",
        "`i(Vleg)`"]
    assert all(len(r) == 7 for r in rows)


@pytest.mark.parametrize("fidelity", list(MODELS))
def test_signals_match_the_a4_table_one_to_one(fidelity):
    """名前と並び、単位、既定で表示するか、モデルごとの有無が表と同じ。"""
    rows = [r for r in a4_signal_rows() if r[MODELS[fidelity]] == "出る"]
    ex = expanded(fidelity)
    assert [s.name for s in ex.signals] == [r[1].strip("`") for r in rows]
    for s, r in zip(ex.signals, rows, strict=True):
        assert s.unit == ("" if r[2] == "なし" else r[2]), s.name
        assert s.shown == (r[3] == "する"), s.name


@pytest.mark.parametrize("fidelity", list(MODELS))
def test_signal_measurement_points_match_the_a4_table_and_the_netlist(fidelity):
    """測定点: 表の「図の測定点」と、構造化した情報と、展開した回路（層B）の三つが合う。

    節点の電圧 → 「節点 X」。素子の電流 → 「X の…」で、正の向きは「（a → b が正）」か「X の中を ＋ → −」。
    正の向きは、その素子の nodes（電流の正は素子の中を n+ から n− へ）と同じ。
    指令 → 「PID → PWM」で、PID の出力の名前。ゲート → 「X のゲート」で、その素子のゲート信号の名前。
    """
    rows = {r[1].strip("`"): r for r in a4_signal_rows() if r[MODELS[fidelity]] == "出る"}
    ex = expanded(fidelity)
    nodes = {n for c in ex.netlist.components for n in c.nodes}
    for s in ex.signals:
        point = rows[s.name][6]
        if s.kind == "node_voltage":
            assert point == f"節点 {s.node}" and s.node in nodes
        elif s.kind == "element_current":
            comp = ex.netlist.component(s.element)
            assert point.startswith(f"{s.element} の")
            assert s.positive == comp.nodes, s.name
            arrow = re.search(r"（(\w+) → (\w+) が正）", point)
            if arrow:
                assert s.positive == arrow.groups(), s.name
            else:
                assert f"{s.element} の中を ＋ → −" in point, s.name
        elif s.kind == "command":
            assert "PID → PWM" in point
            assert s.command in ex.controllers[0].outputs
        else:
            assert point == f"{s.element} のゲート"
            assert ex.netlist.component(s.element).gate == s.gate
    res = ex.run()
    for s in ex.signals:                                             # 結果から名前で取り出せる
        assert len(res[s.name]) == len(res.t)


def test_every_signal_has_a_description_key_in_the_dictionary_table():
    section = a4_section()
    names = {s.name for f in MODELS for s in expanded(f).signals}
    for name in names:
        assert f"`signal.buck_voltage_mode.{name}.desc`" in section, name


def test_signal_information_carries_no_display_text():
    """決まり8: エンジンが返す信号の情報に、画面の文言（日本語の説明）が入っていない。

    文字列は識別子と単位だけ。"""
    for f in MODELS:
        for s in expanded(f).signals:
            texts = [s.name, s.unit, s.kind, s.node, s.element, s.command, s.gate, *(s.positive or ())]
            assert all(t is None or t.isascii() for t in texts), s


def test_signal_info_rejects_inconsistent_fields():
    with pytest.raises(ValueError):
        SignalInfo("v(out)", "V", True, "node_voltage", node="sw")
    with pytest.raises(ValueError):
        SignalInfo("i(L1)", "A", True, "element_current", element="L1")       # 正の向きが無い
    with pytest.raises(ValueError):
        SignalInfo("gate(q1)", "", False, "gate", gate="q1")                  # 動かす素子が無い
