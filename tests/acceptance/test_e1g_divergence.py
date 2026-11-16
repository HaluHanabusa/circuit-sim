"""E1g の独立検証（基準12、D-074）: 発散の検出。

確かめること:
  1. 発散する線形の回路（正帰還、上下限なし）で止まる。止まる格子点と変数が、独立の漸化式（区間ごとの厳密な
     指数関数）と合う。発散とみなす大きさが、設計書の式（目安の 1e12 倍）から自分で計算した値と合う。
  2. 上下限を外した不安定な降圧の状態平均化モデルで止まる。止まる時刻が、閉ループの離散モデルの固有値から
     見積もった時刻と合う。
  3. 上下限のある不安定な降圧（テンプレート）は有界で、止めない（D-074）。有界であることは、制御対象が
     受動で安定な線形系で、入力が上下限で限られることから出る上限（インパルス応答の絶対値の積分 ×
     入力の幅）と比べる。
  4. 安定な回路を誤って止めない（共振の強い回路、初期値の大きい回路、目安が 0 に近い回路、時間に
     比例して育つ回路）。
  5. 決めた大きさに届かない発散は止めない（仕様どおり）。値があふれたら、有限でない値として止める。
  6. 知らせる時刻と変数は、区切りの位置によらない。

目安の式（docs/design/accuracy-and-resolution.md 2.7。この試験の中で自分で計算する）:
    電流 I = max(S_I, S_V / Z_min, S_V·t_end / L_min)、電圧 V = max(S_V, I·Z_max, I·t_end / C_min)
    S_V、S_I は電源と初期値の大きさ（上下限の無い指令で決まる電源は |gain|）、Z は抵抗と √(L/C)。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

from circuit_sim import DC, Command, DivergenceError, Sine, Step, expand, simulate
from circuit_sim.messages import SCREEN_NOTICES

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

FACTOR = 1e12

# ------------------------------------------------------------------ 1. 発散する線形の回路
R, C, G, K, TC, H, T_END = 1e3, 1e-6, 10.0, 0.3, 1e-3, 1e-5, 0.2     # ループの利得 G·K = 3
TAU = R * C


def feedback_rc(gain=G):
    return hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=gain)),
                  hp.comp("R1", "R", "in", "out", R), hp.comp("C1", "C", "out", "0", C))


def diverge(**kw) -> DivergenceError:
    with pytest.raises(DivergenceError) as info:
        simulate(feedback_rc(), T_END, H, controllers=[hp.Feedback(K, period=TC)], **kw)
    return info.value


def first_crossing():
    """独立の漸化式: u[j+1] = K·vC(j·Tc)、区間の中は厳密な指数関数。最初に大きさを超える格子点と変数。"""
    i_scale = G / R                                   # S_V / Z_min（L が無い）
    v_scale = max(G, i_scale * R, i_scale * T_END / C)
    n = round(TC / H)
    u, vc = 0.1, 0.0
    for j in range(round(T_END / TC)):
        u_next = K * vc
        for m in range(1, n + 1):
            v_m = G * u + (vc - G * u) * math.exp(-m * H / TAU)
            # 格子点 j·n + m の記録（m = n は次の変わり目の直前の値）: v(in)、v(out)、i(V1) の順
            for name, value, scale in (("v(in)", G * u, v_scale), ("v(out)", v_m, v_scale),
                                       ("i(V1)", -(G * u - v_m) / R, i_scale)):
                if abs(value) > FACTOR * scale:
                    return j * n + m, name, value, FACTOR * scale, scale
        vc = G * u + (vc - G * u) * math.exp(-TC / TAU)
        u = u_next
    raise AssertionError("超えなかった")


def test_diverging_linear_loop_stops_at_the_first_point_over_the_limit():
    err = diverge()
    k, name, value, limit, scale = first_crossing()
    assert (k, name) == (7001, "i(V1)")               # この回路では、電流が先に大きさ（1e10 A）を超える
    p = err.params
    assert err.code == "RUN_DIVERGED" and isinstance(err, ValueError)
    assert p["k"] == k and p["signal"] == name
    assert p["t"].value == pytest.approx(k * H, rel=1e-12) and p["t"].unit == "s"
    # 値は、刻みの誤差の分だけ厳密解と違う（70 制御周期で、1 周期あたり (h/τ)²/12 ≈ 8e-6 の誤差が積もる。
    # 見積もり 70 × 8e-6 × 数倍 ≈ 2e-3 以内。実測 7e-5）
    assert p["value"].value == pytest.approx(value, rel=2e-3) and p["value"].unit == "A"
    assert abs(p["value"].value) > limit
    assert p["limit"].value == pytest.approx(limit, rel=1e-12) and p["limit"].unit == "A"
    assert p["scale"].value == pytest.approx(scale, rel=1e-12)
    assert p["factor"].value == FACTOR


def test_divergence_error_reads_well_in_both_languages_and_maps_to_the_screen_notice():
    err = diverge()
    ja, en = err.text("ja"), err.text("en")
    assert str(err) == ja
    for text in (ja, en):
        assert "i(V1)" in text and "0.07001" in text and "{" not in text
    assert "発散" in ja and "ゲイン" in ja and "diverged" in en and "gains" in en
    assert SCREEN_NOTICES[err.code] == ("N-RUN-DIVERGED",)
    d = err.to_dict()
    assert d["code"] == "RUN_DIVERGED" and d["params"]["signal"] == "i(V1)"
    assert d["params"]["t"] == {"value": pytest.approx(0.07001), "unit": "s"}


@pytest.mark.parametrize("steps", [1, 7, 64, 997, hp.UNCHUNKED])
def test_reported_point_does_not_depend_on_the_chunking(steps):
    reference = diverge()
    with hp.chunk_steps(steps):
        err = diverge()
    assert err.to_dict() == reference.to_dict()
    with hp.chunk_steps(steps):
        err = diverge(progress=lambda f: None, cancel=lambda: False)
    assert err.to_dict() == reference.to_dict()


def test_when_several_variables_cross_at_the_same_point_the_first_one_is_named():
    """同じ格子点で、いくつもの変数が大きさを超えるとき: 知らせるのは、結果の名前の順で最初の変数
    （basic-design.md 6.7 の決まり。決まっていれば、同じ入力で必ず同じ変数になる）。

    帰還の利得を 1e20 にする。時刻 Tc の測定（vC = 0.63 V）から作った指令は、1 制御周期遅れて 3 つ目の
    周期（k = 200 から）で使われる。その最初の格子点（k = 201）で、v(in)、v(out)、i(V1) がいっせいに
    大きさを超える（変わり目の点 k = 200 そのものは、変わる直前の値なので超えない）。
    """
    with pytest.raises(DivergenceError) as info:
        simulate(feedback_rc(), T_END, H, controllers=[hp.Feedback(1e20, period=TC)])
    p = info.value.params
    u = 1e20 * 0.1 * G * (1 - math.exp(-TC / TAU))                # 3 つ目の周期の指令（厳密解）
    v_in, v_out = G * u, G * u * (1 - math.exp(-H / TAU))         # k = 201 の値（v(out) は 1 刻み分の充電）
    i_scale = G / R
    v_scale = max(G, i_scale * R, i_scale * T_END / C)
    assert v_in > FACTOR * v_scale and v_out > FACTOR * v_scale and v_in / R > FACTOR * i_scale
    assert p["k"] == 201 and p["signal"] == "v(in)"
    assert p["value"].value == pytest.approx(v_in, rel=1e-3) and p["value"].unit == "V"
    assert p["limit"].value == pytest.approx(FACTOR * v_scale, rel=1e-12)


def test_growth_below_the_limit_is_not_stopped():
    """決めた大きさ（目安の 1e12 倍）に届かない間は止めない（仕様どおり。不安定の判定は E2a の役目。D-074）。

    同じ回路を、大きさに届く直前（t = 0.068 s）で終える。値は電源の目安 10 V の 5e11 倍まで育っているが、
    結果が返る。
    """
    res = simulate(feedback_rc(), 0.068, H, controllers=[hp.Feedback(K, period=TC)])
    assert np.isfinite(res.x).all()
    assert 1e11 < np.abs(res["v(out)"]).max() / G < 1e12


# ------------------------------------------------------------------ 2. 上下限を外した不安定な降圧

def plant_matrix():
    """制御対象（r_on − L − C∥R_load）の状態方程式の行列。状態は [iL, vC]。"""
    return np.array([[-hp.R_ON / hp.L_BUCK, -1.0 / hp.L_BUCK],
                     [1.0 / hp.C_BUCK, -1.0 / (hp.R_BUCK * hp.C_BUCK)]])


def closed_loop_radius(kp, ki, kd, t_filter=2.5e-6):
    """閉ループ極の絶対値の最大。制御対象は零次ホールドの厳密な離散化、PID は差分方程式を自分で書いたもの。

    状態 [iL, vC, u, I, D, e_prev]。偏差 e = −vC（指令値のまわりの変化分）。指令は 1 制御周期遅れて使われる。
    """
    a_c = plant_matrix()
    b_c = np.array([[hp.VIN / hp.L_BUCK], [0.0]])
    m = expm(np.block([[a_c, b_c], [np.zeros((1, 3))]]) * hp.T_CTRL)
    a_f, b_f = t_filter / (t_filter + hp.T_CTRL), kd / (t_filter + hp.T_CTRL)
    e = np.array([0.0, -1.0, 0.0, 0.0, 0.0, 0.0])
    integral = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0]) + ki * hp.T_CTRL * e
    derivative = a_f * np.array([0.0, 0.0, 0.0, 0.0, 1.0, 0.0]) + b_f * (e - np.array([0, 0, 0, 0, 0, 1.0]))
    s = np.zeros((6, 6))
    s[:2, :2], s[:2, 2] = m[:2, :2], m[:2, 2]
    s[2] = kp * e + integral + derivative
    s[3], s[4], s[5] = integral, derivative, e
    return float(np.abs(np.linalg.eigvals(s)).max())


UNSTABLE = [(0.6, 147.0, 2.74e-6), (0.3, 147.0, 2.74e-6), (0.0117, 147.0, 40e-6), (0.15, 3000.0, 0.0)]


@pytest.mark.parametrize("kp,ki,kd", UNSTABLE)
def test_unlimited_unstable_average_buck_stops_when_the_eigenvalue_says_so(kp, ki, kd):
    """止まる時刻を、固有値から見積もる: 振れが 1 制御周期に ρ 倍。指令値 12 V の大きさから、発散とみなす
    大きさまで ln(大きさ / 12) / ln ρ 周期。

    許容差 ±20%: 見積もりは、最初の振れの大きさ（12 V とした）と、どの変数が先に超えるかを粗く見ている。
    大きさが 12 V の 10 倍違っても、周期数の差は ln 10 / ln(大きさ / 12) ≈ 6%。実測の差は −3〜+10%。
    安定なゲインと取り違える（止まらない）、1 桁早く止まる、を見分けるには十分。
    """
    rho = closed_loop_radius(kp, ki, kd)
    assert rho > 1.05                                             # 不安定（独立に確かめる）
    t_end = 2e-2
    with pytest.raises(DivergenceError) as info:
        simulate(hp.avg_buck(lower=None, upper=None), t_end, 1e-6,
                 controllers=[hp.pid_controller(kp, ki, kd)])
    p = info.value.params
    # 目安（自分の計算）: S_V = Vin × 1、Z = {Rleg, R_load, √(L/C)}
    z = [hp.R_ON, hp.R_BUCK, math.sqrt(hp.L_BUCK / hp.C_BUCK)]
    i_scale = max(hp.VIN / min(z), hp.VIN * t_end / hp.L_BUCK)
    v_scale = max(hp.VIN, i_scale * max(z), i_scale * t_end / hp.C_BUCK)
    limit = FACTOR * (v_scale if p["signal"].startswith("v(") else i_scale)
    assert p["limit"].value == pytest.approx(limit, rel=1e-9)
    assert abs(p["value"].value) > limit
    periods = p["t"].value / hp.T_CTRL
    estimate = math.log(limit / 12.0) / math.log(rho)
    assert periods == pytest.approx(estimate, rel=0.2), (periods, estimate, rho)


def test_the_same_gains_do_not_stop_when_the_loop_is_stable():
    """対になる確かめ: 安定なゲイン（基準の回路の値）では、上下限を外しても止まらず、指令値に落ち着く。"""
    assert closed_loop_radius(0.0117, 147.0, 2.74e-6) < 0.999
    res = simulate(hp.avg_buck(lower=None, upper=None), 2e-2, 1e-6,
                   controllers=[hp.pid_controller(0.0117, 147.0, 2.74e-6)])
    assert res["v(out)"][-1] == pytest.approx(12.0, abs=0.01)


# ------------------------------------------------------------------ 3. 上下限があれば有界（D-074）

def l1_norms():
    """制御対象のインパルス応答 h(t) = e^{At}·B の絶対値の積分（[iL, vC]）。入力はスイッチ節点の電圧。

    入力が [0, U] に収まるなら、初期値 0 の応答は |x(t)| ≤ ‖h‖₁·U（制御器が何を出しても成り立つ）。"""
    a = plant_matrix()
    b = np.array([1.0 / hp.L_BUCK, 0.0])
    lam, vec = np.linalg.eig(a)
    coef = np.linalg.solve(vec, b.astype(complex))
    dt = 5e-8
    t = np.arange(0.0, 40 * 2 * hp.R_BUCK * hp.C_BUCK, dt)        # 減衰の時定数 2RC の 40 倍まで
    h = (vec[:, None, :] * np.exp(np.outer(t, lam))[None, :, :] * coef[None, None, :]).sum(axis=2).real
    return np.abs(h).sum(axis=1) * dt


BOUNDED = [
    ("average", {"Kp": hp.q(0.6, "1/V")}, 0.05),
    ("average", {"Ki": hp.q(3e5, "1/(V·s)")}, 0.05),
    ("average", {"Kd": hp.q(4e-5, "s/V")}, 0.05),
    ("switching", {"Kp": hp.q(0.6, "1/V")}, 0.005),
    ("switching", {"Ki": hp.q(3e5, "1/(V·s)")}, 0.005),
]


@pytest.mark.parametrize("fidelity,gains,t_end", BOUNDED)
def test_limited_unstable_buck_is_bounded_and_is_not_stopped(fidelity, gains, t_end):
    kp = gains.get("Kp", hp.q(0.0117, ""))["value"]
    ki = gains.get("Ki", hp.q(147.0, ""))["value"]
    kd = gains.get("Kd", hp.q(2.74e-6, ""))["value"]
    assert closed_loop_radius(kp, ki, kd) > 1.05                  # 線形の閉ループは不安定
    res = expand(hp.buck_spec(fidelity, control=gains, sim={"t_end": t_end})).run()     # 止まらない
    norm_i, norm_v = l1_norms()
    # 2次系の式 coth(πζ/(2√(1−ζ²))) = 11.2（ζ = 0.057）と同じ大きさ（r_on の分だけ小さい）
    assert norm_v == pytest.approx(11.0, rel=0.02)
    u_max = hp.VIN * (0.9 if fidelity == "average" else 1.0)      # 平均は Vin·d_max、スイッチングは Vin
    v, i = res["v(out)"], res["i(L1)"]
    assert np.abs(v).max() <= norm_v * u_max and np.abs(i).max() <= norm_i * u_max
    # 実際に上下限に張り付いて振れている（安定に落ち着いた回路を「有界」と言っているのではない）
    duty = res["cmd(duty)"]
    assert np.mean((duty <= 0.0) | (duty >= 0.9)) > 0.5
    half = len(v) // 2
    assert np.ptp(v[half:]) > 1.0
    # 振れが育ち続けていない: 後ろ 1/4 の最大が、その前の 1/4 の最大の 1.5 倍以内（上限に対しても 1/3 以下）
    q3, q4 = slice(half, 3 * len(v) // 4), slice(3 * len(v) // 4, len(v))
    assert np.abs(v[q4]).max() <= 1.5 * np.abs(v[q3]).max()
    assert np.abs(v).max() <= norm_v * u_max / 3


# ------------------------------------------------------------------ 4. 安定な回路を誤って止めない

def lossless_resonance():
    """損失の無い直列 LC を、共振の周波数の正弦波で 2000 周期駆動する（1 周期 20 点）。

    台形則では、回路の共振の周波数が (2/h)·atan(ω₀h/2) にずれる（1 周期 20 点で 0.8% 低い）。電源を f₀ の
    ままにすると、ずれた分のうなりで振れが約 120 倍で頭打ちになり、いちばん育つ場合にならない。計算の上での
    共振に合わせて駆動する。
    """
    ind, cap = 1e-3, 1e-6
    f0 = 1 / (2 * math.pi * math.sqrt(ind * cap))
    h = 1 / (f0 * 20)
    f_drive = (2 / h) * math.atan(2 * math.pi * f0 * h / 2) / (2 * math.pi)
    netlist = hp.net(hp.vsrc("V1", "in", "0", Sine(amplitude=1.0, frequency=f_drive)),
                     hp.comp("L1", "L", "in", "out", ind), hp.comp("C1", "C", "out", "0", cap))
    return netlist, 2000 / f0, h, {}


STABLE = {
    # 損失の無い LC を共振で駆動する（振れが時間に比例して育つ。2000 周期で電源の約 6000 倍）
    "lossless_lc_driven_at_resonance": lossless_resonance,
    "rlc_with_micro_ohm": lambda: (hp.rlc(1e-6), 0.05, 1e-6, {}),
    "rc_initial_1e9_volt_source_1e-6": lambda: (hp.rc(DC(value=1e-6), ic=1e9), 5e-3, 1e-5, {}),
    "rlc_huge_initial_values_no_source": lambda: (
        hp.rlc(1.0, waveform=DC(value=0.0), il=1e6, vc=-1e7), 5e-3, 1e-6, {}),
    "source_1e-300_volt": lambda: (hp.rc(Step(final=1e-300)), 5e-3, 1e-5, {}),
    "source_smallest_positive_number": lambda: (hp.rlc(1.0, waveform=Step(final=5e-324)), 5e-3, 1e-6, {}),
    "no_source_no_initial_value": lambda: (hp.rlc(1.0, waveform=DC(value=0.0)), 5e-3, 1e-6, {}),
    "initial_1e-310_volt_no_source": lambda: (hp.rlc(1.0, waveform=DC(value=0.0), vc=1e-310), 5e-3, 1e-6, {}),
    "source_1e290_volt": lambda: (hp.rc(Step(final=1e290)), 5e-3, 1e-5, {}),
    "current_source_into_1pF": lambda: (
        hp.net(hp.isrc("I1", "0", "a", DC(value=1.0)), hp.comp("C1", "C", "a", "0", 1e-12)), 1.0, 1e-4, {}),
    "voltage_source_across_1nH": lambda: (
        hp.net(hp.vsrc("V1", "a", "0", DC(value=1.0)), hp.comp("L1", "L", "a", "0", 1e-9)), 1.0, 1e-4, {}),
    "boost_duty_0_99": lambda: (hp.net(
        hp.vsrc("V1", "in", "0", DC(value=12.0)), hp.comp("L1", "L", "in", "sw", 100e-6),
        hp.comp("S1", "S", "sw", "0", gate="g"), hp.comp("D1", "D", "sw", "out"),
        hp.comp("C1", "C", "out", "0", 47e-6), hp.comp("R1", "R", "out", "0", 1e4)),
        4e-3, None, {"gates": hp.gate(0.99, 5e4)}),
    "buck_output_precharged_to_1e6_volt": lambda: (hp.buck_diode(vc0=1e6), 5e-4, None, {"gates": hp.gate()}),
}


@pytest.mark.parametrize("case", list(STABLE))
def test_stable_corner_circuits_are_not_stopped(case):
    netlist, t_end, h, kw = STABLE[case]()
    res = simulate(netlist, t_end, h, **kw)                       # DivergenceError にならない
    assert np.isfinite(res.x).all()


def test_resonance_growth_is_far_below_the_limit():
    """根拠の確かめ: 共振で育つ振れは時間に比例する。2000 周期で、電源の大きさの 2000·π ≈ 6283 倍。

    包絡線は ω₀·t/2。1 周期 20 点の台形則では、育つ速さが数 % 違う（許容差 5%。(ω₀h)²/12 = 0.8% の数倍）。
    発散とみなす大きさ（目安の 1e12 倍）に対して、8 桁以上の余裕がある。"""
    netlist, t_end, h, _ = lossless_resonance()
    res = simulate(netlist, t_end, h)
    peak = np.abs(res["v(out)"]).max()
    assert peak == pytest.approx(2000 * math.pi, rel=0.05)
    assert peak < 1e-8 * FACTOR


# ------------------------------------------------------------------ 5. 有限でない値

def test_overflow_is_reported_as_not_finite():
    """目安が倍精度の上限に近い回路（電源 1e300 V）: 大きさを決められないので、あふれた点で止める。"""
    with pytest.raises(DivergenceError) as info:
        with np.errstate(all="ignore"):
            simulate(feedback_rc(gain=1e300), 0.5, H, controllers=[hp.Feedback(3e-300, period=TC)])
    err = info.value
    assert err.code == "RUN_DIVERGED_NOT_FINITE" and SCREEN_NOTICES[err.code] == ("N-RUN-DIVERGED",)
    assert err.params["signal"] in ("v(in)", "v(out)", "i(V1)") and 0.0 < err.params["t"].value < 0.5
    assert "{" not in err.text("ja") + err.text("en")


class BadAt:
    """j 回目の演算で、有限でない指令を返す制御器。"""

    inputs, outputs, period = (), ("u",), TC

    def __init__(self, j_bad, value):
        self.j_bad, self.value = j_bad, value

    def reset(self):
        self.j = 0
        return [0.1]

    def step(self, t, measured):
        self.j += 1
        return [self.value if self.j == self.j_bad else 0.1]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_controller_command_names_the_command_and_the_time(value):
    with pytest.raises(DivergenceError) as info:
        simulate(feedback_rc(), 0.02, H, controllers=[BadAt(5, value)])
    err = info.value
    assert err.code == "CONTROLLER_COMMAND_NOT_FINITE" and SCREEN_NOTICES[err.code] == ("N-RUN-DIVERGED",)
    assert err.params["signal"] == "cmd(u)"                       # 結果の名前の形（画面の {signal} に使う）
    assert err.params["t"].value == pytest.approx(4 * TC)         # 5 回目の演算は t = 4·Tc の測定
