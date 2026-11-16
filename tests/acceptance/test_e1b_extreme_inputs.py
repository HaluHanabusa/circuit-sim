"""E1b の5回目の修正（20c561d）の受け入れ試験。検証担当が追加した。
記録は docs/verification/E1b-verifier-5.md。

確かめること: とても大きい負荷抵抗（R = 1e13〜1e30 Ω、Q ≧ 1e16）、引数の範囲（D-059）、範囲の端どうしの
組み合わせ、半径と最大の余裕の書き方、探索の「満たす」の判定（D-057）。

期待値の作り方（`circuit_sim` の関数を期待値の計算に使わない）:
  - 150 桁の参照の値（このファイルに数字で書いてある。REF_*）。検証担当が作業ツリーの外で、mpmath で求めた:
    植物は 3×3 の拡大行列の指数関数、開ループは L(z) = C(z)·P(z)·z⁻¹ の多項式、交差は |L|² − 1 を
    s = 4 sin²(θ/2) の多項式にした実根、連続の位相は根ごとの偏角の和（単位円の内外は 150 桁の |r| で決める）、
    閉ループは特性多項式の根。格子を使わないので、共振の幅（1/Q）に頼らない。前回までの参照
    （test_e1b_crossings.py の Ref）とは、Q が中くらいの 118 回路で余裕 8e-12°、半径 7e-15 まで一致した。
  - 負荷抵抗の極限: 同じ回路で Q = 1e10 にした R と、理由と余裕が一致するはず（前回と同じ考え）。
  - 閉ループの極の半径: ループは Vin·Kp、Vin·Ki、Vin·Kd だけで決まる（植物は Vin に比例）ので、Vin = 1、
    ゲイン × Vin で 6×6 の状態遷移行列を組み、固有値を求める（_radius_minus_one）。Vin が大きくても行列の
    尺度がそろうので、丸めが増えない。150 桁の値との差は、下の例で 1e-11 より小さい（試験の中で確かめる）。
  - −1 からの角度: Ref（test_e1b_crossings.py）の交差と開ループの値。Ref の連続の位相は、Q が 1e13 を
    超えると格子が共振を分けられないので使わない。
  - 引数の範囲: 決定記録 D-059 の文から書き写した（実装の ARGUMENT_RANGES を読まない）。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。

許容差（測る前に決めた根拠）:
  - 返したゲインの余裕は 目標 − 2e-6° 以上、「満たす」は 目標 − 1e-6° 以上（D-057。前回までと同じ）。
  - 安定と言える半径は 1 − 1e-9 以下（D-057）。参照の丸め（1e-11 より小さい）はその 1/100。
  - 150 桁の値との一致: 余裕 1e-6°（D-057 の「満たす」の許しと同じ。360° の取り違えや別の候補は
    数度以上違う）、半径は相対 1e-9。
  - 極限の一致: 余裕 1e-3°（前回と同じ。Q = 1e10 と 1e30 の違いが余裕に与える差は 1e-8° 程度）。
"""
from __future__ import annotations

import importlib.util
import itertools
import math
import re
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("e1b_crossings_reference_5", _HERE / "test_e1b_crossings.py")
crossings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crossings)
Ref = crossings.Ref

TARGET_TOL = 2e-6         # 返したゲインの余裕（前回までと同じ）
MEETS_TOL = 1e-6          # D-057
STABILITY_EPS = 1e-9      # D-057
REF_PM_TOL = 1e-6
LIMIT_PM_TOL = 1e-3


def _run(plant, f_ctrl, **opts):
    """（結果の名前、ゲインか最良の候補、文）。想定外の例外はそのまま上げる。"""
    try:
        return "ok", buck_voltage_mode_pid(*plant, f_ctrl, **opts), ""
    except GainDesignError as e:
        return e.reason, e.best, str(e)


