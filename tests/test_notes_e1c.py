"""E1c: 注意（notes）と上限の判定の残課題（範囲2〜8、10）。

期待値は、この中で式（RC・RLCのステップ応答）から作るか、回路を別の形に組み替えた計算
（スイッチを抵抗に置き換えた回路）と比べる。
"""
from __future__ import annotations

import importlib
import json
import math
import re
import tracemalloc

import numpy as np
import pytest

from circuit_sim import DC, Component, Netlist, Pwm, Spec, Step, expand, simulate
from circuit_sim.__main__ import main as cli_main

sim = importlib.import_module("circuit_sim.simulate")
TOL = 0.01


def rc_net() -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=1.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])


def buck(inductance=47e-6, cap=100e-6, load=6.0, v0=0.0, replace=False, freewheel=True) -> Netlist:
    """降圧回路。replace=True ならスイッチをオン抵抗、ダイオードをオフ抵抗の抵抗に置き換える。"""
    if replace:
        s1 = Component(id="S1", type="R", nodes=("in", "sw"), value=1e-3)
        d1 = Component(id="D1", type="R", nodes=("0", "sw"), value=1e6)
    else:
        s1 = Component(id="S1", type="S", nodes=("in", "sw"), gate="q1")
        d1 = Component(id="D1", type="D", nodes=("0", "sw"))
    comps = [Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=24.0)), s1]
    if freewheel:
        comps.append(d1)
    comps += [Component(id="L1", type="L", nodes=("sw", "out"), value=inductance)]
    if cap:
        comps.append(Component(id="C1", type="C", nodes=("out", "0"), value=cap, ic=v0))
    comps.append(Component(id="R1", type="R", nodes=("out", "0"), value=load))
    return Netlist(components=comps)


GATES = [Pwm(name="q1", frequency=1e5, duty=0.5)]        # 1周期 200 分割で h = 50 ns


# ------------------------------------------------------------- 範囲2: スイッチの経路の注意

def test_switched_circuit_warns_about_unresolved_lc_like_the_resistor_version():
    """周期 199 ns の LC（1 µH、1 nF、負荷 1 kΩ）を含む降圧回路を h = 50 ns で計算すると、振動の注意が出る。
    スイッチを抵抗に置き換えた回路（スイッチの無い経路）と、文面まで同じ。"""
    switched = simulate(buck(1e-6, 1e-9, 1e3), 2e-4, gates=GATES)
    resistor = simulate(buck(1e-6, 1e-9, 1e3, replace=True), 2e-4, 5e-8)
    assert len(switched.notes) == 1 and "振動" in switched.notes[0] and "周期 1.99e-07 s" in switched.notes[0]
    assert switched.notes == resistor.notes


def test_gates_with_a_linear_circuit_give_the_same_notes():
    """スイッチの無い回路にゲート信号を渡した場合（スイッチの経路に入る）も、同じ注意になる。"""
    with_gates = simulate(buck(1e-6, 1e-9, 1e3, replace=True), 2e-4, gates=GATES)
    plain = simulate(buck(1e-6, 1e-9, 1e3, replace=True), 2e-4, 5e-8)
    assert with_gates.notes == plain.notes != []


@pytest.mark.parametrize("load", [6.0, 100.0])
def test_off_resistance_modes_do_not_cause_notes(load):
    """両方オフの区間には、オフ抵抗とインダクタの時定数 L / r_off（約 50 ps）の成分がある。
    これはモデルの都合で、切り替えの直後の後退オイラーが消すので、注意にしない（基準の回路は注意なし）。"""
    res = simulate(buck(load=load), 2e-3, gates=GATES)
    if load == 100.0:
        assert (res["on(S1)"] + res["on(D1)"]).min() == 0                   # 両方オフの区間がある
    assert res.notes == []


# ------------------------------------------------------------- 範囲4: 減衰の境目のすぐ下

@pytest.mark.parametrize("ratio", [0.6, 1.0, 1.5, 1.99])
def test_rc_just_below_the_damping_threshold_is_accurate(ratio):
    """h が時定数の 0.6〜2 倍でも、格子点の値は真値と 0.5% 以内（減衰ステップを入れる。注意は出ない）。"""
    tau = 1e-3
    res = simulate(rc_net(), 400 * ratio * tau, ratio * tau)
    assert np.max(np.abs(res["v(out)"] - (1 - np.exp(-res.t / tau)))) < 0.005
    assert res.notes == []


