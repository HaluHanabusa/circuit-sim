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


class Watching:
    """決めた指令を出しながら、電源の電流と電圧（代数的な変数）を測る制御器。"""

    period = TC
    inputs = ("i(V1)", "v(in)")
    outputs = ("u",)

    def __init__(self):
        self.schedule = Schedule(U0, CHANGES)
        self.seen = []

    def reset(self):
        self.seen = []
        return [self.schedule.at(0.0)]

    def step(self, t, measured):
        self.seen.append((t, measured.copy()))
        # 次の制御周期の先頭から使われる値（時刻 k·h の丸めを吸収する余裕つき）
        return [self.schedule.at(t + self.period, 1e-6 * self.period)]


def test_controller_measures_the_recorded_value_just_before_the_change():
    """制御器が測る値は、記録される値と同じ（control/interface.py: その時刻の切り替えの直前の値）。
    指令が変わる時刻に測った電源の電流と電圧は、変わる前の指令での値。"""
    ctrl = Watching()
    res = simulate(netlist(), T_END, TC / 20, controllers=[ctrl])
    v, u_now, u_before = exact(res.t)
    assert res.stats["command_changes"] == len(CHANGES)
    for t, measured in ctrl.seen:
        k = int(round(t / res.h))
        assert measured[0] == res["i(V1)"][k] and measured[1] == res["v(in)"][k]        # 記録と同じ値
    for tc, _ in CHANGES:
        k = int(round(tc / res.h))
        assert res["cmd(u)"][k - 1] == u_before[k] and res["cmd(u)"][k] == u_now[k]     # ちょうど tc で変わる
        measured = next(m for t, m in ctrl.seen if abs(t - tc) < 1e-12)
        assert measured[1] == pytest.approx(u_before[k], abs=1e-12)
        assert measured[0] == pytest.approx(-(u_before[k] - v[k]) / R, rel=1e-4)
        assert abs(u_now[k] - u_before[k]) >= 2.0                    # 変わった後の値とは 2 V 以上違う


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


class Sequence:
    """測定値によらず、決めた値を制御周期ごとに1つずつ出す制御器。"""

    period = TC
    inputs = ()
    outputs = ("u",)

    def __init__(self, values):
        self.values = list(values)

    def reset(self):
        self.j = 0
        return [self.values[0]]

    def step(self, t, measured):
        self.j += 1
        return [self.values[min(self.j, len(self.values) - 1)]]


def test_a_command_moving_outside_the_limits_is_not_a_change_of_the_source():
    """変わり目かどうかは、上下限で止めた後の「電源の値」で決める（止める前の指令の値では決めない）。

    電源は 値 = 2 × 指令（指令は −0.5〜1 で止める）。指令が上限の外で 1.5 → 2.5 → 4 と動いても、下限の外で
    −0.7 → −3 と動いても、電源の値は変わらない。そこは不連続点ではない: 変わり目に数えず、直後を
    減衰ステップにもしない。止める前の値で決めると、数えすぎ、余分な減衰ステップで結果が変わる。"""
    gain, lower, upper = 2.0, -0.5, 1.0
    raw = [0.2, 1.5, 2.5, 4.0, 0.6, -0.7, -3.0, -0.5, 0.1, 0.1]
    held = [min(max(u, lower), upper) for u in raw]                # 止めた後の指令
    assert held == [0.2, 1.0, 1.0, 1.0, 0.6, -0.5, -0.5, -0.5, 0.1, 0.1]
    assert sum(a != b for a, b in zip(raw, raw[1:], strict=False)) == 8       # 指令そのものが変わる回数
    assert sum(a != b for a, b in zip(held, held[1:], strict=False)) == 4     # 電源の値が変わる回数

    def circuit(**limits):
        source = Command(command="u", gain=gain, **limits)
        return Netlist(components=[
            Component(id="V1", type="V", nodes=("in", "0"), waveform=source),
            Component(id="R1", type="R", nodes=("in", "out"), value=R),
            Component(id="C1", type="C", nodes=("out", "0"), value=C),
        ])

    n = 20                                                         # 制御周期あたりのステップ数
    t_end = len(raw) * TC
    limited = simulate(circuit(lower=lower, upper=upper), t_end, TC / n, controllers=[Sequence(raw)])
    assert limited.stats["command_changes"] == 4 and limited.stats["state_changes"] == 0
    # 指令の記録は、止める前の値のまま（制御器が出した値）
    assert [limited["cmd(u)"][j * n] for j in range(len(raw))] == raw
    # 電源の電圧は、どの格子点でも、その点までの区間の「止めた後の指令 × gain」（変わり目は変わる前の値）。
    # v(in) は方程式を解いた値なので、丸めの分（2 V に対して 1e-12 V）だけ許す
    expected_v = np.array([gain * held[max(k - 1, 0) // n] for k in range(len(limited.t))])
    assert np.max(np.abs(limited["v(in)"] - expected_v)) <= 1e-12
    # 止めた後の値をそのまま指令にした（上下限の無い）計算と、回路の変数がビット単位で同じ
    plain = simulate(circuit(), t_end, TC / n, controllers=[Sequence(held)])
    assert plain.stats["command_changes"] == 4
    assert limited.x.tobytes() == plain.x.tobytes()
    # 解析解（区間ごとの指数関数）との差は、台形則の誤差の大きさ: h = TC/20 = τ/200 で (h/τ)²/12 ≈ 2e-6。
    # 余裕 5 倍の 1e-5（電源の振れ 3 V に対して）
    v, target = 0.0, []
    for u in held:
        target.append(v)
        v = gain * u + (v - gain * u) * math.exp(-TC / TAU)
    at_periods = limited["v(out)"][::n][: len(raw)]
    assert np.max(np.abs(at_periods - np.array(target))) <= 1e-5 * 3.0


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
