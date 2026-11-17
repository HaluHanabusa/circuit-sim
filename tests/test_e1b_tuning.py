"""E1b: ゲインの初期値の式（tuning/initial.py）の位相余裕と安定性の試験。

期待値は、この試験の中で別に組んだ離散の開ループで作る（実装の関数を使わない）:
  制御対象: scipy.signal.cont2discrete で Gvd(s) = Vin / (LC·s² + (L/R)·s + 1)
  を零次ホールドで離散化した伝達関数
        （PWMの保持 0.5 サンプル分）、演算の遅れ z⁻¹、
  PID : 実装の差分方程式の z 変換  Kp + Ki·Ts·z/(z − 1) + Kd·(z − 1)/((Tf + Ts)·z − Tf)。
  位相: 20 万点の等比の格子で np.unwrap（低い周波数から連続）。位相余裕は、ゲインが 1 を横切る点すべての
        180° + 位相 のうち最小。
  安定: 閉ループの特性多項式  z·Dc(z)·Dp(z) + Nc(z)·Np(z)  の根の絶対値の最大が 1 未満。
"""
import math
import re

import numpy as np
import pytest
from scipy import signal

import circuit_sim.tuning.initial as ini
from circuit_sim.tuning import GainDesignError, PidGains, buck_voltage_mode_pid, loop_response

VIN, L, C, R = 24.0, 47e-6, 100e-6, 6.0
TARGET = 55.0          # 既定の目標の位相余裕


def plant_tf(ts, v_in=VIN, inductance=L, capacitance=C, r_load=R):
    num, den, _ = signal.cont2discrete(([v_in], [inductance * capacitance, inductance / r_load, 1.0]), ts,
                                       method="zoh")
    return np.atleast_1d(np.squeeze(num)), np.atleast_1d(np.squeeze(den))


def pid_tf(kp, ki, kd, tf, ts):
    g = tf + ts
    num = np.polyadd(np.polyadd(kp * np.polymul([1, -1], [g, -tf]), ki * ts * np.polymul([1, 0], [g, -tf])),
                     kd * np.polymul([1, -1], [1, -1]))
    return num, np.polymul([1, -1], [g, -tf])


def independent_loop(gains, f_ctrl, **plant):
    """（開ループの周波数応答の関数、閉ループの特性多項式）。"""
    ts = 1.0 / f_ctrl
    pn, pd = plant_tf(ts, **plant)
    cn, cd = pid_tf(gains.kp, gains.ki, gains.kd, gains.t_filter, ts)

    def loop(w):
        z = np.exp(1j * np.asarray(w, dtype=float) * ts)
        return np.polyval(cn, z) * np.polyval(pn, z) / (np.polyval(cd, z) * np.polyval(pd, z) * z)

    return loop, np.polyadd(np.polymul(np.polymul(cd, pd), [1, 0]), np.polymul(cn, pn))


def independent_crossings(loop, w):
    """格子 w で |L| − 1 の符号が変わる区間を二分法で詰めた交差と、そこでの（低い周波数から連続な）位相。"""
    values = loop(w)
    phase = np.unwrap(np.angle(values))
    s = np.log(np.abs(values))
    cross, phi = [], []
    for i in np.nonzero(np.sign(s[:-1]) != np.sign(s[1:]))[0]:
        lo, hi = w[i], w[i + 1]
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if (math.log(abs(loop(mid))) > 0) == (s[i] > 0):
                lo = mid
            else:
                hi = mid
        cross.append(lo)
        phi.append(phase[i] + np.angle(loop(lo) / values[i]))    # 格子の点から、主値の小さな差で進める
    return np.array(cross), np.array(phi)


def independent_margin_and_radius(gains, f_ctrl, extra_grid=None, **plant):
    """（最小の位相余裕、その交差周波数 [Hz]、閉ループの極の半径）。

    交差は 20 万点の等比の格子（と extra_grid）で見つけて二分法で詰める（位置の誤差 1e-12 程度、位相の
    誤差は 1e-6° 以下）。位相は低い周波数（10 rad/s）から連続。
    """
    loop, char = independent_loop(gains, f_ctrl, **plant)
    w = np.geomspace(10.0, 0.9999 * math.pi * f_ctrl, 200_000)
    if extra_grid is not None:
        w = np.unique(np.concatenate([w, extra_grid]))
    cross, phi = independent_crossings(loop, w)
    margins = np.degrees(phi) + 180.0
    k = int(np.argmin(margins))
    return float(margins[k]), float(cross[k] / (2 * math.pi)), float(np.max(np.abs(np.roots(char))))


def test_loop_response_matches_independent_transfer_function():
    """実装の開ループの周波数応答が、scipy で離散化した制御対象 × PID × z⁻¹ と一致する（相対 1e-9）。"""
    ts = 1e-5
    w = np.geomspace(100.0, 0.99 * math.pi / ts, 500)
    got = loop_response(w, 0.0117, 147.0, 2.74e-6, 2.5e-6, VIN, L, C, R, ts)
    pn, pd = plant_tf(ts)
    cn, cd = pid_tf(0.0117, 147.0, 2.74e-6, 2.5e-6, ts)
    z = np.exp(1j * w * ts)
    want = np.polyval(cn, z) * np.polyval(pn, z) / (np.polyval(cd, z) * np.polyval(pd, z) * z)
    assert np.max(np.abs(got - want) / np.abs(want)) <= 1e-9


def test_reference_circuit_design_meets_target_and_is_stable():
    """基準の回路（f_ctrl = 100 kHz）: 位相余裕 55°（別の開ループでも 0.5° 以内）、閉ループは安定。"""
    g = buck_voltage_mode_pid(VIN, L, C, R, 100e3)
    pm, fc, radius = independent_margin_and_radius(g, 100e3)
    assert g.phase_margin_deg == pytest.approx(TARGET, abs=1e-6)
    assert pm == pytest.approx(g.phase_margin_deg, abs=0.01) and 45.0 <= pm <= 180.0
    assert fc == pytest.approx(g.f_cross, rel=0.01)
    assert radius == pytest.approx(g.pole_radius, rel=1e-9) and radius < 1.0


@pytest.mark.parametrize("args", [
    (VIN, L, C, R, 20e3),
    (VIN, L, C, R, 25e3),
    (VIN, L, C, R, 10e3),
    (VIN, 10e-6, 10e-6, R, 100e3),
], ids=["20kHz", "25kHz", "10kHz", "10uH-10uF-100kHz"])
def test_unreachable_margin_is_reported_as_failure_with_the_best_candidate(args):
    """目標の位相余裕を取れない回路では、満たしたことにせず GainDesignError（ValueError の一種）を出す。

    以前は、折り返した位相（180° + atan2）を余裕と読み、探索の端を交差周波数にして、真の余裕が負の
    不安定なゲインを 250° 前後の余裕として返していた（20 kHz で真の余裕 −106°、極の半径 1.44）。
    例外の最良の候補は、別に組んだ開ループで測った余裕と閉ループの極の半径を正しく持つ。
    """
    *plant, f_ctrl = args
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(*plant, f_ctrl)
    err = info.value
    assert isinstance(err, ValueError)
    assert err.reason == "margin_not_reached"
    assert err.target_deg == TARGET
    best = err.best
    pm, _, radius = independent_margin_and_radius(
        best, f_ctrl, v_in=plant[0], inductance=plant[1], capacitance=plant[2], r_load=plant[3])
    assert pm < TARGET and best.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert best.phase_margin_deg <= 180.0
    assert radius == pytest.approx(best.pole_radius, rel=1e-6)
    assert err.shortfall_deg == pytest.approx(TARGET - best.phase_margin_deg)
    assert "足りない" in str(err)


def test_crossover_at_the_end_of_the_search_range_is_not_a_success():
    """位相余裕が目標以上の区間が探索の上端まで続くとき（上端を 3 kHz に狭めた基準の回路）は、
    上端を交差周波数として返さず、理由 crossover_at_search_edge で失敗する。"""
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(VIN, L, C, R, 100e3, f_cross_max=3e3)
    assert info.value.reason == "crossover_at_search_edge"
    assert info.value.best is not None
    assert buck_voltage_mode_pid(VIN, L, C, R, 100e3, f_cross_max=6e3).f_cross < 6e3    # 上端の手前なら成功


@pytest.mark.parametrize("f_ctrl", [20e3, 30e3, 40e3, 50e3, 100e3, 200e3])
@pytest.mark.parametrize("r_load", [3.0, 6.0, 60.0, 1000.0])
def test_returned_designs_are_always_stable_and_meet_the_target(f_ctrl, r_load):
    """制御周波数と負荷を振っても、返したゲインは必ず目標の余裕を持ち（別の開ループで目標 − 0.01° 以上。
    報告の余裕は別の開ループの値を 0.01° より大きく上回らない）、閉ループが安定。満たせないときは
    GainDesignError（どちらかになる）。"""
    try:
        g = buck_voltage_mode_pid(VIN, L, C, r_load, f_ctrl)
    except GainDesignError as err:
        assert err.reason in ("margin_not_reached", "crossover_at_search_edge", "margin_below_target",
                              "unstable", "no_stability_margin", "negative_gain")
        if err.best is not None:                                 # 最良の候補の余裕を大きく言わない
            pm, _, _ = independent_margin_and_radius(err.best, f_ctrl, r_load=r_load)
            assert err.best.phase_margin_deg <= pm + 0.01
        return
    pm, _, radius = independent_margin_and_radius(g, f_ctrl, r_load=r_load)
    assert TARGET - 0.01 <= pm <= 180.0
    assert g.phase_margin_deg <= pm + 0.01
    assert radius < 1.0
    assert g.kp >= 0.0 and g.kd >= 0.0


def test_sweep_has_both_outcomes():
    """上の試験が両方の場合を通っている（成功する回路と、失敗する回路がある）。"""
    outcomes = set()
    for f_ctrl in (20e3, 100e3):
        try:
            buck_voltage_mode_pid(VIN, L, C, R, f_ctrl)
            outcomes.add("ok")
        except GainDesignError:
            outcomes.add("error")
    assert outcomes == {"ok", "error"}


def test_resonance_above_the_search_range_is_reported():
    with pytest.raises(GainDesignError) as info:                 # 共振が探索の上端より上
        buck_voltage_mode_pid(VIN, 1e-6, 1e-6, R, 100e3)
    assert info.value.reason == "margin_not_reached" and info.value.best is None


@pytest.mark.parametrize("name,value", [
    ("f_ctrl", 0.0), ("f_ctrl", math.inf), ("v_in", -24.0), ("r_load", math.nan),
    ("zero_ratio", -0.5), ("zero_ratio", 0.0), ("zero_ratio", 1.0000001), ("zero_ratio", 1.5),
    ("zero_ratio", math.nan),
    ("zero_damping", 0.0), ("zero_damping", 2.0000001), ("zero_damping", math.inf),
    ("filter_ratio", -0.1), ("filter_ratio", math.inf), ("filter_ratio", math.nan),
    ("phase_margin_deg", 0.0), ("phase_margin_deg", 180.0), ("phase_margin_deg", -10.0),
    ("phase_margin_deg", math.nan), ("phase_margin_deg", math.inf),
    ("f_cross_max", 0.0), ("f_cross_max", -1e3), ("f_cross_max", math.nan), ("f_cross_max", math.inf),
])
def test_invalid_design_arguments_are_rejected_with_a_reason(name, value):
    """設計の引数が範囲の外、NaN、inf なら、計算せずに理由 invalid_argument で断る。

    以前は zero_ratio が負だと「nan°（満たす）」と書いた。"""
    args = {"v_in": VIN, "inductance": L, "capacitance": C, "r_load": R, "f_ctrl": 100e3}
    kwargs = {}
    (args if name in args else kwargs)[name] = value
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(**args, **kwargs)
    assert info.value.reason == "invalid_argument" and info.value.best is None
    assert name in str(info.value) and "満たす" not in str(info.value)


