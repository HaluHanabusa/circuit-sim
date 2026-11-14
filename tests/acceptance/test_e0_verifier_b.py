"""E0（R / L / C / 電源のソルバ核と仕様JSONのスキーマ）の受け入れ試験。

検証担当（verifier）が、実装者のテスト（tests/test_t0.py）とは別に用意した。
期待値は、この中に書いた解析解か、手で導いた状態方程式の厳密解（scipy.linalg.expm）で作る。
実装（circuit_sim）の関数や行列を期待値の計算に使わない。

テスト名の c1〜c8 は受け入れ基準の番号で、記録は docs/verification/E0_verifier_b.md にある。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、xfail を外す。

ファイル名について: 指定の置き場所は tests/acceptance/test_e0.py だったが、同じ作業ツリーで別の検証担当の
実行が同じファイルを同時に書いていた。相手の内容を上書きしないよう、この名前で置いてある。
どちらを残すか、統合するかは、オーケストレーターが決める。
"""
import cmath
import hashlib
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

from circuit_sim import CircuitError, MissingValueError, Netlist, Spec, expand, kernel, simulate
from circuit_sim.__main__ import main as cli_main

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- 許容差（測る前に決めた）
# 解析解との差。分母は信号の代表値（入力電圧、ピーク電流）。
# 最も速い時定数を50分割以上にした2次精度の方法の理論誤差は0.1%以下。定数を1割ずらした参照とは
# 3%以上ずれる（test_c1_check_detects_ten_percent_wrong_reference）ので、1%は実装の誤りを見逃さない。
TOL = 0.01
# 刻みを半分にしたときの誤差の比。2次精度なら4、1次精度なら2。次数にして 1.89〜2.10。
# 刻みは十分細かい側で測る（不連続点の直後だけ後退オイラーにする方式でも、比が4に近づいた範囲になる）。
ORDER_MIN, ORDER_MAX = 3.7, 4.3
# 連立一次方程式を1回解くだけで決まる値（t=0 の整合、電源につながる節点の電圧）。丸め誤差だけのはず。
EXACT = 1e-9


# ---------------------------------------------------------------- 回路を組む小道具

def netlist(*components: dict, ground: str = "0") -> Netlist:
    return Netlist.model_validate({"ground": ground, "components": list(components)})


def res_(name: str, a: str, b: str, value: float) -> dict:
    return {"id": name, "type": "R", "nodes": [a, b], "value": value}


def ind(name: str, a: str, b: str, value: float, ic: float = 0.0) -> dict:
    return {"id": name, "type": "L", "nodes": [a, b], "value": value, "ic": ic}


def cap(name: str, a: str, b: str, value: float, ic: float = 0.0) -> dict:
    return {"id": name, "type": "C", "nodes": [a, b], "value": value, "ic": ic}


def source(element: str, name: str, a: str, b: str, **waveform) -> dict:
    return {"id": name, "type": element, "nodes": [a, b], "waveform": waveform}


def vdc(name: str, a: str, b: str, value: float) -> dict:
    return source("V", name, a, b, kind="dc", value=value)


def vstep(name: str, a: str, b: str, final: float, initial: float = 0.0, t_step: float = 0.0) -> dict:
    return source("V", name, a, b, kind="step", final=final, initial=initial, t_step=t_step)


def vsine(name: str, a: str, b: str, amplitude: float, frequency: float,
          phase_deg: float = 0.0, offset: float = 0.0) -> dict:
    return source("V", name, a, b, kind="sine", amplitude=amplitude, frequency=frequency,
                  phase_deg=phase_deg, offset=offset)


def idc(name: str, a: str, b: str, value: float) -> dict:
    return source("I", name, a, b, kind="dc", value=value)


def istep(name: str, a: str, b: str, final: float, initial: float = 0.0, t_step: float = 0.0) -> dict:
    return source("I", name, a, b, kind="step", final=final, initial=initial, t_step=t_step)


def series_rlc(v_source: dict, r: float, l_: float, c: float, i0: float = 0.0, v0: float = 0.0) -> Netlist:
    """電源(in) - R - (a) - L - (out) - C - 基準。"""
    return netlist(v_source, res_("R1", "in", "a", r), ind("L1", "a", "out", l_, ic=i0),
                   cap("C1", "out", "0", c, ic=v0))


def rc_lowpass(v_source: dict, r: float, c: float, v0: float = 0.0) -> Netlist:
    """電源(in) - R - (out) - C - 基準。"""
    return netlist(v_source, res_("R1", "in", "out", r), cap("C1", "out", "0", c, ic=v0))


# ---------------------------------------------------------------- 実装と別経路の参照解

def exact_response(a_mat: np.ndarray, b_of_step, x0, h: float, n_points: int) -> np.ndarray:
    """x' = A x + b の厳密解を格子点上で返す。b は刻みの区間ごとに一定（b_of_step(k) が区間 k の値）。

    行列指数関数を使う。時間積分の近似を含まないので、台形則の実装とは独立な経路になる。
    """
    a_mat = np.asarray(a_mat, dtype=float)
    prop = expm(a_mat * h)
    x = np.asarray(x0, dtype=float).copy()
    out = np.empty((n_points, len(x)))
    for k in range(n_points):
        out[k] = x
        x_inf = -np.linalg.solve(a_mat, np.asarray(b_of_step(k), dtype=float))
        x = x_inf + prop @ (x - x_inf)
    return out


def series_rlc_matrix(r: float, l_: float, c: float) -> np.ndarray:
    """状態 x = [コンデンサ電圧, インダクタ電流]。C v' = i、L i' = V - R i - v。"""
    return np.array([[0.0, 1.0 / c], [-1.0 / l_, -r / l_]])


def series_rlc_step_closed_form(t: np.ndarray, v: float, r: float, l_: float, c: float):
    """初期値ゼロの直列RLCに直流 v を加えたときのコンデンサ電圧と電流（教科書の式）。"""
    alpha = r / (2.0 * l_)
    w0 = 1.0 / math.sqrt(l_ * c)
    if alpha < w0:      # 不足減衰
        wd = math.sqrt(w0 * w0 - alpha * alpha)
        env = np.exp(-alpha * t)
        vc = v * (1.0 - env * (np.cos(wd * t) + alpha / wd * np.sin(wd * t)))
        cur = v / (wd * l_) * env * np.sin(wd * t)
    elif alpha > w0:    # 過減衰
        d = math.sqrt(alpha * alpha - w0 * w0)
        s1, s2 = -alpha + d, -alpha - d
        vc = v * (1.0 - (s2 * np.exp(s1 * t) - s1 * np.exp(s2 * t)) / (s2 - s1))
        cur = v / (l_ * (s1 - s2)) * (np.exp(s1 * t) - np.exp(s2 * t))
    else:               # 臨界減衰
        env = np.exp(-alpha * t)
        vc = v * (1.0 - env * (1.0 + alpha * t))
        cur = v / l_ * t * env
    return vc, cur