def _radius_minus_one(g, v_in, ind, cap, r, f_ctrl):
    """閉ループの極の半径 − 1（Vin = 1、ゲイン × Vin で組む。状態 [iL, vC, u, I, D, e]）。"""
    ts = 1.0 / f_ctrl
    m3 = np.zeros((3, 3))
    m3[0, 1], m3[1, 0], m3[1, 1], m3[0, 2] = -ts / ind, ts / cap, -ts / (r * cap), ts / ind
    e3 = expm(m3)
    kp, ki, kd, tf = g.kp * v_in, g.ki * v_in, g.kd * v_in, g.t_filter
    a, b = tf / (tf + ts), kd / (tf + ts)
    m = np.zeros((6, 6))
    m[0:2, 0:2], m[0:2, 2] = e3[:2, :2], e3[:2, 2]
    e = np.zeros(6)
    e[1] = -1.0
    i_k = e * ki * ts
    i_k[3] += 1.0
    d_k = b * e
    d_k[4] += a
    d_k[5] -= b
    m[2], m[3], m[4], m[5] = kp * e + i_k + d_k, i_k, d_k, e
    return float(np.max(np.abs(np.linalg.eigvals(m)))) - 1.0


def _q10_load(ind, cap):
    return 1e10 * math.sqrt(ind / cap)


# ---------------------------------------------------------------------------------------------------
# 1. 前回の再現例（Q ≳ 1e16 で位相が 360° ずれた）と、とても大きい負荷抵抗
# ---------------------------------------------------------------------------------------------------

# （回路、制御周波数、設計の引数、結果、150 桁の参照の余裕 [deg]、150 桁の参照の半径 − 1）
REF_HUGE = [
    ((24.0, 47e-6, 100e-6, 1e16), 40e3, {}, "margin_not_reached", 17.2694421346537, -0.005606776774124685),
    ((24.0, 22e-6, 100e-6, 1e17), 100e3, {}, "margin_not_reached", 38.76673016165175, -0.009516037573492455),
    ((12.0, 4.7e-6, 22e-6, 1e17), 1e6, {}, "ok", 55.00000000000005, -0.04275688343581113),
    ((5.134556171699287, 8.288242166937922e-05, 0.00030274709414563895, 3.538544577047874e+29),
     64466.0414610018, {"zero_damping": 1.5510884791046564, "filter_ratio": 0.05,
                        "phase_margin_deg": 21.359689945298047},
     "ok", 21.359689945298058, -0.014475961895598957),
    ((213.13738685199493, 8.557763028926666e-07, 0.0001414005175080265, 4.072523145044717e+25),
     4876347.847420689, {}, "ok", 55.000000000000036, -0.003393501760492423),
    ((118.77285219513752, 9.108250135539697e-05, 0.0002594146743832767, 1.9308092603651336e+20),
     402628.6468291254, {"zero_ratio": 0.1, "zero_damping": 0.30974244812123297,
                         "phase_margin_deg": 75.97974048555123},
     "ok", 75.9797404855512, -0.0010325047218905446),
    ((24.369329557931184, 7.792444382824586e-07, 2.991830310060783e-05, 1.4883293039864455e+21),
     5365759.098299664, {"zero_ratio": 0.03830450016779974, "zero_damping": 0.7896205162251388,
                         "filter_ratio": 0.5}, "ok", 55.00000000000001, -0.00026553209514223574),
    ((7.4495311910944935, 3.6820426023175313e-06, 2.5341239387675095e-05, 1.9710876419804344e+22),
     12114025.591198115, {"zero_ratio": 1.0, "zero_damping": 0.3, "phase_margin_deg": 48.73741330318678},
     "ok", 48.73741330318679, -0.0026326037327620397),
    # 断るが、最良の候補は目標を満たす（前の版は、こういう文を不安定な候補にも書いた）
    ((17.679343368344735, 8.12991735623112e-06, 7.560069738322174e-05, 6.128476544515511e+20),
     2550672.542445447, {"zero_ratio": 0.048027428146160754, "zero_damping": 0.8105962067034586,
                         "phase_margin_deg": 55.94763738886062, "f_cross_max": 36574.92801285609},
     "crossover_at_search_edge", 77.60602265373275, -0.0001462086456797084),
    ((241.10836156522592, 6.177731637799687e-05, 3.0366358387467418e-06, 1.2780753482163887e+29),
     1160125.557917823, {"zero_ratio": 0.013343389583387587, "f_cross_max": 28816.101105771322},
     "crossover_at_search_edge", 69.71269122897496, -2.3031893859808054e-05),
    ((132.76720390006074, 7.942609509628323e-05, 1.0782314329696321e-05, 1.2261392696951905e+23),
     140093.80903307037, {}, "margin_not_reached", 34.32365588901973, -0.006657317718598075),
    ((4.338323551230594, 6.214316673259165e-05, 8.476188072521547e-06, 4.763718775902219e+26),
     89225.65224805514, {}, "margin_not_reached", 0.18121575703401313, -7.954943101606467e-05),
]