def test_close_crossings_in_one_grid_cell_are_not_missed():
    """近い2つの交差（2437.58 Hz と 2437.79 Hz。等比の格子の1区間に入る間隔）を落とさない。

    以前は格子で交差を探したので2つを見落とし、最良の候補の余裕を 595.6 Hz の 70.6° と書いて
    「目標は満たす」としていた（本文は「最大 40.5°」）。いまは、交差を囲い込みで求め（log|L| の値・傾き・
    曲がりの上限で「交差が無い」「交差は高々1つ」と言える区間を決め、残りを二分する。格子の細かさによらない）、
    設計した点の余裕も必ず最小に入れる。期待値は、この試験の開ループに、その付近の細かい格子を足して求める。
    """
    plant, f_ctrl = (VIN, 470e-6, 10e-6, R), 15e3
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(*plant, f_ctrl, zero_damping=0.1, filter_ratio=0.0)
    err = info.value
    assert err.reason == "margin_not_reached"
    best = err.best
    fine = 2 * math.pi * np.linspace(2437.0, 2438.5, 20_001)
    loop, _ = independent_loop(best, f_ctrl, inductance=470e-6, capacitance=10e-6)
    w = np.unique(np.concatenate([np.geomspace(10.0, 0.9999 * math.pi * f_ctrl, 200_000), fine]))
    cross, phi = independent_crossings(loop, w)
    assert np.round(cross / (2 * math.pi), 1).tolist() == [595.6, 2437.6, 2437.8]
    pm = float(np.min(np.degrees(phi) + 180.0))
    assert pm < 41.0
    assert best.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert err.shortfall_deg == pytest.approx(TARGET - pm, abs=0.01) and err.shortfall_deg > 0
    assert "足りない" in str(err) and "満たす" not in str(err)


def test_search_range_above_nyquist_is_limited_and_unstable_candidate_is_not_called_satisfying():
    """f_cross_max をナイキスト周波数より上にしても、探索はナイキストの手前までに制限する。そのときの
    最良の候補（閉ループの極の半径 18.6）を「満たす」と書かない（以前は余裕を inf° として
    「満たす」と書いた）。"""
    plant, f_ctrl = (VIN, 47e-6, 10e-6, 1000.0), 20e3
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(*plant, f_ctrl, f_cross_max=1e9)
    best = info.value.best
    _, char = independent_loop(best, f_ctrl, inductance=47e-6, capacitance=10e-6, r_load=1000.0)
    assert float(np.max(np.abs(np.roots(char)))) == pytest.approx(best.pole_radius, rel=1e-6)
    assert best.pole_radius > 1.0
    assert math.isfinite(best.phase_margin_deg) and best.phase_margin_deg < TARGET
    assert best.f_cross < f_ctrl / 2
    text = str(info.value)
    assert "足りない" in text and "1 以上で不安定" in text and "満たす" not in text
    with pytest.raises(GainDesignError) as at_limit:             # ナイキストの手前を指定したのと同じ
        buck_voltage_mode_pid(*plant, f_ctrl, f_cross_max=0.9999 * f_ctrl / 2)
    assert at_limit.value.best == best


def test_other_crossover_below_target_is_reported():
    """設計した点（余裕 55°）のほかに、余裕が目標に届かない交差周波数があれば margin_below_target。
    別の開ループでも、最小の余裕は目標未満で、設計した点の余裕は目標。"""
    plant, f_ctrl = (VIN, 470e-6, 10e-6, R), 50e3
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(*plant, f_ctrl, zero_ratio=0.8, zero_damping=0.05, filter_ratio=0.25)
    err = info.value
    assert err.reason == "margin_below_target"
    pm, fc, radius = independent_margin_and_radius(err.best, f_ctrl, inductance=470e-6, capacitance=10e-6)
    assert pm < TARGET - 1.0 and err.best.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert abs(fc - err.best.f_cross) > 0.01 * err.best.f_cross              # 設計した点とは別の交差
    loop, _ = independent_loop(err.best, f_ctrl, inductance=470e-6, capacitance=10e-6)
    wc = 2 * math.pi * err.best.f_cross
    assert abs(loop(wc)) == pytest.approx(1.0, rel=1e-9)
    assert "足りない" in str(err)


def test_negative_proportional_gain_is_reported():
    """Kp = 2ζz·Ki/ωz − Ki·Tf は、Tf > 2ζz/ωz のとき負（この試験の式）。そのときは negative_gain。
    基準の回路、f_ctrl 50 kHz、κ 0.3、ζz 0.05、Tf = 2·Ts = 40 µs > 2ζz/ωz = 22.8 µs。"""
    kappa, zeta, ratio, f_ctrl = 0.3, 0.05, 2.0, 50e3
    wz = kappa / math.sqrt(L * C)
    assert ratio / f_ctrl > 2 * zeta / wz
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(VIN, L, C, R, f_ctrl, zero_ratio=kappa, zero_damping=zeta, filter_ratio=ratio)
    assert info.value.reason == "negative_gain"
    best = info.value.best
    assert best.kp < 0.0 and best.kp == pytest.approx(2 * zeta * best.ki / wz - best.ki * ratio / f_ctrl)
    assert "Kp" in str(info.value)


def test_candidate_wording_says_satisfied_only_for_a_finite_margin_at_or_above_target():
    """「満たす」と書くのは、余裕が有限で目標以上のときだけ。NaN と inf は「求められない」。"""
    def text(pm, radius=0.9):
        best = PidGains(0.01, 100.0, 1e-6, 2.5e-6, 3e3, pm, radius)
        return str(GainDesignError("margin_not_reached", "例。", TARGET, best))

    assert "目標の 55° は満たす" in text(55.0) and "満たす" in text(70.0)
    assert "足りない" in text(54.99) and "満たす" not in text(54.99)
    for bad in (math.nan, math.inf, -math.inf):
        assert "求められない" in text(bad) and "満たす" not in text(bad)
    assert "1 以上で不安定" in text(70.0, radius=1.2) and "1 未満で安定" in text(70.0)


# ------------------------------------------------------------ 4回目の修正（E1b-review-3、E1b-verifier-3）

def _design(*args, **opts):
    try:
        return buck_voltage_mode_pid(*args, **opts), None
    except GainDesignError as err:
        return err.best, err


def _loop_of(gains, f_ctrl, v_in=VIN, inductance=L, capacitance=C, r_load=R):
    """実装の開ループ（因子の形）。交差の一覧そのものを確かめる試験だけで使う（期待値には使わない）。"""
    ts = 1.0 / f_ctrl
    factors = ini._loop_factors(gains.kp, gains.ki, gains.kd, gains.t_filter, v_in, inductance, capacitance,
                                r_load, ts)
    return ini._Loop(*factors, ts, 1e-4 / math.sqrt(inductance * capacitance))


@pytest.mark.parametrize("r_load", [1e8, 1e10, 1e11, 1e12, 1e14])
@pytest.mark.parametrize("circuit", [(47e-6, 100e-6, 200e3), (220e-6, 470e-6, 50e3)], ids=["47uH", "220uH"])
def test_very_large_load_resistance_is_designed_not_refused(r_load, circuit):
    """負荷抵抗がとても大きい（無負荷に近い）回路: 制御対象の零点が z = −1 から 1e-13 程度、極が単位円から
    1e-13 程度の距離に来る。以前は、ナイキストの端の区間が傾きの上限を持てずに残り、|L| = 1e-13 の点を
    交差として −267.7° と読み、margin_below_target で断っていた（R = 1e11 Ω の 47 µH、200 kHz）。
    いまは近い根の項を値で上下から抑えるので、成功し、別の開ループの余裕（目標 − 0.01° 以上、報告と
    0.01° 以内）と極の半径（相対 1e-9）と合う。"""
    ind, cap, f_ctrl = circuit
    g = buck_voltage_mode_pid(VIN, ind, cap, r_load, f_ctrl)
    pm, fc, radius = independent_margin_and_radius(g, f_ctrl, inductance=ind, capacitance=cap, r_load=r_load)
    assert pm >= TARGET - 0.01 and g.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert g.phase_margin_deg == pytest.approx(TARGET, abs=1e-6)
    assert radius == pytest.approx(g.pole_radius, rel=1e-9) and radius < 1.0


def test_crossings_near_a_root_close_to_the_unit_circle_have_unit_gain():
    """単位円にごく近い根（R = 1e11 Ω の制御対象の零点、|z| − 1 = −1.7e-13）があっても、返す交差はどれも
    |L| = 1 の点（|log|L|| ≤ 1e-9）。以前は |L| = 1.4e-13 のナイキストの点を入れていた。"""
    g = buck_voltage_mode_pid(VIN, L, C, 1e11, 200e3)
    loop = _loop_of(g, 200e3, r_load=1e11)
    assert np.min(loop._gap[loop._abs > 0.5]) < 1e-12                       # 角を突いている（単位円に近い根）
    cross = loop.crossovers(loop.ts * 1e-4 / math.sqrt(L * C))
    assert cross.size >= 1
    assert np.max(np.abs(np.log(np.abs(loop.response(cross))))) <= 1e-9


NEAR_TANGENT = ((7.2956238588846345, 0.0005008877793464173, 0.0029409513895168223, 0.04597935439105052,
                 611843.093690688),
                {"zero_ratio": 0.6848124950509352, "zero_damping": 0.49453314788438546,
                 "filter_ratio": 0.08545114437388464, "phase_margin_deg": 165.79140605186822,
                 "f_cross_max": 36867417.897361904})


def test_nearly_tangent_crossings_are_not_multiplied_by_rounding():
    """|L| が 1 にほぼ接する（326.010 Hz と 326.178 Hz の間で |L| − 1 が 1e-8 程度）最良の候補: 以前は丸めで
    符号が揺れる交差を 59 個返した（約 1 秒）。いまは h が単調な区間を二分法で詰め、θ の MERGE_TOL 倍より
    近い交差は余裕が最小の1つにまとめる。交差は別の開ループの3つと同じ位置（相対 1e-6）、最小の余裕は
    別の開ループと 0.01° 以内（まとめても最小は変わらない）。"""
    (v_in, ind, cap, r_load, f_ctrl), opts = NEAR_TANGENT
    best, err = _design(v_in, ind, cap, r_load, f_ctrl, **opts)
    assert err is not None and err.reason == "margin_not_reached"
    plant = {"v_in": v_in, "inductance": ind, "capacitance": cap, "r_load": r_load}
    ref_loop, _ = independent_loop(best, f_ctrl, **plant)
    grid = np.geomspace(10.0, 0.9999 * math.pi * f_ctrl, 200_000)
    ref_cross, ref_phi = independent_crossings(ref_loop, grid)
    assert np.round(ref_cross / (2 * math.pi), 2).tolist() == [95.05, 326.01, 326.18]       # 角を突いている
    loop = _loop_of(best, f_ctrl, **plant)
    cross = np.sort(loop.crossovers(loop.ts * 1e-4 / math.sqrt(ind * cap)))
    assert cross.size == 3
    assert np.max(np.abs(cross / ref_cross - 1.0)) < 1e-6
    assert np.min(np.diff(cross) / cross[1:]) > ini.MERGE_TOL
    assert best.phase_margin_deg == pytest.approx(float(np.min(np.degrees(ref_phi) + 180.0)), abs=0.01)


def test_satisfied_wording_uses_the_same_judgement_as_acceptance():
    """「満たす」と書くのは、受け入れと同じ判定（目標 − TARGET_TOL_DEG 以上。ini.meets_target）のとき。
    丸めの分だけ下回る余裕を「0.0° 足りない」と書かない。本当に少しだけ足りないときは、0 でない桁まで書く。"""
    def text(pm):
        return str(GainDesignError("margin_not_reached", "例。", TARGET, PidGains(0.01, 100.0, 1e-6, 2.5e-6,
                                                                              3e3, pm, 0.9)))

    tol = ini.TARGET_TOL_DEG
    for pm in (TARGET - 0.5 * tol, TARGET - 1e-11, TARGET):
        assert ini.meets_target(pm, TARGET)
        assert "目標の 55° は満たす" in text(pm) and "足りない" not in text(pm)
    for pm, shown in ((TARGET - 0.01, "54.99°（目標まで 0.01° 足りない）"),
                      (TARGET - 0.04, "54.96°（目標まで 0.04° 足りない）"),
                      (TARGET - 14.5, "40.5°（目標まで 14.5° 足りない）")):
        assert not ini.meets_target(pm, TARGET) and shown in text(pm) and "満たす" not in text(pm)
    short = float(re.search(r"目標まで ([\d.]+)° 足りない", text(TARGET - 2 * tol)).group(1))
    assert short == pytest.approx(2 * tol, rel=0.1)
    assert not ini.meets_target(math.nan, TARGET) and not ini.meets_target(math.inf, TARGET)