def series_rlc_timing(r: float, l_: float, c: float) -> tuple[float, int]:
    """刻み = 最も速い極の時定数の1/50、計算時間 = 最も遅い減衰の時定数の6倍。"""
    alpha = r / (2.0 * l_)
    root = cmath.sqrt(alpha * alpha - 1.0 / (l_ * c))
    poles = (-alpha + root, -alpha - root)
    h = 1.0 / (50.0 * max(abs(p) for p in poles))
    t_end = 6.0 / min(abs(p.real) for p in poles)
    return h, int(round(t_end / h))


def max_err(got: np.ndarray, ref: np.ndarray, scale: float) -> float:
    return float(np.max(np.abs(np.asarray(got) - np.asarray(ref))) / scale)


def ratios(errors: list[float]) -> list[float]:
    return [errors[k] / errors[k + 1] for k in range(len(errors) - 1)]


# ================================================================ 基準1: 解析解と1%以内

def test_c1_rc_step_matches_closed_form():
    v, r, c = 5.0, 1e3, 1e-6
    tau = r * c
    out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 5 * tau, tau / 50)
    assert max_err(out["v(out)"], v * (1 - np.exp(-out.t / tau)), v) < TOL
    # 電圧源の枝電流は n+ → n- 向きなので、供給しているときは負
    assert max_err(out["i(V1)"], -v / r * np.exp(-out.t / tau), v / r) < TOL


@pytest.mark.parametrize("r", [1.0, 10.0, 20.0, 40.0, 200.0],
                         ids=["zeta0.05", "zeta0.5", "critical", "zeta2", "zeta10"])
def test_c1_series_rlc_step_matches_closed_form(r):
    """L = 1 mH、C = 10 µF（特性インピーダンス 10 Ω）。R で減衰係数を変える。"""
    v, l_, c = 10.0, 1e-3, 10e-6
    h, n = series_rlc_timing(r, l_, c)
    out = simulate(series_rlc(vdc("V1", "in", "0", v), r, l_, c), n * h, h)
    vc, cur = series_rlc_step_closed_form(out.t, v, r, l_, c)
    assert max_err(out["v(out)"], vc, v) < TOL
    assert max_err(out["i(L1)"], cur, np.max(np.abs(cur))) < TOL
    if r > 20.0:
        assert out["v(out)"].max() <= v * (1 + EXACT)   # 過減衰は行き過ぎない


@pytest.mark.parametrize("template,plant,probe_refs", [
    ("rc_lowpass", {"Vin": 5.0, "R": 1e3, "C": 1e-6}, "rc"),
    ("rlc_series", {"Vin": 10.0, "R": 1.0, "L": 1e-3, "C": 10e-6}, "rlc"),
    ("rlc_series", {"Vin": 10.0, "R": 40.0, "L": 1e-3, "C": 10e-6}, "rlc"),
], ids=["rc", "rlc_under", "rlc_over"])
def test_c1_templates_with_their_own_timing(template, plant, probe_refs):
    """仕様（層A）→ 展開 → 計算の経路で、テンプレートが決めた刻みのまま1%以内。"""
    spec = Spec.model_validate({"template": template,
                                "plant": {k: {"value": val, "unit": "SI"} for k, val in plant.items()}})
    ex = expand(spec)
    out = simulate(ex.netlist, ex.t_end, ex.h)
    v = plant["Vin"]
    if probe_refs == "rc":
        tau = plant["R"] * plant["C"]
        assert max_err(out["v(out)"], v * (1 - np.exp(-out.t / tau)), v) < TOL
        assert max_err(out["i(V1)"], -v / plant["R"] * np.exp(-out.t / tau), v / plant["R"]) < TOL
    else:
        vc, cur = series_rlc_step_closed_form(out.t, v, plant["R"], plant["L"], plant["C"])
        assert max_err(out["v(out)"], vc, v) < TOL
        assert max_err(out["i(L1)"], cur, np.max(np.abs(cur))) < TOL


def test_c1_check_detects_ten_percent_wrong_reference():
    """照合が効いていることの確認: 定数を1割ずらした参照とは、許容差の3倍以上ずれる。"""
    v, r, c = 5.0, 1e3, 1e-6
    tau = r * c
    out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 5 * tau, tau / 50)
    assert max_err(out["v(out)"], v * (1 - np.exp(-out.t / (1.1 * tau))), v) > 3 * TOL

    l_, cc = 1e-3, 10e-6
    h, n = series_rlc_timing(1.0, l_, cc)
    out = simulate(series_rlc(vdc("V1", "in", "0", 10.0), 1.0, l_, cc), n * h, h)
    vc, _ = series_rlc_step_closed_form(out.t, 10.0, 1.0, 1.1 * l_, cc)
    assert max_err(out["v(out)"], vc, 10.0) > 3 * TOL


# ================================================================ 基準2: 2次精度

def test_c2_order_rc_step():
    v, r, c = 5.0, 1e3, 1e-6
    tau = r * c
    errors = []
    for n in (400, 800, 1600):
        out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 5 * tau, 5 * tau / n)
        errors.append(max_err(out["v(out)"], v * (1 - np.exp(-out.t / tau)), v))
    assert all(ORDER_MIN < q < ORDER_MAX for q in ratios(errors)), ratios(errors)


def test_c2_order_rlc_all_variables_with_initial_state_and_midrun_step():
    """状態量（コンデンサ電圧、インダクタ電流）も、それ以外（節点電圧、電源電流）も2次で収束する。

    初期値あり、途中（格子点上）で電源が 3 V → 10 V に変わる条件。参照は行列指数関数。
    """
    v_before, v_after, r, l_, c = 3.0, 10.0, 10.0, 1e-3, 10e-6
    v0, i0, t_end, t_switch = 1.0, 0.1, 2e-3, 0.5e-3
    a_mat = series_rlc_matrix(r, l_, c)
    errors: dict[str, list[float]] = {"v(out)": [], "i(L1)": [], "v(a)": [], "i(V1)": []}
    for n in (400, 800, 1600):
        h = t_end / n
        k_switch = int(round(t_switch / h))
        out = simulate(series_rlc(vstep("V1", "in", "0", v_after, v_before, t_switch), r, l_, c, i0, v0),
                       t_end, h)
        vin = np.where(np.arange(n + 1) >= k_switch, v_after, v_before)
        ref = exact_response(a_mat, lambda k, vin=vin: [0.0, vin[k] / l_], [v0, i0], h, n + 1)
        errors["v(out)"].append(max_err(out["v(out)"], ref[:, 0], v_after))
        errors["i(L1)"].append(max_err(out["i(L1)"], ref[:, 1], 1.0))
        errors["v(a)"].append(max_err(out["v(a)"], vin - r * ref[:, 1], v_after))
        errors["i(V1)"].append(max_err(out["i(V1)"], -ref[:, 1], 1.0))
    for name, errs in errors.items():
        assert all(ORDER_MIN < q < ORDER_MAX for q in ratios(errs)), (name, ratios(errs))