@pytest.mark.parametrize("case", REF_HUGE, ids=lambda c: f"R{c[0][3]:.0e}-{c[3]}")
def test_huge_load_agrees_with_150_digit_reference(case):
    """R = 1e16〜3.5e29 Ω（Q = 1e16〜7e29）: 理由、余裕（連続の位相）、半径が 150 桁の参照と合い、
    文が事実と合う。"""
    plant, f_ctrl, opts, expected, ref_pm, ref_r = case
    target = opts.get("phase_margin_deg", 55.0)
    result, g, msg = _run(plant, f_ctrl, **opts)
    assert result == expected
    assert g.phase_margin_deg == pytest.approx(ref_pm, abs=REF_PM_TOL)
    assert g.pole_radius - 1.0 == pytest.approx(ref_r, rel=1e-9)
    assert _radius_minus_one(g, *plant, f_ctrl) == pytest.approx(ref_r, rel=1e-9)   # このファイルの参照も合う
    if result == "ok":
        assert ref_pm >= target - TARGET_TOL and ref_r <= -STABILITY_EPS
        assert g.kp >= 0.0 and g.kd >= 0.0
    else:
        assert ("は満たす）" in msg) == (ref_pm >= target - MEETS_TOL)
        assert ("足りない）" in msg) == (ref_pm < target - MEETS_TOL)
        assert "（1 未満で安定）" in msg                         # どの例も参照の半径は 1 − 1e-9 以下
        shown = float(re.search(r"閉ループの極の半径 ([\d.]+)（1 未満で安定）", msg).group(1))
        assert shown < 1.0


def test_round4_reproductions_state_the_true_reason_and_margin():
    """前回の再現例: 2つは「取れない」（17.3°、38.8°。足りないと書く）、1つは成功（55°、半径 0.957）。"""
    r1, g1, m1 = _run((24.0, 47e-6, 100e-6, 1e16), 40e3)
    r2, g2, m2 = _run((24.0, 22e-6, 100e-6, 1e17), 100e3)
    assert (r1, r2) == ("margin_not_reached", "margin_not_reached")
    assert "取れるのは最大 17.3°" in m1 and "目標まで 37.7° 足りない" in m1 and "満たす" not in m1
    assert "取れるのは最大 38.8°" in m2 and "目標まで 16.2° 足りない" in m2 and "満たす" not in m2
    g3 = buck_voltage_mode_pid(12.0, 4.7e-6, 22e-6, 1e17, 1e6)
    assert g3.phase_margin_deg == pytest.approx(55.0, abs=TARGET_TOL)
    assert g3.pole_radius == pytest.approx(0.9572431165641889, rel=1e-9)


