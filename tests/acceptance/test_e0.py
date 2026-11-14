"""E0 の受け入れ試験（検証担当が追加）。

対象: R / L / C / 電源のソルバ核と、仕様JSON（層A）のスキーマ。
基準と実測値は `docs/verification/E0.md` にある。

期待値の作り方（実装と独立にするための決まり）:
- 解析解はこのファイルの中に式で書く（特性方程式の根から組む）。
- 多状態の回路は、状態方程式 x' = A x + b を手で導き、行列指数関数（scipy.linalg.expm）で厳密解を作る。
  製品は修正節点解析＋台形則なので、式の立て方も時間方向の解き方も別経路になる。
- 正弦波の定常応答はフェーザ（複素インピーダンス）で計算する。

許容差の根拠:
- 解析解との差 1%（受け入れ基準 1）。台形則の局所誤差は (h*λ)^2 / 12 程度で、1周期を200分割すると
  0.01〜0.1% になる。1% はその 10 倍以上緩く、符号や係数の誤り（誤差は数十%以上になる）は確実に落ちる。
- 収束次数は誤差比 3.6〜4.4（理論値 4 の ±10%）。1次精度なら 2、3次なら 8 なので区別できる。
- t=0 の整合と保存則は、丸め誤差だけが残るはずなので 1e-9（相対）。

製品側の不具合で落ちるテストには xfail(strict=True) を付けてある。直ったら印を外すこと。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

from circuit_sim import (
    DC,
    CircuitError,
    Component,
    MissingValueError,
    Netlist,
    Sine,
    Spec,
    Step,
    expand,
    kernel,
    simulate,
)
from circuit_sim.__main__ import main as cli_main

REPO = Path(__file__).resolve().parents[2]
TOL = 0.01          # 受け入れ基準 1: 信号の代表値の 1%
ORDER_BAND = (3.6, 4.4)


# ---------------------------------------------------------------- 部品と参照解

def V(cid, a, b, wave):
    return Component(id=cid, type="V", nodes=(a, b), waveform=wave)


def I(cid, a, b, wave):  # noqa: E743  （素子の記号に合わせた名前）
    return Component(id=cid, type="I", nodes=(a, b), waveform=wave)


def R(cid, a, b, value):
    return Component(id=cid, type="R", nodes=(a, b), value=value)


def L(cid, a, b, value, ic=0.0):
    return Component(id=cid, type="L", nodes=(a, b), value=value, ic=ic)


def C(cid, a, b, value, ic=0.0):
    return Component(id=cid, type="C", nodes=(a, b), value=value, ic=ic)


def net(*comps):
    return Netlist(components=list(comps))


def rc_exact(t, v_in, r, c, v0=0.0):
    """RC のステップ応答。戻り値: コンデンサ電圧、電源から流れ出す電流。"""
    tau = r * c
    v = v_in + (v0 - v_in) * np.exp(-t / tau)
    return v, (v_in - v) / r


def rlc_exact(t, v_in, r, ind, cap):
    """直列RLCのステップ応答（初期値ゼロ）。特性方程式 s^2 + (R/L)s + 1/(LC) = 0 の根から組む。

    戻り値: コンデンサ電圧、インダクタ電流。
    """
    a = r / (2 * ind)
    w0 = 1 / math.sqrt(ind * cap)
    disc = a * a - w0 * w0
    if abs(disc) < 1e-12 * w0 * w0:                     # 臨界減衰（重根）
        v = v_in * (1 - np.exp(-a * t) * (1 + a * t))
        i = v_in / ind * t * np.exp(-a * t)
    elif disc < 0:                                      # 不足減衰
        wd = math.sqrt(-disc)
        v = v_in * (1 - np.exp(-a * t) * (np.cos(wd * t) + a / wd * np.sin(wd * t)))
        i = v_in / (wd * ind) * np.exp(-a * t) * np.sin(wd * t)
    else:                                               # 過減衰
        s1, s2 = -a + math.sqrt(disc), -a - math.sqrt(disc)
        v = v_in * (1 - (s2 * np.exp(s1 * t) - s1 * np.exp(s2 * t)) / (s2 - s1))
        i = v_in / (ind * (s1 - s2)) * (np.exp(s1 * t) - np.exp(s2 * t))
    return v, i


def linear_exact(a_mat, b_of_step, x0, n_steps, h):
    """x' = A x + b の厳密解（b は1ステップの間は一定）。行列指数関数で進める。

    b_of_step(k) は区間 [t_k, t_k+1] での b を返す。
    """
    n = len(x0)
    out = np.empty((n_steps + 1, n))
    z = np.append(np.asarray(x0, dtype=float), 1.0)
    cache: dict[bytes, np.ndarray] = {}
    for k in range(n_steps):
        out[k] = z[:n]
        b = np.asarray(b_of_step(k), dtype=float)
        key = b.tobytes()
        if key not in cache:
            m = np.zeros((n + 1, n + 1))
            m[:n, :n] = a_mat
            m[:n, n] = b
            cache[key] = expm(m * h)
        z = cache[key] @ z
    out[n_steps] = z[:n]
    return out


def series_rlc(v_wave, r, ind, cap, il0=0.0, vc0=0.0):
    return net(
        V("V1", "in", "0", v_wave),
        R("R1", "in", "a", r),
        L("L1", "a", "out", ind, ic=il0),
        C("C1", "out", "0", cap, ic=vc0),
    )


def rc_net(v_wave, r, c, vc0=0.0):
    return net(V("V1", "in", "0", v_wave), R("R1", "in", "out", r), C("C1", "out", "0", c, ic=vc0))


def spec_of(template, simulation=None, **values):
    plant = {k: {"value": v, "unit": u} for k, (v, u) in values.items()}
    return Spec.model_validate({"template": template, "plant": plant, "simulation": simulation or {}})


def max_err(a, b, scale):
    return float(np.max(np.abs(np.asarray(a) - np.asarray(b))) / scale)


def product_bug(severity, *lines):
    """製品側の不具合で落ちるテストに付ける印。直ったら（XPASS で失敗するので）印を外す。"""
    return pytest.mark.xfail(strict=True, reason=f"製品の不具合({severity}): " + "".join(lines))


# ============================================================ 基準 1: 解析解

def test_c1_rc_step_all_variables():
    v_in, r, c = 5.0, 1e3, 1e-6
    ex = expand(spec_of("rc_lowpass", Vin=(v_in, "V"), R=(r, "ohm"), C=(c, "F")))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    v, i = rc_exact(res.t, v_in, r, c)
    assert max_err(res["v(out)"], v, v_in) < TOL
    assert max_err(res["v(in)"], v_in, v_in) < 1e-9
    # 電圧源の電流は n+ → n-（素子の中）を正とする。供給中は負。
    assert max_err(res["i(V1)"], -i, v_in / r) < TOL


RLC_CASES = {
    "不足減衰 ζ=0.05": (10.0, 1.0, 1e-3, 10e-6),
    "不足減衰 ζ=0.5": (10.0, 10.0, 1e-3, 10e-6),
    "臨界減衰 ζ=1": (10.0, 20.0, 1e-3, 10e-6),
    "過減衰 ζ=3": (10.0, 60.0, 1e-3, 10e-6),
}


@pytest.mark.parametrize("name", list(RLC_CASES))
def test_c1_series_rlc_step_all_variables(name):
    v_in, r, ind, cap = RLC_CASES[name]
    period = 2 * math.pi * math.sqrt(ind * cap)
    res = simulate(series_rlc(Step(final=v_in), r, ind, cap), 12 * period, period / 200)
    v, i = rlc_exact(res.t, v_in, r, ind, cap)
    i_peak = float(np.max(np.abs(i)))
    assert max_err(res["v(out)"], v, v_in) < TOL
    assert max_err(res["i(L1)"], i, i_peak) < TOL
    assert max_err(res["i(V1)"], -i, i_peak) < TOL
    assert max_err(res["v(a)"], v_in - r * i, v_in) < TOL
    if "過減衰" in name or "臨界" in name:
        assert res["v(out)"].max() <= v_in * (1 + 1e-9)      # 行き過ぎない


@pytest.mark.parametrize("r", [1.0, 60.0])
def test_c1_rlc_template_default_timing(r):
    """テンプレートが決めた刻みと計算時間でも 1% 以内。"""
    v_in, ind, cap = 10.0, 1e-3, 10e-6
    ex = expand(spec_of("rlc_series", Vin=(v_in, "V"), R=(r, "ohm"), L=(ind, "H"), C=(cap, "F")))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    v, i = rlc_exact(res.t, v_in, r, ind, cap)
    assert max_err(res["v(out)"], v, v_in) < TOL
    assert max_err(res["i(L1)"], i, float(np.max(np.abs(i)))) < TOL
    assert res["v(out)"][-1] == pytest.approx(v_in, rel=0.02)   # 5時定数ぶん計算している


def test_c1_overshoot_matches_theory():
    v_in, r, ind, cap = 10.0, 1.0, 1e-3, 10e-6
    zeta = r / 2 * math.sqrt(cap / ind)
    period = 2 * math.pi * math.sqrt(ind * cap)
    res = simulate(series_rlc(Step(final=v_in), r, ind, cap), 3 * period, period / 400)
    expected = math.exp(-math.pi * zeta / math.sqrt(1 - zeta**2))
    assert res["v(out)"].max() / v_in - 1 == pytest.approx(expected, rel=TOL)


# ========================================================== 基準 2: 収束次数

def _ratios(errors):
    return [errors[k] / errors[k + 1] for k in range(len(errors) - 1)]


def _order_rc():
    v_in, r, c = 5.0, 1e3, 1e-6
    tau = r * c
    out = []
    for n in (40, 80, 160):
        res = simulate(rc_net(Step(final=v_in), r, c), 5 * tau, tau / n)
        out.append(max_err(res["v(out)"], rc_exact(res.t, v_in, r, c)[0], v_in))
    return out


def _order_rlc(r, key):
    v_in, ind, cap = 10.0, 1e-3, 10e-6
    period = 2 * math.pi * math.sqrt(ind * cap)
    out = []
    for n in (200, 400, 800):
        res = simulate(series_rlc(Step(final=v_in), r, ind, cap), 10 * period, period / n)
        v, i = rlc_exact(res.t, v_in, r, ind, cap)
        ref = {"v(out)": v, "i(L1)": i, "v(a)": v_in - r * i}[key]
        out.append(max_err(res[key], ref, 1.0))
    return out


def _order_mid_step_with_ic():
    """初期値つき・途中ステップつきでも2次精度を保つ（ステップ時刻は格子点に乗せる）。"""
    v0, v1, v2, r, c = 1.0, 1.0, 5.0, 1e3, 1e-6
    tau = r * c
    out = []
    for n in (40, 80, 160):
        res = simulate(rc_net(Step(initial=v1, final=v2, t_step=2 * tau), r, c, vc0=v0), 6 * tau, tau / n)
        ks = 2 * n
        ref = np.where(np.arange(len(res.t)) >= ks, rc_exact(res.t - 2 * tau, v2, r, c, v0)[0], v0)
        out.append(max_err(res["v(out)"], ref, v2))
    return out


def _order_sine():
    amp, r, ind, f = 10.0, 5.0, 1e-3, 1e3
    w = 2 * math.pi * f
    z = math.hypot(r, w * ind)
    phi = math.atan2(w * ind, r)
    out = []
    for n in (100, 200, 400):
        res = simulate(
            net(
                V("V1", "in", "0", Sine(amplitude=amp, frequency=f)),
                R("R1", "in", "a", r), L("L1", "a", "0", ind),
            ),
            3 / f, 1 / f / n,
        )
        # 過渡項も含めた厳密解: i = (A/Z) [sin(wt - φ) + sin φ exp(-R t / L)]
        ref = amp / z * (np.sin(w * res.t - phi) + math.sin(phi) * np.exp(-r * res.t / ind))
        out.append(max_err(res["i(L1)"], ref, amp / z))
    return out


@pytest.mark.parametrize("measure", [
    pytest.param(_order_rc, id="RC"),
    pytest.param(lambda: _order_rlc(1.0, "v(out)"), id="RLC不足減衰-状態"),
    pytest.param(lambda: _order_rlc(1.0, "v(a)"), id="RLC不足減衰-代数変数"),
    pytest.param(lambda: _order_rlc(60.0, "i(L1)"), id="RLC過減衰"),
    pytest.param(_order_mid_step_with_ic, id="初期値＋途中ステップ"),
    pytest.param(_order_sine, id="正弦波"),
])
def test_c2_second_order_convergence(measure):
    errors = measure()
    assert errors[0] > 1e-9, "誤差が丸めの水準で、次数を測れていない"
    for ratio in _ratios(errors):
        assert ORDER_BAND[0] < ratio < ORDER_BAND[1], f"誤差 {errors} 比 {_ratios(errors)}"


# ====================================================== 基準 3: t=0 の整合

def _x0(netlist, h=1e-6):
    res = simulate(netlist, 10 * h, h)
    return {name: float(res.x[0, k]) for k, name in enumerate(res.unknowns)}


def _close(actual: dict, expected: dict):
    for name, value in expected.items():
        assert actual[name] == pytest.approx(value, rel=1e-9, abs=1e-9), f"{name}: {actual[name]} != {value}"


def test_c3_series_rlc_initial_values_dc():
    x = _x0(series_rlc(DC(value=10.0), 5.0, 1e-3, 10e-6, il0=0.2, vc0=3.0))
    _close(x, {"v(in)": 10.0, "v(a)": 10.0 - 5.0 * 0.2, "v(out)": 3.0, "i(L1)": 0.2, "i(V1)": -0.2})


def test_c3_step_source_uses_initial_level_before_step():
    x = _x0(series_rlc(Step(initial=2.0, final=10.0, t_step=5e-6), 5.0, 1e-3, 10e-6, il0=0.2, vc0=3.0))
    _close(x, {"v(in)": 2.0, "v(a)": 1.0, "v(out)": 3.0, "i(L1)": 0.2, "i(V1)": -0.2})


@pytest.mark.parametrize("nodes, v_out", [(("in", "out"), 6.0), (("out", "in"), 14.0)])
def test_c3_floating_capacitor_initial_voltage_and_polarity(nodes, v_out):
    """ic は n+ 側を正とした電圧。節点の順を逆にすると極性も逆になる。"""
    x = _x0(net(V("V1", "in", "0", DC(value=10.0)), C("C1", *nodes, 1e-6, ic=4.0), R("R1", "out", "0", 1e3)))
    _close(x, {"v(in)": 10.0, "v(out)": v_out, "i(V1)": -v_out / 1e3})


def test_c3_floating_inductor_initial_current_and_direction():
    """L1 は b → a 向きに 0.3 A。R1 には a → in、R2 には 0 → b に流れる。"""
    x = _x0(net(
        V("V1", "in", "0", DC(value=10.0)), R("R1", "in", "a", 10.0),
        L("L1", "b", "a", 1e-3, ic=0.3), R("R2", "b", "0", 20.0),
    ))
    _close(x, {"v(in)": 10.0, "v(a)": 13.0, "v(b)": -6.0, "i(L1)": 0.3, "i(V1)": 0.3})


def test_c3_current_source_with_inductor_initial_current():
    x = _x0(net(I("I1", "0", "a", DC(value=1.0)), R("R1", "a", "0", 10.0), L("L1", "a", "0", 1e-3, ic=0.4)))
    _close(x, {"v(a)": (1.0 - 0.4) * 10.0, "i(L1)": 0.4})


def test_c3_sine_with_phase_starts_at_its_t0_value():
    x = _x0(net(
        V("V1", "in", "0", Sine(amplitude=10.0, frequency=1e3, phase_deg=90.0, offset=1.0)),
        R("R1", "in", "a", 10.0), L("L1", "a", "0", 1e-3, ic=0.25),
    ))
    _close(x, {"v(in)": 11.0, "v(a)": 11.0 - 2.5, "i(L1)": 0.25, "i(V1)": -0.25})


def test_c3_series_capacitors_keep_their_charges():
    """コンデンサだけがつながる中間節点。初期値と、電荷保存で決まる最終値を見る。"""
    c1, c2, v1, v2, v_in = 1e-6, 2e-6, 1.0, 2.0, 10.0
    netlist = net(V("V1", "in", "0", DC(value=v_in)), R("R1", "in", "a", 1e3), C("C1", "a", "m", c1, ic=v1),
                  C("C2", "m", "0", c2, ic=v2))
    res = simulate(netlist, 20e-3, 1e-5)        # 時定数 R*Ceq = 0.67 ms の 30 倍
    assert res["v(a)"][0] == pytest.approx(v1 + v2)
    assert res["v(m)"][0] == pytest.approx(v2)
    dq = (c1 * c2 / (c1 + c2)) * (v_in - v1 - v2)
    assert res["v(a)"][-1] == pytest.approx(v_in, rel=1e-6)
    assert res["v(m)"][-1] == pytest.approx(v2 + dq / c2, rel=1e-6)


def test_c3_contradictory_initial_values_are_reported():
    netlist = net(
        I("I1", "0", "a", DC(value=0.0)), R("R1", "a", "0", 1e3),
        C("C1", "a", "0", 1e-6, ic=2.0), C("C2", "a", "0", 1e-6, ic=5.0),
    )
    try:
        res = simulate(netlist, 1e-3, 1e-5)
    except (CircuitError, ValueError):
        return
    assert res.notes, f"矛盾を黙って丸めた: v(a)[0] = {res['v(a)'][0]}"


# ================================================== 基準 4: 途中のステップ

def test_c4_rl_algebraic_variable_does_not_ring():
    """V - R - L。v(a) = L di/dt は代数変数で、整合処理が無いと符号が交互に振れる。"""
    v_in, r, ind = 10.0, 10.0, 1e-3
    tau = ind / r
    h, ks = tau / 20, 40
    res = simulate(
        net(
            V("V1", "in", "0", Step(final=v_in, t_step=ks * h)),
            R("R1", "in", "a", r), L("L1", "a", "0", ind),
        ),
        200 * h, h,
    )
    after = np.arange(len(res.t)) >= ks
    td = res.t - ks * h
    i_ref = np.where(after, v_in / r * (1 - np.exp(-td / tau)), 0.0)
    va_ref = np.where(after, v_in * np.exp(-td / tau), 0.0)     # ステップ点は切り替わった後の値
    assert max_err(res["i(L1)"], i_ref, v_in / r) < TOL
    assert max_err(res["v(a)"], va_ref, v_in) < TOL
    assert max_err(res["i(V1)"], -i_ref, v_in / r) < TOL
    assert np.all(np.diff(res["v(a)"][ks:]) < 0), "ステップ後の v(a) が単調に減っていない（数値振動）"
    assert np.all(np.abs(res.x[:ks]) < 1e-12), "ステップ前に動いている"
    assert abs(res["i(L1)"][ks] - res["i(L1)"][ks - 1]) < 1e-12 * v_in / r      # 状態量は連続
    assert res["v(in)"][ks - 1] == pytest.approx(0.0, abs=1e-12)
    assert res["v(in)"][ks] == pytest.approx(v_in)


def test_c4_rlc_mid_step_matches_matrix_exponential():
    """初期値つきの直列RLCで、電源が 3 V → 10 V に変わる。全変数を厳密解と比べる。"""
    v1, v2, r, ind, cap, il0, vc0 = 3.0, 10.0, 2.0, 1e-3, 10e-6, 0.1, -2.0
    period = 2 * math.pi * math.sqrt(ind * cap)
    h, ks, n = period / 200, 300, 1200
    res = simulate(series_rlc(Step(initial=v1, final=v2, t_step=ks * h), r, ind, cap, il0, vc0), n * h, h)
    a_mat = np.array([[0.0, 1 / cap], [-1 / ind, -r / ind]])          # 状態 [vC, iL]
    ref = linear_exact(a_mat, lambda k: [0.0, (v2 if k >= ks else v1) / ind], [vc0, il0], n, h)
    i_scale = v2 / math.sqrt(ind / cap)
    assert max_err(res["v(out)"], ref[:, 0], v2) < TOL
    assert max_err(res["i(L1)"], ref[:, 1], i_scale) < TOL
    v_src = np.where(np.arange(n + 1) >= ks, v2, v1)
    assert max_err(res["v(a)"], v_src - r * ref[:, 1], v2) < TOL
    assert max_err(res["i(V1)"], -ref[:, 1], i_scale) < TOL
    # 状態量は連続: ステップをまたぐ1刻みの変化が、前後の1刻みの変化と同じ大きさに収まる
    for key in ("v(out)", "i(L1)"):
        y = res[key]
        jump = abs(y[ks] - y[ks - 1])
        typical = max(abs(y[ks - 1] - y[ks - 2]), abs(y[ks + 1] - y[ks]))
        assert jump <= 1.5 * typical, f"{key} がステップで跳んでいる"


def test_c4_current_source_step_keeps_inductor_current_continuous():
    r, ind, i1, i2 = 10.0, 1e-3, 0.5, 2.0
    tau = ind / r
    h, ks = tau / 50, 100
    res = simulate(
        net(
            I("I1", "0", "a", Step(initial=i1, final=i2, t_step=ks * h)),
            R("R1", "a", "0", r), L("L1", "a", "0", ind, ic=i1),
        ),
        400 * h, h,
    )
    after = np.arange(len(res.t)) >= ks
    td = res.t - ks * h
    i_ref = np.where(after, i2 + (i1 - i2) * np.exp(-td / tau), i1)
    v_ref = np.where(after, r * (i2 - i1) * np.exp(-td / tau), 0.0)
    assert max_err(res["i(L1)"], i_ref, i2) < TOL
    assert max_err(res["v(a)"], v_ref, r * i2) < TOL
    assert res["i(L1)"][ks] == pytest.approx(i1, rel=1e-9)
    assert res["v(a)"][ks] == pytest.approx(r * (i2 - i1), rel=1e-9)     # 代数変数は切り替わった後の値
    assert np.all(np.diff(res["v(a)"][ks:]) < 0)


@pytest.mark.parametrize("k1, k2", [(300, 300), (300, 301), (100, 700)])
def test_c4_two_sources_stepping(k1, k2):
    """電圧源と電流源が同時刻・隣の時刻・離れた時刻に切り替わる。"""
    v_in, i_in, r, ind, cap = 1.0, 0.05, 2.0, 1e-3, 1e-5
    period = 2 * math.pi * math.sqrt(ind * cap)
    h, n = period / 200, 1000
    netlist = net(
        V("V1", "in", "0", Step(final=v_in, t_step=k1 * h)), R("R1", "in", "a", r), L("L1", "a", "out", ind),
        C("C1", "out", "0", cap), I("I1", "0", "out", Step(final=i_in, t_step=k2 * h)),
    )
    res = simulate(netlist, n * h, h)
    a_mat = np.array([[0.0, 1 / cap], [-1 / ind, -r / ind]])
    def forcing(k):
        return [(i_in if k >= k2 else 0.0) / cap, (v_in if k >= k1 else 0.0) / ind]

    ref = linear_exact(a_mat, forcing, [0.0, 0.0], n, h)
    assert max_err(res["v(out)"], ref[:, 0], v_in) < TOL
    assert max_err(res["i(L1)"], ref[:, 1], 0.1) < TOL


def test_c4_step_off_grid_is_reported():
    res = simulate(rc_net(Step(final=5.0, t_step=1.234e-4), 1e3, 1e-6), 1e-3, 1e-5)
    assert any("V1" in note for note in res.notes), "ステップ時刻を丸めたことが伝わらない"


def _stiff_two_branch(h):
    """遅い枝 R1-C1（1 ms）と速い枝 R2-C2（1 µs）を電源に並列につなぐ。t = 1 ms で 0 → 5 V。"""
    return simulate(net(
        V("V1", "in", "0", Step(final=5.0, t_step=1e-3)),
        R("R1", "in", "out", 1e3), C("C1", "out", "0", 1e-6),
        R("R2", "in", "x", 1e2), C("C2", "x", "0", 1e-8),
    ), 3e-3, h)


def test_c4_stiff_circuit_is_correct_when_step_resolves_fast_branch():
    res = _stiff_two_branch(1e-7)
    after = res.t >= 1e-3 - 1e-12
    td = np.where(after, res.t - 1e-3, 0.0)
    assert max_err(res["v(x)"], np.where(after, 5.0 * (1 - np.exp(-td / 1e-6)), 0.0), 5.0) < TOL
    assert max_err(res["v(out)"], np.where(after, 5.0 * (1 - np.exp(-td / 1e-3)), 0.0), 5.0) < TOL
    assert res["v(x)"].max() <= 5.0 * (1 + 1e-9)


def test_c4_stiff_circuit_does_not_ring_after_step():
    """h = 10 µs（遅い枝を100分割、速い枝の時定数の10倍）。RC だけの回路なので、真の応答は行き過ぎない。"""
    res = _stiff_two_branch(1e-5)
    ks = 100
    after = res.t >= 1e-3 - 1e-12
    slow_ref = np.where(after, 5.0 * (1 - np.exp(-(res.t - 1e-3) / 1e-3)), 0.0)
    assert max_err(res["v(out)"], slow_ref, 5.0) < TOL
    warned = bool(res.notes)
    overshoot = res["v(x)"].max() > 5.0 * (1 + TOL)
    wrong_sign = np.any(res["i(V1)"][ks:] > TOL * 0.055)        # 電源は供給だけ（電流は常に負）
    assert warned or not (overshoot or wrong_sign), (
        f"v(x) 最大 {res['v(x)'].max():.3f} V（真値は 5 V 以下）、"
        f"i(V1) 最大 {res['i(V1)'][ks:].max() * 1e3:.2f} mA"
    )


def test_c4_template_overdamped_with_coarse_h_does_not_ring():
    v_in, r, ind, cap = 10.0, 1e3, 1e-3, 10e-6            # 速い極 L/R = 1 µs、遅い極 RC = 10 ms
    ex = expand(spec_of("rlc_series", {"h": 5e-5, "t_end": 5e-2},
                        Vin=(v_in, "V"), R=(r, "ohm"), L=(ind, "H"), C=(cap, "F")))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    _, i = rlc_exact(res.t, v_in, r, ind, cap)
    assert res.notes or max_err(res["i(L1)"][1:], i[1:], float(i.max())) < 0.1


# ================================================== 基準 5: 解けない回路

def _dc(value=1.0):
    return DC(value=value)


UNSOLVABLE = {
    "浮いた部分": [V("V1", "a", "0", _dc()), R("R1", "a", "0", 1e3), R("R2", "x", "y", 1e3),
              C("C1", "x", "y", 1e-6)],
    "浮いた部分（電源つき）": [V("V1", "a", "0", _dc()), R("R1", "a", "0", 1e3), V("V2", "x", "y", _dc()),
                      R("R2", "x", "y", 1e3)],
    "電圧源の並列（同じ値）": [V("V1", "a", "0", _dc()), V("V2", "a", "0", _dc()), R("R1", "a", "0", 1e3)],
    "電圧源の並列（違う値）": [V("V1", "a", "0", _dc()), V("V2", "a", "0", _dc(2.0)), R("R1", "a", "0", 1e3)],
    "電圧源3個のループ": [V("V1", "a", "0", _dc()), V("V2", "b", "a", _dc()), V("V3", "b", "0", _dc()),
                   R("R1", "b", "0", 1e3)],
    "電圧源とコンデンサの直結": [V("V1", "a", "0", _dc()), C("C1", "a", "0", 1e-6)],
    "電圧源とコンデンサの直結＋抵抗": [V("V1", "a", "0", _dc()), C("C1", "a", "0", 1e-6),
                         R("R1", "a", "0", 1e3)],
    "電圧源とコンデンサ2個のループ": [V("V1", "a", "0", _dc()), C("C1", "a", "m", 1e-6),
                         C("C2", "m", "0", 1e-6), R("R1", "m", "0", 1e3)],
    "電圧源2個とコンデンサのループ": [V("V1", "a", "0", _dc()), V("V2", "b", "a", _dc()),
                         C("C1", "b", "0", 1e-6), R("R1", "b", "0", 1e3)],
    "電流源とインダクタの直列": [I("I1", "0", "a", _dc()), L("L1", "a", "0", 1e-3)],
    "電流源とインダクタの直列（抵抗つき）": [I("I1", "0", "a", _dc()), L("L1", "a", "b", 1e-3),
                            R("R1", "b", "0", 1e3)],
    "電流源どうしの直列": [I("I1", "0", "a", _dc()), I("I2", "a", "b", _dc()), R("R1", "b", "0", 1e3)],
    "電流源だけでつながる部分": [V("V1", "a", "0", _dc()), R("R1", "a", "0", 1e3), I("I1", "0", "x", _dc()),
                      R("R2", "x", "y", 1e3), R("R3", "x", "y", 1e3)],
}


@pytest.mark.parametrize("name", list(UNSOLVABLE))
def test_c5_unsolvable_circuit_is_rejected_before_time_stepping(name, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("解けない回路なのに時間ステップが始まった")

    monkeypatch.setattr(kernel, "run", must_not_run)
    with pytest.raises(CircuitError):
        simulate(Netlist(components=UNSOLVABLE[name]), 1e-3, 1e-5)


SOLVABLE = {
    "純抵抗の分圧": [V("V1", "a", "0", _dc(10.0)), R("R1", "a", "b", 1e3), R("R2", "b", "0", 1e3)],
    "電圧源の直列": [V("V1", "a", "0", _dc()), V("V2", "b", "a", _dc(2.0)), R("R1", "b", "0", 1e3)],
    "電流源とコンデンサ": [I("I1", "0", "a", _dc(1e-3)), C("C1", "a", "0", 1e-6)],
    "電圧源とインダクタの並列": [V("V1", "a", "0", _dc()), L("L1", "a", "0", 1e-3)],
    "コンデンサの直列": [V("V1", "in", "0", _dc()), R("R1", "in", "a", 1e3), C("C1", "a", "m", 1e-6),
                 C("C2", "m", "0", 1e-6)],
    "コンデンサの三角": [V("V1", "in", "0", _dc()), R("R1", "in", "a", 1e3), C("C1", "a", "b", 1e-6),
                 C("C2", "b", "0", 1e-6), C("C3", "a", "0", 1e-6), R("R2", "b", "0", 1e3)],
    "インダクタの並列": [I("I1", "0", "a", _dc()), L("L1", "a", "0", 1e-3), L("L2", "a", "0", 1e-3),
                 R("R1", "a", "0", 1e3)],
}


@pytest.mark.parametrize("name", list(SOLVABLE))
def test_c5_solvable_circuit_is_not_rejected(name):
    res = simulate(Netlist(components=SOLVABLE[name]), 1e-3, 1e-5)
    assert np.all(np.isfinite(res.x))


def test_c5_solvable_special_cases_give_right_values():
    res = simulate(Netlist(components=SOLVABLE["純抵抗の分圧"]), 1e-3, 1e-5)
    assert np.allclose(res["v(b)"], 5.0, rtol=1e-12)
    res = simulate(Netlist(components=SOLVABLE["電流源とコンデンサ"]), 1e-3, 1e-5)
    assert np.allclose(res["v(a)"], 1e-3 * res.t / 1e-6, rtol=1e-9, atol=1e-12)      # 積分: v = I t / C
    res = simulate(Netlist(components=SOLVABLE["電圧源とインダクタの並列"]), 1e-3, 1e-5)
    assert np.allclose(res["i(L1)"], res.t / 1e-3, rtol=1e-9, atol=1e-12)            # i = V t / L
    res = simulate(Netlist(components=SOLVABLE["インダクタの並列"]), 1e-3, 1e-7)
    assert res["i(L1)"][-1] == pytest.approx(0.5, rel=1e-3)                          # 同じLなら半分ずつ


def test_c5_series_inductors_solve_or_error_names_the_cause():
    netlist = net(
        V("V1", "in", "0", _dc()), L("L1", "in", "a", 1e-3), L("L2", "a", "b", 1e-3), R("R1", "b", "0", 10.0),
    )
    try:
        res = simulate(netlist, 1e-3, 1e-6)
    except CircuitError as exc:
        assert "インダクタ" in str(exc) and "直列" in str(exc).replace("電流源とインダクタの直列", "")
        return
    assert res["i(L1)"][-1] == pytest.approx(0.1 * (1 - math.exp(-10.0 * 1e-3 / 2e-3)), rel=TOL)


@pytest.mark.parametrize("build", [
    pytest.param(lambda: R("R1", "a", "0", 0.0), id="R=0"),
    pytest.param(lambda: C("C1", "a", "0", -1e-6), id="C<0"),
    pytest.param(lambda: L("L1", "a", "0", float("nan")), id="L=NaN"),
    pytest.param(lambda: R("R1", "a", "0", float("inf")), id="R=inf"),
    pytest.param(lambda: R("R1", "a", "a", 1.0), id="両端が同じ節点"),
    pytest.param(lambda: Component(id="V1", type="V", nodes=("a", "0")), id="電源に波形なし"),
    pytest.param(lambda: Component(id="R1", type="R", nodes=("a", "0"), value=1.0, ic=1.0), id="Rにic"),
    pytest.param(lambda: net(R("R1", "a", "0", 1.0), R("R1", "a", "0", 1.0)), id="ID重複"),
    pytest.param(
        lambda: net(V("V1", "a", "0", _dc()), R("R1", "a", "0", 1.0), R("R2", "a", "x", 1.0)),
        id="ぶら下がり",
    ),
    pytest.param(lambda: net(V("V1", "a", "g", _dc()), R("R1", "a", "g", 1.0)), id="基準節点なし"),
])
def test_c5_schema_rejects_invalid_netlist(build):
    with pytest.raises(ValueError):          # pydantic の ValidationError は ValueError の一種
        build()


@pytest.mark.parametrize("t_end, h", [
    (1e-3, 0.0), (0.0, 1e-5), (1e-3, -1e-5), (1e-5, 1e-3), (1e-3, float("nan")),
])
def test_c5_invalid_time_settings_are_rejected(t_end, h):
    with pytest.raises(ValueError):
        simulate(rc_net(_dc(), 1e3, 1e-6), t_end, h)


@pytest.mark.parametrize("build", [
    pytest.param(lambda: rc_net(DC(value=float("nan")), 1e3, 1e-6), id="電源=NaN"),
    pytest.param(lambda: rc_net(Step(final=float("inf")), 1e3, 1e-6), id="電源=inf"),
    pytest.param(lambda: rc_net(_dc(), 1e3, 1e-6, vc0=float("nan")), id="ic=NaN"),
])
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_c5_non_finite_source_or_ic_is_rejected(build):
    with pytest.raises(ValueError):
        simulate(build(), 1e-3, 1e-5)


# ====================================================== 基準 6: 値の不足

RLC_NAMES = ["Vin", "R", "L", "C"]
RLC_VALUES = {"Vin": 10.0, "R": 1.0, "L": 1e-3, "C": 10e-6}


@pytest.mark.parametrize("how", ["null", "キー無し"])
@pytest.mark.parametrize("missing", [
    list(c) for n in range(1, 5) for c in itertools.combinations(RLC_NAMES, n)
], ids=lambda m: "+".join(m))
def test_c6_every_missing_value_is_reported_at_once(missing, how):
    """4つのパラメータのどの組み合わせが欠けても、欠けた名前が過不足なく全部報告される。"""
    plant = {}
    for name in RLC_NAMES:
        if name in missing:
            if how == "null":
                plant[name] = {"value": None, "unit": "x", "source": None}
        else:
            plant[name] = {"value": RLC_VALUES[name], "unit": "x"}
    spec = Spec.model_validate({"template": "rlc_series", "plant": plant})
    with pytest.raises(MissingValueError) as exc:
        expand(spec)
    assert exc.value.names == missing
    for name in missing:
        assert name in str(exc.value)


def test_c6_null_fields_lists_all_nulls():
    spec = Spec.model_validate({"template": "rc_lowpass", "plant": {
        "Vin": {"value": None, "unit": "V"},
        "R": {"value": 1e3, "unit": "ohm"},
        "C": {"value": None, "unit": "F"},
    }})
    assert spec.null_fields() == ["Vin", "C"]


def test_c6_cli_reports_missing_values_and_does_not_compute(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"template": "rlc_series", "plant": {
        "Vin": {"value": 10, "unit": "V"},
        "R": {"value": None, "unit": "ohm"},
        "L": {"value": 1e-3, "unit": "H"},
    }}), encoding="utf-8")
    csv_path = tmp_path / "out.csv"
    code = cli_main([str(path), "--csv", str(csv_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "R" in captured.err and "C" in captured.err
    assert captured.out == "" and not csv_path.exists()


def test_c6_non_positive_values_are_reported_together():
    spec = spec_of("rlc_series", Vin=(10.0, "V"), R=(0.0, "ohm"), L=(-1e-3, "H"), C=(1e-6, "F"))
    with pytest.raises(ValueError, match=r"R.*L"):
        expand(spec)


def test_c6_spec_rejects_unknown_keys_and_versions():
    ok = {"template": "rc_lowpass", "plant": {"Vin": {"value": 5, "unit": "V"}}}
    Spec.model_validate(ok)
    for bad in (
        {**ok, "extra": 1},
        {**ok, "schema_version": "9.9"},
        {**ok, "plant": {"Vin": {"value": 5}}},                      # unit が無い
        {**ok, "plant": {"Vin": {"value": "1k", "unit": "V"}}},      # 数値でない
        {**ok, "simulation": {"h": 0}},
    ):
        with pytest.raises(ValueError):
            Spec.model_validate(bad)


def test_c6_unit_is_not_silently_ignored():
    """単位を換算するか、受け付けない単位として断るか、どちらかであること。"""
    spec = spec_of("rc_lowpass", Vin=(5.0, "V"), R=(1.0, "kohm"), C=(1.0, "uF"))
    try:
        ex = expand(spec)
    except ValueError:
        return
    assert ex.netlist.component("R1").value == pytest.approx(1e3)
    assert ex.netlist.component("C1").value == pytest.approx(1e-6)


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_c6_non_finite_spec_value_is_rejected(literal):
    text = (
        '{"template":"rc_lowpass","plant":{"Vin":{"value":VALUE,"unit":"V"},'
        '"R":{"value":1000,"unit":"ohm"},"C":{"value":1e-6,"unit":"F"}}}'
    ).replace("VALUE", literal)
    with pytest.raises(ValueError):
        expand(Spec.model_validate_json(text))


# ========================================================== 基準 7: 再現性

def _mixed_netlist():
    comps = [
        V("V1", "n0", "0", Sine(amplitude=1.0, frequency=3e3, phase_deg=17.0, offset=0.2)),
        I("I1", "0", "n7", Step(final=1e-3, t_step=4e-4)),
    ]
    for k in range(1, 21):
        comps.append(R(f"R{k}", f"n{k - 1}", f"n{k}", 1e3 * (1 + 0.1 * k)))
        comps.append(C(f"C{k}", f"n{k}", "0", 1e-8 * (1 + 0.05 * k), ic=0.01 * k))
    comps += [L("L1", "n20", "m", 1e-3, ic=1e-4), R("R99", "m", "0", 50.0)]
    return Netlist(components=comps)


_DIGEST_SNIPPET = """
import hashlib, json
from circuit_sim import Netlist, simulate
res = simulate(Netlist.model_validate(json.loads({text!r})), 2e-3, 1e-6)
print(hashlib.sha256(res.x.tobytes() + res.t.tobytes()).hexdigest())
"""


def test_c7_same_input_gives_bit_identical_result_in_process():
    a = simulate(_mixed_netlist(), 2e-3, 1e-6)
    b = simulate(_mixed_netlist(), 2e-3, 1e-6)
    assert a.x.tobytes() == b.x.tobytes() and a.t.tobytes() == b.t.tobytes()
    assert a.unknowns == b.unknowns


def test_c7_same_input_gives_bit_identical_result_across_processes(tmp_path):
    """別プロセス・別のハッシュ種でも、結果のバイト列が一致する。"""
    text = _mixed_netlist().model_dump_json()
    here = simulate(Netlist.model_validate(json.loads(text)), 2e-3, 1e-6)
    expected = hashlib.sha256(here.x.tobytes() + here.t.tobytes()).hexdigest()
    for seed in ("0", "4242"):
        out = subprocess.run(
            [sys.executable, "-c", _DIGEST_SNIPPET.format(text=text)],
            cwd=REPO, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == expected


def test_c7_csv_output_is_identical(tmp_path):
    ex = expand(spec_of("rlc_series", Vin=(10.0, "V"), R=(1.0, "ohm"), L=(1e-3, "H"), C=(10e-6, "F")))
    paths = []
    for name in ("a.csv", "b.csv"):
        res = simulate(ex.netlist, ex.t_end, ex.h)
        res.to_csv(tmp_path / name, ex.probes)
        paths.append(tmp_path / name)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    # CSV から読み戻した値が元の倍精度と一致する（桁落ちなし）
    rows = paths[0].read_text(encoding="utf-8").splitlines()
    assert rows[0] == "t,v(out),i(L1)"
    back = np.array([[float(x) for x in row.split(",")] for row in rows[1:]])
    assert np.array_equal(back[:, 1], res["v(out)"]) and np.array_equal(back[:, 0], res.t)


# ====================================================== 基準 8: 極端な条件

@pytest.mark.parametrize("r, c", [
    (1e-6, 1e-12), (1e12, 1e-15), (1e9, 1e3), (1e-9, 1e6), (1e-3, 1e-18), (1e15, 1e-3),
])
def test_c8_rc_with_extreme_constants(r, c):
    """時定数が 1e-21 s から 1e12 s まで。誤差は定数の大きさによらない。"""
    tau = r * c
    res = simulate(rc_net(Step(final=1.0), r, c), 5 * tau, tau / 200)
    v, i = rc_exact(res.t, 1.0, r, c)
    assert max_err(res["v(out)"], v, 1.0) < TOL
    assert max_err(res["i(V1)"], -i, 1.0 / r) < TOL


@pytest.mark.parametrize("r1, c1, r2, c2", [
    (1e-3, 1.0, 1e9, 1e-12), (1.0, 1e-12, 1e6, 1.0), (1e3, 1e-6, 1e-3, 1e-12),
])
def test_c8_two_stage_rc_with_constants_12_decades_apart(r1, c1, r2, c2):
    """V - R1 - (C1 ‖ (R2 - C2))。速い側の時定数の20倍までを、速い側を50分割して計算する。"""
    netlist = net(V("V1", "in", "0", Step(final=1.0)), R("R1", "in", "a", r1), C("C1", "a", "0", c1),
                  R("R2", "a", "b", r2), C("C2", "b", "0", c2))
    a_mat = np.array([[-(1 / r1 + 1 / r2) / c1, 1 / (r2 * c1)], [1 / (r2 * c2), -1 / (r2 * c2)]])
    fast = 1 / np.abs(np.linalg.eigvals(a_mat)).max()
    h, n = fast / 50, 1000
    res = simulate(netlist, n * h, h)
    ref = linear_exact(a_mat, lambda k: [1 / (r1 * c1), 0.0], [0.0, 0.0], n, h)
    assert max_err(res["v(a)"], ref[:, 0], 1.0) < TOL
    assert max_err(res["v(b)"], ref[:, 1], 1.0) < TOL


def test_c8_tiny_capacitor_next_to_huge_inductor_keeps_its_state():
    cs, lb, rc, ic = 1e-12, 1e3, 1e6, 0.7          # 1 pF と 1 kH
    tau = rc * cs
    h = tau / 100
    netlist = net(
        V("V1", "in", "0", Step(final=1.0, t_step=200 * h)),
        R("R1", "in", "out", rc), C("C1", "out", "0", cs, ic=ic),
        R("R2", "in", "b", 1.0), L("L1", "b", "0", lb),
    )
    res = simulate(netlist, 600 * h, h)
    assert res["v(out)"][0] == pytest.approx(ic, rel=1e-6)
    assert abs(res["v(out)"][200] - res["v(out)"][199]) < 0.01


def test_c8_very_fine_step_accumulates_no_error():
    """h = τ/1e9 を 5万ステップ。増分が小さくても丸め誤差が積み上がらない。"""
    r, c = 1e3, 1e-6
    tau = r * c
    h, n = tau * 1e-9, 50_000
    res = simulate(rc_net(Step(final=1.0), r, c), n * h, h)
    exact = -np.expm1(-res.t / tau)
    assert res["v(out)"][-1] == pytest.approx(exact[-1], rel=1e-8)
    assert np.all(np.diff(res["v(out)"]) > 0)


@pytest.mark.parametrize("ratio", [10.0, 1e3, 1e6])
def test_c8_very_coarse_step_stays_bounded_and_settles(ratio):
    """h が時定数の 10〜100万倍でも壊れない（既定の振る舞い）。

    見直し（E1a 追補 A1）: 以前はここで「台形則の増幅率どおりに振れること」を期待値にしていた。
    それは解き方の性質で、要求ではなかった。要求は「発散しない・数値振動しない・黙って誤らない」なので、
    真の応答（1次のRCは入力を超えず、1刻み後にはほぼ入力に達している）を基準にする。
    台形則そのものの性質は、下の damping_steps=0 の試験に分けた。
    """
    r, c, v_in = 1e3, 1e-6, 1.0
    tau = r * c
    res = simulate(rc_net(Step(final=v_in), r, c), 2000 * ratio * tau, ratio * tau)
    v = res["v(out)"]
    assert np.all(np.isfinite(res.x))
    # 数値振動しない: 1次のRCの真の応答は 0 と入力の間にある（注意の有無によらない）
    assert v.min() >= -TOL * v_in and v.max() <= (1 + TOL) * v_in, f"最小 {v.min()}、最大 {v.max()}"
    assert v[-1] == pytest.approx(v_in, rel=TOL)                # 落ち着く
    # 黙って誤らない: 格子点の値が真値と 1% 以内か、粗いことが注意で分かる
    accurate = max_err(v, v_in * (1 - np.exp(-res.t / tau)), v_in) < TOL
    assert accurate or res.notes


@pytest.mark.parametrize("ratio", [10.0, 1e3, 1e6])
def test_c8_trapezoidal_rule_alone_is_stable_with_theoretical_amplification(ratio):
    """減衰ステップを切る（damping_steps=0）と台形則だけになる。発散せず、振れ方は増幅率の式どおり。"""
    r, c, v_in = 1e3, 1e-6, 1.0
    tau = r * c
    res = simulate(rc_net(Step(final=v_in), r, c), 2000 * ratio * tau, ratio * tau, damping_steps=0)
    v = res["v(out)"]
    assert np.all(np.isfinite(res.x))
    assert v.min() >= -1e-9 and v.max() <= 2 * v_in + 1e-9
    rho = (1 - ratio / 2) / (1 + ratio / 2)                    # 台形則の1ステップの増幅率
    k = np.arange(len(v))
    assert np.allclose(v, v_in * (1 - rho**k), atol=1e-9)
    assert res.notes, "台形則だけで振れているのに、刻みが粗いことが注意に出ていない"


def test_c8_coarse_step_gives_no_overshoot_or_warns():
    """RC（1次系）の真の応答は入力を超えない。粗い刻みでは、超えないか、粗いことを注意として返すこと。"""
    r, c, v_in = 1e3, 1e-6, 1.0
    tau = r * c
    res = simulate(rc_net(Step(final=v_in), r, c), 400 * 10 * tau, 10 * tau)
    assert res.notes or res["v(out)"].max() <= v_in * (1 + TOL)


def test_c8_rc_ladder_20_stages_matches_matrix_exponential():
    """v_k' = (v_{k-1} - 2 v_k + v_{k+1}) / (R C)（末端は開放）を行列指数関数で解いて比べる。"""
    n_stage, r, c, v_in = 20, 1e3, 1e-7, 3.0
    comps = [V("V1", "n0", "0", Step(final=v_in))]
    for k in range(1, n_stage + 1):
        comps += [R(f"R{k}", f"n{k - 1}", f"n{k}", r), C(f"C{k}", f"n{k}", "0", c)]
    a_mat = np.zeros((n_stage, n_stage))
    for k in range(n_stage):
        a_mat[k, k] = (-2.0 if k < n_stage - 1 else -1.0) / (r * c)
        if k > 0:
            a_mat[k, k - 1] = 1 / (r * c)
        if k < n_stage - 1:
            a_mat[k, k + 1] = 1 / (r * c)
    b = np.zeros(n_stage)
    b[0] = v_in / (r * c)
    rates = np.abs(np.linalg.eigvals(a_mat).real)
    h = (1 / rates.max()) / 5
    n = int(round(5 / rates.min() / h))
    errors = []
    for div in (1, 2):
        res = simulate(Netlist(components=comps), n * h, h / div)
        ref = linear_exact(a_mat, lambda k: b, np.zeros(n_stage), n * div, h / div)
        sim = np.column_stack([res[f"v(n{k})"] for k in range(1, n_stage + 1)])
        errors.append(max_err(sim, ref, v_in))
        assert res["i(V1)"][0] == pytest.approx(-v_in / r)
    assert errors[0] < TOL
    assert ORDER_BAND[0] < errors[0] / errors[1] < ORDER_BAND[1]
    assert sim[-1, -1] == pytest.approx(v_in, rel=0.02)          # 最終段まで充電されている