def test_c2_order_sine_source():
    """時間とともに変わる電源でも2次（電源を刻みの始点だけで評価すると1次に落ちる）。"""
    amp, f, phase_deg, offset, r, c, v0 = 2.0, 1e3, 40.0, 0.5, 1e3, 0.2e-6, 0.3
    tau, w, phi = r * c, 2 * math.pi * f, math.radians(phase_deg)

    def steady(t):
        return offset + np.imag(amp * np.exp(1j * (w * t + phi)) / (1 + 1j * w * tau))

    errors = []
    for per_period in (800, 1600, 3200):
        out = simulate(rc_lowpass(vsine("V1", "in", "0", amp, f, phase_deg, offset), r, c, v0),
                       5 / f, 1 / f / per_period)
        ref = steady(out.t) + (v0 - steady(0.0)) * np.exp(-out.t / tau)
        errors.append(max_err(out["v(out)"], ref, amp))
    assert all(ORDER_MIN < q < ORDER_MAX for q in ratios(errors)), ratios(errors)


# ================================================================ 基準3: 初期値と t=0 の整合

def test_c3_rc_initial_voltage_all_variables_at_t0():
    out = simulate(rc_lowpass(vdc("V1", "in", "0", 5.0), 1e3, 1e-6, v0=2.0), 5e-3, 1e-5)
    assert out["v(in)"][0] == pytest.approx(5.0, rel=EXACT)
    assert out["v(out)"][0] == pytest.approx(2.0, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-(5.0 - 2.0) / 1e3, rel=EXACT)


def test_c3_rlc_initial_state_all_variables_at_t0():
    v, r, i0, v0 = 10.0, 1.0, 0.5, 3.0
    out = simulate(series_rlc(vdc("V1", "in", "0", v), r, 1e-3, 10e-6, i0, v0), 1e-3, 1e-6)
    assert out["v(in)"][0] == pytest.approx(v, rel=EXACT)
    assert out["v(a)"][0] == pytest.approx(v - r * i0, rel=EXACT)     # 抵抗の電圧降下 R·i0
    assert out["v(out)"][0] == pytest.approx(v0, rel=EXACT)
    assert out["i(L1)"][0] == pytest.approx(i0, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-i0, rel=EXACT)


def test_c3_trajectory_from_initial_state_matches_matrix_exponential():
    v, r, l_, c, i0, v0, h = 10.0, 1.0, 1e-3, 10e-6, 0.5, 3.0, 2e-6
    out = simulate(series_rlc(vdc("V1", "in", "0", v), r, l_, c, i0, v0), 10e-3, h)
    ref = exact_response(series_rlc_matrix(r, l_, c), lambda k: [0.0, v / l_], [v0, i0], h, len(out.t))
    assert max_err(out["v(out)"], ref[:, 0], v) < TOL
    assert max_err(out["i(L1)"], ref[:, 1], np.max(np.abs(ref[:, 1]))) < TOL
    assert max_err(out["v(a)"], v - r * ref[:, 1], v) < TOL