def test_unstable_reason_and_candidate_wording_agree():
    """理由 unstable の本文「位相余裕は目標を満たすが」と、最良の候補の「満たす」が食い違わない（以前は、
    丸めで目標を 2.6e-10° 下回る候補を「目標まで 0.0° 足りない」と書いた。E1b-verifier-3 の軽微2）。"""
    plant = (2.967370111576123, 7.338144645114917e-05, 2.439793958973858e-06, 0.21317375460902435)
    opts = {"zero_damping": 0.1437543236478271, "filter_ratio": 0.0, "phase_margin_deg": 24.361861916713153,
            "zero_ratio": 0.9590984284499318}
    best, err = _design(*plant, 123266.3864601657, **opts)
    assert err.reason == "unstable" and "位相余裕は目標を満たすが" in str(err)
    assert "は満たす）" in str(err) and "足りない" not in str(err) and "1 以上で不安定" in str(err)


@pytest.mark.parametrize("kind", [np.int64, np.int32, np.float32, np.float64])
def test_numpy_scalars_are_accepted_and_computed_in_double_precision(kind):
    """numpy の数（np.int64、np.int32、np.float32、np.float64）を受ける（前の版の後退を直した）。値は
    倍精度に直して計算するので、同じ値の Python の float と結果が同じ（np.float32 は、float32 に丸めた値の
    float と同じ）。"""
    def same(x):
        return float(kind(x))

    if np.issubdtype(kind, np.integer):
        got = buck_voltage_mode_pid(kind(24), L, C, kind(6), kind(100_000), phase_margin_deg=kind(50),
                                    f_cross_max=kind(20_000))
        want = buck_voltage_mode_pid(24.0, L, C, 6.0, 100e3, phase_margin_deg=50.0, f_cross_max=20e3)
    else:
        got = buck_voltage_mode_pid(kind(24), kind(L), kind(C), kind(6), kind(100e3), zero_ratio=kind(0.5),
                                    zero_damping=kind(0.3), filter_ratio=kind(0.25),
                                    phase_margin_deg=kind(55), f_cross_max=kind(20e3))
        want = buck_voltage_mode_pid(24.0, same(L), same(C), 6.0, 100e3, zero_ratio=same(0.5),
                                     zero_damping=same(0.3), filter_ratio=same(0.25), phase_margin_deg=55.0,
                                     f_cross_max=same(20e3))
    assert got == want
    assert all(type(v) is float for v in (got.kp, got.ki, got.kd, got.t_filter, got.phase_margin_deg))


@pytest.mark.parametrize("value", [np.bool_(True), True, "24", None, 1j])
def test_non_real_arguments_are_refused(value):
    """真偽値（numpy の真偽値も）、文字列、None、複素数は数として扱わず invalid_argument。"""
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(value, L, C, R, 100e3)
    assert info.value.reason == "invalid_argument" and "v_in" in str(info.value)


def test_limited_search_range_is_kept_in_the_result_and_the_message():
    """f_cross_max をナイキストの手前（0.9999 倍）に制限したら、結果に残し（指定した値と、制限した後の上端）、
    失敗の文にも「指定した上限 1 GHz を、ナイキスト周波数の手前の 9999 Hz に制限した」と書く。制限しなければ
    書かない。ゲインそのものは、制限した値を指定したときと同じ。"""
    limited = buck_voltage_mode_pid(VIN, L, C, R, 100e3, f_cross_max=1e6)
    assert limited.f_cross_max_limited_from == 1e6
    assert limited.f_search_max == pytest.approx(0.9999 * 50e3, rel=1e-12)
    plain = buck_voltage_mode_pid(VIN, L, C, R, 100e3, f_cross_max=0.9999 * 50e3)
    assert plain.f_cross_max_limited_from is None and plain == limited
    assert buck_voltage_mode_pid(VIN, L, C, R, 100e3).f_search_max == pytest.approx(25e3, rel=1e-12)
    best, err = _design(VIN, 47e-6, 10e-6, 1000.0, 20e3, f_cross_max=1e9)
    assert "（指定した上限 1 GHz を、ナイキスト周波数の手前の 9999 Hz に制限した）" in str(err)
    assert best.f_cross_max_limited_from == 1e9
    _, err = _design(VIN, 47e-6, 10e-6, 1000.0, 20e3, f_cross_max=9e3)
    assert "制限した" not in str(err)


def test_stability_is_said_only_below_one_minus_eps():
    """安定と言うのは半径が 1 − ε 以下のときだけ（ε = STABILITY_EPS）。1 との差が ε 未満は
    「安定の余裕が無い」。"""
    eps = ini.STABILITY_EPS
    radii = (0.5, 1 - 2 * eps, 1 - eps / 2, 1.0, 1 + eps / 2, 1 + eps, 18.6)
    assert [ini.stability_of(r) for r in radii] == [
        "stable", "stable", "no_margin", "no_margin", "no_margin", "unstable", "unstable"]

    def text(radius):
        return str(GainDesignError("margin_not_reached", "例。", TARGET,
                                   PidGains(0.01, 100.0, 1e-6, 2.5e-6, 3e3, 60.0, radius)))

    assert "安定の余裕が無い" in text(1 - 1e-16) and "安定）" not in text(1 - 1e-16)
    assert "1 未満で安定" in text(1 - 2 * eps) and "1 以上で不安定" in text(1 + 2 * eps)


@pytest.mark.parametrize("plant,f_ctrl", [((VIN, 22e-6, 100e-6, 6.0), 100e3),
                                          ((VIN, 4.7e-6, 1e-3, 1000.0), 1e6)])
def test_radius_at_one_within_rounding_is_refused_as_no_stability_margin(plant, f_ctrl):
    """zero_ratio = 1e-9（積分がとても遅い）: 閉ループの極の半径が丸めの範囲で 1 に乗る（以前は、片方を
    0.9999999999999999 で「安定」として返し、もう片方を 1.0000000000000007 で unstable とした）。どちらも
    理由 no_stability_margin で断り、安定とも不安定とも書かない。別に組んだ特性多項式の根でも、半径は
    1 に近い。"""
    best, err = _design(*plant, f_ctrl, zero_ratio=1e-9)
    assert err is not None and err.reason == "no_stability_margin"
    text = str(err)
    assert "安定の余裕が無い" in text and "1 未満で安定" not in text and "1 以上で不安定" not in text
    _, char = independent_loop(best, f_ctrl, v_in=plant[0], inductance=plant[1], capacitance=plant[2],
                               r_load=plant[3])
    assert abs(float(np.max(np.abs(np.roots(char)))) - 1.0) < 1e-4


def test_slow_but_resolvable_integral_is_still_stable():
    """zero_ratio = 1e-4、f_ctrl = 1 MHz（半径 1 − 2.8e-9。ε = 1e-9 より 1 から離れている）は、これまでどおり
    成功する（別の開ループで余裕が目標以上、極の半径が参照と相対 1e-9）。"""
    plant, f_ctrl = (VIN, 4.7e-6, 1e-3, 1000.0), 1e6
    g = buck_voltage_mode_pid(*plant, f_ctrl, zero_ratio=1e-4)
    assert 1.0 - g.pole_radius > ini.STABILITY_EPS
    pm, _, radius = independent_margin_and_radius(g, f_ctrl, inductance=4.7e-6, capacitance=1e-3,
                                                  r_load=1000.0)
    assert pm >= TARGET - 0.01 and radius == pytest.approx(g.pole_radius, rel=1e-9)


@pytest.mark.parametrize("opts", [{"zero_damping": 1.0}, {"zero_damping": 2.0},
                                  {"zero_ratio": 0.3, "zero_damping": 1.0},
                                  {"zero_damping": 1.0, "phase_margin_deg": 45.0}],
                         ids=["double-zero", "real-zeros-14x", "double-zero-low", "double-zero-45deg"])
def test_double_and_real_pid_zeros_are_designed(opts):
    """ζz = 1（零点の重根。普通の設計）と ζz = 2（2つの実根、比 14）も設計する（以前は 0 < ζz < 1 だけ）。
    返したゲインは別の開ループで余裕が目標以上、閉ループは安定（半径は参照と相対 1e-9）。"""
    g = buck_voltage_mode_pid(VIN, L, C, R, 100e3, **opts)
    pm, _, radius = independent_margin_and_radius(g, 100e3)
    assert pm >= opts.get("phase_margin_deg", TARGET) - 0.01
    assert g.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert radius == pytest.approx(g.pole_radius, rel=1e-9) and radius < 1.0


@pytest.mark.parametrize("name,value,accepted", [
    ("zero_ratio", 1.0, True), ("zero_ratio", 1e-9, True), ("zero_ratio", 1.0 + 1e-12, False),
    ("zero_damping", 1.0, True), ("zero_damping", 2.0, True), ("zero_damping", 2.0 + 1e-12, False),
    ("zero_damping", 1e-6, True),
])
def test_zero_ratio_and_damping_ranges(name, value, accepted):
    """範囲: 零点の比 0 < κ ≤ 1、減衰 0 < ζz ≤ 2（端の 1 と 2 は含む）。外は invalid_argument。"""
    _, err = _design(VIN, L, C, R, 100e3, **{name: value})
    assert (err is None or err.reason != "invalid_argument") == accepted


def test_pid_zeros_very_close_to_z_equal_one_are_resolved():
    """PID の零点が z = 1 にごく近い（zero_ratio 3.3e-7、ζz = 1 の重根、ωz·Ts ≈ 2e-9）: 零点を z の
    多項式の根で求めると誤差 √u ≈ 1e-8 が 1 との差を超え、2.0e-9 Hz の交差（余裕 90.00007°）を落として
    最良の候補を「目標の 90.68° は満たす」と書いた。いまは z − 1 の形で求めるので、その交差を拾い「足りない」
    と書く。期待値は、この試験で PID の差分方程式の z 変換と状態方程式を z − 1 = expm1(jωTs) の形で直接
    計算した開ループの交差（brentq）と、そこでの位相（−90° の近くなので主値でよい）。"""
    from scipy.optimize import brentq

    v_in, ind, cap, r_load, f_ctrl = (77.13396730251614, 2.115134199875259e-05, 1.2061149392104576e-05,
                                      16.567279975990026, 9094234.619934775)
    opts = {"zero_ratio": 3.299958554608792e-07, "zero_damping": 1.0, "filter_ratio": 0.0,
            "phase_margin_deg": 90.67692513462626, "f_cross_max": 26706929.13121755}
    best, err = _design(v_in, ind, cap, r_load, f_ctrl, **opts)
    assert err is not None
    ts = 1.0 / f_ctrl
    a = np.array([[0.0, -1.0 / ind], [1.0 / cap, -1.0 / (r_load * cap)]])
    ad, bd, *_ = signal.cont2discrete((a, np.array([[v_in / ind], [0.0]]), np.array([[0.0, 1.0]]),
                                       np.zeros((1, 1))), ts, method="zoh")

    def loop(w):
        x = np.expm1(1j * w * ts)
        c = best.kp + best.ki * ts * (1 + x) / x + best.kd * x / ((best.t_filter + ts) * x + ts)
        return c * np.linalg.solve(x * np.eye(2) - (ad - np.eye(2)), bd[:, 0])[1] / (1 + x)

    wc = math.exp(brentq(lambda lw: math.log(abs(loop(math.exp(lw)))), math.log(1e-14), math.log(1e-4)))
    ref = 180.0 + math.degrees(np.angle(loop(wc)))
    assert wc / (2 * math.pi) == pytest.approx(2.0054e-9, rel=1e-3) and ref < opts["phase_margin_deg"] - 0.5
    assert best.phase_margin_deg == pytest.approx(ref, abs=1e-6)
    assert "足りない" in str(err) and "は満たす）" not in str(err)


