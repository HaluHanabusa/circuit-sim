"""E1g の範囲11（受け入れ基準12）: 回路の変数が発散したら、計算の途中で止めて、時刻と変数の入った番号つきの
エラー（DivergenceError。番号 RUN_DIVERGED / RUN_DIVERGED_NOT_FINITE。画面の N-RUN-DIVERGED）を返す。

「決めた大きさ」は、回路の電圧（電流）の目安の 1e12 倍（simulate._divergence_limits の説明）。
発散の例は、閉ループが不安定で指数的に育つもの:
  - 降圧コンバータの状態平均化モデルに、上下限の無い PID（閉ループ極の絶対値の最大 > 1）をつないだ回路
  - 指令で値が決まる電源 → R → C に、正帰還の制御器をつないだ線形の回路（スイッチなし）
期待値（止まる時刻の目安）は、試験の中で離散時間の伝達の式から独立に求める。

テンプレートから組んだ降圧コンバータは、デューティが d_min〜d_max（指令の電源も 0〜1）で止まるので、
不安定なゲインでも出力は有界のリミットサイクルになり、発散しない（止めない。E2 の評価で扱う）。
そのことも、ここで確かめておく（判断を変えたときに、この試験が知らせる）。
"""
from __future__ import annotations

import importlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import (
    DC,
    Command,
    Component,
    DivergenceError,
    Netlist,
    Sine,
    Spec,
    expand,
    simulate,
)
from circuit_sim.control import Pid, PidController
from circuit_sim.tuning.initial import closed_loop_pole_radius

simulate_module = importlib.import_module("circuit_sim.simulate")   # 名前 simulate は関数を指すので
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"
VIN, L, C, R, TS = 24.0, 47e-6, 100e-6, 6.0, 1e-5
UNSTABLE = (0.0117, 5000.0, 0.0)          # Kp、Ki、Kd: 閉ループ極の絶対値の最大 1.15


def buck_spec(fidelity, kp, ki, kd, t_end):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for k, v in (("Kp", kp), ("Ki", ki), ("Kd", kd)):
        d["control"][k]["value"] = v
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    return expand(Spec.model_validate(d))


def unbounded_average_buck(kp, ki, kd, t_end):
    """テンプレートの平均モデルの回路から、デューティの上下限を外したもの。

    PID の出力も、電源の指令も止めない。"""
    ex = buck_spec("average", kp, ki, kd, t_end)
    comps = []
    for c in ex.netlist.components:
        if c.id == "Vleg":
            c = c.model_copy(update={"waveform": Command(command="duty", gain=VIN)})
        comps.append(c)
    pid = Pid(kp, ki, kd, period=TS, t_filter=2.5e-6)          # 上下限なし
    ctrl = PidController(pid, measure="v(out)", output="duty", reference=12.0)
    return Netlist(components=comps), ex.h, ctrl


def test_unstable_unbounded_buck_stops_with_time_and_signal():
    radius = closed_loop_pole_radius(*UNSTABLE, 2.5e-6, VIN, L, C, R, TS)
    assert radius > 1.1
    netlist, h, ctrl = unbounded_average_buck(*UNSTABLE, t_end=50e-3)
    with pytest.raises(DivergenceError) as info:
        simulate(netlist, 50e-3, h, controllers=[ctrl])
    err = info.value
    assert err.code == "RUN_DIVERGED"
    t = err.params["t"].value
    assert 0 < t < 50e-3 and err.params["signal"] in ("v(out)", "i(L1)", "v(sw)", "v(sw_avg)", "i(Vleg)")
    # 止まる時刻: 振れは1制御周期ごとに radius 倍になるので、発散とみなす大きさ（エラーに入る limit）まで
    # 育つのに log(limit / 初めの振れ) / log(radius) 周期。初めの振れ（起動の過渡。指令値 12 V、電流は数 A〜
    # 数十 A）を 1e-3〜1e3 として幅を取る。成長の速さが閉ループの極と合わない誤り（止まるのが早すぎる、
    # 遅すぎる）は、この幅で見つかる
    limit = err.params["limit"].value
    lo = math.log(limit / 1e3) / math.log(radius) * TS
    hi = math.log(limit / 1e-3) / math.log(radius) * TS
    assert lo <= t <= hi, (lo, t, hi)
    assert "発散" in str(err) and "diverged" in err.text("en")
    value = err.params["value"]
    assert value.unit in ("V", "A") and abs(value.value) > err.params["limit"].value


class PositiveFeedback:
    """u[k+1] = 1 + g·v(out)[k]。指令で値が決まる電源 → R → C の閉ループは g > 1 で発散する。"""

    period = 1e-4
    inputs = ("v(out)",)
    outputs = ("u",)

    def __init__(self, g):
        self.g = g

    def reset(self):
        return [1.0]

    def step(self, t, measured):
        return [1.0 + self.g * float(measured[0])]


