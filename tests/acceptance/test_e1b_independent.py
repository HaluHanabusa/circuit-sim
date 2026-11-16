"""E1b の独立検証の受け入れ試験。検証担当が、実装者の試験（tests/test_e1b_*.py）とは別に用意した。

記録は docs/verification/E1b-verifier.md。
期待値は、このファイルの中に書いた参照モデルで作る。`circuit_sim` の関数を期待値の計算に使わない。
  参照モデル: 降圧コンバータ（同期整流、デッドタイム0）を、区分ごとの厳密解（行列指数関数）で進める。
    状態 x = [iL, vC]、v_out = a·vC + a·Resr·iL（a = R/(R + Resr)）
    L·diL/dt = v_sw − v_out − r_sw·iL、C·dvC/dt = iL − v_out/R
    スイッチング: 搬送波の周期 T の中央にオンの区間（三角波）、または頭にオンの区間（のこぎり波）。
                  デューティは連続値（量子化しない）。
    平均: v_sw = d·Vin を制御周期のあいだ一定。
    オン抵抗 r_sw: 状態の式の −r_sw·iL。相補のゲート（デッドタイム0）では、どちらのスイッチが導通していても
                  出力の経路にオン抵抗が1つ入るので、スイッチングでも平均でも同じ項になる（周期平均を取ると
                  L·d⟨iL⟩/dt = d·Vin − r_sw·⟨iL⟩ − ⟨v_out⟩。オフ抵抗の漏れは入れない）。
    制御: t_k = k·Tc に v_out を測り、このファイルのPIDで u_k を作り、[t_{k+1}, t_{k+2}) で使う。
    周期平均は、区分ごとの ∫x を拡大行列で厳密に求めて足す。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。

2026-11-15 の再検証（c96ad81。記録は docs/verification/E1b-verifier-2.md）で書き換えた:
  エンジンの平均モデルのレグにオン抵抗（1 mΩ）が直列に入った（v(sw) = d·Vin − r_on·iL）ので、平均モデルと
  比べる参照にも r_sw = R_ON を入れた。基準6の平均モデルの v(sw) の照合を、この式に合わせた。
  ゲインの初期値の式の試験の xfail(strict) の印は、c96ad81 で（直ったので）外されていた。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal
from scipy.linalg import expm

from circuit_sim import Spec, expand
from circuit_sim.__main__ import main
from circuit_sim.control import Pid
from circuit_sim.units import UnitError

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"
VIN, L, C, R, VREF = 24.0, 47e-6, 100e-6, 6.0, 12.0
KP, KI, KD, TF = 0.0117, 147.0, 2.74e-6, 2.5e-6
T = 1e-5                  # 搬送波の周期（100 kHz）
R_ON = 1e-3               # エンジンのスイッチのオン抵抗の既定（ir.DEFAULT_R_ON。参照モデルの r_sw に使う。
#                           c96ad81 から平均モデルのレグにも直列に入る）

# 許容差（測る前に決めた根拠）
# 平均モデルと厳密な平均の参照: エンジンは刻み T/10 の台形則。LC の共振は 430 刻みで、台形則の周波数の
#   相対誤差 (ω0·h)²/12 ≈ 2e-5。10 ms（共振 23 周期）で位相のずれ約 3e-3 rad × 振れ幅 2 V 以下 ≈ 6 mV。
#   0.1%（12 mV）は、その2倍で、1% のモデルの誤り（過渡で 0.1 V 以上）を10倍の余裕で見分ける。
TOL_AVG_EXACT = 0.001 * VREF
# 開ループのスイッチングモデルと厳密解（D = 0.5 はちょうど 100 刻みで量子化が無い）: 刻み T/200 の誤差は
#   0.01% 程度の見積もり。0.05%（6 mV）は、1% の誤りを20倍の余裕で見分ける。
TOL_SW_EXACT_OPEN = 0.0005 * VREF
# 閉ループのスイッチングモデルと厳密解（参照はデューティを量子化しない）: エンジンのデューティの分解能は
#   1/200 で、余りを持ち越すので積み上がらない。周期平均の差の上限の見積もりは 0.5 刻み × Vin / 200 = 60 mV。
TOL_SW_EXACT_CLOSED = 0.005 * VREF
TOL_MODELS = 0.02 * VREF  # 受け入れ基準4（ロードマップの基準）


# ------------------------------------------------------------------ 参照モデル

class RefPid:
    """後退差分の微分（Kd·s/(1 + Tf·s) に s → (1 − z⁻¹)/Ts）、積分 Ki·Ts·Σe、上下限、積分の止め方。

    止め方: 積分を進める前の出力（P + I[k−1] + D）が上限以上で Ki·e > 0、下限以下で Ki·e < 0 なら積分しない。
    最初のサンプルは e[−1] = e[0]（微分は 0）。
    """

    def __init__(self, kp, ki, kd, ts, tf, lo, hi, clamp=True):
        self.kp, self.ki, self.kd, self.ts, self.tf, self.lo, self.hi = kp, ki, kd, ts, tf, lo, hi
        self.clamp = clamp
        self.i, self.d, self.e_prev = 0.0, 0.0, None

    def __call__(self, e):
        ep = e if self.e_prev is None else self.e_prev
        self.d = (self.tf / self.ts * self.d + self.kd / self.ts * (e - ep)) / (1.0 + self.tf / self.ts)
        self.e_prev = e
        pre = self.kp * e + self.i + self.d
        hold = self.clamp and ((pre >= self.hi and self.ki * e > 0) or (pre <= self.lo and self.ki * e < 0))
        if not hold:
            self.i = self.i + self.ki * self.ts * e
        u = self.kp * e + self.i + self.d
        return self.hi if u > self.hi else self.lo if u < self.lo else u


def _mats(r_load, resr, r_sw):
    a = r_load / (r_load + resr)
    A = np.array([[-(r_sw + a * resr) / L, -a / L], [a / C, -1.0 / ((r_load + resr) * C)]])
    return A, np.array([1.0 / L, 0.0]), np.array([a * resr, a])


_CACHE: dict = {}


def _advance(A, vsw, x, dt):
    """x' = A·x + [vsw/L, 0] を dt だけ厳密に進め、(x(dt), ∫x) を返す。"""
    key = (A.tobytes(), float(vsw), float(dt))
    M = _CACHE.get(key)
    if M is None:
        Z = np.zeros((5, 5))
        Z[0:2, 0:2] = A
        Z[0, 2] = vsw / L
        Z[3:5, 0:2] = np.eye(2)
        M = _CACHE[key] = expm(Z * dt)
    z = M @ np.concatenate([x, [1.0, 0.0, 0.0]])
    return z[0:2], z[3:5]


