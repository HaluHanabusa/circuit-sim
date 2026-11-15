"""E1b: エンジンへの追加（指令で値が決まる電源、相補のゲート）と、ブリッジのレグの試験。

期待値はこのファイルの中の式で作る。
  指令で決まる電源で RC を駆動: 制御周期 j の間は電源が一定値 V_j なので、
  v(t) = V_j + (v(jT) − V_j)·exp(−(t − jT)/τ)（区分ごとの指数関数。厳密解）
"""
import math

import numpy as np
import pytest

from circuit_sim import DC, Command, Component, Netlist, Pwm, simulate
from circuit_sim.templates import HalfBridgeLeg

R, C = 1e3, 1e-6
TAU = R * C
T_CTRL = 1e-4          # 制御周期
STEPS = 20             # 制御周期あたりの刻み数（h = 5 µs、h/τ = 0.005）
GAIN = 24.0


class Sequence:
    """決めた指令の列を出す制御器。j 回目の呼び出し（時刻 j·T）で values[j + 1] を返す。"""

    def __init__(self, values, period=T_CTRL, inputs=("v(out)",)):
        self.values = list(values)
        self.period = period
        self.inputs = inputs
        self.outputs = ("d",)
        self.seen = []

    def reset(self):
        self.seen = []
        return [self.values[0]]

    def step(self, t, measured):
        self.seen.append((t, measured.copy()))
        j = len(self.seen)
        return [self.values[min(j, len(self.values) - 1)]]