LIMIT_CIRCUITS = [
    ((24.0, 47e-6, 100e-6), 40e3, {}),
    ((24.0, 22e-6, 100e-6), 100e3, {}),
    ((12.0, 4.7e-6, 22e-6), 1e6, {}),
    ((24.0, 22e-6, 100e-6), 200e3, {"zero_ratio": 1.0, "zero_damping": 1.0}),
    ((48.0, 10e-6, 470e-6), 500e3, {"phase_margin_deg": 45.0, "filter_ratio": 0.0}),
    ((5.0, 1e-6, 10e-6), 2e6, {"zero_ratio": 0.2, "zero_damping": 2.0, "phase_margin_deg": 60.0}),
    ((24.0, 22e-6, 100e-6), 200e3, {"f_cross_max": 6e3, "phase_margin_deg": 50.0}),
]
HUGE_LOADS = [1e13, 1e15, 1e16, 1e17, 1e18, 1e20, 1e24, 1e29, 1e30]


@pytest.mark.parametrize("circuit", LIMIT_CIRCUITS, ids=lambda c: f"L{c[0][1]:g}-f{c[1]:g}-{len(c[2])}")
@pytest.mark.parametrize("r_load", HUGE_LOADS)
def test_huge_load_matches_q1e10_and_returned_gains_are_stable(circuit, r_load):
    """R = 1e13〜1e30 Ω: Q = 1e10 にした同じ回路と、理由、余裕、「満たす」の文が一致する。返したゲインは、
    参照の半径が 1 − 1e-9 以下で、−1 からの角度がすべての交差で目標以上。"""
    (v_in, ind, cap), f_ctrl, opts = circuit
    target = opts.get("phase_margin_deg", 55.0)
    result, g, msg = _run((v_in, ind, cap, r_load), f_ctrl, **opts)
    result10, g10, msg10 = _run((v_in, ind, cap, _q10_load(ind, cap)), f_ctrl, **opts)
    assert result == result10
    assert (g is None) == (g10 is None)
    if g is not None:
        assert g.phase_margin_deg == pytest.approx(g10.phase_margin_deg, abs=LIMIT_PM_TOL)
        assert ("は満たす）" in msg) == ("は満たす）" in msg10)
    if result == "ok":
        assert _radius_minus_one(g, v_in, ind, cap, r_load, f_ctrl) <= -STABILITY_EPS
        geometric = [m[2] for m in Ref(g, v_in, ind, cap, r_load, f_ctrl).margins()]
        assert geometric and min(geometric) >= target - TARGET_TOL


# ---------------------------------------------------------------------------------------------------
# 2. 引数の範囲（D-059）
# ---------------------------------------------------------------------------------------------------

BASE = {"v_in": 24.0, "inductance": 22e-6, "capacitance": 100e-6, "r_load": 6.0, "f_ctrl": 200e3}
# D-059 の文から: 名前 → (下限, 下限を含むか, 上限)。上限はどれも含む
D059 = {
    "v_in": (1e-6, True, 1e9), "inductance": (1e-12, True, 1e4), "capacitance": (1e-15, True, 1e6),
    "r_load": (1e-6, True, 1e30), "f_ctrl": (1e-3, True, 1e12), "zero_ratio": (1e-9, True, 1.0),
    "zero_damping": (0.0, False, 2.0), "filter_ratio": (0.0, True, 1000.0),
}


def _call(name, value):
    """（結果の名前、文）。GainDesignError でない例外は「EXC 型」で返す。"""
    kw, opt = dict(BASE), {}
    if name in BASE:
        kw[name] = value
    else:
        opt[name] = value
    try:
        buck_voltage_mode_pid(kw["v_in"], kw["inductance"], kw["capacitance"], kw["r_load"], kw["f_ctrl"],
                              **opt)
        return "ok", ""
    except GainDesignError as e:
        return e.reason, str(e)
    except Exception as e:  # noqa: BLE001  想定外の例外を試験の失敗として見せる
        return f"EXC {type(e).__name__}", str(e)