def rc_with_command():
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=1.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])


def test_divergent_linear_circuit_stops_where_the_discrete_loop_says():
    """v[k+1] = a·v[k] + (1 − a)·u[k]、u[k] = 1 + g·v[k−1]（演算遅れ 1 制御周期）。a = exp(−Ts/RC)。
    特性多項式 z² − a·z − (1 − a)·g の大きい根 ρ で、振れは周期ごとに ρ 倍。"""
    g = 3.0
    a = math.exp(-1e-4 / 1e-3)
    rho = max(abs(np.roots([1.0, -a, -(1 - a) * g])))
    assert rho > 1
    with pytest.raises(DivergenceError) as info:
        simulate(rc_with_command(), 1.0, 1e-5, controllers=[PositiveFeedback(g)])
    err = info.value
    t = err.params["t"].value
    # 目安（電源の gain 1 V と RC から決まる電圧と電流の目安）の 1e12 倍に届く時刻。電圧の目安 ≥ 1 V なので、
    # 少なくとも log(1e12) / log(ρ) 周期。電流の目安は 1 V / 1 kΩ と 1 V·t_end / … で、電圧が先に届く
    limit, value = err.params["limit"].value, err.params["value"].value
    assert abs(value) > limit == pytest.approx(1e12 * err.params["scale"].value)
    lo = math.log(limit / 1e2) / math.log(rho) * 1e-4          # 初めの振れ（数 V、数 mA）を 1e-4〜1e2 とする
    hi = math.log(limit / 1e-4) / math.log(rho) * 1e-4
    assert lo <= t <= hi, (lo, t, hi)
    assert err.params["signal"] in ("v(in)", "v(out)", "i(V1)")


def test_stable_gain_runs_to_the_end():
    """対照: 同じ回路で g = 0.5（安定）は最後まで計算する。"""
    res = simulate(rc_with_command(), 0.05, 1e-5, controllers=[PositiveFeedback(0.5)])
    assert res["v(out)"][-1] == pytest.approx(1.0 / (1 - 0.5), rel=1e-3)


class Exploding(PositiveFeedback):
    def step(self, t, measured):
        return [1e300 if t < 5e-4 else 1e300 * 1e300]       # t = 0.5 ms の演算から inf


def test_circuit_divergence_is_reported_before_the_controller_command():
    """指令 1e300（有限）が t = 0.1 ms から電源に入り、回路の変数が発散とみなす大きさを超える。そのあと
    t = 0.5 ms の演算で指令が inf になるが、知らせるのは先に起きた回路の変数の発散（時刻と変数つき）。
    区切りの位置（確かめの時刻）によらず、同じ時刻と変数になる。"""
    results = []
    for first_chunk in (7, 512, 10**12):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(simulate_module, "CHUNK_STEPS_FIRST", first_chunk)
            with pytest.raises(DivergenceError) as info:
                simulate(rc_with_command(), 1e-2, 1e-5, controllers=[Exploding(0.0)])
        err = info.value
        results.append((err.code, err.params["signal"], err.params["k"]))
    # 指令は 0.1 ms（k = 10）から使われる。その点の記録は変わる前の値（D-048）なので、最初に超えるのは k = 11
    assert results == [("RUN_DIVERGED", "v(in)", 11)] * 3
    assert err.params["t"].value == pytest.approx(11e-5) and err.params["value"].value == pytest.approx(1e300)


class NotANumber(PositiveFeedback):
    def step(self, t, measured):
        return [float("nan")]


def test_command_that_is_not_finite_is_reported_with_time_and_signal():
    """回路の変数は有限のまま、制御器の指令だけが有限でない: 番号 CONTROLLER_COMMAND_NOT_FINITE。
    差し込む値に、時刻と、有限でない指令の名前（結果の名前の形 cmd(名前)）が入る。"""
    with pytest.raises(DivergenceError) as info:
        simulate(rc_with_command(), 1e-2, 1e-5, controllers=[NotANumber(0.0)])
    err = info.value
    assert err.code == "CONTROLLER_COMMAND_NOT_FINITE"
    assert err.params["t"].value == 0.0 and err.params["signal"] == "cmd(u)"
    assert "['u']" in str(err) and "nan" in str(err) and "not finite" in err.text("en")


class Huge(PositiveFeedback):
    def step(self, t, measured):
        return [1e200]


@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
def test_value_that_is_not_finite_is_reported_with_time_and_signal():
    """指令も電源の gain も有限だが、積（1e200 × 1e200）が倍精度をあふれて、電源の値が inf になる:
    番号 RUN_DIVERGED_NOT_FINITE。時刻は、その値が最初に記録された格子点（k = 11）。"""
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=1e200)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])
    with pytest.raises(DivergenceError) as info:
        simulate(netlist, 1e-2, 1e-5, controllers=[Huge(0.0)])
    err = info.value
    assert err.code == "RUN_DIVERGED_NOT_FINITE"
    assert err.params["k"] == 11 and err.params["t"].value == pytest.approx(11e-5)
    assert err.params["signal"] in ("v(in)", "v(out)", "i(V1)")
    assert "有限でなくなった" in str(err) and "stopped being finite" in err.text("en")


