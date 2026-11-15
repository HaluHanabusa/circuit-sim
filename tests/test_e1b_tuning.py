"""E1b: ゲインの初期値の式（tuning/initial.py）の位相余裕と安定性の試験。

期待値は、この試験の中で別に組んだ離散の開ループで作る（実装の関数を使わない）:
  植物: scipy.signal.cont2discrete で Gvd(s) = Vin / (LC·s² + (L/R)·s + 1) を零次ホールドで離散化した伝達関数
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
    """実装の開ループの周波数応答が、scipy で離散化した植物 × PID × z⁻¹ と一致する（相対 1e-9）。"""
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
    """負荷抵抗がとても大きい（無負荷に近い）回路: 植物の零点が z = −1 から 1e-13 程度、極が単位円から
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
    """単位円にごく近い根（R = 1e11 Ω の植物の零点、|z| − 1 = −1.7e-13）があっても、返す交差はどれも
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
    """R = 1e13〜1e30 Ω（Q = R·√(C/L) が 1e13〜1e30）: 植物は Q = 1e10 のときと見分けられないので、理由・
    余裕・ゲインは Q = 1e10 にした同じ回路と一致する（余裕 1e-6°、ゲイン相対 1e-6）。以前は Q ≳ 1e16 で植物の
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
    """植物の極の組の |z|² − 1 は det Ad − 1 = expm1(−Ts/(R·C))（この試験の式）で与え、R が 1e30 Ω でも内側と
    数える（丸めの 2·Re x + |x|² では 1e-17 程度の正の値になりうる）。単位円までの距離は Ts/(2RC) に合う。"""
    f_ctrl, r_load = 100e3, 1e17
    g = buck_voltage_mode_pid(24, 47e-6, 100e-6, 6.0, f_ctrl)
    loop = _loop_of(g, f_ctrl, r_load=r_load)
    n = len(loop._zeros)
    plant = [n + i for i, x in enumerate(loop._poles) if abs(x.imag) > 1e-3]   # 植物の極の組（共振）
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
        m = re.search(r"極の半径 ([\d.]+)（1 未満で安定）", text(radius))
        assert m and float(m.group(1)) < 1.0
    assert "極の半径 0.9700（1 未満で安定）" in text(0.97)
    for radius in (1 + 2 * ini.STABILITY_EPS, 1.00004, 1.2):
        m = re.search(r"極の半径 ([\d.]+)（1 以上で不安定）", text(radius))
        assert m and float(m.group(1)) > 1.0
    assert "極の半径 0.99999999999999989（" in text(1 - 1.1e-16)


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


@pytest.mark.parametrize("name", ["v_in", "inductance", "capacitance", "r_load", "f_ctrl", "zero_ratio",
                                  "filter_ratio"])
@pytest.mark.parametrize("end", [0, 2])
def test_values_at_the_ends_of_the_physical_ranges_are_computed(name, end):
    """範囲の端（両端を含む）の値は計算する: 結果か、invalid_argument 以外の理由の GainDesignError（最良の
    候補の値は有限）。ほかの例外や、警告（あふれ、0 割り）は出さない。"""
    import warnings

    value = ini.ARGUMENT_RANGES[name][end]
    args = {"v_in": VIN, "inductance": L, "capacitance": C, "r_load": R, "f_ctrl": 100e3}
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