def _inside_values():
    out = []
    for name, (lo, lo_closed, hi) in D059.items():
        out += [(name, hi), (name, np.float64(hi)), (name, Fraction(hi))]
        out.append((name, lo if lo_closed else math.nextafter(lo, math.inf)))
    out += [("r_load", 10**30), ("f_ctrl", 10**12), ("v_in", 10**9)]      # 倍精度にすると端ちょうどになる整数
    return out


def _outside_values():
    out = []
    for name, (lo, lo_closed, hi) in D059.items():
        out += [(name, math.nextafter(hi, math.inf)), (name, hi * 2)]
        out.append((name, math.nextafter(lo, -math.inf) if lo_closed else lo))
        out += [(name, math.nan), (name, math.inf), (name, -math.inf), (name, -1.0), (name, 10**400)]
        if lo > 0:      # 正でも、倍精度で 0 や範囲の下になる値
            out += [(name, 0.0), (name, 5e-324), (name, np.float32(1e-45)), (name, np.longdouble("1e-4000")),
                    (name, lo / 2)]
    out += [("f_ctrl", 10**12 + 1), ("r_load", 10**30 + 10**15)]          # 倍精度で上限を超える整数
    return out


@pytest.mark.parametrize(("name", "value"), _inside_values(), ids=lambda v: repr(v)[:28])
def test_d059_range_ends_are_accepted(name, value):
    """範囲の端（両端を含む。zero_damping の下だけ含まない）は invalid_argument にならず、
    想定外の例外も出ない。"""
    result, _ = _call(name, value)
    assert result != "invalid_argument" and not result.startswith("EXC")


@pytest.mark.parametrize(("name", "value"), _outside_values(), ids=lambda v: repr(v)[:28])
def test_d059_outside_is_refused_with_the_argument_name(name, value):
    """範囲の外、NaN、inf、10**400、倍精度で 0 になる値は invalid_argument（文に引数の名前）。"""
    result, msg = _call(name, value)
    assert result == "invalid_argument"
    assert name in msg


def _practical_circuits(n=300):
    """実用の範囲（Vin 0.1 V〜1 MV、L 1 nH〜10 H、C 10 pF〜1 kF、R 1 mΩ〜1 MΩ と無負荷、f 10 Hz〜10 MHz）。"""
    rng = np.random.default_rng(591)
    for _ in range(n):
        v_in, ind, cap = (float(10 ** rng.uniform(lo, hi)) for lo, hi in ((-1, 6), (-9, 1), (-11, 3)))
        r = float(10 ** rng.uniform(-3, 6)) if rng.random() < 0.8 else 1e30
        yield v_in, ind, cap, r, float(10 ** rng.uniform(1, 7))


def test_practical_circuits_are_never_refused_as_invalid_argument():
    """実用の回路 300 通りは、範囲の外として断られない（設計できない理由で断るのは可）。"""
    results = {}
    for v_in, ind, cap, r, f in _practical_circuits():
        result, _, _ = _run((v_in, ind, cap, r), f)
        results[result] = results.get(result, 0) + 1
    assert "invalid_argument" not in results
    assert results.get("ok", 0) >= 30            # 設計できる回路も含まれている


CORNER_VALUES = ([1e-6, 24.0, 1e9], [1e-12, 22e-6, 1e4], [1e-15, 100e-6, 1e6], [1e-6, 6.0, 1e30],
                 [1e-3, 200e3, 1e12])
CORNER_OPTS = [{}, {"zero_ratio": 1e-9, "zero_damping": 2.0, "filter_ratio": 1000.0},
               {"zero_ratio": 1.0, "zero_damping": 5e-324, "filter_ratio": 0.0}]


def test_range_corners_only_raise_gain_design_errors():
    """範囲の端どうしの組み合わせ（3^5 × 3 = 729 通り）: 結果かGainDesignError だけが出て、候補の値は有限。"""
    reasons = set()
    for v_in, ind, cap, r, f in itertools.product(*CORNER_VALUES):
        for opts in CORNER_OPTS:
            result, g, _ = _run((v_in, ind, cap, r), f, **opts)      # ほかの例外が出れば、ここで試験が落ちる
            reasons.add(result)
            if g is not None:
                assert all(math.isfinite(x) for x in (g.kp, g.ki, g.kd, g.f_cross, g.pole_radius))
    assert "invalid_argument" not in reasons