def reference(*, mode, t_end=10e-3, f_ctrl=100e3, carrier="triangle", r_load=R, resr=0.0, r_sw=0.0,
              ref_step=None, load_step=None, fixed=None, kp=KP, ki=KI, kd=KD, tf=TF, dmin=0.0, dmax=0.9):
    """周期平均の v_out と iL、測定値。fixed を与えるとデューティ固定（開ループ）。"""
    tc = 1.0 / f_ctrl
    n_sub = round(tc / T)
    pid = RefPid(kp, ki, kd, tc, tf, dmin, dmax)
    x = np.zeros(2)
    d_next = dmin if fixed is None else fixed
    v_avg, i_avg, samples = [], [], []
    for k in range(round(t_end / tc)):
        tk = k * tc
        rl = load_step[1] if load_step and tk >= load_step[0] - 1e-12 else r_load
        A, _, c = _mats(rl, resr, r_sw)
        y = float(c @ x)
        samples.append(y)
        r = ref_step[1] if ref_step and tk >= ref_step[0] - 1e-12 else VREF
        u = pid(r - y) if fixed is None else fixed
        d, d_next = d_next, u
        for _ in range(n_sub):
            if mode == "average":
                parts = [(d * VIN, T)]
            elif carrier == "triangle":
                parts = [(0.0, (1 - d) * T / 2), (VIN, d * T), (0.0, (1 - d) * T / 2)]
            else:
                parts = [(VIN, d * T), (0.0, (1 - d) * T)]
            acc = np.zeros(2)
            for vsw, dt in parts:
                if dt > 0:
                    x, ix = _advance(A, vsw, x, dt)
                    acc += ix
            v_avg.append(float(c @ acc) / T)
            i_avg.append(acc[0] / T)
    return np.array(v_avg), np.array(i_avg), np.array(samples)