def test_close_crossings_are_merged_keeping_the_smallest_margin():
    """θ の MERGE_TOL 倍より近い交差は1つにまとめ、位相が最小（余裕が最小）の点を残す。離れた点は
    まとめない。"""
    tol = ini.MERGE_TOL
    theta = np.array([1e-3, 1e-3 * (1 + 0.5 * tol), 1e-3 * (1 + tol), 2e-3, 2e-3 * (1 + 10 * tol), 3.0])
    phase = np.array([-100.0, -120.0, -110.0, -90.0, -95.0, -170.0])
    assert ini._merge_close(theta, phase, tol) == [1, 3, 4, 5]
    assert ini._merge_close(theta, phase, 0.0) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("case", ["near-tangent", "review"])
def test_crossing_search_work_stays_small(case, monkeypatch):
    """交差の囲い込みで調べる区間の数（決定的な、手間の目安）: |L| が 1 にほぼ接する例と、近い2つの交差の
    例で 2,500 以下（最初の 2,000 区間と少し）。以前の版は |L| が 1 に近い区間を幅 1e-12 まで細かくし、ほぼ
    接する例で log|L| を約 380 万点計算した（約 1 秒）。h が単調な区間を二分法で詰めないと 4 万〜6 万区間、
    2次の展開で捨てないと約 2,700 区間になる。"""
    if case == "near-tangent":
        (v_in, ind, cap, r_load, f_ctrl), opts = NEAR_TANGENT
    else:
        (v_in, ind, cap, r_load, f_ctrl), opts = (24.0, 470e-6, 10e-6, 6.0, 15e3), {"zero_damping": 0.1,
                                                                                 "filter_ratio": 0.0}
    best, _ = _design(v_in, ind, cap, r_load, f_ctrl, **opts)
    loop = _loop_of(best, f_ctrl, v_in=v_in, inductance=ind, capacitance=cap, r_load=r_load)
    count = [0]
    original = ini._Loop._cell_tests

    def counting(self, m, half):
        count[0] += m.size
        return original(self, m, half)

    monkeypatch.setattr(ini._Loop, "_cell_tests", counting)
    assert loop.crossovers(loop.ts * 1e-4 / math.sqrt(ind * cap)).size == 3
    assert count[0] <= 2500


# ------------------------------------------------------------ 5回目の修正（E1b-review-4、E1b-verifier-4）

HUGE_R_CIRCUITS = [((24.0, 47e-6, 100e-6), 40e3), ((24.0, 22e-6, 100e-6), 100e3),
                   ((12.0, 4.7e-6, 22e-6), 1e6), ((24.0, 47e-6, 100e-6), 200e3)]


def _outcome(*args, **opts):
    best, err = _design(*args, **opts)
    return (err.reason if err else "ok"), best, err


@pytest.mark.parametrize("r_load", [1e13, 1e14, 1e15, 1e16, 1e17, 1e18, 1e20, 1e30])
@pytest.mark.parametrize("circuit", HUGE_R_CIRCUITS, ids=lambda c: f"L{c[0][1]:g}-f{c[1]:g}")
def test_huge_load_gives_the_same_design_as_its_high_q_limit(circuit, r_load):
    """R = 1e13〜1e30 Ω（Q = R·√(C/L) が 1e13〜1e30）: 制御対象は Q = 1e10 のときと見分けられないので、理由・
    余裕・ゲインは Q = 1e10 にした同じ回路と一致する（余裕 1e-6°、ゲイン相対 1e-6）。以前は Q ≳ 1e16
    で制御対象の
    極の |z|² − 1（本当は −Ts/(RC)）が固有値の丸めで正になり、極を単位円の外として位相を 360° ずらし、
    (24 V, 47 µH, 100 µF, 1e16 Ω, 40 kHz) を「位相余裕 254.8°（目標の 55° は満たす）」と書いた（真は約
    −105°）。(12 V, 4.7 µH, 22 µF, 1e17 Ω, 1 MHz) は成功するのに断った。
    この試験の別の開ループは Q が 1e9 を超えると共振を格子で分けられないので、Q = 1e8 の同じ回路の最良の
    候補で確かめる（Q を 1e8 から無限大にしても余裕の変化は 1e-5° 程度。許容差 0.01° に十分小さい）。"""
    (v_in, ind, cap), f_ctrl = circuit
    r_limit = 1e10 / math.sqrt(cap / ind)
    reason, best, err = _outcome(v_in, ind, cap, r_load, f_ctrl)
    reason0, best0, err0 = _outcome(v_in, ind, cap, r_limit, f_ctrl)
    assert reason == reason0
    assert best.phase_margin_deg == pytest.approx(best0.phase_margin_deg, abs=1e-6)
    assert best.ki == pytest.approx(best0.ki, rel=1e-6) and best.kp == pytest.approx(best0.kp, rel=1e-6)
    assert best.phase_margin_deg <= 180.0
    assert ("は満たす）" in str(err)) == ("は満たす）" in str(err0)) if err else True
    r_ref = 1e8 / math.sqrt(cap / ind)
    _, best_ref, _ = _outcome(v_in, ind, cap, r_ref, f_ctrl)
    pm, _, radius = independent_margin_and_radius(best_ref, f_ctrl, v_in=v_in, inductance=ind,
                                                  capacitance=cap, r_load=r_ref)
    assert best.phase_margin_deg == pytest.approx(pm, abs=0.01)
    assert radius == pytest.approx(best.pole_radius, rel=1e-6)
    if err is None:
        assert pm >= TARGET - 0.01 and radius < 1.0


def test_huge_load_reproductions():
    """レビュー（重大1）と検証（軽微1）の再現例: 断る2つは R = 1e12 と同じ margin_not_reached（17.3° と
    38.8°）、「満たす」と書かない。成功するはずの例は成功する。"""
    cases = (((24, 47e-6, 100e-6, 1e16, 40e3), "17.3"), ((24, 22e-6, 100e-6, 1e17, 100e3), "38.8"))
    for args, shown in cases:
        _, best, err = _outcome(*args)
        assert err.reason == "margin_not_reached" and "満たす" not in str(err)
        assert f"位相余裕 {shown}°（目標まで" in str(err)
    g = buck_voltage_mode_pid(12, 4.7e-6, 22e-6, 1e17, 1e6)
    assert g.phase_margin_deg == pytest.approx(TARGET, abs=1e-6) and g.pole_radius < 1.0


def test_plant_poles_of_a_lossless_like_lc_are_inside_the_unit_circle():
    """制御対象の極の組の |z|² − 1 は det Ad − 1 = expm1(−Ts/(R·C))（この試験の式）で与え、R が 1e30 Ω
    でも内側と
    数える（丸めの 2·Re x + |x|² では 1e-17 程度の正の値になりうる）。単位円までの距離は Ts/(2RC) に合う。"""
    f_ctrl, r_load = 100e3, 1e17
    g = buck_voltage_mode_pid(24, 47e-6, 100e-6, 6.0, f_ctrl)
    loop = _loop_of(g, f_ctrl, r_load=r_load)
    n = len(loop._zeros)
    plant = [n + i for i, x in enumerate(loop._poles) if abs(x.imag) > 1e-3]   # 制御対象の極の組（共振）
    assert len(plant) == 2 and all(loop._inside[i] for i in plant)
    ts = 1.0 / f_ctrl
    for i in plant:
        assert loop._gap[i] == pytest.approx(ts / (2 * r_load * C), rel=1e-6)


def test_radius_is_written_with_digits_that_agree_with_the_judgement():
    """半径の書き方: 安定なら 1 より小さく、不安定なら 1 より大きく見える桁まで書く（以前は 0.99999998616 を
    「1.0000（1 未満で安定）」と書いた）。1 から遠ければ小数4桁。安定の余裕が無いときは 17 桁。"""
    def text(radius):
        return str(GainDesignError("margin_not_reached", "例。", TARGET,
                                   PidGains(0.01, 100.0, 1e-6, 2.5e-6, 3e3, 60.0, radius)))

    for radius in (0.99999998616, 1 - 2 * ini.STABILITY_EPS, 0.99996, 0.97):
        m = re.search(r"閉ループ極の絶対値の最大 ([\d.]+)（1 未満で安定）", text(radius))
        assert m and float(m.group(1)) < 1.0
    assert "閉ループ極の絶対値の最大 0.9700（1 未満で安定）" in text(0.97)
    for radius in (1 + 2 * ini.STABILITY_EPS, 1.00004, 1.2):
        m = re.search(r"閉ループ極の絶対値の最大 ([\d.]+)（1 以上で不安定）", text(radius))
        assert m and float(m.group(1)) > 1.0
    assert "閉ループ極の絶対値の最大 0.99999999999999989（" in text(1 - 1.1e-16)


def test_largest_reachable_margin_is_not_rounded_up_to_the_target():
    """本文の「取れるのは最大 …°」は、目標に届かないのに届いたように丸めない（以前は 29.998° を 30.0° と
    書いた）。本文の最大と最良の候補の余裕は、この試験の別の開ループの余裕（29.998°）と合う。"""
    plant, f_ctrl = (24.0, 470e-6, 10e-6, 15.793045736999451), 15e3
    best, err = _design(*plant, f_ctrl, zero_damping=0.1653846153846154, filter_ratio=0.0,
                        phase_margin_deg=30.0)
    shown = float(re.search(r"取れるのは最大 (-?[\d.]+)°", str(err)).group(1))
    assert shown < 30.0 and shown == pytest.approx(29.998, abs=5e-4)
    pm, _, _ = independent_margin_and_radius(best, f_ctrl, inductance=470e-6, capacitance=10e-6,
                                             r_load=15.793045736999451)
    assert pm == pytest.approx(shown, abs=1e-3) and pm < 30.0 - 1e-6


@pytest.mark.parametrize("name,value", [
    ("r_load", 5e-324), ("r_load", np.float32(1e-45)), ("r_load", 1e-7), ("r_load", 2e30),
    ("r_load", 10**400), ("v_in", 5e-324), ("v_in", np.longdouble("1e-4000")), ("v_in", 2e9),
    ("inductance", 1e-13), ("inductance", 2e4),
    ("capacitance", 1e-16), ("capacitance", 2e6), ("f_ctrl", 1e-4), ("f_ctrl", 2e12), ("zero_ratio", 5e-324),
    ("zero_ratio", 1e-10), ("filter_ratio", 1e300), ("filter_ratio", 1001.0), ("phase_margin_deg", 10**400),
    ("f_cross_max", 10**400), ("f_cross_max", np.longdouble("1e-4000")),
])
def test_values_outside_the_physical_ranges_are_refused(name, value):
    """物理として意味のある範囲（ARGUMENT_RANGES）の外、倍精度にできない値（10**400）、倍精度で 0 になる値
    （np.longdouble('1e-4000')）は、ほかの例外でなく invalid_argument で断る（以前は ZeroDivisionError、
    LinAlgError、OverflowError が出た）。"""
    args = {"v_in": VIN, "inductance": L, "capacitance": C, "r_load": R, "f_ctrl": 100e3}
    kwargs = {}
    (args if name in args else kwargs)[name] = value
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(**args, **kwargs)
    assert info.value.reason == "invalid_argument" and name in str(info.value)


# 端の値と組み合わせて、負荷の Q ≥ 1e-3 と f_ctrl/f0 ≤ 1e11 に入る回路（この試験の値。
# 下の値は端の値で置き換える）
END_BASES = {
    ("r_load", 0): {"v_in": VIN, "inductance": 1e-10, "capacitance": 1e-2, "r_load": None, "f_ctrl": 1e8},
    ("inductance", 2): {"v_in": VIN, "inductance": None, "capacitance": 1e-3, "r_load": R, "f_ctrl": 100.0},
    ("capacitance", 0): {"v_in": VIN, "inductance": 1e-9, "capacitance": None, "r_load": R, "f_ctrl": 1e12},
}


