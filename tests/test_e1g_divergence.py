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

値が倍精度をあふれるまで発散したときも、numpy の警告（RuntimeWarning）ではなく、番号つきのエラーで知らせる
（警告を例外にする python -W error でも同じ）。目安を決められない極端な値の回路は、有限かどうかだけを見る。
"""
from __future__ import annotations

import importlib
import json
import math
import warnings
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
    Step,
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


def test_value_that_is_not_finite_is_reported_with_time_and_signal():
    """指令も電源の gain も有限だが、積（1e200 × 1e200）が倍精度をあふれて、電源の値が inf になる:
    番号 RUN_DIVERGED_NOT_FINITE。時刻は、その値が最初に記録された格子点（k = 11）。
    警告を例外にしていても（python -W error）、numpy の警告ではなく、このエラーで終わる。"""
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=1e200)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(DivergenceError) as info:
            simulate(netlist, 1e-2, 1e-5, controllers=[Huge(0.0)])
    err = info.value
    assert err.code == "RUN_DIVERGED_NOT_FINITE"
    assert err.params["k"] == 11 and err.params["t"].value == pytest.approx(11e-5)
    assert err.params["signal"] in ("v(in)", "v(out)", "i(V1)")
    assert "有限でなくなった" in str(err) and "stopped being finite" in err.text("en")


class Growing(PositiveFeedback):
    """u[k+1] = g·v(out)[k]（上下限なし）。電源の gain との積が 1 を超えると、値は制御周期ごとに育つ。"""

    def reset(self):
        return [0.1]

    def step(self, t, measured):
        return [self.g * float(measured[0])]


LOOP_GAIN = 4.0


def overflowing_loop():
    """電源の目安が 1e305 V の正帰還（ループの利得 4。RC = 制御周期 = 0.1 ms）。発散とみなす大きさ
    （目安の 1e12 倍）が倍精度に収まらないので、値があふれるまで進む。"""
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=1e305)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-7),
    ])
    return lambda: simulate(netlist, 5e-2, 1e-5, controllers=[Growing(LOOP_GAIN * 1e-305)])


def overflowing_step():
    """スイッチの無い回路: 電源が 1.7e308 V にステップし、1 mΩ を流れる電流（1.7e311 A）があふれる。"""
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=1.7e308, t_step=1e-3)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e-3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])
    return lambda: simulate(netlist, 5e-3, 1e-5)


@pytest.mark.parametrize("case", ["loop", "step"])
def test_overflow_is_reported_as_divergence_and_never_as_a_numpy_warning(case):
    """値が倍精度をあふれても、numpy の警告（overflow / invalid value encountered。番号の無い英語の文）を
    出さずに、番号つきの発散のエラーで知らせる。警告を例外にしていても（python -W error）同じ。

    直す前は、あふれた後の1ステップごとに RuntimeWarning が出て、警告を例外にしていると、発散のエラーの
    代わりに RuntimeWarning で落ちた。"""
    run = {"loop": overflowing_loop, "step": overflowing_step}[case]()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(DivergenceError) as info:
            run()
    assert [str(w.message) for w in caught] == []
    first = info.value
    assert first.code == "RUN_DIVERGED_NOT_FINITE" and first.params["signal"] in ("v(in)", "v(out)", "i(V1)")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(DivergenceError) as info:
            run()
    assert info.value.to_dict() == first.to_dict()                # 警告の扱いによらず、同じ時刻と変数
    if case == "step":
        # ステップの格子点（k = 100）で、電源の電流があふれる（電源の電圧 1.7e308 V は、まだ有限）
        assert first.params["k"] == 100 and first.params["signal"] == "i(V1)"
    else:
        # v[j+1] = a·v[j] + (1 − a)·g·v[j−1]（a = exp(−Ts/RC) = e^−1、演算遅れ 1 周期）。振れは制御周期
        # （10 ステップ）ごとに、z² − a·z − (1 − a)·g = 0 の大きい根 ρ 倍。初めの振れ（最初の指令 0.1 での
        # 電源 1e304 V の応答）を 1e303〜1e305 V として、倍精度の上限（約 1.8e308）に届くまでの周期の数に
        # 幅を取る
        a = math.exp(-1.0)
        rho = max(abs(np.roots([1.0, -a, -(1 - a) * LOOP_GAIN])))
        top = np.finfo(float).max
        lo = math.log(top / 1e305) / math.log(rho) * 10
        hi = math.log(top / 1e303) / math.log(rho) * 10
        assert 1.7 < rho < 1.9 and lo <= first.params["k"] <= hi, (lo, first.params["k"], hi)


class Peeking(PositiveFeedback):
    """演算のたびに、そのときの numpy の浮動小数点の例外の扱い（np.geterr）を控える。"""

    def __init__(self, g):
        super().__init__(g)
        self.states = []

    def step(self, t, measured):
        self.states.append(np.geterr())
        return super().step(t, measured)


# 呼び出し側の設定（既定と違う値にしておく）と、時間ステップのあいだの設定（あふれと無効の演算だけを止める）
CALLER_STATE = {"divide": "raise", "over": "raise", "under": "ignore", "invalid": "raise"}
QUIET_STATE = {"divide": "raise", "over": "ignore", "under": "ignore", "invalid": "ignore"}


@pytest.mark.parametrize("g", [0.5, 3.0])
def test_numpy_error_handling_of_the_caller_is_put_back_after_the_run(g):
    """警告を止めるのは、時間ステップのあいだの「あふれ」と「無効の演算」だけ（0 で割る、は止めない）。
    計算が終わったら（最後まで計算しても、発散のエラーで終わっても）、呼び出し側の設定に戻す。"""
    with np.errstate(**CALLER_STATE):
        before = np.geterr()
        ctrl = Peeking(g)
        if g > 1:
            with pytest.raises(DivergenceError):
                simulate(rc_with_command(), 1.0, 1e-5, controllers=[ctrl])
        else:
            simulate(rc_with_command(), 5e-3, 1e-5, controllers=[ctrl])
        assert np.geterr() == before
    assert before == CALLER_STATE and ctrl.states and all(state == QUIET_STATE for state in ctrl.states)


@pytest.mark.parametrize("diverging", [False, True])
def test_numpy_error_handling_is_put_back_after_a_run_without_switches(diverging):
    """スイッチも制御器も無い回路の経路でも同じ。途中の設定は、進み具合を受け取る関数の中から見る。"""
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"),
                  waveform=Step(final=1.7e308 if diverging else 1.0, t_step=1e-3)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e-3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])
    states = []
    with np.errstate(**CALLER_STATE), pytest.MonkeyPatch.context() as mp:
        mp.setattr(simulate_module, "PROGRESS_SECONDS", 0.0)
        mp.setattr(simulate_module, "CHUNK_STEPS_FIRST", 20)       # ステップの前（k = 100 の前）にも知らせる
        before = np.geterr()
        if diverging:
            with pytest.raises(DivergenceError):
                simulate(netlist, 5e-3, 1e-5, progress=lambda f: states.append((f, np.geterr())))
        else:
            simulate(netlist, 5e-3, 1e-5, progress=lambda f: states.append((f, np.geterr())))
        assert np.geterr() == before
    during = [state for f, state in states if 0.02 < f < 0.99]     # 時間ステップのあいだの知らせ
    assert during and all(state == QUIET_STATE for state in during)


def test_scale_that_cannot_be_determined_means_finiteness_only():
    """目安を決められない極端な値の回路（√(L/C) が 0 に下あふれする）は、大きさの確かめをせず、有限か
    どうかだけを見る。見積もりの途中の 0 での割り算で、番号の無い例外にしない（E1g の前は計算できていた）。

    回路: Vin 1e-30 V、R 1e-9 Ω、L 1e-30 H、C 1e300 F の直列 RLC。過減衰で、時定数は RC = 1e291 s と
    L/R = 1e-21 s。既定の終了時刻（遅い時定数の 5 倍）で、v(out) = Vin·(1 − e^−5)、i = (Vin/R)·e^−5。"""
    from circuit_sim.mna import build_system

    plant = {"Vin": (1e-30, "V"), "R": (1e-9, "ohm"), "L": (1e-30, "H"), "C": (1e300, "F")}
    spec = Spec.model_validate({"template": "rlc_series", "plant": {
        k: {"value": v, "unit": u} for k, (v, u) in plant.items()}})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")           # 展開（極の計算）の中のあふれの警告は、ここでは見ない
        ex = expand(spec)
    assert math.sqrt(1e-30 / 1e300) == 0.0
    limits, scales = simulate_module._divergence_limits(build_system(ex.netlist), ex.t_end)
    assert np.all(np.isinf(limits)) and limits.shape == (5,)
    res = ex.run()
    assert np.isfinite(res.x).all()
    assert res["v(out)"][-1] == pytest.approx(1e-30 * (1 - math.exp(-5)), rel=1e-6)
    assert res["i(L1)"][-1] == pytest.approx(1e-21 * math.exp(-5), rel=1e-6)


@pytest.mark.parametrize("case, expected", [
    # 電源 10 V、R 10 Ω、L 1 mH、t_end 1 ms: I = max(10/10, 10·1e-3/1e-3) = 10 A、V = max(10, 10·10) = 100 V
    ("plain", (1e14, 1e13)),
    # 電源 1e300 V: 目安の 1e12 倍が倍精度をあふれる
    ("huge_source", (math.inf, math.inf)),
    # 電源も初期値も 0: 目安が 0
    ("zero", (math.inf, math.inf)),
    # R 1e-200 Ω: 電流の目安 1e200 A の 1e12 倍は決まる。電圧の目安（I·t_end/C = 1e306 V）の 1e12 倍はあふれる
    ("voltage_only", (math.inf, 1e212)),
])
def test_divergence_limits_for_plain_and_extreme_circuits(case, expected):
    """発散とみなす大きさ（電圧、電流）。決められるときは式どおり、決められないときは無限大。"""
    from circuit_sim.mna import build_system

    def comp(cid, typ, a, b, value):
        return Component(id=cid, type=typ, nodes=(a, b), value=value)

    def source(value):
        return Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=value))

    nets = {
        "plain": [source(10.0), comp("R1", "R", "a", "b", 10.0), comp("L1", "L", "b", "0", 1e-3)],
        "huge_source": [source(1e300), comp("R1", "R", "a", "b", 10.0), comp("L1", "L", "b", "0", 1e-3)],
        "zero": [source(0.0), comp("R1", "R", "a", "b", 10.0), comp("L1", "L", "b", "0", 1e-3)],
        "voltage_only": [source(1.0), comp("R1", "R", "a", "b", 1e-200), comp("C1", "C", "b", "0", 1e-109)],
    }
    system = build_system(Netlist(components=nets[case]))
    limits, _ = simulate_module._divergence_limits(system, 1e-3)
    want_v, want_i = expected
    for name, limit in zip(system.unknowns, limits, strict=True):
        want = want_v if name.startswith("v(") else want_i
        assert limit == pytest.approx(want, rel=1e-12) if math.isfinite(want) else limit == math.inf, name


def test_template_buck_with_unstable_gains_is_a_bounded_limit_cycle_and_is_not_stopped():
    """デューティの上下限で止まるので発散しない（E2 の評価で、閉ループの安定を判定する）。"""
    for fidelity in ("average", "switching"):
        res = buck_spec(fidelity, *UNSTABLE, 5e-3).run()
        assert np.isfinite(res.x).all()
        assert np.abs(res["v(out)"]).max() < 10 * VIN
        assert set(np.unique(res["cmd(duty)"][len(res.t) // 2:])) >= {0.0, 0.9}       # 上下限に交互に張り付く


@pytest.mark.parametrize("tuned", [False, True])
def test_undamped_resonance_driven_at_resonance_is_not_divergence(tuned):
    """受動素子だけの回路は、共振で振れが育っても（時間に比例して）止めない。損失の無い LC を共振の周波数の
    正弦波で駆動すると、コンデンサ電圧の振れは 電源 × ω₀t/2 で育つ（2000 周期で π·2000 ≈ 6283 倍）。
    目安の 1e12 倍には遠い。

    tuned = True : 電源の周波数を、計算の上での共振の周波数（台形則では (2/h)·atan(ω₀h/2)）に合わせる。
                   振れは理論の π·2000 倍になる（許容差 1%: 1 周期 100 点の、振幅の読み取りの分）。
    tuned = False: 電源を f₀ のままにする。台形則の共振の周波数は f₀ より δ = (ω₀h)²/12 = 3.3e-4（相対）だけ
                   低いので、うなりになる。包絡線は 電源 × (1/δ)·|sin(δ·ω₀t/2)| で、山は 1/δ 倍
                   （1 周期 100 点で 3040 倍。2000 周期の中に山が入る: δ·ω₀t/2 = 2.07 > π/2）。
    どちらも止めない。"""
    f0 = 1.0 / (2 * math.pi * math.sqrt(1e-3 * 1e-6))
    h = 1 / (100 * f0)
    f_numeric = (2 / h) * math.atan(2 * math.pi * f0 * h / 2) / (2 * math.pi)
    f = f_numeric if tuned else f0
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Sine(amplitude=1.0, frequency=f)),
        Component(id="L1", type="L", nodes=("in", "out"), value=1e-3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
        Component(id="R1", type="R", nodes=("out", "0"), value=1e12),
    ])
    res = simulate(netlist, 2000 / f, h)
    peak = np.abs(res["v(out)"]).max()
    if tuned:
        assert peak == pytest.approx(math.pi * 2000, rel=0.01)
    else:
        delta = (2 * math.pi / 100) ** 2 / 12                      # 共振の周波数のずれ（相対）
        assert delta * math.pi * 2000 > math.pi / 2                # うなりの山が、計算の中に入っている
        assert peak == pytest.approx(1 / delta, rel=0.01) and peak > 1000


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