# ------------------------------------------------------------------ エンジンを動かす口

def spec_dict(fidelity, t_end=10e-3, plant=None, control=None, drop=()):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    for k, (v, u) in (plant or {}).items():
        d["plant"][k] = {"value": v, "unit": u, "source": "検証"}
    for k, (v, u) in (control or {}).items():
        d["control"][k] = {"value": v, "unit": u, "source": "検証"}
    for k in drop:
        d["plant"].pop(k, None)
        d["control"].pop(k, None)
    return d


def build(fidelity, t_end=10e-3, plant=None, control=None, carrier=None):
    ex = expand(Spec.model_validate(spec_dict(fidelity, t_end, plant, control)))
    if carrier is not None:
        ex.gates = [g.model_copy(update={"carrier": carrier}) if g.name == "q1" else g for g in ex.gates]
    return ex


def period_avg(res, name="v(out)"):
    """周期 [jT, (j+1)T] の台形則の積分を T で割る（周期の整数倍の区間）。"""
    n = round(T / res.h)
    y = res[name]
    m = (len(y) - 1) // n
    return np.array([np.trapezoid(y[j * n: (j + 1) * n + 1]) / n for j in range(m)])


def max_diff(a, b):
    n = min(len(a), len(b))
    return float(np.max(np.abs(a[1:n] - b[1:n])))       # 最初の1周期を除く


OPEN = {"Kp": (0, "1/V"), "Ki": (0, "1/(V·s)"), "Kd": (0, "s/V"), "d_min": (0.5, "1")}
SCENARIOS = {
    "startup": ({}, {}, {}),
    "reference_step": ({}, {"Vref_step": (10, "V"), "t_ref_step": (5, "ms")}, {"ref_step": (5e-3, 10.0)}),
    "load_step": ({"R_load_step": (3, "ohm"), "t_load_step": (5, "ms")}, {}, {"load_step": (5e-3, 3.0)}),
}


@pytest.fixture(scope="module")
def closed():
    out = {}
    for name, (plant, control, _) in SCENARIOS.items():
        for fidelity in ("switching", "average"):
            ex = build(fidelity, plant=plant, control=control)
            out[name, fidelity] = (ex, ex.run())
    return out


# ------------------------------------------------------------------ 基準1・2: PID

def test_c1_pid_matches_own_difference_equation_on_random_cases():
    """基準1: 乱数（シード固定）で選んだゲイン、周期、上下限、入力列の300通りで、1e-12 以内で一致する。"""
    rng = np.random.default_rng(20261115)
    worst, saturated = 0.0, 0
    for trial in range(300):
        kp, ki, kd = rng.uniform(0, 2), rng.uniform(0, 5e3), rng.uniform(0, 1e-3)
        ts = 10 ** rng.uniform(-6, -2)
        tf = 0.0 if trial % 5 == 0 else ts * rng.uniform(0, 3)
        lo, hi = sorted(rng.uniform(-1, 1.5, 2))
        clamp = trial % 7 != 0
        e = rng.normal(0, 1, 300) * rng.choice([0.01, 1, 10]) + rng.choice([-1, 0, 1]) * rng.uniform(0, 3)
        pid = Pid(kp, ki, kd, period=ts, t_filter=tf, lower=lo, upper=hi,
                  anti_windup="clamp" if clamp else "none")
        mine = RefPid(kp, ki, kd, ts, tf, lo, hi, clamp)
        for x in e:
            u, v = pid.update(x), mine(x)
            worst = max(worst, abs(u - v))
            saturated += v in (lo, hi)
    assert saturated > 10000                              # 上下限が実際に効いている
    assert worst <= 1e-12


