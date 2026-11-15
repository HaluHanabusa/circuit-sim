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

import numpy as np
import pytest
from scipy import signal

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
                              "unstable", "negative_gain")
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
    ("zero_ratio", -0.5), ("zero_ratio", 0.0), ("zero_ratio", 1.0), ("zero_ratio", math.nan),
    ("zero_damping", 0.0), ("zero_damping", 1.0), ("zero_damping", math.inf),
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
    「目標は満たす」としていた（本文は「最大 40.5°」）。いまは、交差を多項式の根から厳密に求め、
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