def test_c3_initial_values_follow_node_order():
    """ic の向きは nodes の並びで決まる（C は n+ 側が正、L は n+ → n-）。"""
    # コンデンサを (基準, out) の向きに置くと、ic = 2 V は v(out) = -2 V
    out = simulate(netlist(vdc("V1", "in", "0", 5.0), res_("R1", "in", "out", 1e3),
                           cap("C1", "0", "out", 1e-6, ic=2.0)), 1e-3, 1e-5)
    assert out["v(out)"][0] == pytest.approx(-2.0, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-(5.0 + 2.0) / 1e3, rel=EXACT)
    # インダクタを (out, a) の向きに置くと、ic = 0.5 A は out → a 向き。電源には逆向きに流れ込む
    out = simulate(netlist(vdc("V1", "in", "0", 10.0), res_("R1", "in", "a", 1.0),
                           ind("L1", "out", "a", 1e-3, ic=0.5), cap("C1", "out", "0", 10e-6, ic=3.0)),
                   1e-3, 1e-6)
    assert out["i(L1)"][0] == pytest.approx(0.5, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(0.5, rel=EXACT)
    assert out["v(a)"][0] == pytest.approx(10.0 + 1.0 * 0.5, rel=EXACT)


def test_c3_floating_capacitor_initial_voltage():
    """両端とも基準節点でないコンデンサの初期値。電流 (10 - 4) / (1k + 3k) = 1.5 mA から各節点が決まる。"""
    out = simulate(netlist(vdc("V1", "in", "0", 10.0), res_("R1", "in", "n1", 1e3),
                           cap("C1", "n1", "n2", 1e-6, ic=4.0), res_("R2", "n2", "0", 3e3)), 20e-3, 1e-5)
    assert out["v(n1)"][0] == pytest.approx(8.5, rel=EXACT)
    assert out["v(n2)"][0] == pytest.approx(4.5, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-1.5e-3, rel=EXACT)
    cur = 1.5e-3 * np.exp(-out.t / 4e-3)
    assert max_err(out["v(n1)"], 10.0 - 1e3 * cur, 10.0) < TOL
    assert max_err(out["v(n2)"], 3e3 * cur, 10.0) < TOL


def test_c3_current_source_with_inductor_initial_current():
    """電流源 1 A、R = 10 Ω と L（初期 0.25 A）の並列。t=0 の節点電圧は (1 - 0.25)·10 = 7.5 V。"""
    out = simulate(netlist(idc("I1", "0", "n", 1.0), res_("R1", "n", "0", 10.0),
                           ind("L1", "n", "0", 1e-3, ic=0.25)), 1e-3, 1e-6)
    assert out["v(n)"][0] == pytest.approx(7.5, rel=EXACT)
    assert out["i(L1)"][0] == pytest.approx(0.25, rel=EXACT)
    cur = 1.0 + (0.25 - 1.0) * np.exp(-out.t * 10.0 / 1e-3)
    assert max_err(out["i(L1)"], cur, 1.0) < TOL


def test_c3_sine_source_phase_and_offset_at_t0():
    amp, phase_deg, offset, r, v0 = 2.0, 40.0, 0.5, 1e3, 0.3
    out = simulate(rc_lowpass(vsine("V1", "in", "0", amp, 1e3, phase_deg, offset), r, 0.2e-6, v0),
                   1e-3, 1e-6)
    vin0 = offset + amp * math.sin(math.radians(phase_deg))
    assert out["v(in)"][0] == pytest.approx(vin0, rel=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-(vin0 - v0) / r, rel=EXACT)


def test_c3_step_at_time_zero_starts_from_final_value():
    """t_step = 0 のステップは、t=0 で既に final。initial が0でなくても同じ。"""
    out = simulate(rc_lowpass(vstep("V1", "in", "0", 7.0, initial=2.0), 1e3, 1e-6), 1e-3, 1e-5)
    assert out["v(in)"][0] == pytest.approx(7.0, rel=EXACT)
    assert out["v(out)"][0] == pytest.approx(0.0, abs=EXACT)
    assert out["i(V1)"][0] == pytest.approx(-7.0 / 1e3, rel=EXACT)


# ================================================================ 基準4: 途中のステップ

def test_c4_node_voltage_jumps_without_ringing_rl():
    """RL回路で電源が 0 → 5 V。インダクタの手前の節点電圧（状態量でない）が振動しない。

    合わせ直しが無いと、この電圧は 10, 0, 10, ... と入力電圧の大きさで振動する。
    """
    v, r, l_, h, k_switch = 5.0, 10.0, 1e-3, 1e-6, 200
    out = simulate(netlist(vstep("V1", "in", "0", v, 0.0, k_switch * h), res_("R1", "in", "a", r),
                           ind("L1", "a", "0", l_)), 1e-3, h)
    elapsed = (np.arange(len(out.t)) - k_switch) * h
    cur = np.where(elapsed >= 0, v / r * (1 - np.exp(-np.clip(elapsed, 0, None) * r / l_)), 0.0)
    node = np.where(elapsed >= 0, v - r * cur, 0.0)
    assert out["v(a)"][k_switch - 1] == pytest.approx(0.0, abs=EXACT)
    assert out["v(a)"][k_switch] == pytest.approx(v, rel=EXACT)        # 切り替わった直後の値
    assert max_err(out["v(a)"], node, v) < 1e-3
    assert max_err(out["i(L1)"], cur, v / r) < 1e-3
    assert out.notes == []


def test_c4_source_current_has_no_ringing_rc():
    """RC回路で電源が 1 → 4 V（定常から）。電源電流（状態量でない）が振動しない。"""
    r, c, h, k_switch = 1e3, 1e-6, 1e-5, 100
    out = simulate(rc_lowpass(vstep("V1", "in", "0", 4.0, 1.0, k_switch * h), r, c, v0=1.0), 6e-3, h)
    elapsed = np.clip((np.arange(len(out.t)) - k_switch) * h, 0, None)
    after = np.arange(len(out.t)) >= k_switch
    vc = np.where(after, 4.0 - 3.0 * np.exp(-elapsed / (r * c)), 1.0)
    cur = -(np.where(after, 4.0, 1.0) - vc) / r
    assert max_err(out["v(out)"], vc, 3.0) < 1e-3
    assert max_err(out["i(V1)"], cur, 3.0 / r) < 1e-3


def test_c4_states_are_continuous_across_step():
    """定常（2 V）から 10 V へ。切り替えの格子点で、状態量は動かず、それ以外だけが跳ぶ。"""
    r, l_, c, h, k_switch = 1.0, 1e-3, 10e-6, 2e-6, 1500
    out = simulate(series_rlc(vstep("V1", "in", "0", 10.0, 2.0, k_switch * h), r, l_, c, v0=2.0),
                   13e-3, h)
    k = k_switch
    assert np.max(np.abs(out["v(out)"][:k] - 2.0)) < EXACT * 10.0          # 切り替え前は静止
    assert np.max(np.abs(out["i(L1)"][:k])) < EXACT
    assert abs(out["v(out)"][k] - out["v(out)"][k - 1]) < EXACT * 10.0     # コンデンサ電圧は連続
    assert abs(out["i(L1)"][k] - out["i(L1)"][k - 1]) < EXACT              # インダクタ電流は連続
    assert out["v(in)"][k] == pytest.approx(10.0, rel=EXACT)               # 電源は跳ぶ
    assert out["v(a)"][k] == pytest.approx(10.0, rel=EXACT)                # 電流0なので R の降下なし
    # 切り替え後は、8 V のステップ応答を 2 V に重ねたもの
    vc, cur = series_rlc_step_closed_form(np.clip(out.t - k * h, 0, None), 8.0, r, l_, c)
    assert max_err(out["v(out)"], 2.0 + vc, 8.0) < TOL
    assert max_err(out["i(L1)"], cur, np.max(np.abs(cur))) < TOL


def test_c4_sources_stepping_at_different_times():
    """電圧源2つと電流源1つが別々の時刻に変わる。参照は、区間ごとに入力一定の厳密解。"""
    r1, r2, r3, c, l_, h, t_end = 1e3, 2e3, 500.0, 1e-6, 10e-3, 5e-6, 8e-3
    out = simulate(netlist(
        vstep("V1", "a", "0", 5.0, 0.0, 1e-3), res_("R1", "a", "n", r1),
        vstep("V2", "b", "0", -3.0, 2.0, 2.5e-3), res_("R2", "b", "n", r2),
        istep("I1", "0", "n", 1e-3, 0.0, 4e-3),
        cap("C1", "n", "0", c, ic=0.4), ind("L1", "n", "m", l_, ic=1e-3), res_("R3", "m", "0", r3),
    ), t_end, h)
    # 状態 [v(n), i(L1)]:  C v' = (V1 - v)/R1 + (V2 - v)/R2 + I1 - i、L i' = v - R3 i
    a_mat = np.array([[-(1 / r1 + 1 / r2) / c, -1 / c], [1 / l_, -r3 / l_]])

    def forcing(k):
        mid = (k + 0.5) * h
        v1 = 5.0 if mid > 1e-3 else 0.0
        v2 = -3.0 if mid > 2.5e-3 else 2.0
        i1 = 1e-3 if mid > 4e-3 else 0.0
        return [(v1 / r1 + v2 / r2 + i1) / c, 0.0]

    ref = exact_response(a_mat, forcing, [0.4, 1e-3], h, len(out.t))
    assert max_err(out["v(n)"], ref[:, 0], 5.0) < TOL
    assert max_err(out["i(L1)"], ref[:, 1], np.max(np.abs(ref[:, 1]))) < TOL
    assert max_err(out["v(m)"], r3 * ref[:, 1], 5.0) < TOL
    assert out.notes == []


def test_c4_offgrid_step_time_is_rounded_to_nearest_grid_point_and_noted():
    h = 1e-5
    out = simulate(rc_lowpass(vstep("V1", "in", "0", 4.0, 1.0, 100.4 * h), 1e3, 1e-6, v0=1.0), 6e-3, h)
    assert out["v(in)"][99] == pytest.approx(1.0, rel=EXACT)
    assert out["v(in)"][100] == pytest.approx(4.0, rel=EXACT)     # 最も近い格子点 100 で切り替わる
    assert len(out.notes) == 1 and "V1" in out.notes[0]           # 丸めたことを知らせる

    out = simulate(rc_lowpass(vstep("V1", "in", "0", 4.0, 1.0, 100.6 * h), 1e3, 1e-6, v0=1.0), 6e-3, h)
    assert out["v(in)"][100] == pytest.approx(1.0, rel=EXACT)
    assert out["v(in)"][101] == pytest.approx(4.0, rel=EXACT)
    assert len(out.notes) == 1


def test_c4_no_overshoot_after_midrun_step_with_coarse_step():
    """1次のRC回路は行き過ぎない。刻みが粗くても、途中のステップの後に入力電圧を超えない。"""
    tau = 1e-3
    h = 100 * tau
    out = simulate(rc_lowpass(vstep("V1", "in", "0", 1.0, 0.0, 20 * h), 1e3, 1e-6), 100 * h, h)
    assert out["v(out)"].max() <= 1.0 + TOL
    assert out["v(out)"].min() >= -TOL


# ================================================================ 基準5: 解けない回路

UNSOLVABLE = {
    "基準節点から浮いたRC": [vdc("V1", "in", "0", 5.0), res_("R1", "in", "0", 1e3),
                     res_("R2", "x", "y", 1e3), cap("C2", "x", "y", 1e-6)],
    "基準節点から浮いた電源つきの部分": [vdc("V1", "in", "0", 5.0), res_("R1", "in", "0", 1e3),
                           vdc("V2", "x", "y", 1.0), res_("R2", "x", "y", 1e3)],
    "電圧源の並列（同じ電圧）": [vdc("V1", "in", "0", 5.0), vdc("V2", "in", "0", 5.0),
                      res_("R1", "in", "0", 1e3)],
    "電圧源の並列（違う電圧）": [vdc("V1", "in", "0", 5.0), vdc("V2", "in", "0", 6.0),
                      res_("R1", "in", "0", 1e3)],
    "電圧源3つのループ": [vdc("V1", "a", "0", 5.0), vdc("V2", "b", "a", 1.0), vdc("V3", "b", "0", 6.0),
                   res_("R1", "b", "0", 1e3)],
    "電圧源とコンデンサの直結": [vdc("V1", "in", "0", 5.0), cap("C1", "in", "0", 1e-6),
                       res_("R1", "in", "0", 1e3)],
    "電圧源とコンデンサ2つのループ": [vdc("V1", "in", "0", 5.0), cap("C1", "in", "n", 1e-6),
                          cap("C2", "n", "0", 1e-6), res_("R1", "n", "0", 1e3)],
    "電流源とインダクタの直列": [idc("I1", "0", "n", 1.0), ind("L1", "n", "m", 1e-3),
                       res_("R1", "m", "0", 10.0)],
    "電流源の直列": [idc("I1", "0", "n", 1.0), idc("I2", "n", "m", 2.0), res_("R1", "m", "0", 10.0)],
    "電流源に挟まれたコンデンサ": [idc("I1", "0", "n", 1e-3), cap("C1", "n", "m", 1e-6),
                        idc("I2", "m", "0", 1e-3)],
}


@pytest.mark.parametrize("name", list(UNSOLVABLE))
def test_c5_unsolvable_circuit_is_rejected(name):
    with pytest.raises(CircuitError):
        simulate(netlist(*UNSOLVABLE[name]), 1e-3, 1e-5)


def test_c5_error_is_raised_before_time_stepping(monkeypatch):
    calls = []
    original = kernel.run

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel, "run", spy)
    # 対照: 解ける回路では時間ステップの核が1回呼ばれる（この確認が空振りでないこと）
    simulate(rc_lowpass(vdc("V1", "in", "0", 5.0), 1e3, 1e-6), 1e-3, 1e-5)
    assert calls == [1]
    for name in UNSOLVABLE:
        with pytest.raises(CircuitError):
            simulate(netlist(*UNSOLVABLE[name]), 1e-3, 1e-5)
    assert calls == [1]      # 解けない回路では、核に入る前にエラーになっている