def test_c8_lc_ladder_with_floating_inductors_and_initial_values():
    """LCはしご5段（インダクタは両端とも基準でない）＋終端抵抗。全状態に初期値を与える。"""
    n_stage, ind, cap, rs, rl, v_in = 5, 1e-3, 1e-6, 20.0, 50.0, 2.0
    i0 = np.array([0.012, -0.007, 0.004, 0.015, -0.011])
    v0 = np.array([0.5, -0.8, 0.3, 0.9, -0.2])
    comps = [V("V1", "s", "0", DC(value=v_in)), R("Rs", "s", "n0", rs)]
    for k in range(1, n_stage + 1):
        comps += [L(f"L{k}", f"n{k - 1}", f"n{k}", ind, ic=float(i0[k - 1])),
                  C(f"C{k}", f"n{k}", "0", cap, ic=float(v0[k - 1]))]
    comps.append(R("Rl", f"n{n_stage}", "0", rl))
    # 状態 [i_1..i_N, v_1..v_N]:
    #   L i_1' = V - Rs i_1 - v_1,  L i_k' = v_{k-1} - v_k,  C v_k' = i_k - i_{k+1},  C v_N' = i_N - v_N / Rl
    a_mat = np.zeros((2 * n_stage, 2 * n_stage))
    b = np.zeros(2 * n_stage)
    a_mat[0, 0] = -rs / ind
    a_mat[0, n_stage] = -1 / ind
    b[0] = v_in / ind
    for k in range(1, n_stage):
        a_mat[k, n_stage + k - 1] = 1 / ind
        a_mat[k, n_stage + k] = -1 / ind
    for k in range(n_stage):
        a_mat[n_stage + k, k] = 1 / cap
        if k < n_stage - 1:
            a_mat[n_stage + k, k + 1] = -1 / cap
    a_mat[-1, -1] = -1 / (rl * cap)
    period = 2 * math.pi * math.sqrt(ind * cap)
    h, n = period / 400, 8000
    res = simulate(Netlist(components=comps), n * h, h)
    ref = linear_exact(a_mat, lambda k: b, np.concatenate([i0, v0]), n, h)
    i_scale, v_scale = np.abs(ref[:, :n_stage]).max(), np.abs(ref[:, n_stage:]).max()
    for k in range(1, n_stage + 1):
        assert max_err(res[f"i(L{k})"], ref[:, k - 1], i_scale) < TOL
        assert max_err(res[f"v(n{k})"], ref[:, n_stage + k - 1], v_scale) < TOL
    assert res["v(n0)"][0] == pytest.approx(v_in - rs * i0[0])
    assert max_err(res["v(n0)"], v_in - rs * ref[:, 0], v_scale) < TOL