def rc_netlist(gain=GAIN, lower=None, upper=None):
    return Netlist(components=[
        Component(id="E1", type="V", nodes=("in", "0"),
                  waveform=Command(command="d", gain=gain, lower=lower, upper=upper)),
        Component(id="R1", type="R", nodes=("in", "out"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
    ])


def command_values(n=40):
    j = np.arange(n)
    return list(0.5 + 0.4 * np.sin(2 * math.pi * j / 13.0) + 0.05 * (j % 3))


def piecewise_exponential(values, gain, n_periods):
    """区分一定の電源 V_j = gain·values[j] で駆動した RC のコンデンサ電圧（各刻みの点）。"""
    t_local = np.arange(STEPS + 1) * (T_CTRL / STEPS)
    v, out = 0.0, [0.0]
    for j in range(n_periods):
        target = gain * values[j]
        seg = target + (v - target) * np.exp(-t_local / TAU)
        out.extend(seg[1:])
        v = seg[-1]
    return np.array(out)


def test_command_source_matches_piecewise_exponential():
    """指令で決まる電源: 制御周期ごとの区分一定の値で、RC の厳密解と一致する（電源の振幅の 1e-4 以内）。

    許容差: 台形則（h/τ = 0.005）の大域誤差は約 (h/τ)²/12 ≈ 2e-6 の程度、不連続点の直後の減衰ステップ
    （1024 分割の後退オイラー）の誤差は約 h/(2·1024·τ) ≈ 2.4e-6。1e-4 はその約40倍で、
    指令の反映が1周期ずれる誤り（振幅の約 5%）は確実に見つかる。
    """
    values = command_values()
    n = len(values) - 1
    res = simulate(rc_netlist(), n * T_CTRL, T_CTRL / STEPS, controllers=[Sequence(values)])
    # 指令は reset の値 → 時刻 jT の演算の値が (j+1)T から。つまり周期 j の値は values[j]
    expected = piecewise_exponential(values, GAIN, n)
    assert np.max(np.abs(res["v(out)"] - expected)) <= 1e-4 * GAIN
    # 電源の値（v(in)）は、区間 [jT, (j+1)T) で GAIN·values[j]（周期の頭の点は変わった後の値）
    v_in = res["v(in)"]
    for j in range(n):
        assert np.allclose(v_in[j * STEPS: (j + 1) * STEPS], GAIN * values[j], rtol=0, atol=1e-12 * GAIN)


def test_command_source_check_detects_one_period_shift():
    """失敗すべきときに失敗する: 指令が1周期ずれた参照とは、許容差の何倍も違う。"""
    values = command_values()
    n = len(values) - 1
    res = simulate(rc_netlist(), n * T_CTRL, T_CTRL / STEPS, controllers=[Sequence(values)])
    shifted = piecewise_exponential([values[0]] + values[:-1], GAIN, n)
    assert np.max(np.abs(res["v(out)"] - shifted)) > 10 * 1e-4 * GAIN
    off_gain = piecewise_exponential(values, 1.1 * GAIN, n)
    assert np.max(np.abs(res["v(out)"] - off_gain)) > 10 * 1e-4 * GAIN


def test_command_source_measurement_and_delay():
    """測定は時刻 jT の記録された値。指令は次の制御周期の頭から（1サンプル遅れ）。"""
    values = [0.0, 1.0] + [0.0] * 8
    ctrl = Sequence(values)
    res = simulate(rc_netlist(), 6 * T_CTRL, T_CTRL / STEPS, controllers=[ctrl])
    v_in = res["v(in)"]
    tol = 1e-12 * GAIN                                        # v(in) は解いた値（電源の値と丸め誤差だけ違う）
    assert np.allclose(v_in[:STEPS], 0.0, rtol=0, atol=tol)
    assert np.allclose(v_in[STEPS: 2 * STEPS], GAIN, rtol=0, atol=tol)       # t = 0 の演算の結果が T から
    assert np.allclose(v_in[2 * STEPS: -1], 0.0, rtol=0, atol=tol)
    assert np.all(res["cmd(d)"][STEPS: 2 * STEPS] == 1.0)
    for j, (t, measured) in enumerate(ctrl.seen):
        assert t == pytest.approx(j * T_CTRL, rel=1e-9, abs=1e-15)
        assert measured[0] == res["v(out)"][j * STEPS]


def test_command_source_clamps_command():
    values = [2.0, -1.0, 0.5] + [0.5] * 5
    res = simulate(
        rc_netlist(lower=0.0, upper=1.0), 3 * T_CTRL, T_CTRL / STEPS, controllers=[Sequence(values)]
    )
    v_in = res["v(in)"]
    assert np.allclose(v_in[[0, STEPS, 2 * STEPS]], [GAIN, 0.0, 0.5 * GAIN], rtol=0, atol=1e-12 * GAIN)
    assert res["cmd(d)"][0] == 2.0                             # 指令そのものは止めない（電源の値だけ止める）


def test_command_source_needs_a_controller_with_that_command():
    with pytest.raises(ValueError, match="制御器"):
        simulate(rc_netlist(), 1e-3, 1e-5)
    other = Sequence([0.5])
    other.outputs = ("other",)
    with pytest.raises(ValueError, match="'d'"):
        simulate(rc_netlist(), 1e-3, 1e-5, controllers=[other])
    with pytest.raises(ValueError, match="lower"):
        Command(command="d", gain=1.0, lower=1.0, upper=0.0)


# ---------------------------------------------------------------- 相補のゲート

VIN, L_BUCK, C_BUCK, FSW, N = 24.0, 47e-6, 100e-6, 100e3, 200
TS = 1.0 / FSW


def sync_buck(load: float, extra=()) -> tuple[Netlist, list[Pwm]]:
    leg = HalfBridgeLeg(positive="in", negative="0", output="sw", duty=0.5, frequency=FSW, v_dc=VIN)
    parts, gates = leg.expand("switching")
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=VIN)),
        *parts,
        Component(id="L1", type="L", nodes=("sw", "out"), value=L_BUCK),
        Component(id="C1", type="C", nodes=("out", "0"), value=C_BUCK),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
        *extra,
    ])
    return net, gates


def test_complementary_gates_drive_upper_and_lower_switch():
    net, gates = sync_buck(6.0)
    res = simulate(net, 10 * TS, gates=gates)
    g1, g2 = res["gate(q1)"], res["gate(q2)"]
    assert np.all(g1 + g2 == 1.0)                              # どの区間でもちょうど一方だけがオン
    assert np.all(res["on(S1)"] == g1) and np.all(res["on(S2)"] == g2)
    assert [int(g1[p * N: (p + 1) * N].sum()) for p in range(10)] == [100] * 10
    assert res.stats["states"] == 2