def test_rc_below_half_tau_is_trapezoidal_only_and_second_order():
    """h ≤ τ/2 では減衰ステップを入れず、台形則だけで2次精度（刻み半分で誤差が約 1/4）。"""
    tau, errors = 1e-3, []
    for h in (4e-4, 2e-4, 1e-4):
        res = simulate(rc_net(), 5e-3, h)
        errors.append(np.max(np.abs(res["v(out)"] - (1 - np.exp(-res.t / tau)))))
    assert 3.6 < errors[0] / errors[1] < 4.4 and 3.6 < errors[1] / errors[2] < 4.4


# ------------------------------------------------------------- 範囲6: 減衰を切ったときの文面

def test_note_says_damping_is_off_when_it_is_off():
    res = simulate(rc_net(), 4.0, 1e-2, damping_steps=0)
    assert len(res.notes) == 1
    assert "damping_steps=0" in res.notes[0] and "減衰させている" not in res.notes[0]
    assert "減衰させている" in simulate(rc_net(), 4.0, 1e-2).notes[0]


# ------------------------------------------------------------- 範囲8: インダクタ電流の遮断

def test_interrupted_reverse_current_is_reported():
    """出力を 30 V に充電した降圧回路: 逆向きの電流がスイッチのオフで行き場を失う。

    注意には、時刻と素子と、記録された見かけの電圧の大きさを書く。
    """
    res = simulate(buck(v0=30.0), 1e-4, gates=GATES)
    assert len(res.notes) == 1
    note = res.notes[0]
    assert "L1" in note and "t=5e-06 s" in note and "S1, D1" in note and "還流ダイオード" in note
    assert f"{res['v(sw)'].max():.3g} V" in note                       # 記録された見かけの電圧


def test_missing_freewheeling_diode_is_reported():
    res = simulate(buck(freewheel=False, cap=None), 1e-4, gates=GATES)
    assert any("L1" in n and "オフのスイッチやダイオード（S1）" in n for n in res.notes)


@pytest.mark.parametrize("inductance", [47e-6, 2.5e-3, 10e-3])
def test_ordinary_turn_off_at_zero_current_is_not_reported(inductance):
    """不連続導通でダイオードが電流 0 付近で切れるのは遮断ではない（L = 10 mH まで注意なし）。"""
    res = simulate(buck(inductance, load=100.0 * inductance / 47e-6), 2e-3, gates=GATES)
    assert res.notes == []


# ------------------------------------------------------------- 範囲3・5: テンプレートの既定と振動

def rlc_spec(r, simulation=None):
    plant = {"Vin": (10.0, "V"), "R": (r, "ohm"), "L": (1e-3, "H"), "C": (10e-6, "F")}
    return Spec.model_validate({"template": "rlc_series", "simulation": simulation or {},
                                "plant": {k: {"value": v, "unit": u} for k, (v, u) in plant.items()}})


def rlc_error(res, r) -> float:
    ind, cap = 1e-3, 10e-6
    a, w0 = r / (2 * ind), 1 / math.sqrt(ind * cap)
    wd = math.sqrt(w0 * w0 - a * a)
    v = 10.0 * (1 - np.exp(-a * res.t) * (np.cos(wd * res.t) + a / wd * np.sin(wd * res.t)))
    return float(np.max(np.abs(res["v(out)"] - v)) / 10.0)


@pytest.mark.parametrize("r,simulation", [
    (1.0, {"t_end": 1.0}), (1.0, {"t_end": 0.5}), (0.01, None), (1e-5, None),
])
def test_phase_drift_is_noted_and_the_suggested_step_fixes_it(r, simulation):
    """誤差が 1% を超える既定には、精度が落ちることと直し方（simulation.h）の注意が出る。
    勧められた h を指定すると 1% 以内になる。"""
    ex = expand(rlc_spec(r, simulation))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    assert rlc_error(res, r) > TOL
    phase = [n for n in ex.notes if "位相" in n]
    assert len(phase) == 1 and "精度が落ちる" in phase[0] and "振動" in phase[0]
    estimate = float(re.search(r"約 ([0-9.]+)% ずれる", phase[0]).group(1)) / 100
    assert estimate == pytest.approx(rlc_error(res, r), rel=0.2)         # 見積もりは実測と 2割以内
    h_fine = float(re.search(r"simulation\.h を ([0-9.e+-]+) s 以下", phase[0]).group(1))
    fixed = dict(simulation or {}, h=h_fine)
    ex2 = expand(rlc_spec(r, fixed))
    if r < 0.1:
        ex2 = expand(rlc_spec(r, dict(fixed, t_end=ex.t_end)))           # 同じ計算時間で比べる
    assert rlc_error(simulate(ex2.netlist, ex2.t_end, ex2.h), r) < TOL