def test_c5_parallel_capacitors_are_solved():
    """似ているが解ける回路を、誤って断らない。R = 1 kΩ から C = 1 µF + 2 µF を充電。"""
    out = simulate(netlist(vdc("V1", "in", "0", 5.0), res_("R1", "in", "n", 1e3),
                           cap("C1", "n", "0", 1e-6), cap("C2", "n", "0", 2e-6)), 15e-3, 1e-5)
    assert max_err(out["v(n)"], 5.0 * (1 - np.exp(-out.t / 3e-3)), 5.0) < TOL


def test_c5_voltage_source_across_inductor_is_solved():
    """電圧源にインダクタを直結すると、電流が V / L の傾きで増える。"""
    out = simulate(netlist(vdc("V1", "in", "0", 5.0), ind("L1", "in", "0", 1e-3)), 1e-3, 1e-5)
    assert max_err(out["i(L1)"], 5.0 / 1e-3 * out.t, 5.0) < TOL


def test_c5_parallel_inductors_are_solved():
    """R = 10 Ω から L = 1 mH と 2 mH の並列へ。電流はインダクタンスの逆比 2 : 1 に分かれる。"""
    out = simulate(netlist(vdc("V1", "in", "0", 5.0), res_("R1", "in", "n", 10.0),
                           ind("L1", "n", "0", 1e-3), ind("L2", "n", "0", 2e-3)), 5e-4, 5e-7)
    total = 0.5 * (1 - np.exp(-out.t * 10.0 / (2e-3 / 3)))
    assert max_err(out["i(L1)"], total * 2 / 3, 0.5) < TOL
    assert max_err(out["i(L2)"], total * 1 / 3, 0.5) < TOL


def test_c5_current_source_into_capacitor_is_solved():
    """電流源とコンデンサだけ（直流的には基準節点への道が無い）でも、電圧は I t / C で決まる。"""
    out = simulate(netlist(idc("I1", "0", "n", 1e-3), cap("C1", "n", "0", 1e-6)), 1e-3, 1e-5)
    assert max_err(out["v(n)"], 1e-3 * out.t / 1e-6, 1.0) < TOL


# ================================================================ 基準6: 足りない値

def quantity(value) -> dict:
    return {"value": value, "unit": "SI"}


@pytest.mark.parametrize("template,plant,expected", [
    ("rlc_series", {"Vin": None, "R": 1.0, "L": None, "C": None}, {"Vin", "L", "C"}),     # null が3つ
    ("rlc_series", {"Vin": 10.0}, {"R", "L", "C"}),                                       # 項目ごと無い
    ("rlc_series", {"Vin": None, "L": 1e-3}, {"Vin", "R", "C"}),                          # 混在
    ("rlc_series", {}, {"Vin", "R", "L", "C"}),
    ("rc_lowpass", {"Vin": None, "R": None, "C": None}, {"Vin", "R", "C"}),
    ("rc_lowpass", {"Vin": 5.0, "R": 1e3}, {"C"}),
], ids=["nulls", "absent", "mixed", "empty", "rc_all", "rc_one"])
def test_c6_all_missing_values_are_reported_together(template, plant, expected):
    spec = Spec.model_validate({"template": template, "plant": {k: quantity(v) for k, v in plant.items()}})
    with pytest.raises(MissingValueError) as info:
        expand(spec)
    assert set(info.value.names) == expected
    assert len(info.value.names) == len(expected)
    for name in expected:
        assert name in str(info.value)


