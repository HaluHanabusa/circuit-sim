"""E1c（E1a の残課題）の受け入れ試験。検証担当が、実装者の試験とは別に用意した。

対象は `docs/plan/E1c-followups.md` の基準1〜5と、仕様に無い角。記録は docs/verification/E1c-verifier.md。
試験名の c1〜c5 が基準の番号、x は仕様に無い角。

期待値の作り方（実装と独立にするための決まり）:
- 三相全波整流の直流平均は、このファイルで導いた式（理想の平均 3√3·Vm/π から、重なり角の降下
  3ωLs·Id/π と、導通する2個のダイオードの降下 2·r_on·Id を引く）で作る。
- RC と RLC のステップ応答は、このファイルに書いた解析解で作る。
- 注意（notes）の比較は、スイッチを抵抗に置き換えた回路（スイッチの無い計算の経路）と比べる。
- `circuit_sim` の関数を期待値の計算に使わない。

製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import importlib
import math
import re
import tracemalloc

import numpy as np
import pytest

from circuit_sim import CircuitError, Netlist, Pwm, Spec, expand, simulate

sim_module = importlib.import_module("circuit_sim.simulate")

R_ON, R_OFF = 1e-3, 1e6       # スイッチとダイオードの既定のオン抵抗・オフ抵抗
TOL_DC = 0.02                 # 基準1の許容（仕様の値）
TOL_ANALYTIC = 0.01           # 解析解との差（roadmap.md の既存の基準。仕様の基準3も同じ）


def product_bug(severity: str, *lines: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"製品の不具合({severity}): " + "".join(lines))


def netlist(*components: dict, ground: str = "0") -> Netlist:
    return Netlist.model_validate({"ground": ground, "components": list(components)})


def comp(cid: str, kind: str, a: str, b: str, value: float | None = None, **kw) -> dict:
    d = {"id": cid, "type": kind, "nodes": [a, b], **kw}
    if value is not None:
        d["value"] = value
    return d


def dc(cid: str, a: str, b: str, v: float) -> dict:
    return comp(cid, "V", a, b, waveform={"kind": "dc", "value": v})


# ================================================================ 基準1: 三相整流

def bridge(vm: float, *, f: float = 50.0, ls: float = 1e-3, r: float = 10.0, ld: float | None = None,
           ground: str = "N+R", ics: dict | None = None, extra: tuple = ()) -> Netlist:
    """星形の三相電源 - 直列 Ls - ダイオード6個 - 負荷 R（ld を与えると直流リアクトルを直列に入れる）。

    ground: "N+R" 中性点を接地し、直流の負極を 1 MΩ で接地 / "M+R" 負極を接地し、中性点を 1 MΩ で接地 /
            "N+Y" 中性点を接地し、直流の正極と負極をそれぞれ 1 MΩ で接地 /
            "N" 中性点だけ接地（直流側は浮く）/ "M" 負極だけ接地（中性点は浮く）
    """
    ics = ics or {}
    parts = []
    for k, ph in enumerate("abc"):
        parts += [
            comp(f"V{ph}", "V", f"s{ph}", "N",
                 waveform={"kind": "sine", "amplitude": vm, "frequency": f, "phase_deg": -120.0 * k}),
            comp(f"L{ph}", "L", f"s{ph}", ph, ls, ic=ics.get(f"L{ph}", 0.0)),
            comp(f"D{ph}1", "D", ph, "P"),
            comp(f"D{ph}2", "D", "M", ph),
        ]
    if ld:
        parts += [comp("Ld", "L", "P", "X", ld, ic=ics.get("Ld", 0.0)), comp("RL", "R", "X", "M", r)]
    else:
        parts.append(comp("RL", "R", "P", "M", r))
    gnd = {"N+R": "N", "M+R": "M", "N+Y": "N", "N": "N", "M": "M"}[ground]
    if ground == "N+R":
        parts.append(comp("Rg", "R", "M", "N", 1e6))
    elif ground == "M+R":
        parts.append(comp("Rg", "R", "N", "M", 1e6))
    elif ground == "N+Y":
        parts += [comp("Ry1", "R", "P", "N", 1e6), comp("Ry2", "R", "M", "N", 1e6)]
    return netlist(*parts, *extra, ground=gnd)


def dc_theory(vm: float, f: float, ls: float, r: float) -> float:
    """直流電流 Id を一定とみなした平均。

    転流（重なり）の間は、入る相と出る相の Ls に線間電圧の半分ずつがかかり、出力は2相の平均になる。
    1回の転流で失う電圧の面積は（電気角で）ωLs·Id。転流は π/3 ごとなので、平均の降下は 3ωLs·Id/π。
    導通は常に上下1個ずつなので、オン抵抗の降下は 2·r_on·Id。Id = Vd / R で解く。
    """
    vd0 = 3 * math.sqrt(3) * vm / math.pi
    return vd0 / (1 + (3 * 2 * math.pi * f * ls / math.pi + 2 * R_ON) / r)


def three_phase_cases() -> list[dict]:
    """165例: 振幅10〜400 V（端点と、修正前に止まった 47、48、100 V を含む）、50/60 Hz、Ls 3通り、
    負荷4通り（うち2割は直流リアクトルつき）、接地3通り、初期値ゼロと乱数（半々）。乱数はシード固定。"""
    rng = np.random.default_rng(424242)
    cases = [dict(vm=vm, f=50.0, ls=1e-3, r=10.0, ground=g, ics={}, ld=None)
             for g in ("N+R", "M+R", "N+Y") for vm in (10.0, 47.0, 48.0, 100.0, 400.0)]
    for k in range(150):
        vm = float(math.exp(rng.uniform(math.log(10.0), math.log(400.0))))
        f = float(rng.choice([50.0, 60.0]))
        ls = float(rng.choice([0.2e-3, 1e-3, 2e-3]))
        r = float(rng.choice([5.0, 10.0, 30.0, 100.0]))
        ld = 0.2 if k % 5 == 4 else None
        peak = 2 * 1.65 * vm / r
        ics = {}
        if k % 2 == 1:
            ics = {f"L{p}": float(rng.uniform(-peak, peak)) for p in "abc"}
            if ld:
                ics["Ld"] = float(rng.uniform(0, peak))
        cases.append(dict(vm=vm, f=f, ls=ls, r=r, ground=("N+R", "M+R", "N+Y")[k % 3], ics=ics, ld=ld))
    return cases


def run_bridge(case: dict):
    """1周期 2000 点。抵抗負荷は4周期（過渡の時定数 Ls/R は最大 0.4 ms）、リアクトルつきは12周期
    （Ld/R は最大 40 ms）。平均は最後の1周期で取り、その前の1周期との差で定常を確かめる。"""
    n = 2000
    h = 1.0 / (case["f"] * n)
    periods = 12 if case["ld"] else 4
    res = simulate(bridge(case["vm"], f=case["f"], ls=case["ls"], r=case["r"], ld=case["ld"],
                          ground=case["ground"], ics=case["ics"]), periods / case["f"], h)
    v = res.v("P", "M")
    return float(v[-n - 1:-1].mean()), float(v[-2 * n - 1:-n - 1].mean()), res


def test_c1_three_phase_dc_average_for_165_cases():
    """基準1: 165例すべてでエラーにならず、直流側の平均が理論値と 2% 以内（実測の最大は 0.51%）。"""
    worst = 0.0
    for case in three_phase_cases():
        last, prev, _ = run_bridge(case)
        expected = dc_theory(case["vm"], case["f"], case["ls"], case["r"])
        assert abs(last - prev) < 1e-3 * expected, f"定常に達していない: {case}"
        error = abs(last - expected) / expected
        worst = max(worst, error)
        assert error < TOL_DC, f"{case}: {error:.2%}"
    assert worst < TOL_DC


def test_c1_check_is_sensitive_to_the_commutation_drop():
    """試験の実効性: 重なり角の降下を入れない式（3√3·Vm/π − 2·r_on·Id）とは 2% を超えて食い違う。"""
    case = dict(vm=200.0, f=50.0, ls=2e-3, r=5.0, ground="N+R", ics={}, ld=None)
    last, _, _ = run_bridge(case)
    no_overlap = 3 * math.sqrt(3) * 200.0 / math.pi / (1 + 2 * R_ON / 5.0)
    assert abs(last - no_overlap) / no_overlap > TOL_DC


@pytest.mark.parametrize("ground", ["N", "M"])
def test_c1_fully_floating_bridge_is_refused_with_cause_and_fix(ground):
    """接地が中性点だけ（直流側が浮く）か負極だけ（中性点が浮く）だと、インダクタだけで外とつながる
    部分ができて解けない（E0 からの制約）。エラーに原因（La, Lb, Lc）と直し方（大きな抵抗）が出る。"""
    with pytest.raises(CircuitError) as exc:
        simulate(bridge(100.0, ground=ground), 0.02, 1e-5)
    text = str(exc.value)
    assert "La, Lb, Lc だけで回路の残りとつながって" in text
    assert "大きな抵抗" in text


def test_c1_the_suggested_resistor_does_not_change_the_result():
    """直し方どおり 1 MΩ を足すと、どこに足しても直流側の電圧は同じ（漏れ電流は µA 程度）。"""
    a = simulate(bridge(100.0, ground="N+R"), 0.04, 1e-5).v("P", "M")
    b = simulate(bridge(100.0, ground="M+R"), 0.04, 1e-5).v("P", "M")
    c = simulate(bridge(100.0, ground="N+Y"), 0.04, 1e-5).v("P", "M")
    assert np.max(np.abs(a - b)) < 1e-9 * 100.0
    assert np.max(np.abs(a - c)) < 1e-5 * 100.0


def test_c1_reproducible_with_random_initial_values():
    ics = {"La": 3.0, "Lb": -7.0, "Lc": 1.5}
    case = dict(vm=200.0, f=50.0, ls=1e-3, r=10.0, ground="N+Y", ics=ics, ld=None)
    _, _, a = run_bridge(case)
    _, _, b = run_bridge(case)
    assert np.array_equal(a.x, b.x) and a.notes == b.notes


# ================================================================ 基準2: スイッチの経路の注意

H_SW = 50e-9                                  # 100 kHz、1周期200分割の既定の刻み


def buck_lc(kind: str = "switch", lf: float | None = 1e-6, cf: float = 1e-9) -> Netlist:
    """入力に LC（1 µH、1 nF: 周期 198.7 ns、h = 50 ns で1周期 3.97 点）を入れた降圧。

    kind="resistor": スイッチを r_on、ダイオードを r_off の抵抗に置き換える（スイッチの無い計算の経路）。
    """
    parts = [dc("V1", "src", "0", 24.0)]
    top = "src"
    if lf:
        parts += [comp("Lf", "L", "src", "in", lf), comp("Cf", "C", "in", "0", cf)]
        top = "in"
    if kind == "switch":
        parts += [comp("S1", "S", top, "sw", gate="g"), comp("D1", "D", "0", "sw")]
    else:
        parts += [comp("S1", "R", top, "sw", R_ON), comp("D1", "R", "0", "sw", R_OFF)]
    parts += [comp("L1", "L", "sw", "out", 47e-6), comp("C1", "C", "out", "0", 100e-6),
              comp("R1", "R", "out", "0", 6.0)]
    return netlist(*parts)


def oscillation_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if "回路の振動" in n]


def test_c2_lc_in_switched_circuit_gives_the_same_note_as_the_resistor_circuit():
    """基準2: 周期 199 ns の LC を含む降圧（h = 50 ns）で、振動の注意が、抵抗に置き換えた回路と
    同じ文面で出る。"""
    switched = simulate(buck_lc(), 2e-4, H_SW, gates=[Pwm(name="g", frequency=1e5, duty=0.5)])
    linear = simulate(buck_lc("resistor"), 2e-4, H_SW)
    assert oscillation_notes(linear.notes)                        # 比べる相手に注意がある
    assert oscillation_notes(switched.notes) == oscillation_notes(linear.notes)
    # スイッチがオンの間は、L1（47 µH）が出力の C1 を通して Cf に並列に入る: ω² = (1/Lf + 1/L1) / Cf
    period = 2 * math.pi / math.sqrt((1 / 1e-6 + 1 / 47e-6) / 1e-9)
    points = float(re.search(r"1周期が ([0-9.]+) 点", oscillation_notes(switched.notes)[0]).group(1))
    assert abs(points - period / H_SW) < 0.01 * period / H_SW


def test_c2_no_oscillation_note_when_the_lc_is_resolved_or_absent():
    """出るべきでないとき: 刻みが LC を解像している（h = 5 ns、1周期 40 点）、または LC が無い。"""
    fine = simulate(buck_lc(), 2e-5, 5e-9, gates=[Pwm(name="g", frequency=1e5, duty=0.5)])
    assert oscillation_notes(fine.notes) == []
    plain = simulate(buck_lc(lf=None), 2e-4, H_SW, gates=[Pwm(name="g", frequency=1e5, duty=0.5)])
    assert plain.notes == []


def dcm_ring(L: float, csw: float, kind: str = "switch") -> Netlist:
    """不連続導通の降圧（出力 20 V から始める）。スイッチもダイオードもオフの区間に、L とスイッチ節点の
    容量 csw が振動する。減衰はオフ抵抗だけなので Q = (r_off/2)·√(csw/L)。"""
    parts = [dc("V1", "in", "0", 24.0)]
    if kind == "switch":
        parts += [comp("S1", "S", "in", "sw", gate="g"), comp("D1", "D", "0", "sw")]
    else:  # 両方オフの状態を抵抗に置き換える
        parts += [comp("S1", "R", "in", "sw", R_OFF), comp("D1", "R", "0", "sw", R_OFF)]
    parts += [comp("L1", "L", "sw", "out", L), comp("C1", "C", "out", "0", 100e-6, ic=20.0),
              comp("R1", "R", "out", "0", 2000.0), comp("Csw", "C", "sw", "0", csw)]
    return netlist(*parts)


def test_c2_off_state_ringing_with_high_q_is_noted():
    """出るべきとき（Q が高い）: L = 47 µH、21.3 pF（周期 199 ns、Q = 337）で注意が出る。"""
    res = simulate(dcm_ring(47e-6, 21.3e-12), 2e-4, H_SW, gates=[Pwm(name="g", frequency=1e5, duty=0.3)])
    assert np.sum((res["on(S1)"] == 0) & (res["on(D1)"] == 0)) > 1000     # オフ・オフの区間を通っている
    assert oscillation_notes(res.notes)


@pytest.mark.parametrize("L,csw", [(1e-3, 5e-12), (470e-6, 2e-12)])
def test_c2_off_state_ringing_with_moderate_q_is_noted(L, csw):
    res = simulate(dcm_ring(L, csw), 2e-4, H_SW, gates=[Pwm(name="g", frequency=1e5, duty=0.3)])
    assert np.sum((res["on(S1)"] == 0) & (res["on(D1)"] == 0)) > 1000
    assert oscillation_notes(simulate(dcm_ring(L, csw, "resistor"), 2e-4, H_SW).notes)
    assert oscillation_notes(res.notes)


# ================================================================ 基準3: 振動する回路の既定

def rlc_spec(R: float, simulation: dict) -> Spec:
    return Spec.model_validate({
        "schema_version": "0.1", "template": "rlc_series",
        "plant": {"Vin": {"value": 10, "unit": "V"}, "R": {"value": R, "unit": "ohm"},
                  "L": {"value": 1e-3, "unit": "H"}, "C": {"value": 10e-6, "unit": "F"}},
        "simulation": simulation})


def rlc_error(R: float, simulation: dict) -> tuple[float, list[str]]:
    """v(out) の最大誤差（入力 10 V に対する比）と注意。解析解は不足減衰の直列RLCのステップ応答。"""
    ex = expand(rlc_spec(R, simulation))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    L, C = 1e-3, 10e-6
    a, w0 = R / (2 * L), 1 / math.sqrt(L * C)
    wd = math.sqrt(w0 ** 2 - a ** 2)
    t = res.t
    exact = 10.0 * (1 - np.exp(-a * t) * (np.cos(wd * t) + a / wd * np.sin(wd * t)))
    return float(np.max(np.abs(res["v(out)"] - exact)) / 10.0), ex.notes + res.notes


def recommended_h(notes: list[str]) -> float:
    found = [re.search(r"simulation\.h を ([0-9.e+-]+) s 以下", n) for n in notes]
    found = [float(m.group(1)) for m in found if m]
    assert found, f"刻みを勧める注意が無い: {notes}"
    return found[0]


def test_c3_long_t_end_on_oscillating_circuit_warns_and_the_advice_works():
    """基準3: R = 1 Ω、L = 1 mH、C = 10 µF、t_end = 1 s だけ指定。誤差が 1% を超えるなら、精度が落ちることと
    直し方が注意に出て、勧めの刻みに従うと 1% 以内になる（実測 14.4% → 0.50%）。"""
    error, notes = rlc_error(1.0, {"t_end": 1.0})
    if error > TOL_ANALYTIC:
        assert any("精度が落ちる" in n for n in notes), notes
        again, _ = rlc_error(1.0, {"t_end": 1.0, "h": recommended_h(notes)})
        assert again < TOL_ANALYTIC


@pytest.mark.parametrize("R", [0.01, 0.1, 1.0, 10.0])
@pytest.mark.parametrize("t_end", [0.02, 0.2, 1.0, 5.0])
def test_c3_error_above_one_percent_is_never_silent(R, t_end):
    """ζ = 0.0005〜0.5、t_end = 0.02〜5 s のどの組でも、1% を超える誤差は精度の注意を伴う。"""
    error, notes = rlc_error(R, {"t_end": t_end})
    assert error < TOL_ANALYTIC or any("精度" in n or "正しく出ない" in n for n in notes)


@pytest.mark.parametrize("R,t_end", [(1.0, 5.0), (0.01, 1.0), (10.0, 20.0)])
def test_c3_following_the_advice_once_is_enough(R, t_end):
    _, notes = rlc_error(R, {"t_end": t_end})
    again, _ = rlc_error(R, {"t_end": t_end, "h": recommended_h(notes)})
    assert again < TOL_ANALYTIC


# ================================================================ 基準5: 減衰の境目（h = τ/2）と精度

def rc_step(tau: float = 1e-3) -> Netlist:
    return netlist(comp("V1", "V", "in", "0", waveform={"kind": "step", "final": 1.0}),
                   comp("R1", "R", "in", "out", 1e3), comp("C1", "C", "out", "0", tau / 1e3))


def rc_error(ratio: float, tau: float = 1e-3) -> tuple[float, list[str]]:
    h = ratio * tau
    res = simulate(rc_step(tau), 400 * h, h)
    return float(np.max(np.abs(res["v(out)"] - (1 - np.exp(-res.t / tau))))), res.notes


@pytest.mark.parametrize("ratio", [0.3, 0.45, 0.49, 0.4999, 0.5001, 0.51, 0.6, 0.8, 1.0, 1.5, 1.99])
def test_c5_rc_step_is_within_one_percent_on_both_sides_of_the_damping_threshold(ratio):
    """h/τ = 0.3〜1.99（注意が出ない範囲）で、真値と 1% 以内（E1a-A の指摘1: main では 1.99τ で 13.4%）。"""
    error, notes = rc_error(ratio)
    assert error < TOL_ANALYTIC, f"h/τ={ratio}: {error:.2%}, notes={notes}"


def test_c5_no_jump_up_at_the_damping_threshold():
    """境目のすぐ上の誤差が、すぐ下の誤差より大きくならない（不連続に悪くならない）。"""
    below, _ = rc_error(0.4999)
    above, _ = rc_error(0.5001)
    assert above <= below


def two_branch_rc(tf: float = 1e-4, ts: float = 1e-2) -> Netlist:
    return netlist(comp("V1", "V", "in", "0", waveform={"kind": "step", "final": 1.0}),
                   comp("R1", "R", "in", "a", 100.0), comp("C1", "C", "a", "0", tf / 100.0),
                   comp("R2", "R", "in", "b", 1e4), comp("C2", "C", "b", "0", ts / 1e4))


def test_c5_second_order_is_kept_across_the_threshold():
    """遅い枝の誤差は、刻みを半分にするたびに約1/4（3.6〜4.4。E0 と同じ帯）。h は速い時定数の
    1.2、0.6、0.3、0.15 倍で、減衰が入る側から入らない側へまたぐ。"""
    tf, ts = 1e-4, 1e-2
    errors = []
    for ratio in (1.2, 0.6, 0.3, 0.15):
        h = ratio * tf
        res = simulate(two_branch_rc(tf, ts), round(0.02 / h) * h, h)
        errors.append(float(np.max(np.abs(res["v(b)"] - (1 - np.exp(-res.t / ts))))))
    ratios = [a / b for a, b in zip(errors, errors[1:], strict=False)]
    assert all(3.6 < r < 4.4 for r in ratios), ratios


def test_c5_template_defaults_keep_the_e0_accuracy():
    """RC 0.003%、RLC 0.06% の水準（E1a-A と同じく README の値の2倍まで）。"""
    ex = expand(Spec.model_validate({
        "schema_version": "0.1", "template": "rc_lowpass",
        "plant": {"Vin": {"value": 5, "unit": "V"}, "R": {"value": 1e3, "unit": "ohm"},
                  "C": {"value": 1e-6, "unit": "F"}}}))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    assert np.max(np.abs(res["v(out)"] - 5 * (1 - np.exp(-res.t / 1e-3)))) / 5 < 0.006e-2
    error, notes = rlc_error(1.0, {})
    assert error < 0.12e-2 and notes == []


# ================================================================ 仕様に無い角: ダイオードの救済

def island(v: float) -> tuple[dict, dict]:
    """回路の残りと電気的に独立した高電圧の部分（同じ基準節点を持つ）。"""
    return dc("Vhv", "hv", "N", v), comp("Rhv", "R", "hv", "N", 1e9)


@pytest.mark.parametrize("vm,vhv", [(0.01, 1e6), (0.01, 1e8), (1.0, 1e10)])
def test_x_large_voltage_elsewhere_does_not_change_the_diode_states(vm, vhv):
    """救済の幅は節点電圧の最大に比例する。桁違いに高い電圧の部分があっても、小さい整流器の
    ダイオードの状態は、その部分が無いときと全時刻で同じ。"""
    ics = {"La": 3e-4 * vm, "Lb": -1e-4 * vm, "Lc": -2e-4 * vm}
    ref = simulate(bridge(vm, ics=ics), 0.04, 1e-5)
    res = simulate(bridge(vm, ics=ics, extra=island(vhv)), 0.04, 1e-5)
    for d in ("Da1", "Da2", "Db1", "Db2", "Dc1", "Dc2"):
        assert np.array_equal(res[f"on({d})"], ref[f"on({d})"]), d


def twelve_pulse(vm: float) -> Netlist:
    """2つの三相ブリッジを直列（片方の電源は 30° ずらす）。ダイオード12個。"""
    parts = []
    for side, shift, n0, top, bot in (("y", 0.0, "Ny", "P", "Q"), ("d", 30.0, "Nd", "Q", "M")):
        for k, ph in enumerate("abc"):
            parts += [
                comp(f"V{side}{ph}", "V", f"s{side}{ph}", n0,
                     waveform={"kind": "sine", "amplitude": vm, "frequency": 50.0,
                               "phase_deg": shift - 120.0 * k}),
                comp(f"L{side}{ph}", "L", f"s{side}{ph}", f"{side}{ph}", 1e-3),
                comp(f"D{side}{ph}1", "D", f"{side}{ph}", top),
                comp(f"D{side}{ph}2", "D", bot, f"{side}{ph}"),
            ]
    parts += [comp("RL", "R", "P", "M", 10.0), comp("Rg1", "R", "M", "Ny", 1e6),
              comp("Rg2", "R", "Nd", "Ny", 1e6)]
    return netlist(*parts, ground="Ny")


def worst_diode_violation(res, net: Netlist) -> tuple[float, float]:
    """各時刻の値と回路状態で、オンのダイオードの逆向きの電圧と、オフのダイオードの順方向の電圧の最大 [V]。"""
    worst_on, worst_off = 0.0, 0.0
    for bit, dev in enumerate(res.devices):
        c = net.component(dev)
        if c.type != "D":
            continue
        vd = (res.v(c.nodes[0]) if c.nodes[0] != net.ground else 0.0) - (
            res.v(c.nodes[1]) if c.nodes[1] != net.ground else 0.0)
        on = (res.sample_state >> bit) & 1
        worst_on = max(worst_on, float(np.max(np.where(on == 1, -vd, 0.0))))
        worst_off = max(worst_off, float(np.max(np.where(on == 0, vd, 0.0))))
    return worst_on, worst_off


@pytest.mark.parametrize("vm", [48.0, 400.0, 1e4])
@pytest.mark.parametrize("which", ["three_phase_light_load", "twelve_pulse"])
def test_x_accepted_diode_states_are_physical_at_every_sample(vm, which):
    """救済で採った状態も含め、全時刻で、オンのダイオードに逆電流、オフのダイオードに順電圧が無い。
    許容は救済の幅そのもの（節点電圧の最大の 1e-8 倍、下限 1e-9 V）。実測はその 1/240 以下。"""
    net = bridge(vm, r=1e5) if which == "three_phase_light_load" else twelve_pulse(vm)
    res = simulate(net, 0.04, 1e-5)
    vmax = max(float(np.max(np.abs(res.v(n)))) for n in net.nodes())
    band = max(1e-9, 1e-8 * vmax)
    worst_on, worst_off = worst_diode_violation(res, net)
    assert worst_on <= band and worst_off <= band


# ================================================================ 仕様に無い角: インダクタ電流の遮断の注意

def interruption_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if "還流経路" in n]      # E1g（D-063）: 「行き場」→「還流経路」


def test_x_interruption_example_of_the_review_is_noted():
    """範囲8の例: 出力の初期値 30 V の降圧。注意に素子名と直し方が出る。"""
    net = netlist(dc("V1", "in", "0", 24.0), comp("S1", "S", "in", "sw", gate="g"),
                  comp("D1", "D", "0", "sw"), comp("L1", "L", "sw", "out", 47e-6),
                  comp("C1", "C", "out", "0", 100e-6, ic=30.0), comp("R1", "R", "out", "0", 6.0))
    res = simulate(net, 1e-4, gates=[Pwm(name="g", frequency=1e5, duty=0.5)])
    notes = interruption_notes(res.notes)
    assert len(notes) == 1 and "L1" in notes[0] and "還流ダイオード" in notes[0]


@product_bug(
    "軽微",
    "遮断の判定は L·|i|/h を電圧源とコンデンサの電圧の最大と比べる。電流源で駆動し、電圧源もコンデンサも無い",
    "回路では基準が 0 になり、電流が 0 を横切るときの普通の転流（1.4 mA、0.08 V）を『電流が1刻みで消えた。",
    "実際の回路ではサージ電圧になる』と知らせる",
)
def test_x_normal_turn_off_in_current_driven_circuit_is_not_an_interruption():
    net = netlist(comp("I1", "I", "0", "a", waveform={"kind": "sine", "amplitude": 1.0, "frequency": 50.0}),
                  comp("R1", "R", "a", "0", 10.0), comp("L1", "L", "a", "b", 1e-3),
                  comp("D1", "D", "b", "c"), comp("R2", "R", "c", "0", 5.0))
    res = simulate(net, 0.06, 1e-5)
    assert res["i(L1)"].min() > -1e-3                     # 逆向きにはほとんど流れない（普通の転流）
    assert interruption_notes(res.notes) == []


@product_bug(
    "軽微",
    "還流の経路の無い S-L-R で、スイッチ節点から接地へ 100 kΩ の抵抗があると、v(sw) に 1.76 kV の点"
    "（刻みで決まる値。モデルの真値は 190 kV が 0.5 ns）が出るのに、注意が1つも出ない。",
    "遮断の判定は抵抗の経路があるので対象外とし、速い時定数 L/R = 0.47 ns は r_off（並列の 1 MΩ）を",
    "10倍にすると 9% 動くので見かけの成分として除かれる",
)
def test_x_interruption_through_a_bleeder_resistor_is_noted():
    net = netlist(dc("V1", "in", "0", 24.0), comp("S1", "S", "in", "sw", gate="g"),
                  comp("L1", "L", "sw", "out", 47e-6), comp("R1", "R", "out", "0", 6.0),
                  comp("Rb", "R", "sw", "0", 1e5))
    res = simulate(net, 1e-4, gates=[Pwm(name="g", frequency=1e5, duty=0.5)])
    assert np.abs(res.v("sw")).max() > 10 * 24.0
    assert res.notes != []


# ================================================================ 仕様に無い角: 記憶量の上限

def multiplier(stages: int, vm: float = 100.0) -> Netlist:
    """コッククロフト・ウォルトンの倍電圧回路（ダイオード 2·stages 個）。"""
    parts = [comp("V1", "V", "s", "0", waveform={"kind": "sine", "amplitude": vm, "frequency": 50.0})]
    top, bot = "s", "0"
    for n in range(stages):
        a, b = f"a{n}", f"b{n}"
        parts += [comp(f"Ca{n}", "C", top, a, 10e-6), comp(f"Da{n}", "D", bot, a),
                  comp(f"Db{n}", "D", a, b), comp(f"Cb{n}", "C", b, bot, 10e-6)]
        top, bot = a, b
    parts.append(comp("RL", "R", bot, "0", 1e6))
    return netlist(*parts)


def peak_at_the_limit(monkeypatch, net: Netlist, t_end: float, h: float) -> tuple[int, int]:
    """上限をこの計算の見積もりちょうどにして（通る最大の計算）、tracemalloc の最大と上限を返す。"""
    seen = {}
    estimate = sim_module._memory_bytes

    def spy(*args, **kwargs):
        seen["total"] = estimate(*args, **kwargs)[0]
        return estimate(*args, **kwargs)

    monkeypatch.setattr(sim_module, "_memory_bytes", spy)
    simulate(net, t_end, h)
    monkeypatch.setattr(sim_module, "MAX_RESULT_BYTES", seen["total"])
    tracemalloc.start()
    simulate(net, t_end, h)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak, seen["total"]


def test_x_memory_limit_bounds_the_peak_for_a_small_switched_circuit(monkeypatch):
    """三相ブリッジ（ダイオード6個）: 上限ちょうどの計算で、実際の最大が上限を超えない（実測 0.96 倍）。"""
    peak, limit = peak_at_the_limit(monkeypatch, bridge(100.0), 0.5, 1e-5)
    assert peak <= limit


@product_bug(
    "軽微",
    "記憶量の見積もりの『ステップ数によらない分』が 1 MB 固定で、回路状態ごとの行列を数えていない。",
    "倍電圧回路（ダイオード40個、回路状態250個）では実際の最大が見積もりの 1.2〜1.8 倍（15〜31 MB 多い）。",
    "上限 2 GiB の近くでは、上限を超えて確保する",
)
def test_x_memory_limit_bounds_the_peak_for_many_diodes(monkeypatch):
    peak, limit = peak_at_the_limit(monkeypatch, multiplier(20), 0.25, 2e-5)
    assert peak <= limit