# ---------------------------------------------------------------------------------------------------
# 3. 書き方と「満たす」の判定
# ---------------------------------------------------------------------------------------------------

MINOR3 = ((24.0, 470e-6, 10e-6, 15.793045736999451), 15e3,
          {"zero_damping": 0.1653846153846154, "filter_ratio": 0.0})


def test_largest_reachable_margin_is_shown_below_the_target():
    """前回の軽微3: 最大 29.998° を「30.0°」（目標 30°）と書かない。"""
    plant, f_ctrl, opts = MINOR3
    result, g, msg = _run(plant, f_ctrl, phase_margin_deg=30.0, **opts)
    assert result == "margin_not_reached"
    shown = float(re.search(r"取れるのは最大 (-?[\d.]+)°", msg).group(1))
    assert shown < 30.0
    assert shown == pytest.approx(29.998, abs=1e-3)           # 150 桁の参照の最大は 29.99773°


def test_target_and_largest_reachable_margin_are_not_shown_as_the_same_number():
    plant, f_ctrl, opts = MINOR3
    result, _, msg = _run(plant, f_ctrl, phase_margin_deg=29.99773, **opts)
    assert result == "margin_not_reached"
    shown_target = re.search(r"位相余裕 (-?[\d.]+)° を取れる", msg).group(1)
    shown_max = re.search(r"取れるのは最大 (-?[\d.]+)°", msg).group(1)
    assert float(shown_max) < float(shown_target)


def test_margin_shown_with_meets_is_not_below_the_shown_target():
    result, g, msg = _run((24.0, 22e-6, 100e-6, 6.0), 200e3, zero_ratio=1e-7, phase_margin_deg=55.04)
    assert result == "no_stability_margin" and g.phase_margin_deg >= 55.04 - MEETS_TOL
    m = re.search(r"位相余裕 (-?[\d.]+)°（目標の (-?[\d.]+)° は満たす）", msg)
    assert float(m.group(1)) >= float(m.group(2))


def _shape_margin_at(w, plant, f_ctrl, zero_ratio=0.5, zero_damping=0.3, filter_ratio=0.25):
    """設計の形（Ki = 1。余裕は Ki に依らない）の、交差周波数 w での位相余裕
    （主値。−180°〜0° の位相のとき）。"""
    v_in, ind, cap, r = plant
    ts = 1.0 / f_ctrl
    wz, tf = zero_ratio / math.sqrt(ind * cap), filter_ratio * ts
    kp = 2.0 * zero_damping / wz - tf

    class Shape:
        pass

    g = Shape()
    g.kp, g.ki, g.kd, g.t_filter = kp, 1.0, 1.0 / wz**2 - kp * tf, tf
    v = Ref(g, v_in, ind, cap, r, f_ctrl)(np.array([w]))[0]
    return 180.0 + math.degrees(math.atan2(v.imag, v.real))


def test_search_counts_the_top_end_as_meeting_within_the_d057_tolerance():
    """探索の「満たす」は受け入れと同じ判定（目標 − 1e-6° 以上）。余裕が上端に向かって下がる回路で、目標を
    上端の余裕 + 5e-7° に置くと、上端も「満たす」ので区間が上端まで続く。+ 2e-6° なら上端は満たさず、
    成功する。"""
    plant, f_ctrl = (24.0, 22e-6, 100e-6, 6.0), 200e3
    w0 = 1.0 / math.sqrt(22e-6 * 100e-6)
    f_top = 1.5 * w0 / (2.0 * math.pi)
    at_top = _shape_margin_at(1.5 * w0, plant, f_ctrl)
    assert at_top == pytest.approx(61.277059601632, abs=1e-9)          # 150 桁の参照の値
    assert _shape_margin_at(1.2 * w0, plant, f_ctrl) > at_top + 1.0    # 上端に向かって下がっている
    result, g, msg = _run(plant, f_ctrl, f_cross_max=f_top, phase_margin_deg=at_top + 5e-7)
    assert result == "crossover_at_search_edge"
    assert "は満たす）" in msg
    result, g, _ = _run(plant, f_ctrl, f_cross_max=f_top, phase_margin_deg=at_top + 2e-6)
    assert result == "ok"
    assert g.f_cross < f_top and g.phase_margin_deg >= at_top + 2e-6 - TARGET_TOL


