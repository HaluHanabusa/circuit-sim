"""E1g の範囲5（受け入れ基準6）: 指令で値が決まる電源の変わり目では、代数的な変数（電源の電流など）を
変わる直前の値で記録する（スイッチの切り替え点と同じ。D-019、D-048）。積分の精度（台形則の2次）は保つ。

回路: 指令で値が決まる電源 V1（値 = 指令 u）→ R1 → C1。
指令は時刻で決まる区分一定の値（1 ms と 2 ms で変わる）。
解析解: 区間ごとに v_C は指数関数で u に近づき、電源の電流（V1 の中を ＋ → − が正）は i(V1) = −(u − v_C) / R。
変わり目 t_c の記録の期待値は、変わる直前の u を使った −(u_old − v_C(t_c)) / R（左からの極限）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from circuit_sim import Command, Component, Netlist, Step, simulate
from circuit_sim.control import Schedule, ScheduleController

R, C = 1e3, 1e-6
TAU = R * C
TC = 1e-4                                       # 制御周期
CHANGES = ((1e-3, 3.0), (2e-3, -1.0))
U0 = 1.0
T_END = 3e-3


def netlist():
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=1.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
    ])


def run(n_per_period):
    ctrl = ScheduleController(Schedule(U0, CHANGES), "u", period=TC)
    return simulate(netlist(), T_END, TC / n_per_period, controllers=[ctrl])


def exact(t):
    """v_C(t) と、各時刻の u（区間の値。変わり目では変わる前の値も返す）。"""
    times = [0.0] + [tc for tc, _ in CHANGES] + [math.inf]
    values = [U0] + [u for _, u in CHANGES]
    v = np.empty_like(t)
    u_now = np.empty_like(t)
    u_before = np.empty_like(t)
    v_start = 0.0
    for j in range(len(values)):
        a, b = times[j], times[j + 1]
        inside = (t >= a - 1e-15) & (t < b - 1e-15)
        v[inside] = values[j] + (v_start - values[j]) * np.exp(-(t[inside] - a) / TAU)
        u_now[inside] = values[j]
        u_before[inside] = values[j]
        if j > 0:
            at = np.abs(t - a) < 1e-15
            u_before[at] = values[j - 1]
        if math.isfinite(b):
            v_start = values[j] + (v_start - values[j]) * math.exp(-(b - a) / TAU)
    return v, u_now, u_before


def errors(res):
    v, _, u_before = exact(res.t)
    i_expected = -(u_before - v) / R
    return (np.max(np.abs(res["v(out)"] - v)) / max(abs(U0), 3.0),
            np.max(np.abs(res["i(V1)"] - i_expected)) / (3.0 / R))


def test_source_current_at_the_change_is_the_value_just_before():
    res = run(20)
    v, u_now, u_before = exact(res.t)
    for tc, _ in CHANGES:
        k = int(round(tc / res.h))
        before = -(u_before[k] - res["v(out)"][k]) / R
        after = -(u_now[k] - res["v(out)"][k]) / R
        assert res["i(V1)"][k] == pytest.approx(before, rel=1e-9, abs=1e-12)
        assert abs(after - before) > 1e-3                         # 変わった後の値とは見分けられる
        assert res["v(in)"][k] == pytest.approx(u_before[k], abs=1e-12)      # 電源の電圧も変わる直前の値
        assert res["cmd(u)"][k] == u_now[k]                       # 指令は区間 [t_k, t_k+1) の値（そのまま）
    assert res.stats["command_changes"] == len(CHANGES)


def test_second_order_is_kept_when_halving_the_step():
    """刻みを半分にしていくと、v(out) と i(V1) の誤差が約 1/4 になる（比 3.5 以上）。

    基準は「誤差の比 ≥ 3.5」（仕様の受け入れ基準6）。後退オイラーに落とす（1次）と比は約 2 になり、
    変わり目で変わった後の値を記録すると（E1g の前の形）、i(V1) の誤差が刻みによらず 4 mA（u が 3 V → −1 V に
    変わる分。3 V / 1 kΩ に対して 1.33）残って比は 1 になる（E1g の前のコードで確かめた）。
    """
    errs = [errors(run(n)) for n in (10, 20, 40, 80)]
    for (ev0, ei0), (ev1, ei1) in zip(errs, errs[1:], strict=False):
        assert ev0 / ev1 >= 3.5, errs
        assert ei0 / ei1 >= 3.5, errs
    assert errs[-1][0] < 1e-5 and errs[-1][1] < 1e-5


def test_time_defined_step_still_records_the_value_after():
    """時刻で決まる電源のステップは、これまでどおり変わった後の値（D-019）。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(initial=1.0, final=3.0, t_step=1e-3)),
        Component(id="R1", type="R", nodes=("in", "out"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
    ])
    ctrl = ScheduleController(Schedule(0.0), "unused", period=TC)        # 制御器のある経路で計算する
    res = simulate(net, T_END, TC / 20, controllers=[ctrl])
    k = int(round(1e-3 / res.h))
    assert res["v(in)"][k] == pytest.approx(3.0, abs=1e-12)
    assert res["i(V1)"][k] == pytest.approx(-(3.0 - res["v(out)"][k]) / R, rel=1e-9)
