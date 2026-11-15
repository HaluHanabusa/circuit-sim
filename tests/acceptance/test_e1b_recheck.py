"""E1b の再検証（c96ad81。レビューと独立検証の指摘を直したあと）の受け入れ試験。検証担当が追加した。

記録は docs/verification/E1b-verifier-2.md。期待値は、このファイルと test_e1b_independent.py の
参照モデル（区分ごとの厳密解）で作る。`circuit_sim` の関数を期待値の計算に使わない。
  ゲインの初期値の確かめ: このファイルの開ループと閉ループは、実装（tuning/initial.py の多項式と
  根の偏角）とも、実装担当の試験（scipy の cont2discrete と np.unwrap）とも別の経路で作る:
    植物は状態 [iL, vC] を拡大行列の expm で零次ホールドに離散化し、P(z) = c·(zI − Ad)⁻¹·bd を
    連立方程式で解く。
    閉ループは状態 [x_k, u_k, I_{k−1}, D_{k−1}, e_{k−1}] の 6×6 の行列の固有値（多項式を作らない）。
    位相は、隣り合う点の主値の差が 3° を超える区間を二分して細かくしてから np.unwrap（低い周波数から連続）。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import re
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm
from scipy.optimize import brentq

from circuit_sim import Spec, expand
from circuit_sim.control import Pid, PidController
from circuit_sim.control.schedule import Schedule
from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("e1b_independent_reference", _HERE / "test_e1b_independent.py")
ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ref)

VIN, L, C, R, VREF, T, R_ON = ref.VIN, ref.L, ref.C, ref.R, ref.VREF, ref.T, ref.R_ON

# 許容差（測る前に決めた根拠）
# 開ループ（D = 0.5 の起動、3 ms）の平均モデルと厳密な平均: LC の振れの振れ幅は約 12 V で、時定数
#   2RC = 1.2 ms で減る。台形則（刻み T/10）の周波数の相対誤差 (ω0·h)²/12 ≈ 1.8e-5 による位相のずれ
#   ω0·t·1.8e-5 と振れ幅の積は t = 2RC で最大になり、12 V × 14586 × 1.2e-3 × 1.8e-5 / e ≈ 1.4 mV。
#   許容差はその約2.5倍の 0.03%（3.6 mV）。
#   オン抵抗の有無の効果の見積もり: 減衰の速さが r_on/(2L) = 10.6 /s 変わり、t = 2RC で
#   12 V × e⁻¹ × 1.27% ≈ 56 mV（0.47%）。許容差の約15倍なので、オン抵抗が無い・2倍の誤りを見分ける。
TOL_AVG_OPEN = 0.0003 * VREF
EFFECT_R_ON = 0.003 * VREF          # オン抵抗の有無の効果の見積もり 0.47% より小さい下限
OPEN = ref.OPEN


def pct(x):
    return f"{x / VREF * 100:.4f}%"


# ------------------------------------------------------------------ 平均モデルのオン抵抗

def test_average_leg_is_a_command_source_with_the_on_resistance_in_series():
    """平均モデルのレグ: 電圧源の端 v(sw_avg) = Vin·指令、オン抵抗の先 v(sw) = v(sw_avg) − r_on·i(L1)、
    電圧源の電流 i(Vleg)（素子の中を sw_avg → 0 が正）は −i(L1)。すべての時刻で 1e-9 以内。"""
    ex = ref.build("average", 2e-3, control={"Vref_step": (10, "V"), "t_ref_step": (1, "ms")})
    res = ex.run()
    v_avg, v_sw, i_l, i_v, cmd = (res[k] for k in ("v(sw_avg)", "v(sw)", "i(L1)", "i(Vleg)", "cmd(duty)"))
    assert np.ptp(cmd) > 0.3 and np.ptp(i_l) > 1.0                     # 指令も電流も実際に動いている
    assert np.max(np.abs(v_avg - VIN * cmd)) <= 1e-9
    assert np.max(np.abs(v_sw - (v_avg - R_ON * i_l))) <= 1e-9
    assert np.max(np.abs(i_v + i_l)) <= 1e-9
    assert np.max(np.abs(v_sw - VIN * cmd)) > 1e-3                      # オン抵抗の電圧降下が見える大きさ


def test_open_loop_average_model_matches_exact_average_with_on_resistance():
    """固定デューティ 0.5 の起動（Q = 8.75）: 平均モデルは、オン抵抗 1 mΩ を入れた厳密な平均と 0.03% 以内。
    オン抵抗の無い参照・2倍の参照とは 0.3% 以上違う（この照合でオン抵抗の有無と大きさが見える）。"""
    pa = ref.period_avg(ref.build("average", 3e-3, control=OPEN).run())
    with_r, _, _ = ref.reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=R_ON)
    without, _, _ = ref.reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=0.0)
    double, _, _ = ref.reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=2 * R_ON)
    assert ref.max_diff(pa, with_r) <= TOL_AVG_OPEN, pct(ref.max_diff(pa, with_r))
    assert ref.max_diff(pa, without) > EFFECT_R_ON
    assert ref.max_diff(pa, double) > EFFECT_R_ON


def test_open_loop_switching_and_average_differ_only_by_the_exact_ripple_effect():
    """開ループの高い Q の過渡で、エンジンのスイッチングと平均の周期平均の差は、厳密な参照どうし（どちらも
    オン抵抗 1 mΩ）の差に、2つの照合の許容差（平均 0.03%、スイッチング 0.05%）を足した値以下。
    オン抵抗を平均に入れない場合の差（参照で 0.4% 以上）より小さい。"""
    ps = ref.period_avg(ref.build("switching", 3e-3, control=OPEN).run())
    pa = ref.period_avg(ref.build("average", 3e-3, control=OPEN).run())
    rs, _, _ = ref.reference(mode="switching", t_end=3e-3, fixed=0.5, r_sw=R_ON)
    ra, _, _ = ref.reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=R_ON)
    ra0, _, _ = ref.reference(mode="average", t_end=3e-3, fixed=0.5, r_sw=0.0)
    bound = ref.max_diff(rs, ra) + TOL_AVG_OPEN + ref.TOL_SW_EXACT_OPEN
    assert ref.max_diff(ps, pa) <= bound, (pct(ref.max_diff(ps, pa)), pct(bound))
    assert ref.max_diff(rs, ra0) > 0.004 * VREF
    assert bound < 0.004 * VREF


# ------------------------------------------------------------------ 三角波の測定点のずれ

def _offset_formula(vref):
    """自分で導いた式。周期の頭（オフの区間の中央）ではコンデンサの電流が 0 から負に変わるので、そこが
    コンデンサ電圧の山の頂点。区分ごとに iC を積分し、周期平均との差を取ると  ΔI·T·(1 + D)/(24·C)、
    ΔI = (Vin − Vo)·D·T/L、D = Vo/Vin。"""
    d = vref / VIN
    return (VIN - vref) * d * T / L * T * (1 + d) / (24 * C)


@pytest.mark.parametrize("vref", [6.0, 18.0])
def test_triangle_sampling_offset_follows_the_general_formula(vref):
    """D = 0.25・0.75: 定常の Vref − 周期平均 が一般の式に合う。
    厳密な参照（デューティを量子化しない）とは 0.05 mV 以内（式の省略は負荷電流のリプルと損失で、
    見積もり 0.01 mV 以下）。エンジン（刻み T/200）とは 0.3 mV 以内（区別したい D = 0.5 の形 ΔV/2 との差は
    1.0 mV で、その 1/3）。"""
    formula = _offset_formula(vref)
    half_ripple = (VIN - vref) * (vref / VIN) * T / L * T / (16 * C)
    assert abs(formula - half_ripple) > 0.9e-3                          # 2つの式を区別できる条件
    saved = ref.VREF
    try:
        ref.VREF = vref
        exact, _, _ = ref.reference(mode="switching", t_end=12e-3, dmax=0.95)
    finally:
        ref.VREF = saved
    assert abs((vref - exact[-300:].mean()) - formula) <= 0.05e-3
    ex = ref.build("switching", 10e-3, control={"Vref": (vref, "V"), "d_max": (0.95, "1")})
    pa = ref.period_avg(ex.run())
    assert abs((vref - pa[-300:].mean()) - formula) <= 0.3e-3


# ------------------------------------------------------------------ 微分を測定値に掛けるPID

class MeasPid:
    """自分で導いた差分方程式。D(s) = −Kd·s/(1 + Tf·s)·Y(s) に s → (1 − z⁻¹)/Ts:
    D_k·(Ts + Tf) − Tf·D_{k−1} = −Kd·(y_k − y_{k−1})、y_{−1} = y_0。P と I と止め方は偏差に掛ける形と同じ。"""

    def __init__(self, kp, ki, kd, ts, tf, lo, hi, clamp=True):
        self.kp, self.ki, self.kd, self.ts, self.tf = kp, ki, kd, ts, tf
        self.lo, self.hi, self.clamp = lo, hi, clamp
        self.i, self.d, self.y_prev = 0.0, 0.0, None

    def __call__(self, r, y):
        e = r - y
        yp = y if self.y_prev is None else self.y_prev
        self.d = (self.tf * self.d - self.kd * (y - yp)) / (self.ts + self.tf)
        self.y_prev = y
        pre = self.kp * e + self.i + self.d
        hold = (pre >= self.hi and self.ki * e > 0) or (pre <= self.lo and self.ki * e < 0)
        if not (self.clamp and hold):
            self.i += self.ki * self.ts * e
        u = self.kp * e + self.i + self.d
        return self.hi if u > self.hi else self.lo if u < self.lo else u


def test_pid_derivative_on_measurement_matches_own_difference_equation():
    """乱数（シード固定）の300通り（指令値は6段の段差、上下限、clamp / none、Tf = 0 を含む）で 1e-12 以内。"""
    rng = np.random.default_rng(1115)
    worst, saturated = 0.0, 0
    for trial in range(300):
        kp, ki, kd = rng.uniform(0, 2), rng.uniform(0, 5e3), rng.uniform(0, 1e-3)
        ts = 10 ** rng.uniform(-6, -2)
        tf = 0.0 if trial % 5 == 0 else ts * rng.uniform(0, 3)
        lo, hi = sorted(rng.uniform(-1, 1.5, 2))
        clamp = trial % 7 != 0
        y = rng.normal(0, 1, 300) * rng.choice([0.01, 1, 10]) + rng.uniform(-3, 3)
        r = np.repeat(rng.uniform(-3, 3, 6), 50)
        pid = Pid(kp, ki, kd, period=ts, t_filter=tf, lower=lo, upper=hi,
                  anti_windup="clamp" if clamp else "none", derivative_on="measurement")
        mine = MeasPid(kp, ki, kd, ts, tf, lo, hi, clamp)
        for ri, yi in zip(r, y, strict=True):
            u, v = pid.update(ri - yi, measurement=yi), mine(ri, yi)
            worst = max(worst, abs(u - v))
            saturated += v in (lo, hi)
    assert saturated > 10000
    assert worst <= 1e-12


def test_pid_derivative_on_measurement_differs_from_error_only_at_reference_steps():
    """指令値が一定なら偏差に掛けるものと同じ（丸めの分 1e-10 以内: r − y の差の丸め 1e-16·|y| を
    Kd/(Tf + Ts) ≦ 1e3 倍しても 1e-11 程度）。指令値の段差 Δr では、偏差に掛けるものだけが
    Kd/(Tf + Ts)·Δr だけ余分に跳ねる。"""
    rng = np.random.default_rng(3)
    for _ in range(50):
        kp, ki, kd = rng.uniform(0, 2), rng.uniform(0, 5e3), rng.uniform(0, 1e-3)
        ts, tf = 10 ** rng.uniform(-6, -3), 10 ** rng.uniform(-7, -3)
        pe = Pid(kp, ki, kd, period=ts, t_filter=tf, derivative_on="error")
        pm = Pid(kp, ki, kd, period=ts, t_filter=tf, derivative_on="measurement")
        for yi in rng.normal(0, 10, 100):
            assert abs(pe.update(1.0 - yi) - pm.update(1.0 - yi, measurement=yi)) <= 1e-10
    kp, ki, kd, ts, tf = ref.KP, ref.KI, ref.KD, 1e-5, ref.TF
    out = {}
    for mode in ("error", "measurement"):
        pid = Pid(kp, ki, kd, period=ts, t_filter=tf, derivative_on=mode)
        out[mode] = [pid.update(r - 12.0, measurement=12.0) if mode == "measurement" else pid.update(r - 12.0)
                     for r in (12.0, 12.0, 10.0)]
    kick = (out["error"][2] - out["error"][1]) - (out["measurement"][2] - out["measurement"][1])
    assert kick == pytest.approx(kd / (tf + ts) * (10.0 - 12.0), rel=1e-12)


def test_pid_controller_passes_the_measurement_to_the_derivative():
    pid = Pid(ref.KP, ref.KI, ref.KD, period=T, t_filter=ref.TF, lower=0.0, upper=0.9,
              derivative_on="measurement")
    ctrl = PidController(pid, measure="v(out)", output="duty", reference=Schedule(12.0, ((2 * T, 10.0),)),
                         initial_output=0.0)
    ctrl.reset()
    mine = MeasPid(ref.KP, ref.KI, ref.KD, T, ref.TF, 0.0, 0.9)
    for k, y in enumerate([0.0, 3.0, 7.0, 11.0, 12.5, 11.8]):
        r = 12.0 if k < 2 else 10.0
        assert ctrl.step(k * T, np.array([y]))[0] == pytest.approx(mine(r, y), abs=1e-15)


# ------------------------------------------------------------------ ゲインの初期値（自分の開ループ）

def _plant_zoh(v_in, ind, cap, r_load, ts):
    m = np.zeros((3, 3))
    m[:2, :2] = np.array([[0.0, -1.0 / ind], [1.0 / cap, -1.0 / (r_load * cap)]]) * ts
    m[:2, 2] = np.array([v_in / ind, 0.0]) * ts
    e = expm(m)
    return e[:2, :2], e[:2, 2]


def my_radius(g, v_in, ind, cap, r_load, f_ctrl):
    """閉ループの状態 [x1, x2, u, I_{k−1}, D_{k−1}, e_{k−1}] の行列の固有値の絶対値の最大。"""
    ts = 1.0 / f_ctrl
    ad, bd = _plant_zoh(v_in, ind, cap, r_load, ts)
    a, b = g.t_filter / (g.t_filter + ts), g.kd / (g.t_filter + ts)
    ek = np.array([0.0, -1.0, 0, 0, 0, 0])                       # e_k = −vC（小信号、指令値 0）
    ik = g.ki * ts * ek + np.array([0, 0, 0, 1.0, 0, 0])
    dk = b * ek + np.array([0, 0, 0, 0, a, -b])
    m = np.zeros((6, 6))
    m[0:2, 0:2], m[0:2, 2] = ad, bd
    m[2], m[3], m[4], m[5] = g.kp * ek + ik + dk, ik, dk, ek
    return float(np.max(np.abs(np.linalg.eigvals(m))))


class MyLoop:
    def __init__(self, kp, ki, kd, tf, v_in, ind, cap, r_load, f_ctrl):
        self.kp, self.ki, self.kd, self.tf, self.ts = kp, ki, kd, tf, 1.0 / f_ctrl
        self.ad, self.bd = _plant_zoh(v_in, ind, cap, r_load, self.ts)

    def __call__(self, w):
        z = np.exp(1j * np.atleast_1d(np.asarray(w, dtype=float)) * self.ts)
        m = z[:, None, None] * np.eye(2)[None] - self.ad[None]
        x = np.linalg.solve(m, np.broadcast_to(self.bd.astype(complex), (len(z), 2))[..., None])[..., 0]
        cz = (self.kp + self.ki * self.ts * z / (z - 1)
              + self.kd * (z - 1) / ((self.tf + self.ts) * z - self.tf))
        return cz * x[:, 1] / z

    def phase(self, w_lo, w_hi, extra=()):
        w = np.unique(np.concatenate([np.geomspace(w_lo, w_hi, 20000), np.asarray(extra, dtype=float)]))
        val = self(w)
        for _ in range(40):
            bad = np.nonzero(np.abs(np.angle(val[1:] / val[:-1])) > math.radians(3.0))[0]
            if bad.size == 0:
                break
            mid = np.sqrt(w[bad] * w[bad + 1])
            w, val = np.insert(w, bad + 1, mid), np.insert(val, bad + 1, self(mid))
        return w, val, np.unwrap(np.angle(val))

    def margins(self, w_lo, w_hi):
        """[(交差周波数, 180° + 連続の位相, −1 からの角度)]。"""
        w, val, ph = self.phase(w_lo, w_hi)
        s = np.log(np.abs(val))
        out = []
        for i in np.nonzero(np.sign(s[:-1]) != np.sign(s[1:]))[0]:
            lw = brentq(lambda x: math.log(abs(self(math.exp(x))[0])), math.log(w[i]), math.log(w[i + 1]),
                        xtol=1e-14)
            v = self(math.exp(lw))[0]
            p = math.atan2(v.imag, v.real)
            p += 2 * math.pi * round((ph[i] - p) / (2 * math.pi))
            geometric = 180.0 - abs(math.degrees(math.atan2(v.imag, v.real)))
            out.append((math.exp(lw), 180.0 + math.degrees(p), geometric))
        return out


def _ranges(ind, cap, f_ctrl, zero_ratio=0.5, f_cross_max=None):
    w0 = 1.0 / math.sqrt(ind * cap)
    wn = math.pi * f_ctrl
    w_hi = min(2 * math.pi * f_cross_max if f_cross_max else 0.5 * wn, 0.9999 * wn)
    return w0, 1e-4 * min(w0, zero_ratio * w0), wn, w_hi


def _shape_margin(args, target_opts):
    """設計の形（Ki = 1。位相は Ki によらない）の 180° + 位相 を、探索の範囲 [1.05·ω0, 上端] で。
    形は自分で係数を比べて導いた: Kp + Ki·Tf = 2ζ·Ki/ωz、Kp·Tf + Kd = Ki/ωz²。"""
    v_in, ind, cap, r_load, f_ctrl = args
    zr, zeta, fr = (target_opts.get(k, d) for k, d in (("zero_ratio", 0.5), ("zero_damping", 0.3),
                                                          ("filter_ratio", 0.25)))
    w0, w_lo, _, w_hi = _ranges(ind, cap, f_ctrl, zr, target_opts.get("f_cross_max"))
    wz, tf = zr * w0, fr / f_ctrl
    kp = 2 * zeta / wz - tf
    lp = MyLoop(kp, 1.0, 1 / wz**2 - kp * tf, tf, v_in, ind, cap, r_load, f_ctrl)
    w, _, ph = lp.phase(w_lo, w_hi, extra=np.geomspace(1.05 * w0, w_hi, 4000))
    sel = w >= 1.05 * w0 * (1 - 1e-12)
    return w[sel], 180.0 + np.degrees(ph[sel])


def _check_design(args, opts):
    """返したゲインは、自分の閉ループで安定、自分の位相余裕（連続の位相、−1 からの角度の両方）が目標以上。
    失敗なら、理由のコードと最良の候補が自分の計算と合う。戻り値は結果の種類。"""
    target = opts.get("phase_margin_deg", 55.0)
    _, w_lo, wn, _ = _ranges(args[1], args[2], args[4], opts.get("zero_ratio", 0.5), opts.get("f_cross_max"))
    try:
        g, err = buck_voltage_mode_pid(*args, **opts), None
    except GainDesignError as exc:
        g, err = exc.best, exc
    if g is not None:
        ms = MyLoop(g.kp, g.ki, g.kd, g.t_filter, *args).margins(w_lo, 0.9999 * wn)
        pm = min(m[1] for m in ms)
        geo = min(m[2] for m in ms)
        radius = my_radius(g, *args)
        assert g.pole_radius == pytest.approx(radius, rel=1e-6)
        assert g.phase_margin_deg == pytest.approx(pm, abs=0.05)
        assert any(abs(m[0] / (2 * math.pi) / g.f_cross - 1) < 1e-4 for m in ms)     # 設計した点は交差周波数
    if err is None:
        assert radius < 1.0
        assert pm >= target - 1e-3 and geo >= target - 1e-3
        assert g.kp >= 0 and g.kd >= 0
        return "ok"
    if err.reason == "margin_not_reached" and g is not None:
        w, margin = _shape_margin(args, opts)
        assert margin.max() < target
        stated = float(re.search(r"最大 (-?[\d.]+)°", str(err)).group(1))
        assert stated == pytest.approx(margin.max(), abs=0.06)
        # 最良の候補は、余裕が最大になる点に交差周波数を置いたもの（実装の格子 600 点の粗さの分 0.06°）
        at_best = np.interp(math.log(2 * math.pi * g.f_cross), np.log(w), margin)
        assert at_best >= margin.max() - 0.06
    elif err.reason == "crossover_at_search_edge":
        w, margin = _shape_margin(args, opts)
        assert margin[-1] >= target
        assert 2 * math.pi * g.f_cross == pytest.approx(w[-1], rel=1e-9)     # 最良の候補は上端に置いたもの
    elif err.reason == "margin_below_target":
        assert pm < target
    elif err.reason == "unstable":
        assert radius >= 1.0
    elif err.reason == "negative_gain":
        assert g.kp < 0 or g.kd < 0
    if g is not None:
        assert err.shortfall_deg == pytest.approx(target - g.phase_margin_deg)
        assert ("足りない" in str(err)) == (g.phase_margin_deg < target)
    return err.reason


@pytest.mark.parametrize("f_ctrl", [20e3, 25e3, 30e3])
def test_initial_gains_former_bug_now_refuses_with_a_true_reason(f_ctrl):
    """前回の不具合の条件（f_ctrl ≦ 30 kHz で約 255° と報告して不安定なゲインを返した）: 失敗を返し、
    理由は margin_not_reached。最良の候補の余裕と極の半径は自分の計算と合う。"""
    assert _check_design((VIN, L, C, R, f_ctrl), {}) == "margin_not_reached"


GRID = [(VIN, ind, cap, r_load, f) for ind in (4.7e-6, 47e-6, 470e-6) for cap in (10e-6, 100e-6)
        for r_load in (0.5, 6.0, 1000.0) for f in (40e3, 100e3, 500e3)]


@pytest.mark.parametrize("args", GRID, ids=lambda a: f"L{a[1]:g}-C{a[2]:g}-R{a[3]:g}-f{a[4]:g}")
def test_initial_gains_on_a_grid_are_stable_with_margin_or_refused_truthfully(args):
    _check_design(args, {})


@pytest.mark.parametrize("args,opts,reason", [
    ((VIN, L, C, R, 100e3), {}, "ok"),
    ((VIN, L, C, R, 100e3), {"phase_margin_deg": 45.0}, "ok"),
    ((VIN, 1e-3, 1e-6, 0.5, 50e3), {}, "margin_below_target"),
    ((12.0, L, C, 0.5, 100e3), {"f_cross_max": 5e3}, "crossover_at_search_edge"),
    ((58.42461303225645, 2.44056228832164e-05, 7.05878209533947e-07, 0.9375425336950132, 560545.6798850347),
     {"filter_ratio": 2.8283006068324945}, "negative_gain"),
])
def test_initial_gains_each_failure_reason_is_true(args, opts, reason):
    assert _check_design(args, opts) == reason


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合（軽微。既定の探索範囲では起きない）: f_cross_max を f_ctrl/2 以上にすると、最良の候補を"
    "探索の上端（0.9999·ナイキスト）に置き、その交差周波数が確かめの範囲の端にあって見つからないので、"
    "位相余裕を inf と報告し「目標の 55° は満たす」と書く。その候補の閉ループの極の半径は 18.6（不安定）"))
def test_initial_gains_best_candidate_is_never_reported_as_meeting_the_target_when_unstable():
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(VIN, 47e-6, 10e-6, 1000.0, 20e3, f_cross_max=1e9)
    best = info.value.best
    assert my_radius(best, VIN, 47e-6, 10e-6, 1000.0, 20e3) > 1.0
    assert math.isfinite(best.phase_margin_deg) and best.phase_margin_deg < 55.0
    assert "足りない" in str(info.value)


# ------------------------------------------------------------------ 版の推定、精度の注意、stats、文言

def _control_bare():
    return {"f_sw": 100e3, "f_ctrl": 100e3, "Vref": 12, "Kp": ref.KP, "Ki": ref.KI, "Kd": ref.KD,
            "Tf": ref.TF, "d_min": 0, "d_max": 0.9}


def test_missing_version_is_inferred_from_the_control_values():
    d = ref.spec_dict("average", 1e-3)
    d.pop("schema_version")
    s = Spec.model_validate(d)
    assert (s.schema_version, s.version_inferred) == ("0.2", True)
    assert not any("版" in n for n in expand(s).notes)
    d1 = copy.deepcopy(d)
    d1["control"] = _control_bare()
    s1 = Spec.model_validate(d1)
    assert (s1.schema_version, s1.version_inferred) == ("0.1", True)
    assert any("版（schema_version）が無く" in n and "記載なし" in n for n in expand(s1).notes)
    assert np.array_equal(expand(s1).run().x, expand(s).run().x)
    d2 = copy.deepcopy(d)
    d2["control"]["Kp"] = ref.KP                                    # 1つだけ裸の数値
    s2 = Spec.model_validate(d2)
    assert s2.schema_version == "0.1"
    assert any("Kp [1/V]" in n and "Ki" not in n for n in expand(s2).notes if "版" in n)
    d3 = copy.deepcopy(d1)
    d3["schema_version"] = "0.1"
    s3 = Spec.model_validate(d3)
    assert not s3.version_inferred and any("仕様の版 0.1 の形なので" in n for n in expand(s3).notes)


def test_fidelity_is_noted_when_the_template_cannot_choose():
    d = json.loads((ref.ROOT / "examples" / "rlc_series.json").read_text(encoding="utf-8"))
    d.setdefault("simulation", {}).pop("fidelity", None)
    assert not any("fidelity" in n for n in expand(Spec.model_validate(d)).notes)
    d["simulation"]["fidelity"] = "average"
    notes = expand(Spec.model_validate(d)).notes
    assert any("fidelity" in n and "average" in n and "使わなかった" in n for n in notes)


def test_stats_separate_command_changes_from_state_changes():
    """平均モデル: 回路状態の変化は負荷の段差の1回だけ。指令の変化の数は、cmd(duty) を周期の頭で見て
    値が変わった数（自分で数える）。スイッチングモデル: 指令で決まる電源は無いので 0、回路状態の変化は
    0 < d < 1 の周期ごとに2回（最初の周期は d_min = 0 でオンが無い）。"""
    for fidelity in ("average", "switching"):
        res = ref.build(fidelity, plant={"R_load_step": (3, "ohm"), "t_load_step": (5, "ms")}).run()
        n = round(T / res.h)
        heads = res["cmd(duty)"][: len(res.t) - 1: n]
        if fidelity == "average":
            assert res.stats["state_changes"] == 1
            assert res.stats["command_changes"] == int(np.count_nonzero(heads[1:] != heads[:-1]))
        else:
            assert res.stats["command_changes"] == 0
            inner = int(np.count_nonzero((heads > 0) & (heads < 1)))
            assert res.stats["state_changes"] == 2 * inner + 1          # ＋負荷の段差


@pytest.mark.parametrize("r_load,r_step,small", [(6.0, 0.0009, "R_load_step"), (0.0009, 6.0, "R_load")])
def test_load_step_error_names_the_value_that_is_too_small(r_load, r_step, small):
    d = ref.spec_dict("average", 1e-3, plant={"R_load": (r_load, "ohm"), "R_load_step": (r_step, "ohm"),
                                              "t_load_step": (0.5, "ms")})
    with pytest.raises(ValueError, match=rf"^{small}（0.0009 Ω）が小さすぎて"):
        expand(Spec.model_validate(d))