def test_c2_anti_windup_on_the_buck_load_step():
    """基準2（降圧コンバータで）: 重い負荷の段差（6 → 1 Ω、d_max 0.51）で上限に張り付く間、
    積分を進める前の出力（P + I[k−1] + D）が上限以上で偏差が正のサンプルでは、積分が変わらない
    （処理なしでは増える）。オーバーシュートは処理なしの半分以下。"""
    runs = {}
    for aw in ("clamp", "none"):
        ex = build("average", plant={"R_load_step": (1, "ohm"), "t_load_step": (3, "ms")},
                   control={"d_max": (0.51, "1")})
        old = ex.controllers[0].pid
        pid = Pid(old.kp, old.ki, old.kd, period=old.period, t_filter=old.t_filter,
                  lower=old.limiter.lower, upper=old.limiter.upper, anti_windup=aw)
        log = []
        update = pid.update

        def logged(e, _update=update, _pid=pid, _log=log):
            before = _pid.integral
            u = _update(e)
            pre = _pid.kp * e + before + _pid.derivative
            _log.append((e, pre, before, _pid.integral, u))
            return u

        pid.update = logged
        ex.controllers[0].pid = pid
        runs[aw] = (period_avg(ex.run()), np.array(log))
    e, pre, before, after, u = runs["clamp"][1].T
    pushing = (u >= 0.51) & (pre >= 0.51) & (e > 0)
    assert pushing.sum() >= 5                            # 積分の止め方が実際に働く場面になっている
    assert np.all(after[pushing] == before[pushing])
    e0, pre0, before0, after0, u0 = runs["none"][1].T
    pushing0 = (u0 >= 0.51) & (pre0 >= 0.51) & (e0 > 0)
    assert np.sum(after0[pushing0] - before0[pushing0]) > 0.02     # 処理なしは積み上がる
    k0 = round(3e-3 / T) + 1
    over = runs["clamp"][0][k0:].max() - VREF
    over0 = runs["none"][0][k0:].max() - VREF
    assert over0 > 0.5
    assert over <= 0.5 * over0


# ------------------------------------------------------------------ 基準3・4: 厳密な参照モデルとの照合

@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_c4_average_model_matches_exact_average_reference(closed, scenario):
    _, res = closed[scenario, "average"]
    ref, _, _ = reference(mode="average", r_sw=R_ON, **SCENARIOS[scenario][2])
    assert max_diff(period_avg(res), ref) <= TOL_AVG_EXACT


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_c4_switching_model_matches_exact_switching_reference(closed, scenario):
    _, res = closed[scenario, "switching"]
    ref, _, _ = reference(mode="switching", r_sw=R_ON, **SCENARIOS[scenario][2])
    assert max_diff(period_avg(res), ref) <= TOL_SW_EXACT_CLOSED


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_c4_models_agree_within_two_percent(closed, scenario):
    ps = period_avg(closed[scenario, "switching"][1])
    pa = period_avg(closed[scenario, "average"][1])
    assert len(ps) == len(pa) == 1000
    assert max_diff(ps, pa) <= TOL_MODELS