def test_c6_cli_reports_all_missing_values_and_does_not_compute(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text('{"template": "rlc_series", "plant": {"Vin": {"value": null, "unit": "V"},'
                    ' "R": {"value": 1, "unit": "ohm"}, "L": {"value": null, "unit": "H"}}}',
                    encoding="utf-8")
    code = cli_main([str(path)])
    captured = capsys.readouterr()
    assert code != 0
    for name in ("Vin", "L", "C"):
        assert name in captured.err
    assert captured.out == ""        # 結果らしきものを何も出さない


def test_c6_nonpositive_value_is_rejected_not_repaired():
    spec = Spec.model_validate({"template": "rlc_series", "plant": {
        "Vin": quantity(10.0), "R": quantity(0.0), "L": quantity(1e-3), "C": quantity(-1e-6)}})
    with pytest.raises(ValueError) as info:
        expand(spec)
    assert "R" in str(info.value) and "C" in str(info.value)


def test_c6_unit_is_not_silently_ignored():
    """SI基本単位でない単位が付いた値は、換算するか、断る。黙って基本単位として使わない。"""
    spec = Spec.model_validate({"template": "rlc_series", "plant": {
        "Vin": {"value": 10.0, "unit": "V"}, "R": {"value": 1.0, "unit": "ohm"},
        "L": {"value": 1.0, "unit": "mH"}, "C": {"value": 10.0, "unit": "uF"}}})
    try:
        ex = expand(spec)
    except ValueError:
        return      # 断るのでもよい
    assert ex.netlist.component("L1").value == pytest.approx(1e-3)
    assert ex.netlist.component("C1").value == pytest.approx(10e-6)


# ================================================================ 基準7: 再現性

def reproducibility_cases() -> list[tuple[Netlist, float, float]]:
    ladder = [vstep("V1", "n0", "0", 1.0)]
    for k in range(1, 21):
        ladder += [res_(f"R{k}", f"n{k - 1}", f"n{k}", 1e3), cap(f"C{k}", f"n{k}", "0", 1e-9)]
    mixed = netlist(vsine("V1", "in", "0", 1.0, 1e3, phase_deg=30.0), res_("R1", "in", "a", 10.0),
                    ind("L1", "a", "b", 1e-3, ic=0.01), cap("C1", "b", "0", 1e-6, ic=0.2),
                    istep("I1", "0", "b", 1e-3, 0.0, 1e-3))
    return [
        (series_rlc(vstep("V1", "in", "0", 10.0), 1.0, 1e-3, 10e-6), 10e-3, 3.1e-6),
        (netlist(*ladder), 2e-4, 1e-7),
        (mixed, 5e-3, 1e-6),
    ]


def test_c7_same_input_gives_bitwise_identical_result():
    for net, t_end, h in reproducibility_cases():
        first = simulate(net, t_end, h)
        second = simulate(net, t_end, h)
        assert first.unknowns == second.unknowns
        assert np.array_equal(first.t, second.t)
        assert np.array_equal(first.x, second.x)       # 許容差なしの完全一致


def digest_of_all_cases() -> str:
    sha = hashlib.sha256()
    for net, t_end, h in reproducibility_cases():
        out = simulate(net, t_end, h)
        sha.update(out.t.tobytes())
        sha.update(out.x.tobytes())
    return sha.hexdigest()


def test_c7_same_input_gives_bitwise_identical_result_across_processes():
    """別のプロセス（ハッシュの種も変える）で計算しても、全サンプルが完全に一致する。"""
    module = Path(__file__).stem
    code = (f"import sys; sys.path.insert(0, 'tests/acceptance'); import {module};"
            f" print({module}.digest_of_all_cases())")
    digests = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                              text=True, check=True)
        digests.append(done.stdout.strip())
    assert len(digests[0]) == 64
    assert digests[0] == digests[1] == digest_of_all_cases()


# ================================================================ 基準8: 極端な条件

@pytest.mark.parametrize("v,l_,c", [
    (1e-6, 1e-12, 1e-15),     # 全部小さい（µV、pH、fF）
    (1e6, 1e3, 1e3),          # 全部大きい
    (1e3, 1e-9, 1e3),         # nH と kF: 特性インピーダンス 1 µΩ
    (1.0, 1.0, 1e-12),        # H と pF: 特性インピーダンス 1 MΩ
], ids=["tiny", "huge", "nH_kF", "H_pF"])
def test_c8_series_rlc_with_extreme_magnitudes(v, l_, c):
    """減衰係数0.5の直列RLCを、桁の極端な定数で。固有周期の200分の1の刻みで10周期。"""
    r = math.sqrt(l_ / c)
    h = 2 * math.pi * math.sqrt(l_ * c) / 200
    out = simulate(series_rlc(vdc("V1", "in", "0", v), r, l_, c), 2000 * h, h)
    vc, cur = series_rlc_step_closed_form(out.t, v, r, l_, c)
    assert max_err(out["v(out)"], vc, v) < TOL
    assert max_err(out["i(L1)"], cur, np.max(np.abs(cur))) < TOL


# 時定数が6桁離れた2段RC: 電源 - R1 - (a, C1) - R2 - (b, C2)。速い側 1 ns、遅い側 1 s。
STIFF = {"v": 5.0, "r1": 1.0, "c1": 1e-9, "r2": 1e3, "c2": 1e-3}


def stiff_two_stage() -> tuple[Netlist, np.ndarray, list[float]]:
    p = STIFF
    net = netlist(vdc("V1", "in", "0", p["v"]), res_("R1", "in", "a", p["r1"]), cap("C1", "a", "0", p["c1"]),
                  res_("R2", "a", "b", p["r2"]), cap("C2", "b", "0", p["c2"]))
    a_mat = np.array([[-(1 / p["r1"] + 1 / p["r2"]) / p["c1"], 1 / (p["r2"] * p["c1"])],
                      [1 / (p["r2"] * p["c2"]), -1 / (p["r2"] * p["c2"])]])
    return net, a_mat, [p["v"] / (p["r1"] * p["c1"]), 0.0]


def test_c8_time_constants_six_orders_apart_with_step_resolving_the_fast_one():
    net, a_mat, forcing = stiff_two_stage()
    h = 1e-10
    out = simulate(net, 20000 * h, h)
    ref = exact_response(a_mat, lambda k: forcing, [0.0, 0.0], h, len(out.t))
    assert max_err(out["v(a)"], ref[:, 0], STIFF["v"]) < TOL
    assert max_err(out["v(b)"], ref[:, 1], STIFF["v"]) < TOL


def test_c8_time_constants_six_orders_apart_slow_node_with_coarse_step():
    """遅い側に合わせた刻み（速い時定数の100万倍）でも、遅い節点は正しい。"""
    net, a_mat, forcing = stiff_two_stage()
    h = 1e-3
    out = simulate(net, 5000 * h, h)
    ref = exact_response(a_mat, lambda k: forcing, [0.0, 0.0], h, len(out.t))
    assert np.isfinite(out.x).all()
    assert max_err(out["v(b)"], ref[:, 1], STIFF["v"]) < TOL


def test_c8_time_constants_six_orders_apart_fast_node_with_coarse_step():
    """同じ条件で、速い節点も（最初の2点を除き）正しい値に落ち着く。"""
    net, a_mat, forcing = stiff_two_stage()
    h = 1e-3
    out = simulate(net, 5000 * h, h)
    ref = exact_response(a_mat, lambda k: forcing, [0.0, 0.0], h, len(out.t))
    assert max_err(out["v(a)"][2:], ref[2:, 0], STIFF["v"]) < TOL


def test_c8_extremely_fine_step():
    """刻みが時定数の1000万分の1。1000ステップでの変化量に対して誤差 1e-6 以内（丸めが積もらない）。"""
    v, r, c = 1.0, 1e3, 1e-6
    tau = r * c
    h = tau * 1e-7
    out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 1000 * h, h)
    change = v * 1000 * h / tau
    assert max_err(out["v(out)"], -v * np.expm1(-out.t / tau), change) < 1e-6


def test_c8_extremely_coarse_step_is_stable_and_converges():
    """刻みが時定数の100倍でも発散せず、最終値に収束する（台形則はA安定）。"""
    v, r, c = 1.0, 1e3, 1e-6
    h = 100 * r * c
    out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 400 * h, h)
    assert np.isfinite(out.x).all()
    assert np.max(np.abs(out["v(out)"])) <= 2.0 * v + EXACT        # 入力の2倍を超えて膨らまない
    assert out["v(out)"][-1] == pytest.approx(v, abs=1e-3 * v)
    assert out["i(V1)"][-1] == pytest.approx(0.0, abs=1e-3 * v / r)