def test_c8_bridged_capacitor_between_two_rc_nodes():
    """節点 a, b の間にコンデンサがあり、容量行列が対角でない場合。"""
    r1, r2, c1, c2, cb, v_in = 1e3, 2e3, 1e-6, 0.5e-6, 0.3e-6, 4.0
    netlist = net(V("V1", "in", "0", Step(final=v_in)), R("R1", "in", "a", r1), C("C1", "a", "0", c1, ic=1.0),
                  C("Cb", "a", "b", cb, ic=1.5), C("C2", "b", "0", c2, ic=-0.5), R("R2", "b", "0", r2))
    cmat = np.array([[c1 + cb, -cb], [-cb, c2 + cb]])
    gmat = np.array([[1 / r1, 0.0], [0.0, 1 / r2]])
    a_mat = -np.linalg.solve(cmat, gmat)
    b = np.linalg.solve(cmat, np.array([v_in / r1, 0.0]))
    h, n = 1e-6, 8000
    res = simulate(netlist, n * h, h)
    ref = linear_exact(a_mat, lambda k: b, [1.0, -0.5], n, h)
    assert max_err(res["v(a)"], ref[:, 0], v_in) < TOL
    assert max_err(res["v(b)"], ref[:, 1], v_in) < TOL


@pytest.mark.parametrize("f_ratio", [0.2, 1.0, 3.0])
def test_c8_sine_steady_state_matches_phasor(f_ratio):
    """直列RLCに正弦波（位相30度、直流分0.5 V）。共振の下・共振点・上で、振幅と位相をフェーザと比べる。"""
    amp, r, ind, cap, phase, offset = 2.0, 5.0, 1e-3, 1e-6, 30.0, 0.5
    f = f_ratio / (2 * math.pi * math.sqrt(ind * cap))
    w = 2 * math.pi * f
    z = r + 1j * w * ind + 1 / (1j * w * cap)
    h_c = (1 / (1j * w * cap)) / z                    # コンデンサ電圧 / 電源電圧
    y = 1 / z                                         # 電流 / 電源電圧
    settle = 25 * 2 * ind / r                         # 包絡線の時定数 2L/R の 25 倍で過渡は exp(-25)
    periods = math.ceil(settle * f) + 5
    res = simulate(series_rlc(Sine(amplitude=amp, frequency=f, phase_deg=phase, offset=offset), r, ind, cap),
                   periods / f, 1 / f / 400)
    steady = res.t > settle
    arg = w * res.t + math.radians(phase)
    v_ss = offset + amp * abs(h_c) * np.sin(arg + np.angle(h_c))
    i_ss = amp * abs(y) * np.sin(arg + np.angle(y))
    assert max_err(res["v(out)"][steady], v_ss[steady], amp * abs(h_c)) < TOL
    assert max_err(res["i(L1)"][steady], i_ss[steady], amp * abs(y)) < TOL