def test_c3_steady_state_is_vref_minus_half_ripple(closed):
    """基準3と、測る位置: 測定は周期の頭（三角波ではオフの区間の中央）で、そこではインダクタ電流が平均に
    等しく、コンデンサの電圧は山の頂点。積分があるので頂点が Vref に合い、周期平均は Vref − ΔV/2 になる。
    ΔV = ΔI·T/(8C)、ΔI = (Vin − Vo)·D·T/L、D = Vo/Vin（約 8 mV）。差 2 mV 以内（のこぎり波なら 8 mV ずれる）。
    合わせて、基準3（最後の 2 ms の平均が Vref と 0.2% 以内）。"""
    pa = period_avg(closed["startup", "switching"][1])
    last = pa[-200:].mean()
    d = VREF / VIN
    ripple = (VIN - VREF) * d * T / L * T / (8 * C)
    assert abs(last - (VREF - ripple / 2)) <= 2e-3
    assert abs(last - VREF) <= 0.002 * VREF


def test_open_loop_switching_matches_exact_solution_for_both_carriers():
    """固定デューティ 0.5 の起動（Q = 8.75 で大きく振れる）: エンジンのスイッチングモデルは、
    どちらの搬送波でも厳密解（オン抵抗 1 mΩ を入れた参照）と 0.05% 以内。
    平均モデルは厳密な平均（オン抵抗 1 mΩ を入れた参照）と 0.1% 以内。"""
    for carrier in ("triangle", "sawtooth"):
        res = build("switching", 3e-3, control=OPEN, carrier=carrier).run()
        ref, _, _ = reference(mode="switching", t_end=3e-3, fixed=0.5, carrier=carrier, r_sw=R_ON)
        assert max_diff(period_avg(res), ref) <= TOL_SW_EXACT_OPEN, carrier
    res = build("average", 3e-3, control=OPEN).run()
    ref, _, _ = reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=R_ON)
    assert max_diff(period_avg(res), ref) <= TOL_AVG_EXACT


def test_open_loop_sawtooth_would_break_criterion_4():
    """既定を三角波にした理由の確認: 参照モデルでも、のこぎり波の周期平均は平均モデルより 2% 以上先に進む。"""
    saw, _, _ = reference(mode="switching", t_end=3e-3, fixed=0.5, carrier="sawtooth")
    tri, _, _ = reference(mode="switching", t_end=3e-3, fixed=0.5, carrier="triangle")
    avg, _, _ = reference(mode="average", t_end=3e-3, fixed=0.5)
    assert max_diff(saw, avg) > TOL_MODELS
    assert max_diff(tri, avg) < 0.1 * TOL_MODELS
    assert build("switching").gates[0].carrier == "triangle"


# ------------------------------------------------------------------ 基準6: 遅れ

@pytest.mark.parametrize("fidelity", ["switching", "average"])
@pytest.mark.parametrize("f_ctrl_khz", [100, 50])
def test_c6_command_equals_own_pid_on_samples_one_period_later(fidelity, f_ctrl_khz):
    """エンジンが記録した v(out) を t_k で取り、このファイルのPIDに通すと、その出力は cmd(duty) の
    [t_{k+1}, t_{k+2}) の値と 1e-12 以内で一致する。0周期・2周期ずらすと合わない。
    平均モデルでは、レグの電圧源の端 v(sw_avg) が Vin·指令、オン抵抗の先 v(sw) が Vin·指令 − r_on·i(L1)。"""
    tc = 1.0 / (f_ctrl_khz * 1e3)
    ex = build(fidelity, 3e-3, control={"f_ctrl": (f_ctrl_khz, "kHz"), "Vref_step": (10, "V"),
                                        "t_ref_step": (2, "ms")})
    res = ex.run()
    n = round(tc / res.h)
    k_max = (len(res.t) - 1) // n
    pid = RefPid(KP, KI, KD, tc, TF, 0.0, 0.9)
    v = res["v(out)"]
    mine = np.array([pid((VREF if k * tc < 2e-3 - 1e-12 else 10.0) - v[k * n]) for k in range(k_max)])
    cmd = res["cmd(duty)"]
    used = np.array([cmd[k * n] for k in range(k_max)])
    assert all(np.ptp(cmd[k * n:(k + 1) * n]) == 0.0 for k in range(k_max))
    assert used[0] == 0.0
    assert np.max(np.abs(used[1:] - mine[:-1])) <= 1e-12
    assert np.max(np.abs(used[2:] - mine[:-2])) > 1e-3 and np.max(np.abs(used - mine)) > 1e-3
    if fidelity == "average":
        mid = n // 2
        i_l = res["i(L1)"][mid::n][:k_max]
        assert np.max(np.abs(res["v(sw_avg)"][mid::n][:k_max] - VIN * used)) <= 1e-9
        assert np.max(np.abs(res["v(sw)"][mid::n][:k_max] - (VIN * used - R_ON * i_l))) <= 1e-9