def test_c8_extremely_coarse_step_does_not_overshoot_first_order_circuit():
    v, r, c = 1.0, 1e3, 1e-6
    h = 100 * r * c
    out = simulate(rc_lowpass(vdc("V1", "in", "0", v), r, c), 400 * h, h)
    assert out["v(out)"].max() <= v * (1 + TOL)


def rc_ladder(rs, cs, v: float) -> tuple[Netlist, np.ndarray, list[float]]:
    """電源(n0) - R1 - (n1, C1) - R2 - (n2, C2) - ... 。状態は各段のコンデンサ電圧。"""
    n = len(rs)
    parts = [vdc("V1", "n0", "0", v)]
    a_mat = np.zeros((n, n))
    for k in range(n):
        parts += [res_(f"R{k + 1}", f"n{k}", f"n{k + 1}", float(rs[k])),
                  cap(f"C{k + 1}", f"n{k + 1}", "0", float(cs[k]))]
        a_mat[k, k] -= 1 / (rs[k] * cs[k])
        if k > 0:
            a_mat[k, k - 1] += 1 / (rs[k] * cs[k])
        if k < n - 1:
            a_mat[k, k] -= 1 / (rs[k + 1] * cs[k])
            a_mat[k, k + 1] += 1 / (rs[k + 1] * cs[k])
    forcing = [v / (rs[0] * cs[0])] + [0.0] * (n - 1)
    return netlist(*parts), a_mat, forcing


@pytest.mark.parametrize("rs,cs,steps_per_fast_tau,n_steps", [
    (np.full(20, 1e3), np.full(20, 1e-9), 5, 10000),                     # 同じ値の20段
    (np.logspace(-3, 6, 20), np.logspace(0, -12, 20), 4, 4000),          # R は9桁、C は12桁にわたる20段
], ids=["uniform", "nine_decades"])
def test_c8_rc_ladder_20_stages(rs, cs, steps_per_fast_tau, n_steps):
    net, a_mat, forcing = rc_ladder(rs, cs, 1.0)
    fastest = 1.0 / np.max(np.abs(np.linalg.eigvals(a_mat)))
    h = fastest / steps_per_fast_tau
    out = simulate(net, n_steps * h, h)
    ref = exact_response(a_mat, lambda k: forcing, np.zeros(20), h, len(out.t))
    got = np.column_stack([out[f"v(n{k})"] for k in range(1, 21)])
    assert max_err(got, ref, 1.0) < TOL          # 20節点すべて
    assert max_err(out["i(V1)"], -(1.0 - ref[:, 0]) / rs[0], 1.0 / rs[0]) < TOL


def test_c8_sine_steady_state_amplitude_phase_and_offset():
    """RC低域フィルタの定常応答。振幅 A / sqrt(1 + (ωτ)^2)、位相 -atan(ωτ)、平均はオフセット。"""
    amp, f, phase_deg, offset, r, c = 2.0, 1e3, 40.0, 0.5, 1e3, 0.2e-6
    tau, w, per_period = r * c, 2 * math.pi * f, 200
    out = simulate(rc_lowpass(vsine("V1", "in", "0", amp, f, phase_deg, offset), r, c), 20 / f,
                   1 / f / per_period)
    # 過渡（時定数 0.2 ms）が収まった後の、最後の5周期ちょうど（15τ以上あと）を使う
    t = out.t[-5 * per_period - 1:-1]
    y = out["v(out)"][-5 * per_period - 1:-1]
    mean = y.mean()
    in_phase = 2 * np.mean((y - mean) * np.sin(w * t))
    quadrature = 2 * np.mean((y - mean) * np.cos(w * t))
    assert math.hypot(in_phase, quadrature) == pytest.approx(amp / math.sqrt(1 + (w * tau) ** 2), rel=TOL)
    phase = math.degrees(math.atan2(quadrature, in_phase))
    assert phase == pytest.approx(phase_deg - math.degrees(math.atan(w * tau)), abs=0.5)   # 0.5° ≒ 1%rad
    assert mean == pytest.approx(offset, abs=TOL * amp)


def test_c8_sine_at_resonance_of_series_rlc():
    """Q = 10 の直列RLCを共振周波数で駆動。コンデンサ電圧は入力の10倍まで育つ。"""
    r, l_, c, per_period = 1.0, 1e-3, 10e-6, 400
    f0 = 1 / (2 * math.pi * math.sqrt(l_ * c))
    w, h = 2 * math.pi * f0, 1 / f0 / per_period
    out = simulate(series_rlc(vsine("V1", "in", "0", 1.0, f0), r, l_, c), 60 / f0, h)
    current = 1.0 / (r + 1j * w * l_ + 1 / (1j * w * c))       # フェーザ
    v_cap = current / (1j * w * c)
    assert abs(v_cap) == pytest.approx(10.0, rel=1e-9)

    def steady(t):
        return np.array([np.imag(v_cap * np.exp(1j * w * t)), np.imag(current * np.exp(1j * w * t))])

    # 厳密解 = 定常解 + 同次解（初期値ゼロに合わせる）。同次解は行列指数関数で進める
    transient = exact_response(series_rlc_matrix(r, l_, c), lambda k: [0.0, 0.0], -steady(0.0), h,
                               len(out.t))
    ref = steady(out.t).T + transient
    assert max_err(out["v(out)"], ref[:, 0], abs(v_cap)) < TOL
    assert max_err(out["i(L1)"], ref[:, 1], abs(current)) < TOL
    assert np.max(np.abs(out["v(out)"][-per_period:])) == pytest.approx(10.0, rel=TOL)


def test_c8_current_source_step_into_parallel_rlc():
    """電流源 2 mA を R = 1 kΩ、L = 10 mH、C = 0.1 µF の並列に流し込む（不足減衰）。"""
    i_src, r, l_, c = 2e-3, 1e3, 10e-3, 0.1e-6
    alpha, w0 = 1 / (2 * r * c), 1 / math.sqrt(l_ * c)
    wd = math.sqrt(w0 * w0 - alpha * alpha)
    t_end = 10 / alpha
    out = simulate(netlist(istep("I1", "0", "n", i_src), res_("R1", "n", "0", r), ind("L1", "n", "0", l_),
                           cap("C1", "n", "0", c)), t_end, t_end / 2000)
    env = np.exp(-alpha * out.t)
    v_ref = i_src / (c * wd) * env * np.sin(wd * out.t)
    i_ref = i_src * (1 - env * (np.cos(wd * out.t) + alpha / wd * np.sin(wd * out.t)))
    assert max_err(out["v(n)"], v_ref, np.max(np.abs(v_ref))) < TOL
    assert max_err(out["i(L1)"], i_ref, i_src) < TOL