# ---------------------------------------------------------------------------------------------------
# 4. Vin が大きいとき、閉ループの極の半径の丸めが D-057 の ε（1e-9）を超える（重大。決まり4）
# ---------------------------------------------------------------------------------------------------

# （回路、制御周波数、設計の引数、150 桁の参照の半径 − 1）。どれも 1 との差が 1e-9 より小さい
# （安定の余裕が無い）
# 上の4つは実用の範囲（Vin 83 kV〜895 kV、f 23 kHz〜6.2 MHz）、5つ目は範囲の端（Vin 1 GV）
HIGH_VIN = [
    ((412169.5762239939, 0.16160871635527527, 0.4632826986189658, 1e30), 22803.275120932136,
     {"zero_ratio": 5.316531590111157e-07, "zero_damping": 0.814948799243281}, -7.818e-14),
    ((82888.19000954041, 7.687175724559705e-08, 232.59255161739404, 30806.96514674552), 1060587.972794125,
     {"zero_ratio": 3.5986891371643046e-05, "zero_damping": 1.84838158173341}, -3.083e-10),
    ((200970.73372559165, 1.9916952286041042e-07, 18.39576101718791, 178.35381990409903), 6190295.795299207,
     {"zero_ratio": 1.0137587406690042e-08, "zero_damping": 0.4440977980145964}, -2.847e-17),
    ((894691.6551502685, 0.017291779018258607, 0.0005703546193605544, 26747.439184322197), 2206187.1073270487,
     {"zero_ratio": 2.206924647994628e-08, "zero_damping": 0.2352991152860896}, -1.349e-16),
    ((1e9, 1e-12, 1e6, 6.0), 1e12, {"zero_ratio": 1.0, "zero_damping": 2.0, "filter_ratio": 0.0}, -2.679e-10),
]
_HIGH_VIN_IDS = [f"Vin{c[0][0]:.3g}" for c in HIGH_VIN]


@pytest.mark.parametrize("case", HIGH_VIN, ids=_HIGH_VIN_IDS)
def test_same_circuits_at_24_volts_are_refused_for_no_stability_margin(case):
    """上の回路を Vin = 24 V にすると（ループは Vin·K だけで決まるので、極は同じ）、正しく
    「安定の余裕が無い」で断る。このファイルの参照の半径は 150 桁の値と 1e-11 で合う。"""
    (_, ind, cap, r), f_ctrl, opts, ref_r = case
    result, g, _ = _run((24.0, ind, cap, r), f_ctrl, **opts)
    assert result == "no_stability_margin"
    assert _radius_minus_one(g, 24.0, ind, cap, r, f_ctrl) == pytest.approx(ref_r, abs=1e-11)
    assert abs(ref_r) < STABILITY_EPS


@pytest.mark.parametrize("case", HIGH_VIN, ids=_HIGH_VIN_IDS)
def test_high_vin_design_is_not_returned_without_stability_margin(case):
    """返すなら参照の半径が 1 − 1e-9 以下。そうでなければ断る（Vin = 24 V のときと同じ理由で）。"""
    plant, f_ctrl, opts, ref_r = case
    result, g, _ = _run(plant, f_ctrl, **opts)
    assert _radius_minus_one(g, *plant, f_ctrl) == pytest.approx(ref_r, abs=1e-11)
    assert result == "no_stability_margin"