@pytest.mark.parametrize("duty", [0.25, 0.8])
def test_c6_triangle_pwm_has_the_same_small_signal_delay_as_the_average_model(duty):
    """三角波のPWMの小信号の遅れは、どのデューティでも平均モデル（零次ホールド、0.5 サンプル）と同じ。
    開ループでデューティに 10 kHz の小さな正弦波を足し、周期平均の位相の差を測る。差 0.05 サンプル以内
    （のこぎり波なら (0.5 − D) サンプル = 0.25 以上ずれる）。"""
    f = 10e3

    class SineDuty:
        period, inputs, outputs = T, (), ("duty",)

        def reset(self):
            return [duty]

        def step(self, t, measured):
            return [duty + 0.01 * math.sin(2 * math.pi * f * (t + T))]

    phases = {}
    for fidelity in ("switching", "average"):
        ex = build(fidelity, 12e-3)
        ex.controllers[0] = SineDuty()
        pa = period_avg(ex.run())
        t = (np.arange(len(pa)) + 0.5) * T
        s = t >= 6e-3
        A = np.column_stack([np.sin(2 * np.pi * f * t[s]), np.cos(2 * np.pi * f * t[s]), np.ones(s.sum())])
        coef, *_ = np.linalg.lstsq(A, pa[s], rcond=None)
        phases[fidelity] = math.atan2(coef[1], coef[0])
    diff = (phases["switching"] - phases["average"] + math.pi) % (2 * math.pi) - math.pi
    assert abs(diff / (2 * math.pi) * (1 / f) / T) <= 0.05


# ------------------------------------------------------------------ 仕様に無い角

def test_control_period_twice_the_carrier_period():
    """f_ctrl = f_sw/2: 平均モデルは厳密な平均と、スイッチングモデルは厳密解と、両者は 2% 以内で一致。"""
    control = {"f_ctrl": (50, "kHz")}
    pa = period_avg(build("average", control=control).run())
    ps = period_avg(build("switching", control=control).run())
    ra, _, _ = reference(mode="average", f_ctrl=50e3, r_sw=R_ON)
    rs, _, _ = reference(mode="switching", f_ctrl=50e3, r_sw=R_ON)
    assert max_diff(pa, ra) <= TOL_AVG_EXACT
    assert max_diff(ps, rs) <= TOL_SW_EXACT_CLOSED
    assert max_diff(ps, pa) <= TOL_MODELS


def test_light_load_negative_inductor_current_still_matches_average():
    """軽負荷（1 kΩ）: 同期整流なのでインダクタ電流は負になる。周期平均は平均モデルと 2% 以内。"""
    plant = {"R_load": (1000, "ohm")}
    sw = build("switching", plant=plant).run()
    pa = period_avg(build("average", plant=plant).run())
    ra, _, _ = reference(mode="average", r_load=1000.0, r_sw=R_ON)
    assert sw["i(L1)"].min() < -0.3
    assert max_diff(period_avg(sw), pa) <= TOL_MODELS
    assert max_diff(pa, ra) <= TOL_AVG_EXACT