@pytest.mark.parametrize("name", ["v_in", "inductance", "capacitance", "r_load", "f_ctrl", "zero_ratio",
                                  "filter_ratio"])
@pytest.mark.parametrize("end", [0, 2])
def test_values_at_the_ends_of_the_physical_ranges_are_computed(name, end):
    """範囲の端（両端を含む）の値は計算する: 結果か、invalid_argument 以外の理由の GainDesignError（最良の
    候補の値は有限）。ほかの例外や、警告（あふれ、0 割り）は出さない。ほかの値は、組み合わせの範囲（負荷の Q
    と
    f_ctrl/f0）に入るものを選ぶ（END_BASES。基準の回路のままだと、R = 1 µΩ などは Q が 1e-3 を下回る）。"""
    import warnings

    value = ini.ARGUMENT_RANGES[name][end]
    args = dict(END_BASES.get((name, end), {"v_in": VIN, "inductance": L, "capacitance": C, "r_load": R,
                                             "f_ctrl": 100e3}))
    kwargs = {}
    (args if name in args else kwargs)[name] = value
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        g, err = _design(**args, **kwargs)
    assert err is None or err.reason != "invalid_argument"
    if g is not None:
        assert all(math.isfinite(x) for x in (g.kp, g.ki, g.kd, g.phase_margin_deg, g.pole_radius))


def test_search_uses_the_same_meets_judgement_as_acceptance():
    """交差周波数の探索でも「目標を満たす」は受け入れと同じ判定（目標 − TARGET_TOL_DEG 以上）: 探索の格子で
    取れる最大の余裕が目標を 0.5e-6° だけ下回るときは margin_not_reached にしない（最良の候補を「満たす」と
    書きながら「取れる交差周波数が無い」と書く食い違いを作らない）。2e-6° 下回るときは margin_not_reached で、
    本文の最大は目標より小さく書く。格子の最大は、実装の部品（形の開ループと格子）で求める（境目を作るため）。"""
    v_in, ind, cap, r_load, f_ctrl = 24.0, 470e-6, 10e-6, 15.793045736999451, 15e3
    zeta, ratio, kappa = 0.1653846153846154, 0.0, 0.5
    ts, w0 = 1.0 / f_ctrl, 1.0 / math.sqrt(ind * cap)
    wz = kappa * w0
    kp1, kd1 = ini._shape_gains(1.0, wz, zeta, 0.0)
    shape = ini._Loop(*ini._loop_factors(kp1, 1.0, kd1, 0.0, v_in, ind, cap, r_load, ts), ts, 1e-3 * wz)
    grid = np.geomspace(1.05 * w0, 0.5 * math.pi / ts, ini.GRID_POINTS)
    vmax = float(np.max(180.0 + shape.phase_deg(grid)))
    _, err = _design(v_in, ind, cap, r_load, f_ctrl, zero_damping=zeta, filter_ratio=ratio,
                     phase_margin_deg=vmax + 0.5 * ini.TARGET_TOL_DEG)
    assert err is None or err.reason != "margin_not_reached"
    _, err = _design(v_in, ind, cap, r_load, f_ctrl, zero_damping=zeta, filter_ratio=ratio,
                     phase_margin_deg=vmax + 2 * ini.TARGET_TOL_DEG)
    assert err.reason == "margin_not_reached"
    shown = float(re.search(r"取れるのは最大 (-?[\d.]+)°", str(err)).group(1))
    assert shown < vmax + 2 * ini.TARGET_TOL_DEG and "は満たす）" not in str(err)


# ---------------------------------------------- 6回目の修正（E1b-verifier-5、E1b-review-5、D-062）
#
# 期待値の作り方（実装の関数を使わない）:
#   - 150 桁・60 桁の参照の値（REF150、PLANT60）。作業ツリーの外で mpmath を使って求め、数字で書いた
#     （制御対象は 3×3 の拡大行列の指数関数、交差は |L|² − 1 の多項式の根、連続の位相は根ごとの偏角の和、
#     閉ループは
#     特性多項式の根。検証担当の 5 回目の参照 mpref.py と同じもの）。
#   - 独立な半径: 正規化した 6×6 の状態遷移行列の固有値（_independent_radius_minus_one。制御対象は scipy の
#     行列指数で組み、実装の解析式とも特性多項式とも別の経路）。宣言した範囲の 13,824 の候補で 150
#     桁の値との差は
#     最大 1.0e-10（臨界減衰 Q = 0.5 で、PID の零点の重根が制御対象の極の重根に重なり、固有値が集まるとき。
#     ほかは
#     4e-11 以下）だったので、許容は INDEPENDENT_TOL = 3e-10（安定の余白 ε = 1e-9 の 1/3。前の版の誤り 2.7e-9
#     は
#     見分ける）。
#   - 宣言した範囲（DECLARED）は、この修正で決めた値を書き写した（実装の ARGUMENT_RANGES を読まない）。
# 許容差: 150 桁の値との一致は、余裕 1e-7°（判定の余白 1e-6° の 1/10。REF150 の例で測った差は 1e-9° 以下）、
# 半径 − 1 は 1e-12（ε の 1/1000。測った差は 1e-13 以下）。

DECLARED = {"v_in": (1e-6, 1e9), "inductance": (1e-12, 1e4), "capacitance": (1e-15, 1e6),
            "r_load": (1e-6, 1e30), "f_ctrl": (1e-3, 1e12)}
DECLARED_Q_MIN, DECLARED_F_RATIO_MAX = 1e-3, 1e11
EPS = 1e-9
INDEPENDENT_TOL = 3e-10


def _independent_radius_minus_one(g, v_in, ind, cap, r_load, f_ctrl):
    """閉ループの極の半径 − 1。状態 [iL·√(L/C), vC, 前回の指令, I, D, 前回の偏差]、時間は Ts 単位、Vin = 1 に
    正規化した 6×6 の状態遷移行列の固有値（尺度がそろうので、Vin や Ts の大きさで丸めが増えない）。"""
    ts = 1.0 / f_ctrl
    a = ts / math.sqrt(ind * cap)
    q = 0.0 if math.isinf(r_load) else math.sqrt(ind / cap) / r_load
    from scipy.linalg import expm

    e3 = expm(np.array([[0.0, -a, a], [a, -a * q, 0.0], [0.0, 0.0, 0.0]]))
    kp, ki, kd, r = g.kp * v_in, g.ki * v_in * ts, g.kd * v_in / ts, g.t_filter / ts
    alpha, beta = r / (r + 1.0), kd / (r + 1.0)
    m = np.zeros((6, 6))
    m[0:2, 0:2], m[0:2, 2] = e3[:2, :2], e3[:2, 2]
    m[3, 1], m[3, 3] = -ki, 1.0                                  # I[k] = I[k−1] + Ki·e[k]、e = −vC
    m[4, 1], m[4, 4], m[4, 5] = -beta, alpha, -beta              # D[k] = α·D[k−1] + β·(e[k] − e[k−1])
    m[5, 1] = -1.0                                               # 前回の偏差
    m[2] = -kp * np.eye(6)[1] + m[3] + m[4]                      # u = Kp·e + I + D（次の周期に使う）
    return float(np.max(np.abs(np.linalg.eigvals(m)))) - 1.0


# (a, q, p1, p0, b1): 正規化した制御対象（A' = a·[[0, −1], [1, −q]]、B' = [a, 0]）の零次ホールドの 60 桁の値。
# p1 = −tr Δ、p0 = det Δ（Δ = Ad − I）、b1 = Bd の2番目。無損失、臨界減衰（q = 2）、その近く、硬い（q = 1e3）
PLANT60 = [
    (1e-11, 0.0, 9.9999999999999988e-23, 9.9999999999999988e-23, 4.9999999999999994e-23),
    (1e-11, 1000.0, 9.9999999500000996e-9, 9.999999949999999e-23, 4.9999999833333328e-23),
    (1e-06, 1e-20, 9.9999999999992658e-13, 9.9999999999991658e-13, 4.9999999999995829e-13),
    (0.03, 2.0, 0.059108932902983644, 0.00087346648723235561, 0.00044110044503657773),
    (0.7, 0.001, 0.47085080858123918, 0.47015105352408251, 0.23510340815856976),
    (1.3, 2.0, 1.4549364139319748, 0.52920999214630869, 0.37317687602177103),
    (2.0, 2.04, 1.7186454396781392, 0.73555290533084443, 0.58686190362624879),
    (2.9, 1.5, 2.077351076276052, 1.0902578888565319, 0.91755037384686472),
    (2.9, 1000.0, 1.002895801953496, 0.0028958019534959993, 0.0028948048463066302),
    (0.1, 1000.0, 1.0000999951001569, 9.9995100156862981e-5, 9.8995197152253124e-5),
    (0.0001, 1000.0, 0.09516259148029862, 9.5162581884751498e-9, 4.8374180319554851e-9),
    (2.9, 1e-30, 3.941916330299181, 3.941916330299181, 1.9709581651495905),
]


@pytest.mark.parametrize("case", PLANT60, ids=lambda c: f"a{c[0]:g}-q{c[1]:g}")
def test_normalized_plant_agrees_with_60_digit_values(case):
    """制御対象の零次ホールドの量（解析式）が 60 桁の値と相対 1e-13 で合う（無損失、臨界減衰とその近く、
    宣言した範囲で最も硬い q = 1e3）。以前の行列指数で組む形は、q = 1e9（範囲の外になった）で分子の相対誤差が
    1e-9 だった。"""
    a, q, p1, p0, b1 = case
    plant = ini._plant_n(a, q)
    assert plant.p1 == pytest.approx(p1, rel=1e-13)
    assert plant.p0 == pytest.approx(p0, rel=1e-13)
    assert plant.b1 == pytest.approx(b1, rel=1e-13)
    assert -complex(np.sum(plant.poles)).real == pytest.approx(p1, rel=1e-13)        # 極は分母の根
    assert complex(np.prod(plant.poles)).real == pytest.approx(p0, rel=1e-13)


@pytest.mark.parametrize("a", [1e-12, 1e-6, 0.01, 0.4, 1.0, 1.7, 2.99])
def test_normalized_plant_closed_forms(a):
    """閉じた形: 無損失（q = 0）は極 e^{±ja} − 1、p1 = p0 = 4·sin²(a/2)、b1 = 1 − cos a = 2·sin²(a/2)、|z|² −
    1 = 0
    （円の上。無負荷の極限）。臨界減衰（q = 2）は2つの極が expm1(−a)、b1 = 1 − e^{−a}·(1 + a)（a
    が小さいときは
    級数 Σ (−1)^n·(n − 1)·aⁿ/n!）。以前は極を固有値で求めたので、臨界減衰で極の相対誤差が 1e-8（重根の
    固有値の √u）だった。"""
    s2 = math.sin(0.5 * a) ** 2
    lossless = ini._plant_n(a, 0.0)
    assert lossless.p1 == pytest.approx(4 * s2, rel=1e-14) and lossless.p0 == pytest.approx(4 * s2, rel=1e-14)
    assert lossless.b1 == pytest.approx(2 * s2, rel=1e-14)
    assert np.all(lossless.excess == 0.0)
    critical = ini._plant_n(a, 2.0)
    assert np.allclose(critical.poles, math.expm1(-a), rtol=1e-14, atol=0.0)
    if a < 0.5:
        b1 = sum((-1) ** n * (n - 1) * a**n / math.factorial(n) for n in range(2, 30))
    else:
        b1 = 1.0 - math.exp(-a) * (1.0 + a)
    assert critical.b1 == pytest.approx(b1, rel=1e-13)


def _says(err):
    """最良の候補の文の（満たす、安定の言い方）。"""
    text = str(err)
    stab = [k for k, v in (("stable", "（1 未満で安定）"), ("no_margin", "安定の余裕が無い）"),
                           ("unstable", "（1 以上で不安定）")) if v in text]
    return "は満たす）" in text, stab