def test_long_user_time_note_states_the_real_reason():
    """t_end の指定が振動の周期に比べて長いことが理由（「速い成分と遅い成分が離れている」ではない）。"""
    ex = expand(rlc_spec(1.0, {"t_end": 1.0}))
    coarse = [n for n in ex.notes if "粗く" in n]
    assert len(coarse) == 1 and "t_end=1 s" in coarse[0] and "1周期 12.6 点" in coarse[0]
    assert not any("遅い成分" in n for n in ex.notes)


@pytest.mark.parametrize("r", [1.0, 0.1])
def test_no_phase_note_when_accurate(r):
    ex = expand(rlc_spec(r))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    assert rlc_error(res, r) < TOL
    assert not any("位相" in n for n in ex.notes)


# ------------------------------------------------------------- 範囲7: 文面

def test_cli_limit_error_does_not_suggest_max_steps(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"template": "rlc_series", "simulation": {"h": 1e-9}, "plant": {
        "Vin": {"value": 10, "unit": "V"}, "R": {"value": 10, "unit": "kohm"},
        "L": {"value": 1, "unit": "mH"}, "C": {"value": 10, "unit": "uF"}}}), encoding="utf-8")
    assert cli_main([str(path)]) == 2
    err = capsys.readouterr().err
    assert "上限" in err and "max_steps" not in err and "simulation.h" in err
    with pytest.raises(ValueError, match="max_steps"):                     # 関数から呼ぶときは勧める
        simulate(rc_net(), 1.0, 1e-8)


@pytest.mark.parametrize("units,heading", [
    (("V", "V"), "単位の量の種類が違う項目がある"),
    (("V", "xx"), "単位を読めない項目がある"),
    (("kg", "V"), "単位を読めない、または量の種類が違う項目がある"),
])
def test_unit_error_heading_matches_the_problem(units, heading):
    spec = Spec.model_validate({"template": "rc_lowpass", "plant": {
        "Vin": {"value": 1, "unit": units[0]}, "R": {"value": 1, "unit": units[1]},
        "C": {"value": 1, "unit": "uF"}}})
    with pytest.raises(ValueError) as exc:
        expand(spec)
    assert str(exc.value).startswith(heading + "。")


@pytest.mark.parametrize("t_end,h", [(0.4999995, 1e-6), (1.234567, 1e-3), (1e-3, 6.7e-5)])
def test_end_time_rounding_note_shows_different_numbers(t_end, h):
    """指定の t_end と、実際に終える時刻が、同じ数字に見えない。指定の値はそのまま読める。"""
    res = simulate(rc_net(), t_end, h)
    note = next(n for n in res.notes if "整数倍でない" in n)
    given, used = re.search(r"t_end=([0-9.e+-]+) s .*?([0-9.e+-]+) s（", note).groups()
    assert given != used and float(given) == t_end
    assert float(used) == pytest.approx((len(res.t) - 1) * h, rel=1e-6)


# ------------------------------------------------------------- 範囲10: 作業用の配列を含めた上限

def test_memory_estimate_matches_measurement():
    """見積もりは tracemalloc の最大値と比べて 1.0〜1.2 倍（少なく見積もらない）。"""
    for kwargs, run in [
        (dict(switched=True, n_gates=1, n_devices=2), lambda: simulate(buck(), 1e-2, gates=GATES)),
        ({}, lambda: simulate(rc_net(), 2.0, 1e-5)),
    ]:
        tracemalloc.start()
        res = run()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        n_sources = 1
        estimate, result = sim._memory_bytes(len(res.t) - 1, len(res.unknowns), n_sources, **kwargs)
        assert result == res.x.nbytes
        assert 1.0 <= estimate / peak < 1.2, (estimate, peak)


def test_switched_limit_counts_the_working_arrays(monkeypatch):
    """結果の配列だけなら上限に収まっても、作業用の配列を足して超えるなら、計算の前に断る。"""
    n_steps, n = 200_000, 5
    result = (n_steps + 1) * 8 * n
    monkeypatch.setattr(sim, "MAX_RESULT_BYTES", 2 * result)              # 結果の2倍（実測は約3.5倍）
    with pytest.raises(ValueError, match="作業用の配列"):
        simulate(buck(), n_steps * 5e-8, gates=GATES)
    assert len(simulate(buck(), n_steps * 5e-8, gates=GATES, max_steps=n_steps).t) == n_steps + 1