def test_esr_average_model_matches_exact_reference():
    pa = period_avg(build("average", plant={"C_esr": (100, "mohm")}).run())
    ra, _, _ = reference(mode="average", resr=0.1, r_sw=R_ON)
    assert max_diff(pa, ra) <= TOL_AVG_EXACT


def _closed_loop_poles(kp, ki, kd, tf, f_ctrl):
    """このファイルで組んだ離散の閉ループ（零次ホールドの植物 × z⁻¹ × 後退差分のPID）の極の絶対値の最大。"""
    ts = 1.0 / f_ctrl
    nd, dd, _ = signal.cont2discrete(([VIN], [L * C, L / R, 1.0]), ts, method="zoh")
    nd = np.atleast_1d(np.squeeze(nd))
    a, g = tf / (tf + ts), kd / (tf + ts)
    cn = np.polyadd(np.polyadd(kp * np.polymul([1, -1], [1, -a]), ki * ts * np.polymul([1, 0], [1, -a])),
                    g * np.polymul([1, -1], [1, -1]))
    cd = np.polymul([1, -1], [1, -a])
    char = np.polyadd(np.polymul(np.polymul(cd, dd), [1, 0]), np.polymul(cn, nd))
    return float(np.max(np.abs(np.roots(char))))


def test_example_gains_give_a_stable_closed_loop():
    assert _closed_loop_poles(KP, KI, KD, TF, 100e3) < 0.98


@pytest.mark.parametrize("f_ctrl", [25e3])
def test_initial_gain_formula_never_reports_an_unstable_design(f_ctrl):
    from circuit_sim.tuning import buck_voltage_mode_pid
    try:
        g = buck_voltage_mode_pid(VIN, L, C, R, f_ctrl)
    except ValueError:
        return                                          # 見つからないと断るのは正しい
    assert 45.0 <= g.phase_margin_deg <= 180.0
    assert _closed_loop_poles(g.kp, g.ki, g.kd, g.t_filter, f_ctrl) < 1.0


# ------------------------------------------------------------------ 基準7: 仕様の読み込み

@pytest.mark.parametrize("key,unit", [
    ("Kp", "V"), ("Kp", "1"), ("Kp", "%"), ("Kp", "s/V"), ("Kp", "1/(V·s)"), ("Kp", ""), ("Kp", "k1/V"),
    ("Ki", "1/V"), ("Ki", "s/V"), ("Ki", "Hz"), ("Kd", "1/V"), ("Kd", "us"), ("d_max", "V"), ("d_max", ""),
    ("d_max", "1/V"), ("d_max", "m%"), ("f_sw", "1"), ("f_sw", "%"), ("Vref", "1"), ("Vref", "%"),
    ("Tf", "1"), ("Tf", "s/V"),
])
def test_c7_wrong_control_units_are_rejected(key, unit):
    d = spec_dict("switching")
    d["control"][key] = {"value": 1, "unit": unit, "source": "検証"}
    with pytest.raises(UnitError):
        expand(Spec.model_validate(d))


@pytest.mark.parametrize("key,value,unit,si", [
    ("d_max", 90, "%", 0.9), ("Ki", 147, "1/(V*s)", 147.0), ("Ki", 147, "1/(V s)", 147.0),
    ("Tf", 2500, "ns", 2.5e-6), ("Vref", 0.012, "kV", 12.0),
])
def test_c7_control_units_are_converted(key, value, unit, si):
    d = spec_dict("switching")
    d["control"][key] = {"value": value, "unit": unit, "source": "検証"}
    ex = expand(Spec.model_validate(d))
    pid, ctrl = ex.controllers[0].pid, ex.controllers[0]
    got = {"d_max": pid.limiter.upper, "Ki": pid.ki, "Tf": pid.t_filter, "Vref": ctrl.reference.initial}[key]
    assert got == pytest.approx(si, rel=1e-12)