# 断るが、理由が事実と違う（「1 以上で不安定」と書く。150 桁の参照の半径は 1 − 3.4e-11）。Vin 16 MV
FALSE_UNSTABLE = ((16286220.011097308, 0.0003486222953146516, 0.03046573234076631, 36.301777798882036),
                  3831461.8219012544,
                  {"zero_ratio": 1.1490533679001927e-05, "zero_damping": 1.099325229684776})


def test_high_vin_refusal_does_not_call_a_marginal_candidate_unstable():
    plant, f_ctrl, opts = FALSE_UNSTABLE
    result, g, msg = _run(plant, f_ctrl, **opts)
    result24, _, _ = _run((24.0, *plant[1:]), f_ctrl, **opts)
    assert result24 == "no_stability_margin"
    assert abs(_radius_minus_one(g, *plant, f_ctrl)) < STABILITY_EPS
    assert result == "no_stability_margin" and "1 以上で不安定" not in msg


def test_practical_voltages_up_to_1_kv_keep_the_stability_judgement():
    """Vin 0.1 V〜1 kV の実用の回路（零点の比 1e-9〜1）で返したゲインは、参照の半径が 1 − 1e-9 以下。"""
    rng = np.random.default_rng(12)
    returned = 0
    for _ in range(250):
        while True:
            v_in = float(10 ** rng.uniform(-1, 3))
            ind, cap = float(10 ** rng.uniform(-9, 1)), float(10 ** rng.uniform(-11, 3))
            r = float(10 ** rng.uniform(-3, 6)) if rng.random() < 0.85 else 1e30
            f = float(10 ** rng.uniform(1, 7))
            if f * 2.0 * math.pi * math.sqrt(ind * cap) >= 10.0:
                break
        ratio = float(10 ** rng.uniform(-9, 0)) if rng.random() < 0.6 else float(rng.uniform(0.2, 1))
        opts = {"zero_ratio": ratio, "zero_damping": float(rng.uniform(0.05, 2))}
        result, g, _ = _run((v_in, ind, cap, r), f, **opts)
        if result == "ok":
            returned += 1
            assert _radius_minus_one(g, v_in, ind, cap, r, f) <= -STABILITY_EPS + 1e-11
    assert returned >= 100


# ---------------------------------------------------------------------------------------------------
# 5. Q がとても小さいとき（負荷がほぼ短絡）、|L| が平らで交差の位置が丸めで動く（軽微）
# ---------------------------------------------------------------------------------------------------

LOW_Q = ((10.81936124982362, 0.12746071594513633, 5.631368828574676e-09, 2.1410342282585894e-06),
         20412490.22544481, {"zero_ratio": 0.6474596869314334, "zero_damping": 1.569212487698608,
                             "filter_ratio": 0.0, "phase_margin_deg": 22.628123695975038})
LOW_Q_KI, LOW_Q_F_CROSS = 3207592692509.048, 4457919.2645621      # 150 桁の参照を求めたときのゲインと交差


class GainsChanged(Exception):
    """記録したゲインと違うゲインが返った（150 桁の参照の値を取り直す必要がある）。"""


def test_low_q_returned_margin_agrees_with_150_digit_reference():
    """断るのは可。返すなら、書いた余裕は 150 桁の参照の余裕（22.615532°）と 1e-6° で合う。"""
    plant, f_ctrl, opts = LOW_Q
    result, g, _ = _run(plant, f_ctrl, **opts)
    if result != "ok":
        return
    same = (g.f_cross == pytest.approx(LOW_Q_F_CROSS, rel=1e-6) and g.ki == pytest.approx(LOW_Q_KI, rel=1e-6))
    if not same:
        raise GainsChanged
    assert g.phase_margin_deg == pytest.approx(22.615532027, abs=REF_PM_TOL)