def test_synchronous_buck_follows_duty_at_light_load():
    """同期整流は軽負荷でも連続導通（電流が負になれる）。周期平均は D·Vin。

    （ダイオード整流なら不連続導通になり、約 18.6 V。E1a の基準3）

    負荷 100 Ω、D = 0.5。過渡: 包絡は exp(−t/(2RC))、2RC = 20 ms なので 120 ms（6倍、e^-6 = 0.25%）を見る。
    電流リプル (Vin − Vout)·D/(L·fsw) = 1.28 A に対して平均電流は 0.12 A なので、谷は負になる。
    """
    load = 100.0
    net, gates = sync_buck(load)
    res = simulate(net, 120e-3, gates=gates, steps_per_period=50)
    n_per = 50
    window = res["v(out)"][-20 * n_per:]
    assert abs(window.mean() - 0.5 * VIN) < 0.01 * 0.5 * VIN
    i_l = res["i(L1)"][-20 * n_per:]
    assert i_l.min() < -0.4 and i_l.max() > 0.6


def test_pwm_complement_and_dead_time_validation():
    assert Pwm(name="a", frequency=1e3, duty=0.5, complement="b").outputs == ["a", "b"]
    with pytest.raises(ValueError, match="デッドタイム"):
        Pwm(name="a", frequency=1e3, duty=0.5, complement="b", dead_time=1e-7)
    with pytest.raises(ValueError, match="complement"):
        Pwm(name="a", frequency=1e3, duty=0.5, complement="a")
    net, _ = sync_buck(6.0)
    with pytest.raises(ValueError, match="重複"):
        simulate(net, 2 * TS, gates=[Pwm(name="q1", frequency=FSW, duty=0.5, complement="q2"),
                                     Pwm(name="q2", frequency=FSW, duty=0.5)])


# ---------------------------------------------------------------- ブリッジのレグ

def leg_buck(fidelity, inductance=L_BUCK, duty=0.5):
    leg = HalfBridgeLeg(positive="in", negative="0", output="sw", duty=duty, frequency=FSW, v_dc=VIN)
    parts, gates = leg.expand(fidelity)
    comps = []
    if fidelity == "switching":
        comps.append(Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=VIN)))
    net = Netlist(components=comps + parts + [
        Component(id="L1", type="L", nodes=("sw", "out"), value=inductance),
        Component(id="C1", type="C", nodes=("out", "0"), value=C_BUCK),
        Component(id="R1", type="R", nodes=("out", "0"), value=6.0),
    ])
    return net, gates


def period_averages(res, steps):
    """出力電圧の周期平均（周期 j の区間 [jT, (j+1)T] を台形則で積分して T で割る）。"""
    y = res["v(out)"]
    m = (len(y) - 1) // steps
    return np.array([np.trapezoid(y[j * steps: (j + 1) * steps + 1]) / steps for j in range(m)])


def test_leg_average_matches_switching_open_loop():
    """レグの平均モデル: 固定デューティの起動（LC が大きく振動する）で、周期平均がスイッチングモデルと
    Vin·D の 2% 以内で一致する（最初の1周期を除く）。L を1割ずらした平均モデルとは 2% を超えて違う。"""
    t_end = 3e-3
    net_s, gates = leg_buck("switching")
    sw = simulate(net_s, t_end, gates=gates)
    net_a, _ = leg_buck("average")
    av = simulate(net_a, t_end, TS / 10)
    ps, pa = period_averages(sw, N), period_averages(av, 10)
    ref = 0.5 * VIN
    assert np.abs(ps - pa)[1:].max() < 0.02 * ref
    net_bad, _ = leg_buck("average", inductance=1.1 * L_BUCK)
    bad = period_averages(simulate(net_bad, t_end, TS / 10), 10)
    assert np.abs(ps - bad)[1:].max() > 0.02 * ref


def test_leg_expansion_names_and_errors():
    leg = HalfBridgeLeg(positive="p", negative="n", output="o", duty="duty", frequency=FSW, v_dc=48.0)
    parts, gates = leg.expand("switching")
    assert [(c.id, c.type, c.nodes, c.gate) for c in parts] == [
        ("S1", "S", ("p", "o"), "q1"), ("S2", "S", ("o", "n"), "q2")]
    assert gates[0].outputs == ["q1", "q2"] and gates[0].duty == "duty"
    parts, gates = leg.expand("average")
    assert gates == [] and parts[0].nodes == ("o", "n")
    w = parts[0].waveform
    assert (w.command, w.gain, w.lower, w.upper) == ("duty", 48.0, 0.0, 1.0)
    with pytest.raises(ValueError):
        leg.expand("ideal")