def test_c7_version_01_gives_the_same_result_as_02_and_says_so():
    d02 = spec_dict("switching", 1e-3)
    d01 = copy.deepcopy(d02)
    d01["schema_version"] = "0.1"
    d01["control"] = {"f_sw": 100e3, "f_ctrl": 100e3, "Vref": 12, "Kp": KP, "Ki": KI, "Kd": KD, "Tf": TF,
                      "d_min": 0, "d_max": 0.9}
    ex01, ex02 = expand(Spec.model_validate(d01)), expand(Spec.model_validate(d02))
    assert any("0.1" in n and "記載なし" in n for n in ex01.notes)
    assert not any("版 0.1" in n for n in ex02.notes)
    assert np.array_equal(ex01.run().x, ex02.run().x)
    d01["control"]["Kp"] = True
    with pytest.raises(ValueError):
        Spec.model_validate(d01)


def test_c7_omitted_esr_is_noted_in_the_command_line_output(tmp_path, capsys):
    path = tmp_path / "buck.json"
    path.write_text(json.dumps(spec_dict("average", 1e-3)), encoding="utf-8")
    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "注意: C_esr" in out and "計算に入れていない" in out


# ------------------------------------------------------------------ 基準8: 再現性（別のプロセスでも）

_DIGEST = """
import hashlib, json, sys
from circuit_sim import Spec, expand
ex = expand(Spec.model_validate(json.loads(sys.argv[1])))
r = ex.run()
h = hashlib.sha256(r.t.tobytes() + r.x.tobytes())
for k in sorted(r.signals):
    h.update(k.encode() + r.signals[k].tobytes())
print(h.hexdigest())
"""


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_c8_same_result_in_other_processes_with_other_hash_seeds(fidelity):
    d = spec_dict(fidelity, 1e-3, plant={"R_load_step": (3, "ohm"), "t_load_step": (0.4, "ms")},
                  control={"Vref_step": (10, "V"), "t_ref_step": (0.6, "ms")})
    digests = set()
    for seed in ("0", "4242"):
        env = {**os.environ, "PATH": os.environ["CIRCUIT_SIM_SAFE_PATH"], "PYTHONHASHSEED": seed,
               "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-c", _DIGEST, json.dumps(d)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=120, check=True)
        digests.add(out.stdout.strip())
    r = expand(Spec.model_validate(d)).run()
    h = hashlib.sha256(r.t.tobytes() + r.x.tobytes())
    for k in sorted(r.signals):
        h.update(k.encode() + r.signals[k].tobytes())
    digests.add(h.hexdigest())
    assert len(digests) == 1


# ------------------------------------------------------------------ 実装者の試験で生き残った分岐

def test_off_grid_load_step_is_moved_to_the_next_carrier_period():
    """負荷の段差の時刻 1.0033 ms は、次の搬送波の周期の頭 1.01 ms に切り上げる（早めない）。"""
    ex = build("average", 2e-3, plant={"R_load_step": (3, "ohm"), "t_load_step": (1.0033, "ms")})
    res = ex.run()
    gate = res["gate(q_load)"]
    assert int(np.argmax(gate > 0.5)) == round(1.01e-3 / res.h)
    assert any("0.00101 s" in n and "合わせた" in n for n in ex.notes)


def test_reference_below_the_lower_duty_limit_is_noted():
    ex = build("average", 1e-3, control={"Vref": (0.5, "V"), "d_min": (0.1, "1")})
    assert any("届かない" in n and "0.5 V" in n for n in ex.notes)


@pytest.mark.parametrize("section,key,value,unit", [
    ("control", "Kp", -0.01, "1/V"), ("control", "Ki", -1, "1/(V·s)"), ("control", "Kd", -1e-6, "s/V"),
    ("plant", "C_esr", -1, "mohm"),
])
def test_negative_gains_and_esr_are_rejected(section, key, value, unit):
    d = spec_dict("average", 1e-3)
    d[section][key] = {"value": value, "unit": unit, "source": "検証"}
    with pytest.raises(ValueError, match=key):
        expand(Spec.model_validate(d))