def _check_sentence_numbers(err, target):
    """文の中で比べる2つの数字（余裕と目標、最大の余裕と目標、半径と 1）が、判定と食い違って見えないこと。
    判定は実装の値と、受け入れの決まり（目標 − 1e-6° で満たす、1 − 1e-9 以下で安定）
    から（実装の関数を使わない）。"""
    text = str(err)
    best = err.best
    m = re.search(r"位相余裕 (-?[\d.]+)° を取れるゲイン交差周波数が無い（取れるのは最大 (-?[\d.]+)°）", text)
    if m:
        assert float(m.group(1)) == target and float(m.group(2)) < float(m.group(1))
    if best is None:
        return
    meets = best.phase_margin_deg >= target - 1e-6
    m = re.search(r"位相余裕 (-?[\d.]+)°（目標の (-?[\d.]+)° は満たす）", text)
    assert (m is not None) == meets
    if m:
        assert float(m.group(1)) >= float(m.group(2)) and float(m.group(2)) == target
    m = re.search(r"位相余裕 (-?[\d.]+)°（目標まで (-?[\d.]+)° 足りない）", text)
    assert (m is not None) == (not meets)
    if m:
        assert float(m.group(1)) < target and float(m.group(2)) > 0.0
    r1 = best.pole_radius - 1.0
    expected = "stable" if r1 <= -EPS else ("unstable" if r1 >= EPS else "no_margin")
    assert _says(err)[1] == [expected]
    for pattern, below in ((r"閉ループ極の絶対値の最大 ([\d.]+)（1 未満で安定）", True),
                           (r"閉ループ極の絶対値の最大 ([\d.]+)（1 以上で不安定）", False),
                           (r"閉ループ極の絶対値の最大が ([\d.]+)（1 以上）", False)):
        m = re.search(pattern, text)
        if m:
            assert (float(m.group(1)) < 1.0) if below else (float(m.group(1)) > 1.0)