def test_c8_current_source_direction_and_midrun_step():
    """電流は素子の中を n+ → n- に流れる。(n, 基準) の向きに置くと節点から引き抜く。"""
    out = simulate(netlist(idc("I1", "n", "0", 1e-3), res_("R1", "n", "0", 1e3), cap("C1", "n", "0", 1e-6)),
                   5e-3, 1e-5)
    assert max_err(out["v(n)"], -(1 - np.exp(-out.t / 1e-3)), 1.0) < TOL
    # 定常（1 mA、1 V）から 3 mA へ、格子点 200 で切り替え
    h = 1e-5
    out = simulate(netlist(istep("I1", "0", "n", 3e-3, 1e-3, 200 * h), res_("R1", "n", "0", 1e3),
                           cap("C1", "n", "0", 1e-6, ic=1.0)), 8e-3, h)
    elapsed = np.clip((np.arange(len(out.t)) - 200) * h, 0, None)
    assert max_err(out["v(n)"], 3.0 - 2.0 * np.exp(-elapsed / 1e-3), 2.0) < TOL


def test_c8_floating_parallel_lc_between_non_ground_nodes():
    """電源 - R1 - (a) - [L と C の並列] - (b) - R2 - 基準。L も C も両端が基準節点でない。"""
    v, r1, r2, l_, c, i0, v0, t_end, n = 10.0, 50.0, 150.0, 1e-3, 1e-6, 0.02, 1.5, 4e-3, 4000
    # 状態 [vC = v(a) - v(b), iL]。R1 と R2 を流れる電流は (V - vC) / (R1 + R2)
    a_mat = np.array([[-1 / (c * (r1 + r2)), -1 / c], [1 / l_, 0.0]])
    out = simulate(netlist(vdc("V1", "in", "0", v), res_("R1", "in", "a", r1), ind("L1", "a", "b", l_, ic=i0),
                           cap("C1", "a", "b", c, ic=v0), res_("R2", "b", "0", r2)), t_end, t_end / n)
    ref = exact_response(a_mat, lambda k: [v / (c * (r1 + r2)), 0.0], [v0, i0], t_end / n, n + 1)
    through = (v - ref[:, 0]) / (r1 + r2)
    assert out["v(a)"][0] == pytest.approx(v - r1 * (v - v0) / (r1 + r2), rel=EXACT)
    assert out["v(b)"][0] == pytest.approx(r2 * (v - v0) / (r1 + r2), rel=EXACT)
    assert max_err(out["v(a)"], v - r1 * through, v) < TOL
    assert max_err(out["v(b)"], r2 * through, v) < TOL
    assert max_err(out["i(L1)"], ref[:, 1], np.max(np.abs(ref[:, 1]))) < TOL


def test_c8_floating_inductor_in_pi_filter():
    """電源 12 V - 2 Ω - (a, C1) - L - (b, C2 と負荷 8 Ω)。3つの状態すべてに初期値。"""
    v, rs, c1, l_, c2, rl, t_end, n = 12.0, 2.0, 10e-6, 100e-6, 22e-6, 8.0, 2e-3, 4000
    # 状態 [v(a), v(b), iL]
    a_mat = np.array([[-1 / (rs * c1), 0.0, -1 / c1], [0.0, -1 / (rl * c2), 1 / c2], [1 / l_, -1 / l_, 0.0]])
    out = simulate(netlist(vdc("V1", "in", "0", v), res_("Rs", "in", "a", rs),
                           cap("C1", "a", "0", c1, ic=1.0), ind("L1", "a", "b", l_, ic=0.3),
                           cap("C2", "b", "0", c2, ic=2.0), res_("RL", "b", "0", rl)), t_end, t_end / n)
    ref = exact_response(a_mat, lambda k: [v / (rs * c1), 0.0, 0.0], [1.0, 2.0, 0.3], t_end / n, n + 1)
    assert out["i(V1)"][0] == pytest.approx(-(v - 1.0) / rs, rel=EXACT)
    assert max_err(out["v(a)"], ref[:, 0], v) < TOL
    assert max_err(out["v(b)"], ref[:, 1], v) < TOL
    assert max_err(out["i(L1)"], ref[:, 2], np.max(np.abs(ref[:, 2]))) < TOL
    assert out["v(b)"][-1] == pytest.approx(v * rl / (rs + rl), rel=TOL)      # 直流の分圧に落ち着く


def test_c8_lossless_lc_conserves_energy_over_many_steps():
    """抵抗の無いLC（保存則）。10万ステップ（500周期）の間、エネルギーが変わらない。

    最初の10ステップは別扱いにする（計算開始の直後だけ減衰のある方法を使う実装でも成り立つように）。
    そこでの減りは1%以内、その後の変化は 1e-6 以内。
    """
    l_, c = 1e-3, 1e-6
    h = 2 * math.pi * math.sqrt(l_ * c) / 200
    out = simulate(netlist(ind("L1", "n", "0", l_), cap("C1", "n", "0", c, ic=1.0)), 100_000 * h, h)
    energy = 0.5 * c * out["v(n)"] ** 2 + 0.5 * l_ * out["i(L1)"] ** 2
    assert energy[0] == pytest.approx(0.5 * c * 1.0**2, rel=EXACT)
    assert abs(energy[10] / energy[0] - 1.0) < 0.01
    assert np.max(np.abs(energy[10:] / energy[10] - 1.0)) < 1e-6


def test_c8_ground_node_can_have_any_name():
    out = simulate(netlist(vdc("V1", "in", "gnd", 5.0), res_("R1", "in", "out", 1e3),
                           cap("C1", "out", "gnd", 1e-6), ground="gnd"), 5e-3, 1e-5)
    assert sorted(out.unknowns) == ["i(V1)", "v(in)", "v(out)"]
    assert max_err(out["v(out)"], 5.0 * (1 - np.exp(-out.t / 1e-3)), 5.0) < TOL


def test_c8_extreme_capacitance_to_inductance_ratio():
    """L = 1 pH、C = 1 kF（比 1e15）。電圧源の節点は t=0 から電源電圧に等しい。"""
    v, l_, c = 1.0, 1e-12, 1e3
    r = math.sqrt(l_ / c)
    h = 2 * math.pi * math.sqrt(l_ * c) / 200
    out = simulate(series_rlc(vdc("V1", "in", "0", v), r, l_, c), 2000 * h, h)
    assert out["v(in)"][0] == pytest.approx(v, rel=EXACT)
    vc, _ = series_rlc_step_closed_form(out.t, v, r, l_, c)
    assert max_err(out["v(out)"], vc, v) < TOL


def test_c8_template_step_count_stays_practical_for_strongly_overdamped_rlc():
    """定数の桁が大きく違う仕様でも、既定の刻みで現実的なステップ数（1000万以下）に収まるか、断る。"""
    spec = Spec.model_validate({"template": "rlc_series", "plant": {
        "Vin": quantity(10.0), "R": quantity(1e3), "L": quantity(1e-6), "C": quantity(1e-6)}})
    try:
        ex = expand(spec)
    except ValueError:
        return      # 理由の分かるエラーで断るのでもよい
    assert ex.t_end / ex.h <= 1e7


def test_c8_impossible_step_count_is_reported_clearly():
    """1e15 ステップのような計算できない指定は、配列を確保する前に、理由の分かるエラーにする。"""
    with pytest.raises((ValueError, CircuitError)):
        simulate(rc_lowpass(vdc("V1", "in", "0", 1.0), 1e3, 1e-6), 1.0, 1e-15)