def test_c8_current_source_direction_and_response():
    """電流源は n+ → n-（素子の中）に流す。nodes=("0","out") なら out に流れ込み、電圧は正になる。"""
    i_in, r, c = 1e-3, 1e3, 1e-6
    tau = r * c
    netlist = net(I("I1", "0", "out", DC(value=i_in)), R("R1", "out", "0", r), C("C1", "out", "0", c))
    res = simulate(netlist, 5 * tau, tau / 100)
    assert max_err(res["v(out)"], i_in * r * (1 - np.exp(-res.t / tau)), i_in * r) < TOL
    flipped = net(I("I1", "out", "0", DC(value=i_in)), R("R1", "out", "0", r), C("C1", "out", "0", c))
    assert np.allclose(simulate(flipped, 5 * tau, tau / 100)["v(out)"], -res["v(out)"], atol=1e-12)


def test_c8_energy_balance():
    """保存則: 電源が出したエネルギー = L と C の蓄積の増加 + 抵抗の損失（台形則で積分）。"""
    v_in, r, ind, cap = 10.0, 3.0, 1e-3, 10e-6
    period = 2 * math.pi * math.sqrt(ind * cap)
    res = simulate(series_rlc(Step(final=v_in), r, ind, cap, il0=0.2, vc0=1.0), 6 * period, period / 400)
    i, v = res["i(L1)"], res["v(out)"]
    def integral(y):                                   # 台形公式（numpy 1.26 でも動く書き方）
        return float(np.sum((y[1:] + y[:-1]) * np.diff(res.t)) / 2)

    supplied = integral(res["v(in)"] * -res["i(V1)"])
    lost = integral(r * i**2)
    stored = 0.5 * cap * (v[-1] ** 2 - v[0] ** 2) + 0.5 * ind * (i[-1] ** 2 - i[0] ** 2)
    assert supplied == pytest.approx(stored + lost, rel=1e-3)
    assert np.allclose(res["i(V1)"], -i, atol=1e-9)                       # 電流則
    assert np.allclose(res["v(in)"] - res["v(a)"], r * i, atol=1e-9)      # 電圧則


