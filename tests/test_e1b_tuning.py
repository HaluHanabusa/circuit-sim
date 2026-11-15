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

from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid, loop_response

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


def independent_margin_and_radius(gains, f_ctrl, **plant):
    """（最小の位相余裕、その交差周波数 [Hz]、閉ループの極の半径）。"""
    ts = 1.0 / f_ctrl
    pn, pd = plant_tf(ts, **plant)
    cn, cd = pid_tf(gains.kp, gains.ki, gains.kd, gains.t_filter, ts)
    w = np.geomspace(10.0, 0.9999 * math.pi / ts, 200_000)
    z = np.exp(1j * w * ts)
    loop = np.polyval(cn, z) * np.polyval(pn, z) / (np.polyval(cd, z) * np.polyval(pd, z) * z)
    mag, phase = np.abs(loop), np.unwrap(np.angle(loop))
    cross = np.nonzero(np.diff(np.sign(mag - 1.0)))[0]
    margins = np.degrees(phase[cross]) + 180.0
    char = np.polyadd(np.polymul(np.polymul(cd, pd), [1, 0]), np.polymul(cn, pn))
    radius = float(np.max(np.abs(np.roots(char))))
    k = int(np.argmin(margins))
    return float(margins[k]), float(w[cross[k]] / (2 * math.pi)), radius


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
    assert pm == pytest.approx(g.phase_margin_deg, abs=0.5) and 45.0 <= pm <= 180.0
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
    assert pm < TARGET and best.phase_margin_deg == pytest.approx(pm, abs=0.5)
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
    """制御周波数と負荷を振っても、返したゲインは必ず目標の余裕を持ち（別の開ループで 0.5° 以内）、
    閉ループが安定。満たせないときは GainDesignError（どちらかになる）。"""
    try:
        g = buck_voltage_mode_pid(VIN, L, C, r_load, f_ctrl)
    except GainDesignError as err:
        assert err.reason in ("margin_not_reached", "crossover_at_search_edge", "margin_below_target",
                              "unstable", "negative_gain")
        return
    pm, _, radius = independent_margin_and_radius(g, f_ctrl, r_load=r_load)
    assert TARGET - 0.5 <= pm <= 180.0
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


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError, match="f_ctrl"):
        buck_voltage_mode_pid(VIN, L, C, R, 0.0)
    with pytest.raises(GainDesignError) as info:                 # 共振が探索の上端より上
        buck_voltage_mode_pid(VIN, 1e-6, 1e-6, R, 100e3)
    assert info.value.reason == "margin_not_reached" and info.value.best is None
