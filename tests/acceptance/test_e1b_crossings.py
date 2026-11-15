"""E1b の3回目の修正（9260308）の受け入れ試験。検証担当が追加した。
記録は docs/verification/E1b-verifier-3.md。

期待値は、このファイルの参照で作る。`circuit_sim` の関数（tuning の内部、loop_response など）を
期待値の計算に使わない。参照の開ループと閉ループは、実装の「因子の形と囲い込み」とも、実装担当の試験の
「cont2discrete と格子」とも別の経路:
  植物: 状態 [iL, vC] を拡大行列の expm で零次ホールドに離散化する。Δ = Ad − I と bd は φ1(X) = (e^X − I)/X
        から求め（z = 1 の近くの打ち消しを避ける）、P(z) = c·(zI − Ad)⁻¹·bd は連立方程式で解く。
  閉ループ: 状態 [iL, vC, u_{k−1}, I_{k−1}, D_{k−1}, e_{k−1}] の 6×6 の行列の固有値。
  交差（|L| = 1）: 2つの経路の和。
    (a) 代数: z = 1 + w と置き、単位円の上で s = |w|² = 4 sin²(θ/2)、w + w̄ = −s を使うと
        |N|² − |D|² が s の多項式になる。その (0, 4) の実根の近く ±0.1% を 4001 点で調べ、log|L| の符号が
        変わる点をすべて二分法で詰める（代数の根が粗くても、近い2つの交差を両方拾う）。
    (b) 格子: 等比の 2 万点を、隣り合う点の主値の差が 3° を超える区間で二分して細かくし、log|L| の符号の変わる
        点を二分法で詰める。
  位相余裕: 交差ごとに、連続の位相（(b) の格子の np.unwrap に、交差の点の主値を 2π の整数倍で合わせる）と、
        −1 からの角度（180° − |主値|）の両方。
製品側の不具合（軽微）で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import copy
import json
import math
import typing
from pathlib import Path

import numpy as np
import pytest
from numpy.polynomial import polynomial as npoly
from pydantic import ValidationError
from scipy.linalg import expm
from scipy.optimize import brentq

import circuit_sim
from circuit_sim import Quantity, Spec, expand
from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

ROOT = Path(__file__).resolve().parents[2]

# 許容差（測る前に決めた根拠）
# 報告の余裕と参照の差 0.01°: 実装も参照も交差を 1e-12 程度まで詰めるので、位相の差は 1e-8° 程度。0.01° は
#   その 1e6 倍で十分に緩く、近い2つの交差の片方を落としたとき（再現例で 70.6° と 40.5°）は必ず見分ける。
# 返したゲインの余裕: 目標 − 1e-6°（実装の TARGET_TOL_DEG と同じ。二分法で目標に合わせる丸めの分）に、
#   参照の丸め 1e-6° を足して 目標 − 2e-6°。
# 極の半径: 参照の固有値と相対 1e-9（同じ行列の別の組み方。丸めは 1e-13 程度）。
PM_TOL = 0.01
TARGET_TOL = 2e-6


# ------------------------------------------------------------------ 参照

def _delta_plant(v_in, ind, cap, r, ts):
    a = np.array([[0.0, -1.0 / ind], [1.0 / cap, -1.0 / (r * cap)]])
    b = np.array([v_in / ind, 0.0])
    m = np.zeros((4, 4))
    m[:2, :2] = a * ts
    m[:2, 2:] = np.eye(2)
    phi1 = expm(m)[:2, 2:]
    return (a * ts) @ phi1, phi1 @ (b * ts)


class Ref:
    """参照の開ループ L(e^{jωTs}) = C(z)·P(z)·z⁻¹ と閉ループ。"""

    def __init__(self, g, v_in, ind, cap, r, f_ctrl):
        self.kp, self.ki, self.kd, self.tf = g.kp, g.ki, g.kd, g.t_filter
        self.ts = 1.0 / f_ctrl
        self.w0 = 1.0 / math.sqrt(ind * cap)
        self.delta, self.bd = _delta_plant(v_in, ind, cap, r, self.ts)
        self.ad = np.eye(2) + self.delta

    def __call__(self, w):
        # z − 1 = expm1(jωTs) と Δ で組む（z ≈ 1 で I + Δ を作ると Δ の桁が落ちる）
        theta = np.atleast_1d(np.asarray(w, dtype=float)) * self.ts
        z, zm1 = np.exp(1j * theta), np.expm1(1j * theta)
        ms = zm1[:, None, None] * np.eye(2)[None] - self.delta[None]
        rhs = np.broadcast_to(self.bd.astype(complex), (len(z), 2))[..., None]
        p = np.linalg.solve(ms, rhs)[..., 0][:, 1]
        cz = (self.kp + self.ki * self.ts * z / zm1
              + self.kd * zm1 / ((self.tf + self.ts) * zm1 + self.ts))
        return cz * p / z

    def radius(self):
        ts, a, b = self.ts, self.tf / (self.tf + self.ts), self.kd / (self.tf + self.ts)
        m = np.zeros((6, 6))
        m[0:2, 0:2], m[0:2, 2] = self.ad, self.bd
        e = np.zeros(6)
        e[1] = -1.0
        i_k = e * self.ki * ts
        i_k[3] += 1.0
        d_k = b * e
        d_k[4] += a
        d_k[5] -= b
        m[2], m[3], m[4], m[5] = self.kp * e + i_k + d_k, i_k, d_k, e
        return float(np.max(np.abs(np.linalg.eigvals(m))))

    def _phase_grid(self, w_lo, w_hi, n=20000):
        w = np.geomspace(w_lo, w_hi, n)
        val = self(w)
        for _ in range(40):
            bad = np.nonzero(np.abs(np.angle(val[1:] / val[:-1])) > math.radians(3.0))[0]
            if bad.size == 0:
                break
            mid = np.sqrt(w[bad] * w[bad + 1])
            w, val = np.insert(w, bad + 1, mid), np.insert(val, bad + 1, self(mid))
        return w, val, np.unwrap(np.angle(val))

    def _algebraic_thetas(self):
        d, b = self.delta, self.bd
        n_p = np.array([d[1, 0] * b[0] - d[0, 0] * b[1], b[1]])
        d_p = np.array([d[0, 0] * d[1, 1] - d[0, 1] * d[1, 0], -(d[0, 0] + d[1, 1]), 1.0])
        filt = np.array([self.ts, self.tf + self.ts])
        n_c = npoly.polyadd(npoly.polyadd(self.kp * npoly.polymul([0.0, 1.0], filt),
                                          self.ki * self.ts * npoly.polymul([1.0, 1.0], filt)),
                            self.kd * np.array([0.0, 0.0, 1.0]))

        def abs2(a):
            p = [np.array([2.0]), np.array([0.0, -1.0])]          # w^d + w̄^d を s の多項式で
            for _ in range(2, len(a)):
                p.append(npoly.polyadd(npoly.polymulx(-p[-1]), npoly.polymulx(-p[-2])))
            out = np.zeros(1)
            for i in range(len(a)):
                for k in range(i, len(a)):
                    si = np.zeros(i + 1)
                    si[i] = 1.0
                    out = npoly.polyadd(out, a[i] * a[k] * npoly.polymul(si, p[k - i] if k > i else [1.0]))
            return out

        q = npoly.polysub(abs2(npoly.polymul(n_c, n_p)),
                          abs2(npoly.polymul(npoly.polymul([0.0, 1.0], filt), d_p)))
        q = q[: np.nonzero(q)[0][-1] + 1]
        return [2.0 * math.asin(math.sqrt(r.real) / 2.0) for r in npoly.polyroots(q)
                if abs(r.imag) <= 1e-7 * abs(r) and 0.0 < r.real < 4.0]

    def margins(self):
        """[(交差 [Hz], 連続の位相の余裕 [deg], −1 からの角度 [deg])]。"""
        ts = self.ts

        def h_theta(t):
            return math.log(abs(self(t / ts)[0]))

        def root(f, a, b, **tol):
            # 格子の値（まとめて計算）と1点の値は丸めが違う。1点で詰めて符号がそろうときは、|log|L|| が丸めの
            # 大きさ（1e-9 未満）の端を根とし、そうでなければ数えない（丸めだけの符号の変わり目）
            fa, fb = f(a), f(b)
            if fa * fb <= 0:
                return a if fa == 0.0 else b if fb == 0.0 else brentq(f, a, b, **tol)
            return (a if abs(fa) < abs(fb) else b) if min(abs(fa), abs(fb)) < 1e-9 else None

        found = []
        for t in self._algebraic_thetas():
            tt = np.linspace(t * (1 - 1e-3), min(t * (1 + 1e-3), math.pi * (1 - 1e-12)), 4001)
            hv = np.log(np.abs(self(tt / ts)))
            for i in np.nonzero((hv[:-1] > 0) != (hv[1:] > 0))[0]:
                t_root = root(h_theta, tt[i], tt[i + 1], xtol=1e-17, rtol=1e-15)
                if t_root is not None:
                    found.append(t_root / ts)
        w_lo = 1e-4 * min([self.w0, *found])
        w, val, ph = self._phase_grid(w_lo, 0.9999 * math.pi / ts)
        s = np.log(np.abs(val))
        for i in np.nonzero((s[:-1] > 0) != (s[1:] > 0))[0]:
            lw_root = root(lambda lw: math.log(abs(self(math.exp(lw))[0])),
                           math.log(w[i]), math.log(w[i + 1]), xtol=1e-14)
            if lw_root is not None:
                found.append(math.exp(lw_root))
        unique = []
        for x in sorted(found):
            if not unique or x / unique[-1] - 1 > 1e-9:
                unique.append(x)
        out = []
        for wc in unique:
            v = self(wc)[0]
            p = math.atan2(v.imag, v.real)
            j = int(np.clip(np.searchsorted(w, wc), 1, len(w) - 1))
            near = ph[j - 1] if abs(math.log(w[j - 1] / wc)) < abs(math.log(w[j] / wc)) else ph[j]
            p += 2 * math.pi * round((near - p) / (2 * math.pi))
            geometric = 180.0 - abs(math.degrees(math.atan2(v.imag, v.real)))
            out.append((wc / (2 * math.pi), 180.0 + math.degrees(p), geometric))
        return out

    def min_margin(self):
        ms = self.margins()
        return min(min(m[1], m[2]) for m in ms) if ms else math.inf


def _design(plant, f_ctrl, **opts):
    try:
        return buck_voltage_mode_pid(*plant, f_ctrl, **opts), None
    except GainDesignError as err:
        return err.best, err


# ------------------------------------------------------------------ 1. 近い交差

REVIEW = ((24.0, 470e-6, 10e-6, 6.0), 15e3, {"zero_damping": 0.1, "filter_ratio": 0.0})


def test_review_case_reference_finds_the_three_crossings():
    """レビューの再現例の最良の候補: 参照が 595.6 / 2437.58 / 2437.79 Hz の3つの交差を見つける
    （この試験の参照が近い2つを拾えることの確かめ。前回の検証担当の格子の経路は、595.6 Hz しか
    拾えなかった）。"""
    best, err = _design(*REVIEW[:2], **REVIEW[2])
    ms = Ref(best, *REVIEW[0], REVIEW[1]).margins()
    assert [round(m[0], 2) for m in ms] == [595.64, 2437.59, 2437.79]
    assert [round(m[1], 1) for m in ms] == [70.6, 40.5, 40.5]


def test_review_case_best_candidate_is_40_5_and_not_enough():
    """再現例: 理由 margin_not_reached、最良の候補の余裕は参照の最小 40.46°（0.01° 以内）、「足りない」と書き
    「満たす」と書かない。本文の「最大 40.5°」とも合う。"""
    best, err = _design(*REVIEW[:2], **REVIEW[2])
    assert err is not None and err.reason == "margin_not_reached"
    ref = Ref(best, *REVIEW[0], REVIEW[1])
    assert best.phase_margin_deg == pytest.approx(ref.min_margin(), abs=PM_TOL)
    assert best.phase_margin_deg == pytest.approx(40.457, abs=0.01)
    assert best.pole_radius == pytest.approx(ref.radius(), rel=1e-9) and best.pole_radius < 1.0
    text = str(err)
    assert "位相余裕 40.5°（目標まで 14.5° 足りない）" in text and "満たす" not in text
    assert "最大 40.5°" in text


# 掃引（docs/verification/E1b-verifier-3.md）で見つけた例。
# (v_in, L, C, R, f_ctrl, opts, 期待の結果, 近い交差の相対の間隔の上限 または None)
NEAR_CASES = [
    # 近い2つの交差（相対の間隔 1e-4 未満）。共振の上の探索の下端の近く（失敗）と、PID の零点の谷（返す）
    (24.0, 470e-6, 10e-6, 6.0, 15e3, {"zero_damping": 0.1, "filter_ratio": 0.0}, "margin_not_reached", 1e-4),
    (24.0, 470e-6, 10e-6, 4.308869380063768, 15e3,
     {"zero_damping": 0.18461538461538463, "filter_ratio": 0.0}, "margin_not_reached", 1e-5),
    (24.0, 470e-6, 10e-6, 6.140581259515699, 15e3,
     {"zero_damping": 0.09230769230769231, "filter_ratio": 0.0}, "margin_not_reached", 2e-5),
    (24.0, 470e-6, 10e-6, 6.0, 14996.976835931895,
     {"zero_damping": 0.1, "filter_ratio": 0.0}, "margin_not_reached", 3e-5),
    (24.0, 22e-6, 100e-6, 0.5, 100e3,
     {"zero_ratio": 0.3, "zero_damping": 0.528653137671, "filter_ratio": 0.0, "phase_margin_deg": 30.0},
     "ok", 3e-5),
    (24.0, 100e-6, 1e-3, 60.0, 20e3,
     {"zero_ratio": 0.4, "zero_damping": 0.455712954253, "phase_margin_deg": 30.0}, "ok", 6e-5),
    # LC の共振が制御周波数の 5000 倍以上低い（交差が z = 1 の近くに集まる。参照の代数の根も粗くなる）
    (125.19125615712926, 0.0005216888377446962, 8.060795014029424e-05, 0.3462880804383837, 4114038.143910967,
     {"zero_damping": 0.015353809544988523, "filter_ratio": 0.0, "phase_margin_deg": 66.6871109337676},
     "margin_below_target", None),
    (241.2950426271385, 0.0007230489267792082, 7.855659326761026e-07, 1.3736479052158856, 50819928.07217801,
     {"zero_damping": 0.01957184134947072, "filter_ratio": 0.0, "zero_ratio": 0.4809781017824939},
     "margin_below_target", None),
    (22.007277003401143, 0.001503230030016501, 0.00012696686583186227, 0.27073003399699397,
     1937904.3556003633,
     {"zero_damping": 0.016130511792223415, "filter_ratio": 0.0}, "margin_below_target", None),
    # 零点の比が極端に小さい（積分がとても遅い）
    (24.0, 4.7e-06, 0.001, 1000.0, 1e6, {"zero_ratio": 1e-4}, "ok", None),
    (5.0, 1e-3, 1e-3, 0.1, 1e3, {"zero_ratio": 1e-4}, "margin_not_reached", None),
]


@pytest.mark.parametrize("case", NEAR_CASES, ids=lambda c: f"{c[4]:.6g}Hz-{c[6]}")
def test_near_crossings_returned_designs_meet_target_and_best_is_true(case):
    """近い交差などの例: 返したゲインは参照で安定かつ余裕が目標以上。失敗のときは、最良の候補の余裕と
    極の半径が参照と合い（落とした交差があれば余裕を大きく言う）、「満たす」と書くのは参照の余裕が
    目標以上のときだけ。
    近い交差の例では、参照が本当に近い2つを見つけていることも確かめる（試験が角を突いていることの確かめ）。"""
    *plant, f_ctrl, opts, expected, gap = case
    best, err = _design(tuple(plant), f_ctrl, **opts)
    target = opts.get("phase_margin_deg", 55.0)
    assert (err.reason if err else "ok") == expected
    ref = Ref(best, *plant, f_ctrl)
    ms = ref.margins()
    if gap is not None:
        assert min(ms[i + 1][0] / ms[i][0] - 1 for i in range(len(ms) - 1)) < gap
    pm, radius = ref.min_margin(), ref.radius()
    assert best.phase_margin_deg == pytest.approx(pm, abs=PM_TOL)
    assert best.pole_radius == pytest.approx(radius, rel=1e-9)
    if err is None:
        assert pm >= target - TARGET_TOL and radius < 1.0
    else:
        assert ("は満たす）" in str(err)) == (pm >= target)


def _sweep_cases(seed, n):
    """近い交差が出やすい条件の乱数（零点の減衰 0.01〜0.15、f_ctrl/f0 = 2.5〜3e4、filter_ratio 0 が半分）。"""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        ind, cap = 10 ** rng.uniform(-6.5, -2.5), 10 ** rng.uniform(-6.5, -2.5)
        f0 = 1 / (2 * math.pi * math.sqrt(ind * cap))
        f_ctrl, r = f0 * 10 ** rng.uniform(0.4, 4.5), 10 ** rng.uniform(-1, 3.5)
        opts = {"zero_damping": float(10 ** rng.uniform(-2, math.log10(0.15)))}
        if rng.random() < 0.5:
            opts["filter_ratio"] = 0.0
        if rng.random() < 0.6:
            opts["phase_margin_deg"] = float(rng.uniform(20, 85))
        out.append(((float(10 ** rng.uniform(0, 2.5)), ind, cap, r), f_ctrl, opts))
    return out


@pytest.mark.parametrize("case", _sweep_cases(20261116, 120), ids=lambda c: f"{c[1]:.4g}Hz")
def test_sweep_returned_gains_are_stable_with_margin_and_failures_tell_the_truth(case):
    """乱数 120 通り（記録の掃引の一部と同じ作り方）: 返したゲインは参照の閉ループで安定、すべての交差の余裕
    （連続の位相と −1 からの角度の両方）が目標以上。失敗の最良の候補の余裕は参照の最小と 0.01° 以内。"""
    plant, f_ctrl, opts = case
    best, err = _design(plant, f_ctrl, **opts)
    target = opts.get("phase_margin_deg", 55.0)
    if best is None:
        assert err.reason == "margin_not_reached"
        return
    ref = Ref(best, *plant, f_ctrl)
    pm = ref.min_margin()
    assert best.phase_margin_deg == pytest.approx(pm, abs=PM_TOL)
    if err is None:
        assert pm >= target - TARGET_TOL and ref.radius() < 1.0 and best.kp >= 0 and best.kd >= 0


UNSTABLE_CASE = ((2.967370111576123, 7.338144645114917e-05, 2.439793958973858e-06, 0.21317375460902435),
                 123266.3864601657)


def test_unstable_reason_is_reachable_and_true():
    """余裕は目標を満たすが閉ループが不安定、という例（掃引で見つけた）: 理由 unstable、
    参照の極の半径 1.0187、参照の余裕は目標（24.36°）。前回は到達できなかった確かめが、ここで効く。"""
    plant, f_ctrl = UNSTABLE_CASE
    opts = {"zero_damping": 0.1437543236478271, "filter_ratio": 0.0, "phase_margin_deg": 24.361861916713153,
            "zero_ratio": 0.9590984284499318}
    best, err = _design(plant, f_ctrl, **opts)
    assert err is not None and err.reason == "unstable"
    ref = Ref(best, *plant, f_ctrl)
    assert ref.radius() == pytest.approx(1.0187, abs=1e-4)
    assert best.pole_radius == pytest.approx(ref.radius(), rel=1e-9)
    assert ref.min_margin() == pytest.approx(opts["phase_margin_deg"], abs=1e-6)
    assert "1 以上で不安定" in str(err)


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合（軽微。文言）: 設計した点の余裕は二分法で目標に合わせるので、丸めで目標を 1e-11° ほど下回る"
    "ことがある。そのとき最良の候補を「目標まで 0.0° 足りない」と書き、理由 unstable では本文の「位相余裕は"
    "目標を満たすが」と食い違う。受け入れの判定（目標 − 1e-6°）と文言の判定（目標ちょうど）の許容差がそろって"
    "いない"))
def test_candidate_at_the_target_within_rounding_is_not_called_short():
    plant, f_ctrl = UNSTABLE_CASE
    opts = {"zero_damping": 0.1437543236478271, "filter_ratio": 0.0, "phase_margin_deg": 24.361861916713153,
            "zero_ratio": 0.9590984284499318}
    _, err = _design(plant, f_ctrl, **opts)
    assert "目標まで 0.0° 足りない" not in str(err)


# ------------------------------------------------------------------ 2. 設計の引数

BASE = (24.0, 22e-6, 100e-6, 6.0)
ARGS = ("v_in", "inductance", "capacitance", "r_load", "f_ctrl")


def _call(**over):
    pos = dict(zip(ARGS, (*BASE, 100e3), strict=True))
    kw = {}
    for k, v in over.items():
        (pos if k in pos else kw)[k] = v
    return buck_voltage_mode_pid(*(pos[a] for a in ARGS), **kw)


@pytest.mark.parametrize("name", [*ARGS, "f_cross_max", "zero_ratio", "zero_damping", "filter_ratio",
                                  "phase_margin_deg"])
@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf, -math.inf])
def test_bad_design_argument_is_refused_with_its_name(name, value):
    """負、NaN、±inf はどの引数でも計算せずに invalid_argument。最良の候補は無く、文に引数の名前と値があり、
    「満たす」と書かない。"""
    with pytest.raises(GainDesignError) as info:
        _call(**{name: value})
    err = info.value
    assert err.reason == "invalid_argument" and err.best is None
    assert name in str(err) and "満たす" not in str(err)


@pytest.mark.parametrize("name", ["v_in", "f_ctrl", "zero_ratio", "phase_margin_deg"])
@pytest.mark.parametrize("value", [True, "24", None])
def test_bool_text_and_none_arguments_are_refused(name, value):
    """真偽値（1 として計算しない）、文字列、None も invalid_argument。"""
    with pytest.raises(GainDesignError) as info:
        _call(**{name: value})
    assert info.value.reason == "invalid_argument" and name in str(info.value)


@pytest.mark.parametrize("name,value,ok", [
    ("zero_ratio", 1.0, False), ("zero_ratio", 0.0, False), ("zero_ratio", 0.999, True),
    ("zero_damping", 1.0, False), ("zero_damping", 0.0, False), ("zero_damping", 0.5, True),
    ("filter_ratio", 0.0, True), ("phase_margin_deg", 180.0, False), ("phase_margin_deg", 0.0, False),
    ("phase_margin_deg", 30.0, True), ("f_cross_max", 0.0, False), ("v_in", 0.0, False),
])
def test_design_argument_boundaries(name, value, ok):
    """範囲の端: 開いた区間の端は断り、内側は計算する（断るなら invalid_argument 以外の理由）。"""
    try:
        _call(**{name: value})
        reason = None
    except GainDesignError as err:
        reason = err.reason
    assert (reason != "invalid_argument") == ok


@pytest.mark.xfail(strict=True, reason=(
    "製品の不具合（軽微。前の版からの後退）: 引数の確かめが isinstance(v, int | float) なので、numpy の"
    " np.int64 と np.float32 を、値が正の有限値でも「0 より大きい有限値にする（現在 np.int64(24)）」と断る。"
    "前の版（math.isfinite(v) and v > 0）は通していた"))
@pytest.mark.parametrize("value", [np.int64(24), np.float32(24.0)])
def test_numpy_scalar_arguments_are_accepted(value):
    g = _call(v_in=value)
    assert g.phase_margin_deg == pytest.approx(_call(v_in=24.0).phase_margin_deg, abs=1e-6)


def test_f_cross_max_above_nyquist_is_limited_to_the_same_design():
    """ナイキストより上の f_cross_max は 0.9999·ナイキストに制限する: 返すゲインは制限の値を
    指定したときと同じで、    交差は利用者の上限の下にある（上限の約束は破らない）。"""
    limited = _call(f_cross_max=0.9999 * 50e3)
    for big in (5e4, 1e6, 1e300):
        g = _call(f_cross_max=big)
        assert g == limited and g.f_cross < big


def test_f_cross_max_above_nyquist_failure_shows_the_limited_range():
    """失敗の文には制限した後の上端（9999 Hz）が出る（指定した 1e9 Hz ではない）。最良の候補は不安定と書き、
    「満たす」と書かない（前回の軽微1が直った）。"""
    best, err = _design((24.0, 47e-6, 10e-6, 1000.0), 20e3, f_cross_max=1e9)
    assert err.reason == "margin_not_reached"
    text = str(err)
    assert "〜9999 Hz" in text and "1e+09" not in text
    assert "1 以上で不安定" in text and "満たす" not in text
    ref = Ref(best, 24.0, 47e-6, 10e-6, 1000.0, 20e3)
    assert ref.radius() == pytest.approx(18.591, abs=1e-3)
    assert best.phase_margin_deg == pytest.approx(ref.margins()[0][1], abs=PM_TOL)
    assert best.phase_margin_deg < 0


# ------------------------------------------------------------------ 3. 版の推定（Quantity のオブジェクト）

def _buck_dict():
    d = json.loads((ROOT / "examples" / "buck_voltage_mode.json").read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": "average", "t_end": 5e-4}
    return d


def _signature(spec):
    ex = expand(spec)
    res = ex.run()
    waves = [np.asarray(res.t).tobytes()] + [np.asarray(res[n]).tobytes() for n in ex.probes]
    return waves, ex.notes + res.notes


def _as_objects(d):
    d = copy.deepcopy(d)
    d.pop("schema_version", None)
    d["plant"] = {k: Quantity(**v) for k, v in d["plant"].items()}
    d["control"] = {k: Quantity(**v) for k, v in d["control"].items()}
    return d


def test_quantity_objects_without_version_read_as_02_and_match_the_file():
    """Python の Quantity のオブジェクトで版の欄なしに作った仕様は、版 0.2（推定）で読め、版 0.2 のファイルと
    結果がビット一致し、版 0.1 の注意が出ない。Spec(**d) でも model_validate でも同じ。"""
    ref_sig, ref_notes = _signature(Spec.model_validate(_buck_dict()))
    for make in (lambda d: Spec(**d), Spec.model_validate):
        spec = make(_as_objects(_buck_dict()))
        assert (spec.schema_version, spec.version_inferred, spec.legacy_control()) == ("0.2", True, [])
        sig, notes = _signature(spec)
        assert sig == ref_sig and notes == ref_notes


def test_quantity_objects_mixed_with_a_bare_number_read_as_01_naming_only_the_bare_item():
    """Quantity のオブジェクトに裸の数値（Kp）が1つ混ざると版 0.1 と推定し、注意に挙げるのはその項目だけ。
    数値は SI とみなすので、結果は版 0.2 のファイルと一致する。明示の 0.1 にオブジェクトを入れても読める。"""
    ref_sig, _ = _signature(Spec.model_validate(_buck_dict()))
    d = _as_objects(_buck_dict())
    d["control"]["Kp"] = 0.0117
    spec = Spec(**d)
    assert (spec.schema_version, spec.version_inferred, spec.legacy_control()) == ("0.1", True, ["Kp"])
    sig, notes = _signature(spec)
    assert sig == ref_sig
    assert [n for n in notes if "版" in n and "Kp" in n and "Ki" not in n]
    explicit = _as_objects(_buck_dict())
    explicit["schema_version"] = "0.1"
    assert _signature(Spec(**explicit))[0] == ref_sig


@pytest.mark.parametrize("bad", ["12 V", [1, 2], True])
def test_control_value_that_is_neither_number_nor_quantity_is_refused(bad):
    d = _as_objects(_buck_dict())
    d["control"]["Kp"] = bad
    with pytest.raises((ValidationError, ValueError)):
        Spec(**d)


# ------------------------------------------------------------------ 4. 読める版の一覧

def test_supported_versions_are_exactly_the_readable_ones_from_dict_and_json():
    """SUPPORTED_SCHEMA_VERSIONS と、辞書からも JSON からも実際に読める版が一致する（文字列の版 0.0〜2.9、
    書き方の揺れ、数値、null）。書き出す版はその最新。"""
    supported = circuit_sim.SUPPORTED_SCHEMA_VERSIONS
    assert set(typing.get_args(Spec.model_fields["schema_version"].annotation)) == set(supported)
    assert circuit_sim.SCHEMA_VERSION == supported[-1]
    candidates = [f"{a}.{b}" for a in range(3) for b in range(10)]
    candidates += ["0.10", "0.2.0", " 0.2", "v0.2", "", 0.1, 0.2, None]
    readable_dict, readable_json = [], []
    for v in candidates:
        d = _buck_dict()
        d["schema_version"] = v
        if v == "0.1":
            d["control"] = {k: q["value"] for k, q in d["control"].items()}
        readers = ((Spec.model_validate, readable_dict),
                   (lambda x: Spec.model_validate_json(json.dumps(x)), readable_json))
        for reader, sink in readers:
            try:
                reader(d)
                sink.append(v)
            except (ValidationError, ValueError):
                pass
    assert tuple(readable_dict) == tuple(readable_json) == tuple(supported)
    out = Spec.model_validate(_buck_dict()).model_dump()
    assert out["schema_version"] == circuit_sim.SCHEMA_VERSION