def test_c8_long_run_keeps_constraints_and_energy():
    """ほぼ無損失のLCを1000周期（10万ステップ）。エネルギーが理論どおりで、代数拘束の残差が育たない。"""
    ind, cap, r = 1e-3, 1e-6, 1e-6
    period = 2 * math.pi * math.sqrt(ind * cap)
    res = simulate(series_rlc(DC(value=0.0), r, ind, cap, vc0=1.0), 1000 * period, period / 100)
    energy = 0.5 * cap * res["v(out)"] ** 2 + 0.5 * ind * res["i(L1)"] ** 2
    assert energy[-1] / energy[0] == pytest.approx(math.exp(-r / ind * res.t[-1]), rel=1e-6)
    residual = np.abs(res["i(V1)"] + res["i(L1)"]) / np.abs(res["i(L1)"]).max()
    assert residual.max() < 1e-9


def test_c8_result_accessors_follow_the_documented_sign_convention():
    """Result.v / Result.i の向き。電流は素子の中を n+ → n- に流れる向きが正。"""
    v_in, r1, r2, ind = 6.0, 1e3, 2e3, 1e-3
    netlist = net(V("V1", "in", "0", DC(value=v_in)), R("R1", "in", "a", r1), L("L1", "a", "b", ind),
                  R("R2", "0", "b", r2), C("C1", "b", "0", 1e-9))
    res = simulate(netlist, 1e-4, 1e-8)               # 時定数（L/R と RC、どちらも µs 台）の数十倍
    i_dc = v_in / (r1 + r2)
    assert res.i("L1")[-1] == pytest.approx(i_dc, rel=1e-6)
    assert res.i("R1")[-1] == pytest.approx(i_dc, rel=1e-6)
    assert res.i("R2")[-1] == pytest.approx(-i_dc, rel=1e-6)          # R2 は (0, b) の向きに置いた
    assert res.i("V1")[-1] == pytest.approx(-i_dc, rel=1e-6)
    assert res.v("in", "a")[-1] == pytest.approx(r1 * i_dc, rel=1e-6)
    assert res.v("a", "in")[-1] == pytest.approx(-r1 * i_dc, rel=1e-6)
    assert res.v("b")[-1] == pytest.approx(r2 * i_dc, rel=1e-6)
    assert np.all(res.v("0") == 0.0) and res.v("0", "b")[-1] == pytest.approx(-r2 * i_dc, rel=1e-6)
    with pytest.raises(KeyError):
        res["v(nope)"]
    with pytest.raises(KeyError):
        res.i("C1")                                   # コンデンサの電流は未対応（README にも無い）