# 150 桁の参照の値: (回路 (Vin, L, C, R, f_ctrl)、設計の引数、理由、参照の余裕 [deg]、参照の半径 − 1)。
# 参照の余裕は、すべての交差の、連続の位相の余裕と −1 からの角度の小さいほうの最小。
# 前半: 検証担当の再現例（Vin が大きい、重大1）、基準の回路、レビューの軽1の回路（f_ctrl/f0 = 3e9。
# ζz = 1e-9 は D-066 で範囲の外になったので、下限の 1e-6 にした）。
# 後半: 宣言した範囲の端と端どうしの組み合わせ（a = ω0·Ts の両端、Q の下限、無負荷、ζz = 1e-6（下限。D-066）、
# r = 1000、κ = 1e-9、目標の両端、回路の値の端）と、|L| が平らな領域とナイキストのすぐ下を狙った例から、
# 理由と端ごとに選んだ。
# 判定の境目の近く（半径 − 1 が ±1e-9 から 1e-11 以内、余裕が目標 − 1e-6° から 1e-8° 以内）
# の例は選んでいない。
REF150 = [
    ((412169.5762239939, 0.16160871635527527, 0.4632826986189658, 1e+30, 22803.275120932136),
     {"zero_ratio": 5.316531590111157e-07, "zero_damping": 0.814948799243281},
     "no_stability_margin", 55.000000000000014, -7.818108891605754e-14),
    ((82888.19000954041, 7.687175724559705e-08, 232.59255161739404, 30806.96514674552, 1060587.972794125),
     {"zero_ratio": 3.5986891371643046e-05, "zero_damping": 1.84838158173341},
     "no_stability_margin", 55.00000000000001, -3.083172862631542e-10),
    ((200970.73372559165, 1.9916952286041042e-07, 18.39576101718791,
      178.35381990409903, 6190295.795299207),
     {"zero_ratio": 1.0137587406690042e-08, "zero_damping": 0.4440977980145964},
     "no_stability_margin", 54.99999999999997, -2.8467611020230464e-17),
    ((894691.6551502685, 0.017291779018258607, 0.0005703546193605544,
      26747.439184322197, 2206187.1073270487),
     {"zero_ratio": 2.206924647994628e-08, "zero_damping": 0.2352991152860896},
     "no_stability_margin", 54.999999999999986, -1.34915233304758e-16),
    ((1000000000.0, 1e-12, 1000000.0, 6.0, 1000000000000.0),
     {"zero_ratio": 1.0, "zero_damping": 2.0, "filter_ratio": 0.0},
     "no_stability_margin", 54.99999999999999, -2.6794919209000866e-10),
    ((16286220.011097308, 0.0003486222953146516, 0.03046573234076631,
      36.301777798882036, 3831461.8219012544),
     {"zero_ratio": 1.1490533679001927e-05, "zero_damping": 1.099325229684776},
     "no_stability_margin", 55.00000000000002, -3.3680109612994295e-11),
    ((24.0, 4.7e-05, 0.0001, 6.0, 100000.0),
     {},
     "ok", 55.0, -0.03310946848420156),
    ((24.0, 4.7e-05, 0.0001, 6.0, 20000.0),
     {},
     "margin_not_reached", 17.576013834941776, -0.012103761191206788),
    ((24.0, 0.001, 0.1, 1e+20, 47746482927.568596),
     {"zero_ratio": 1.0, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "no_stability_margin", 55.00000000000002, -2.0965883478156823e-15),
    ((1188622.2515277644, 1.5950623799130652, 91.75693517390556,
      0.0001318467371285491, 1315563592.2933636),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "no_stability_margin", 55.000000000000036, -6.412444274340045e-21),
    ((3.343592045621104, 487.55977079403755, 3.090484724311343e-06, 12.560317899412732, 410008536438.01),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "no_stability_margin", 55.000000000000014, -8.522138027643618e-22),
    ((24173.1985763023, 220.18762382660734, 99825.92929568484, 4.696504810175176e-05, 3394704.6741435593),
     {"zero_ratio": 1e-09, "zero_damping": 1.0, "filter_ratio": 1000.0},
     "no_stability_margin", 55.00000000000001, -8.297026098000123e-22),
    ((716978625.0788454, 5400.423900439963, 1.8942095975565048, 0.053394916161858104, 157359218.45742017),
     {"zero_ratio": 0.5, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "no_stability_margin", 55.00000000000005, -3.142122288987488e-17),
    ((181.20682891490014, 4131.786975631857, 844677.357014965, 6.993966293941741e+18, 269405.05743948556),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "no_stability_margin", 55.0, -6.412447776677569e-21),
    ((158428.15556461102, 2387.0039962019323, 407630.99240345095,
      7.652317394522004e+18, 510222.91599968565),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "no_stability_margin", 55.0, -8.519973580482071e-22),
    ((4653370.591165951, 0.0016818268092500634, 881349.5509980618, math.inf, 413385655.29149884),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "no_stability_margin", 55.0, -6.412447776677568e-21),
    ((3.0508367775430376e-05, 3.910879447206543e-09, 495699.8721118126, math.inf, 361471044219.04596),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "no_stability_margin", 54.99999999999999, -8.519973580482071e-22),
    ((220404158.0738578, 4.064190075904287, 2.3363976810014724, 0.0013189053267176664, 324518.613077635),
     {"zero_ratio": 0.5, "zero_damping": 0.3, "filter_ratio": 0.25},
     "ok", 54.999999999999986, -1.4946536815700278e-07),
    ((69417156.2426336, 494.1768497444666, 84.09392407093355, 0.002424146649475405, 4905.423333218625),
     {"zero_ratio": 0.5, "zero_damping": 1.0, "filter_ratio": 1000.0},
     "ok", 55.00000000000002, -4.118289011906388e-07),
    ((0.08511069099102997, 0.09102474741785996, 3769.411315924075,
      4.914088529822073e+17, 53986.29130233523),
     {"zero_ratio": 0.5, "zero_damping": 0.3, "filter_ratio": 0.25},
     "ok", 55.000000000000014, -1.5000160744836342e-07),
    ((0.0042665140909394494, 0.00028678855805998045, 1.947371563517915e-06, math.inf, 42315032409.15526),
     {"zero_ratio": 0.5, "zero_damping": 0.3, "filter_ratio": 0.25},
     "ok", 55.000000000000014, -1.500016074483634e-07),
    ((2.8085833849362185, 4813.697540903106, 765448.6957613723,
      7.930149151695328e-05, 0.016474132585820107),
     {"zero_ratio": 0.0001, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "ok", 54.99999999999988, -1.3667676944334035e-08),
    ((0.006600127850630996, 33.242114632795094, 10519.731856464981,
      0.028106838014315735, 1.691037909278302),
     {"zero_ratio": 1.0, "zero_damping": 1.0, "filter_ratio": 1000.0},
     "margin_not_reached", 43.52688391122837, -0.000498982412733893),
    ((749693583.9845914, 0.00010498108274308515, 85528.86842578971,
      35.034761124306876, 333.7245169212596),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "margin_not_reached", 43.498859261088604, -1.4158473585603324e-22),
    ((84829936.21208356, 5814.318975985031, 10145.081812525737,
      7.570449207992271e+19, 0.13020354127904937),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "margin_not_reached", 43.49827232871313, -1.4158473584860386e-22),
    ((598498.8113567875, 7.876784836309975e-07, 274.6600398184853, math.inf, 67987.29612184752),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 1000.0},
     "margin_not_reached", 43.498272328713135, -1.4158473584860384e-22),
    ((0.0034723160917744652, 0.060535289470146386, 0.00019747998445216277,
      0.017508251221291342, 2892.238787411467),
     {"zero_ratio": 0.5, "zero_damping": 1.0, "filter_ratio": 1000.0},
     "margin_not_reached", 46.653980439697094, -0.034986992040549184),
    ((2.0731398684082233e-05, 4.442973958468772e-11, 1.776678743760363e-13,
      0.01581366122251705, 119038507914.50056),
     {"zero_ratio": 1e-09, "zero_damping": 1e-06, "filter_ratio": 0.25, "f_cross_max": 1e+300},
     "margin_not_reached", -179.78013336602868, 0.09549396486848614),
    ((1188622.2515277644, 1.5950623799130652, 91.75693517390556,
      0.0001318467371285491, 1315563592.2933636),
     {"phase_margin_deg": 1e-06},
     "no_stability_margin", 1.0000000510805073e-06, -9.424777217752154e-12),
    ((3.343592045621104, 487.55977079403755, 3.090484724311343e-06, 12.560317899412732, 410008536438.01),
     {"phase_margin_deg": 179.99},
     "margin_not_reached", 50.368908108258125, -4.728471589848645e-12),
    ((0.05191578877365984, 761.7689912500842, 1.73022627827477e-15, 663529.4346499054, 871037600992.883),
     {"phase_margin_deg": 1e-06},
     "ok", 9.999999919160168e-07, -3.947365728175925e-09),
    ((1e-06, 1e-12, 0.0001, 1e-06, 1000000000000.0),
     {},
     "ok", 55.000000000000014, -1.4520598028586153e-05),
    ((1e-06, 1e-12, 0.0001, 1e-06, 1000000000000.0),
     {"zero_ratio": 1e-09, "zero_damping": 2.0, "filter_ratio": 1000.0},
     "no_stability_margin", 55.000000000000014, -1.1514488592899077e-19),
    ((1e-06, 1e-12, 0.0001, 1e-06, 1000000000000.0),
     {"zero_ratio": 1.0, "zero_damping": 1e-06, "filter_ratio": 0.0},
     "margin_below_target", 0.1983538162798843, -5.105147309526943e-09),
    ((1e-06, 1e-12, 1000000.0, 1e-06, 200000.0),
     {"zero_ratio": 1e-09, "zero_damping": 2.0, "filter_ratio": 1000.0},
     "margin_not_reached", 10.914717898136072, -2.6099694507992773e-21),
    ((14650.156387032057, 1900.9847158383923, 2.941555909885684e-14,
      834501.0902244574, 1770160.2062951662),
     {"zero_ratio": 0.006085379077864706, "zero_damping": 0.36651839333185526,
      "filter_ratio": 4.489325387107757e-06, "phase_margin_deg": 166.6297224821777},
     "margin_below_target", 87.71372129275512, -0.0001462308192038239),
    ((25880246.647817776, 0.3881741787536656, 3.1578585967107175e-10,
      59.320936543853435, 310379.2972930506),
     {"zero_ratio": 0.04629921975720888, "zero_damping": 1.5919450612319073,
      "filter_ratio": 6.6586003425169525e-06, "phase_margin_deg": 79.35201584020815},
     "ok", 79.3520158402157, -0.002237411461532888),
    ((231705.41807498576, 2.2157653023789888e-10, 8.792124119070602e-14,
      0.05434742656460506, 433403808407.0672),
     {"zero_ratio": 9.786222319517828e-08, "zero_damping": 1.5699099125950853, "filter_ratio": 0.0,
      "phase_margin_deg": 26.15475256972286},
     "no_stability_margin", 26.154752569715072, -4.630579378689185e-12),
    ((155600340.74114728, 0.00026165855476572105, 2.7706059293250264e-15,
      326.71811280920247, 13876854748.938286),
     {"zero_ratio": 4.532722574158798e-05, "zero_damping": 0.05114749347085543, "filter_ratio": 0.0,
      "phase_margin_deg": 153.88351328582644},
     "margin_below_target", 90.1459629605247, -1.6349350222865497e-07),
    ((0.03209512655268457, 1.4126797289321341e-12, 2.595467838754901e-08,
      1.8899795479006908e-05, 6491013790.9143505),
     {"zero_ratio": 0.27310109921527076, "zero_damping": 0.21818038427030195, "filter_ratio": 0.0,
      "phase_margin_deg": 5.621306875567512},
     "unstable", 5.621306875567776, 0.0004626185015735746),
    ((93615.55344896228, 110.81818131877579, 3402.651015511974,
      0.0001839797752703878, 0.0018936559330423645),
     {"zero_ratio": 0.0026573144064233824, "zero_damping": 1.9583906416670107, "filter_ratio": 0.0,
      "phase_margin_deg": 65.64110971280218},
     "ok", 65.64110971284369, -0.00033268725572665775),
    ((0.011131255954057787, 0.22108036924859567, 3.995947130375682e-12,
      4494.271454985592, 20470817.590788193),
     {"zero_ratio": 0.5600916647461457, "zero_damping": 0.8629304828988834, "filter_ratio": 0.0,
      "phase_margin_deg": 141.08139007322671},
     "margin_not_reached", 141.08128216843338, -0.012640460797338217),
    ((30.704330169254103, 2.857118795527659e-12, 6.423781607222391e-08, math.inf, 838642259.8156372),
     {"zero_ratio": 2.2539972182140105e-05, "filter_ratio": 0.0, "phase_margin_deg": 0.19184428475680354,
      "f_cross_max": 1e+300},
     "margin_not_reached", -244.89279574206202, -3.7016717074679616e-10),
    ((0.0034613636558734244, 530.4443958680124, 91.55295328251589,
      0.010796096301016559, 0.0028733413218780934),
     {"zero_ratio": 0.9053034869956731, "zero_damping": 1e-06, "filter_ratio": 0.0, "f_cross_max": 1e+300,
      "phase_margin_deg": 0.020476819561526385},
     "margin_not_reached", -50.86684777294839, 0.46367753145966784),
    ((2942.427691786271, 4.3252262198520104e-09, 1.935063497676644e-13,
      2721.43233165733, 13184163167.511265),
     {"zero_ratio": 1.0781482142226897e-09, "filter_ratio": 1000.0, "f_cross_max": 1e+300,
      "phase_margin_deg": 0.004229843476651757},
     "margin_not_reached", -180.25155846914384, 1.0913558384974087),
    ((23736.835331005146, 4.3635645746592194e-07, 2.8531190144951185e-13,
      1.2449264379744105, 1764882196.4006615),
     {"zero_ratio": 0.024865908723994773, "zero_damping": 0.5574890480304739, "filter_ratio": 0.0,
      "phase_margin_deg": 25.30464686775412, "f_cross_max": 1e+300},
     "margin_not_reached", -14.362557074995918, 2.4308826923159953e-05),
    ((0.00031765683712195176, 0.0049617643771035285, 5.3963064103293066e-06,
      0.3239899858413193, 94124.60462983768),
     {"zero_ratio": 0.2777023037794482, "zero_damping": 0.468972742226965, "filter_ratio": 650.2373744022118,
      "phase_margin_deg": 55.55971626441401},
     "negative_gain", 55.55971626441378, -0.008392270891070518),
]


def _ref_id(case):
    (v_in, _, _, r_load, f_ctrl), _, reason = case[0], case[1], case[2]
    return f"Vin{v_in:.2g}-R{r_load:.2g}-f{f_ctrl:.2g}-{reason}"


@pytest.mark.parametrize("case", REF150, ids=_ref_id)
def test_designs_agree_with_150_digit_reference(case):
    """宣言した範囲の端と再現例: 理由、余裕、半径が 150 桁の参照と合い、文の数字が判定と合う。返すなら参照で
    余裕が目標 − 2e-6° 以上、半径が 1 − 1e-9 以下。"""
    plant, opts, expected, ref_pm, ref_r1 = case
    target = opts.get("phase_margin_deg", 55.0)
    g, err = _design(*plant, **opts)
    assert (err.reason if err else "ok") == expected
    assert g.phase_margin_deg == pytest.approx(ref_pm, abs=1e-7)
    assert g.pole_radius - 1.0 == pytest.approx(ref_r1, abs=1e-12)
    assert _independent_radius_minus_one(g, *plant) == pytest.approx(ref_r1, abs=INDEPENDENT_TOL)
    if err is None:
        assert ref_pm >= target - 2e-6 and ref_r1 <= -EPS and g.kp >= 0.0 and g.kd >= 0.0
    else:
        _check_sentence_numbers(err, target)


# 検証担当の 5 回目の再現例（重大1）: Vin が大きいと、物理の単位のまま組んだ 6×6 の行列の尺度がそろわず、
# 半径の
# 丸めが 2.7e-9 になって、安定の余裕が無い設計を成功として返した。同じ回路を Vin = 24 V
# にしたものと同じ結果になる
HIGH_VIN = [
    ((412169.5762239939, 0.16160871635527527, 0.4632826986189658, 1e30, 22803.275120932136),
     {"zero_ratio": 5.316531590111157e-07, "zero_damping": 0.814948799243281}),
    ((82888.19000954041, 7.687175724559705e-08, 232.59255161739404,
      30806.96514674552, 1060587.972794125),
     {"zero_ratio": 3.5986891371643046e-05, "zero_damping": 1.84838158173341}),
    ((1e9, 1e-12, 1e6, 6.0, 1e12), {"zero_ratio": 1.0, "zero_damping": 2.0, "filter_ratio": 0.0}),
    ((16286220.011097308, 0.0003486222953146516, 0.03046573234076631,
      36.301777798882036, 3831461.8219012544),
     {"zero_ratio": 1.1490533679001927e-05, "zero_damping": 1.099325229684776}),
]


@pytest.mark.parametrize("case", HIGH_VIN, ids=lambda c: f"Vin{c[0][0]:.3g}")
@pytest.mark.parametrize("v_other", [1e-6, 24.0, 1e9])
def test_design_depends_on_vin_only_through_the_gain_scale(case, v_other):
    """ループは Vin·Kp、Vin·Ki、Vin·Kd だけで決まる。設計は Vin = 1 に正規化して行うので、Vin を変えても理由・
    余裕・半径・交差周波数はまったく同じで、ゲインは Vin に反比例する（相対 1e-15）。"""
    plant, opts = case
    g, err = _design(*plant, **opts)
    g2, err2 = _design(v_other, *plant[1:], **opts)
    assert (err.reason if err else "ok") == (err2.reason if err2 else "ok") == "no_stability_margin"
    assert (g.phase_margin_deg, g.pole_radius, g.f_cross) == (g2.phase_margin_deg, g2.pole_radius, g2.f_cross)
    for k in ("kp", "ki", "kd"):
        assert getattr(g, k) * plant[0] == pytest.approx(getattr(g2, k) * v_other, rel=1e-15)
    assert abs(_independent_radius_minus_one(g, *plant)) < EPS


@pytest.mark.parametrize("scale", [1e-3, 1e4, 1e6])
def test_design_is_unchanged_by_the_time_scale(scale):
    """(f_ctrl, L, C) を (s·f_ctrl, L/s, C/s) にすると a = ω0·Ts と Q は変わらない: 理由・余裕・半径は同じ
    （a の丸めの分だけ。余裕 1e-9°、半径 − 1 は相対 1e-9）、Ki は s 倍、Kd は 1/s 倍、交差周波数は s 倍。"""
    for (v_in, ind, cap, r_load, f_ctrl), opts in (((VIN, L, C, R, 100e3), {}),
                                                   ((VIN, 4.7e-6, 1e-3, 1000.0, 1e6), {"zero_ratio": 1e-4})):
        g, err = _design(v_in, ind, cap, r_load, f_ctrl, **opts)
        g2, err2 = _design(v_in, ind / scale, cap / scale, r_load, f_ctrl * scale, **opts)
        assert err is None and err2 is None
        assert g2.phase_margin_deg == pytest.approx(g.phase_margin_deg, abs=1e-9)
        assert g2.pole_radius - 1.0 == pytest.approx(g.pole_radius - 1.0, rel=1e-9)
        assert g2.ki == pytest.approx(g.ki * scale, rel=1e-9)
        assert g2.kd == pytest.approx(g.kd / scale, rel=1e-9)
        assert g2.kp == pytest.approx(g.kp, rel=1e-9)
        assert g2.f_cross == pytest.approx(g.f_cross * scale, rel=1e-9)


def _log_uniform(rng, lo, hi):
    return float(10 ** rng.uniform(math.log10(lo), math.log10(hi)))


def _declared_range_cases(n, seed):
    """宣言した範囲の全体から、無次元の量（a = ω0·Ts、Q、κ、ζz、r、目標、探索の上端）を対数で一様に選び、
    回路の値（Vin、f_ctrl、L、C、R）を1つずつの範囲の中から、その a と Q になるように選ぶ。"""
    rng = np.random.default_rng(seed)
    a_min = 2.0 * math.pi / DECLARED_F_RATIO_MAX
    out = []
    while len(out) < n:
        a = _log_uniform(rng, a_min, 3.0)
        u = rng.random()
        q_load = math.inf if u < 0.1 else _log_uniform(rng, DECLARED_Q_MIN, 1.0 if u < 0.4 else 1e35)
        v_in, f_ctrl = _log_uniform(rng, *DECLARED["v_in"]), _log_uniform(rng, *DECLARED["f_ctrl"])
        lc = (1.0 / f_ctrl / a) ** 2
        lo = max(DECLARED["inductance"][0], lc / DECLARED["capacitance"][1])
        hi = min(DECLARED["inductance"][1], lc / DECLARED["capacitance"][0])
        if lo > hi:
            continue
        ind = _log_uniform(rng, lo, hi)
        cap = lc / ind
        r_load = math.inf if math.isinf(q_load) else q_load * math.sqrt(ind / cap)
        if not (DECLARED["r_load"][0] <= r_load <= DECLARED["r_load"][1] or math.isinf(r_load)):
            continue
        kappa = _log_uniform(rng, 1e-9, 1.0) if rng.random() < 0.7 else float(rng.choice([0.5, 1.0]))
        zeta = float(rng.uniform(1e-3, 2.0)) if rng.random() < 0.8 else float(rng.choice([1.0, 2.0]))
        opts = {"zero_ratio": kappa, "zero_damping": zeta,
                "filter_ratio": float(rng.choice([0.0, 0.25, 1.0, 10.0, 1000.0])),
                "phase_margin_deg": float(rng.uniform(5.0, 120.0)) if rng.random() < 0.7 else 55.0}
        if rng.random() < 0.15:
            opts["f_cross_max"] = float(f_ctrl * 10 ** rng.uniform(-3, 0.5))
        out.append(((v_in, ind, cap, r_load, f_ctrl), opts))
    return out


def test_declared_range_sweep_has_no_wrong_result_or_sentence():
    """宣言した範囲の全体（対数で一様な 150 通り）: GainDesignError のほかの例外を出さず、
    範囲の外として断らない。
    返したゲインも最良の候補も、半径が独立な 6×6 の固有値と 3e-10 で合い（返すなら 1 − 1e-9 以下）、
    文の数字が判定と
    合う。同じ回路を別の Vin（1つずつの範囲の反対の端の近く）にしても、理由・余裕・半径がまったく同じ。"""
    results = {}
    for plant, opts in _declared_range_cases(150, 62):
        target = opts["phase_margin_deg"]
        g, err = _design(*plant, **opts)
        reason = err.reason if err else "ok"
        results[reason] = results.get(reason, 0) + 1
        assert reason != "invalid_argument"
        if g is not None:
            indep = _independent_radius_minus_one(g, *plant)
            assert g.pole_radius - 1.0 == pytest.approx(indep, abs=INDEPENDENT_TOL)
            if err is None:
                assert indep <= -EPS + INDEPENDENT_TOL and g.phase_margin_deg >= target - 1e-6
        if err is not None:
            _check_sentence_numbers(err, target)
        v_other = 1e-3 if plant[0] > 1e2 else 1e8
        g2, err2 = _design(v_other, *plant[1:], **opts)
        assert (err2.reason if err2 else "ok") == reason
        if g is not None:
            assert (g2.phase_margin_deg, g2.pole_radius) == (g.phase_margin_deg, g.pole_radius)
    assert results.get("ok", 0) >= 20 and results.get("no_stability_margin", 0) >= 20      # 両方の側を通る


@pytest.mark.parametrize("name,plant,accepted", [
    ("Q", (VIN, L, C, 1e-3 * math.sqrt(L / C) * (1 + 1e-12), 100e3), True),
    ("Q", (VIN, L, C, 1e-3 * math.sqrt(L / C) * (1 - 1e-9), 100e3), False),
    ("Q", (VIN, 1.0, 1e-15, 1e-3, 1e9), False),                   # 1つずつは範囲の中、Q = 3e-11
    ("ratio", (VIN, 1.0, 1.0, R, 1e11 / (2 * math.pi) * (1 - 1e-12)), True),
    ("ratio", (VIN, 1.0, 1.0, R, 1e11 / (2 * math.pi) * (1 + 1e-9)), False),
    ("ratio", (VIN, 1e4, 1e6, 1e30, 1e12), False),                  # 1つずつは範囲の中、f_ctrl/f0 = 6e17
])
def test_combination_ranges(name, plant, accepted):
    """組み合わせで効く量の範囲: 負荷の Q = R·√(C/L) ≥ 1e-3、f_ctrl/f0 = 2π·f_ctrl·√(LC) ≤ 1e11。外は
    invalid_argument で、文に量と引数の名前を書く。端の内側は計算する。"""
    _, err = _design(*plant)
    if accepted:
        assert err is None or err.reason != "invalid_argument"
    else:
        assert err.reason == "invalid_argument" and err.best is None
        text = str(err)
        assert "組み合わせが範囲の外" in text
        assert ("負荷の Q = r_load·√(capacitance/inductance)" in text) == (name == "Q")
        assert ("f_ctrl/f0 = 2π·f_ctrl·√(inductance·capacitance)" in text) == (name == "ratio")


def test_verifier_low_q_case_is_outside_the_declared_range():
    """検証担当の軽微1（Q = 4.5e-10。負荷がほぼ短絡で |L| が平らになり、返したゲインの余裕が 0.013°
    足りなかった）は、
    範囲の外として断る。"""
    _, err = _design(10.81936124982362, 0.12746071594513633, 5.631368828574676e-09, 2.1410342282585894e-06,
                     20412490.22544481, zero_ratio=0.6474596869314334, zero_damping=1.569212487698608,
                     filter_ratio=0.0, phase_margin_deg=22.628123695975038)
    assert err.reason == "invalid_argument" and "負荷の Q" in str(err)


@pytest.mark.parametrize("value", [math.inf, np.inf, float("inf"), np.float64("inf")])
def test_no_load_is_infinite_resistance_and_equals_the_large_r_limit(value):
    """無負荷は r_load = inf（q = 0、無損失の LC）。1e30 Ω と同じ結果（理由、余裕 1e-12°、半径）。−inf と NaN
    は断る。"""
    for plant, f_ctrl, opts in (((VIN, 47e-6, 100e-6), 100e3, {}), ((12.0, 4.7e-6, 22e-6), 1e6, {}),
                                ((VIN, 22e-6, 100e-6), 200e3, {"zero_ratio": 1.0, "zero_damping": 1.0})):
        g, err = _design(*plant, value, f_ctrl, **opts)
        g30, err30 = _design(*plant, 1e30, f_ctrl, **opts)
        assert (err.reason if err else "ok") == (err30.reason if err30 else "ok")
        assert g.phase_margin_deg == pytest.approx(g30.phase_margin_deg, abs=1e-12)
        assert g.pole_radius == pytest.approx(g30.pole_radius, abs=1e-15)
    for bad in (-math.inf, math.nan):
        _, err = _design(VIN, L, C, bad, 100e3)
        assert err.reason == "invalid_argument" and "r_load" in str(err)


def test_phase_is_anchored_below_every_root():
    """連続な位相を主値に合わせる点は、0 でないどの根よりも下に置く。以前は ωz·Ts の 1e-3 倍で合わせたので、
    負荷が
    ほぼ短絡で制御対象の遅い極（a·Q）がそれより下に来ると、そこでの位相が −180°
    の近くになって主値の枝を取り違え、
    余裕を 360° 大きく読んだ（Q = 1.2e-5、r = 1000 の形の開ループで、ナイキストの半分の余裕を 311.13°
    と読んだ。
    150 桁の参照は −48.8693°）。この回路は宣言した範囲の外（Q < 1e-3）なので、開ループの部品で確かめる。"""
    v_in, ind, cap, r_load, f_ctrl = (0.003001517407364138, 3637.539403155559, 49.04008797307621,
                                      0.00010336094193058964, 0.03168574296656154)
    ts, zeta, ratio = 1.0 / f_ctrl, 1.9320731060615932, 1000.0
    wz = 0.5 / math.sqrt(ind * cap)
    kp1, kd1 = ini._shape_gains(1.0, wz, zeta, ratio * ts)
    loop = ini._Loop(*ini._loop_factors(kp1, 1.0, kd1, ratio * ts, v_in, ind, cap, r_load, ts), ts, 1e-3 * wz)
    margins = 180.0 + loop.phase_deg(np.array([0.25, 0.5]) * math.pi / ts)
    assert margins == pytest.approx([13.228538117152667, -48.86933207892739], abs=1e-6)     # 150 桁の値


def test_numbers_in_sentences_agree_with_the_judgements():
    """文の中で比べる2つの数字（検証担当の軽微2〜4、レビューの軽2）: 「満たす」なら書いた余裕 ≥ 書いた目標、
    「足りない」と「最大 …°」なら書いた値 < 書いた目標、半径は判定と同じ側に見える。目標はちょうどの値で書く。
    """
    _, err = _design(VIN, 22e-6, 100e-6, 6.0, 200e3, zero_ratio=1e-7, phase_margin_deg=55.04)
    assert err.reason == "no_stability_margin" and "位相余裕 55.04°（目標の 55.04° は満たす）" in str(err)
    _, err = _design(VIN, 470e-6, 10e-6, 15.793045736999451, 15e3, zero_damping=0.1653846153846154,
                     filter_ratio=0.0, phase_margin_deg=29.99773)
    assert "位相余裕 29.99773° を取れるゲイン交差周波数が無い（取れるのは最大 29.9977°）" in str(err)
    _check_sentence_numbers(err, 29.99773)

    def text(pm, radius, target=TARGET, reason="unstable", body=""):
        best = PidGains(0.01, 100.0, 1e-6, 2.5e-6, 3e3, pm, radius)
        return str(GainDesignError(reason, body, target, best))

    long_target = 22.628123695975038
    shown = re.search(r"位相余裕 ([\d.]+)°（目標の ([\d.]+)° は満たす）",
                      text(long_target - 5e-7, 0.9, long_target))
    assert float(shown.group(1)) >= float(shown.group(2)) == long_target
    for radius, below in ((1.0000000013, False), (1.00004, False), (0.99999998616, True), (1 - 2e-9, True)):
        side = "1 未満で安定" if below else "1 以上で不安定"
        pattern = rf"閉ループ極の絶対値の最大 ([\d.]+)（{side}）"
        shown = float(re.search(pattern, text(60.0, radius)).group(1))
        assert (shown < 1.0) if below else (shown > 1.0)
    # 理由 unstable の本文の半径も、最良の候補と同じ書き方（以前は本文だけ小数4桁で、1.0000 と書きえた）
    plant = (2.967370111576123, 7.338144645114917e-05, 2.439793958973858e-06, 0.21317375460902435)
    opts = {"zero_damping": 0.1437543236478271, "filter_ratio": 0.0, "phase_margin_deg": 24.361861916713153,
            "zero_ratio": 0.9590984284499318}
    best, err = _design(*plant, 123266.3864601657, **opts)
    body = re.search(r"閉ループ極の絶対値の最大が ([\d.]+)（1 以上）", str(err)).group(1)
    candidate = re.search(r"閉ループ極の絶対値の最大 ([\d.]+)（1 以上で不安定）", str(err)).group(1)
    assert err.reason == "unstable" and body == candidate and float(body) > 1.0
    assert "180° より小さい値" in str(_design(VIN, L, C, R, 100e3, phase_margin_deg=200.0)[1])


# ---------------------------------------------- 7回目の修正（D-066）


def test_zero_damping_lower_bound_is_one_millionth():
    """零点の減衰の範囲は [1e-6, 2]（D-066）。1e-6 は計算し、そのすぐ下は invalid_argument（文に名前と
    今の値）。
    ζz ≲ 1e-10 かつ f_ctrl/f0 ≳ 1e9 では、PID の零点のすぐ横の交差の余裕の丸めが 2.6e-5° になり、目標を
    1.27e-5° 下回る候補を「満たす」と書いた（検証担当の 6 回目の再現例）。いまは範囲の外として断る。"""
    _, err = _design(VIN, L, C, R, 100e3, zero_damping=1e-6)
    assert err is None or err.reason != "invalid_argument"
    _, err = _design(VIN, L, C, R, 100e3, zero_damping=math.nextafter(1e-6, 0.0))
    assert err.reason == "invalid_argument"
    assert "zero_damping は 1e-06 以上 2 以下の値にする（現在 9.999999999999997e-07）" in str(err)
    _, err = _design(24, 1e4, 1e-4, 5000, 15915494309.17362, zero_ratio=1, zero_damping=5e-324,
                     filter_ratio=0, phase_margin_deg=18.743734753558844)
    assert err.reason == "invalid_argument" and "zero_damping" in str(err) and "満たす" not in str(err)


@pytest.mark.parametrize("plant,f_ctrl,limit,shown", [
    ((VIN, 1e-6, 1e-6, 0.9996e-3), 20e6, "0.001", "0.0009996"),
    ((VIN, 1e-6, 1e-6, math.nextafter(1e-3, 0.0)), 20e6, "0.001", "0.0009999999999999998"),
    ((VIN, 1.0, 1.0, R), 1.0004e11 / (2 * math.pi), "1e+11", "1.0004e+11"),
])
def test_value_outside_a_combination_range_is_shown_apart_from_the_limit(plant, f_ctrl, limit, shown):
    """範囲の外の文では、今の値を境目と見分けられる有効桁まで書く（D-066。以前は 3 桁で「0.001 以上にする
    （現在 0.001）」と見えた）。書いた今の値は、境目の外の側に見える。"""
    _, err = _design(*plant, f_ctrl)
    m = re.search(r"は ([\d.e+\-]+) (以上|以下)にする（現在 ([\d.e+\-]+)", str(err))
    assert err.reason == "invalid_argument" and m and (m.group(1), m.group(3)) == (limit, shown)
    limit_value, current = float(m.group(1)), float(m.group(3))
    assert (current < limit_value) if m.group(2) == "以上" else (current > limit_value)