def test_template_buck_with_unstable_gains_is_a_bounded_limit_cycle_and_is_not_stopped():
    """デューティの上下限で止まるので発散しない（E2 の評価で、閉ループの安定を判定する）。"""
    for fidelity in ("average", "switching"):
        res = buck_spec(fidelity, *UNSTABLE, 5e-3).run()
        assert np.isfinite(res.x).all()
        assert np.abs(res["v(out)"]).max() < 10 * VIN
        assert set(np.unique(res["cmd(duty)"][len(res.t) // 2:])) >= {0.0, 0.9}       # 上下限に交互に張り付く


def test_undamped_resonance_driven_at_resonance_is_not_divergence():
    """受動素子だけの回路は、共振で振れが育っても（目安の Q 倍、線形に）止めない。LC を共振周波数の正弦波で
    20 万ステップ（共振 2000 周期）駆動すると、振れは電源の約 π·2000 倍（6e3 倍）まで育つが、
    目安の 1e12 倍には遠い。"""
    f0 = 1.0 / (2 * math.pi * math.sqrt(1e-3 * 1e-6))
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Sine(amplitude=1.0, frequency=f0)),
        Component(id="L1", type="L", nodes=("in", "out"), value=1e-3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
        Component(id="R1", type="R", nodes=("out", "0"), value=1e12),
    ])
    res = simulate(netlist, 2000 / f0, 1 / (100 * f0))
    assert np.abs(res["v(out)"]).max() > 1000


def test_variables_without_a_scale_are_checked_for_finiteness_only():
    """目安が 0（電源も初期値も 0）で大きさを決められない変数は、有限かどうかだけを見る:
    大きな有限の値では止めず、inf と NaN で止める（最初の格子点と変数を知らせる）。"""
    scales = {"V": (0.0, "WORD_VOLTAGE"), "A": (0.0, "WORD_CURRENT")}
    for bad in (math.inf, -math.inf, math.nan):
        monitor = simulate_module._Monitor(4, 1e-3, None, None)
        out = np.zeros((5, 2))
        monitor.attach(out, ["v(a)", "i(L1)"], np.array([math.inf, math.inf]), scales)
        out[1] = [1e300, -1e300]
        monitor.check_values(2)                                  # 有限なら、どれだけ大きくても止めない
        out[3, 1], out[4, 0] = bad, bad
        with pytest.raises(DivergenceError) as info:
            monitor.check_values(4)
        err = info.value
        assert err.code == "RUN_DIVERGED_NOT_FINITE" and err.params["signal"] == "i(L1)"
        assert err.params["k"] == 3 and err.params["t"].value == pytest.approx(3e-3)


def test_first_point_and_variable_over_the_limit_are_reported():
    """大きさを超えた最初の格子点と、その点の最初の変数を知らせる（確かめを何回に分けても同じ）。"""
    scales = {"V": (2.0, "WORD_VOLTAGE"), "A": (0.5, "WORD_CURRENT")}
    limits = np.array([2e12, 0.5e12])
    for split in ((6,), (2, 6), (4, 6), (1, 2, 3, 4, 5, 6)):
        monitor = simulate_module._Monitor(6, 1e-3, None, None)
        out = np.ones((7, 2))
        monitor.attach(out, ["v(a)", "i(L1)"], limits, scales)
        out[3] = [1.9e12, 0.4e12]              # どちらも大きさの中（電圧の大きさは電流の大きさより大きい）
        out[4] = [1.9e12, -0.6e12]             # 電流だけが超える
        out[5] = [-3e12, 9e12]
        with pytest.raises(DivergenceError) as info:
            for k in split:
                monitor.check_values(k)
        err = info.value
        assert (err.code, err.params["signal"], err.params["k"]) == ("RUN_DIVERGED", "i(L1)", 4)
        assert err.params["value"].value == -0.6e12 and err.params["limit"].value == 0.5e12
        assert err.params["scale"].value == 0.5 and err.params["value"].unit == "A"


def test_limits_scale_with_the_circuit():
    """目安は電源・初期値と素子の値から決まる: 電源を 1000 倍にすると、発散とみなす大きさも 1000 倍。"""
    from circuit_sim.mna import build_system

    def limits(v):
        net = Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=v)),
            Component(id="R1", type="R", nodes=("a", "b"), value=10.0),
            Component(id="L1", type="L", nodes=("b", "0"), value=1e-3)])
        return simulate_module._divergence_limits(build_system(net), 1e-3)[0]

    assert np.allclose(limits(1000.0), 1000.0 * limits(1.0), rtol=1e-12)
    assert np.all(limits(1.0) >= 1e12)