def test_c8_end_time_and_grid():
    res = simulate(rc_net(_dc(), 1e3, 1e-6), 1e-3, 1e-5)
    assert len(res.t) == 101 and res.t[0] == 0.0
    assert np.allclose(np.diff(res.t), 1e-5, rtol=1e-9)
    assert res.x.shape == (101, len(res.unknowns))


def test_c8_template_default_timing_stays_computable_for_stiff_overdamped():
    """ありふれた過減衰の定数で、既定のままの計算が現実的な大きさに収まること（計算はしない）。

    上限の根拠: 結果配列 1 GB（5 変数 × 8 バイト × 2.5e7 ステップ）。README の例の 8000 倍にあたる。
    """
    ex = expand(spec_of("rlc_series", Vin=(10.0, "V"), R=(1e4, "ohm"), L=(1e-3, "H"), C=(10e-6, "F")))
    assert ex.t_end / ex.h <= 2.5e7, f"{ex.t_end / ex.h:.3g} ステップ"


def test_c8_unresolved_sine_source_is_reported():
    netlist = rc_net(Sine(amplitude=1.0, frequency=1.001e6), 1e3, 1e-9)
    res = simulate(netlist, 5e-3, 1e-5)                      # 1 MHz の電源に 10 µs 刻み
    true_amplitude = 1 / math.hypot(1.0, 2 * math.pi * 1.001e6 * 1e-6)
    assert res.notes or np.abs(res["v(out)"][250:]).max() == pytest.approx(true_amplitude, rel=0.1)


def test_c8_end_time_rounding_is_reported():
    res = simulate(rc_net(_dc(), 1e3, 1e-6), 1e-3, 6.7e-4)
    assert res.notes or res.t[-1] == pytest.approx(1e-3)
