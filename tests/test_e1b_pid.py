"""E1b: 制御ブロック（PID、リミッタ、区分一定の値）の試験。

期待値は、この試験の中に書いた差分方程式で作る（実装の関数を使わない）。
  P[k] = Kp·e[k]、I[k] = I[k−1] + Ki·Ts·e[k]、
  D[k] = a·D[k−1] + b·(e[k] − e[k−1])（a = Tf/(Tf+Ts)、b = Kd/(Tf+Ts)、e[−1] = e[0]、D[−1] = 0）、
  u[k] = clip(P + I + D, lower, upper)
  積分の飽和を防ぐ処理: P[k] + I[k−1] + D[k] が上限以上で Ki·e[k] > 0
  （下限以下で Ki·e[k] < 0）なら I[k] = I[k−1]
"""
import math

import numpy as np
import pytest
from scipy.signal import lfilter

from circuit_sim.control import Limiter, Pid, PidController, Schedule, ScheduleController

TS = 1e-5
KP, KI, KD, TF = 0.012, 150.0, 2.7e-6, 2.5e-6


def error_sequence(n: int = 400) -> np.ndarray:
    """決めた入力列（正負に振れ、段差を含む）。"""
    k = np.arange(n)
    return 3.0 * np.sin(2 * math.pi * k / 57.0) + np.where(k > 150, 4.0, -2.0) + 0.1 * (k % 7)


def test_c1_pid_matches_difference_equation_without_limits():
    """基準1（上下限なし）: 出力が差分方程式と 1e-12 以内で一致する。

    微分は lfilter（a·D[k−1] + b·Δe の再帰）、積分は累積和で、実装と別の書き方で作る。
    """
    e = error_sequence()
    pid = Pid(KP, KI, KD, period=TS, t_filter=TF)
    u = np.array([pid.update(x) for x in e])
    a, b = TF / (TF + TS), KD / (TF + TS)
    de = np.diff(e, prepend=e[0])                         # e[−1] = e[0]
    d = lfilter([b], [1.0, -a], de)
    expected = KP * e + KI * TS * np.cumsum(e) + d
    scale = np.abs(expected).max()
    assert np.max(np.abs(u - expected)) <= 1e-12 * scale
    assert pid.integral == pytest.approx(KI * TS * e.sum(), rel=1e-12)


def test_c1_pid_matches_difference_equation_with_limits_and_clamp():
    """基準1（上下限と積分の飽和を防ぐ処理つき）: 上下限の両側に張り付く入力列で、差分方程式と一致する。"""
    e = 40.0 * error_sequence()                         # 上限にも下限にも張り付く大きさ
    lo, hi = 0.05, 0.9
    pid = Pid(KP, KI, KD, period=TS, t_filter=TF, lower=lo, upper=hi)
    u = np.array([pid.update(x) for x in e])

    a, b = TF / (TF + TS), KD / (TF + TS)
    integ, deriv, prev, expected, sat = 0.0, 0.0, e[0], [], 0
    for x in e:
        deriv = a * deriv + b * (x - prev)
        prev = x
        v = KP * x + integ + deriv                       # 積分を進める前の出力
        if not ((v >= hi and KI * x > 0) or (v <= lo and KI * x < 0)):
            integ = integ + KI * TS * x
        y = KP * x + integ + deriv
        sat += y > hi or y < lo
        expected.append(min(max(y, lo), hi))
    assert sat > 50                                      # 上下限が実際に効いている
    assert np.max(np.abs(u - np.array(expected))) <= 1e-12


def test_pid_derivative_on_measurement_matches_difference_equation():
    """微分を測定値に掛ける: D[k] = a·D[k−1] − b·(y[k] − y[k−1])（y[−1] = y[0]）、P と I は偏差 e = r − y。
    上下限と積分の飽和を防ぐ処理つきで、差分方程式と 1e-12 以内で一致する。

    指令値 r に段差があっても、微分の項は y だけで決まる（偏差に掛ける場合は Kd·Δr/(Tf + Ts) だけ跳ねる）。
    """
    k = np.arange(400)
    r = np.where(k < 200, 12.0, 10.0)
    y = 11.0 + 0.8 * np.sin(2 * math.pi * k / 61.0) + 0.05 * (k % 5)
    lo, hi = 0.0, 0.9
    pid = Pid(KP, KI, KD, period=TS, t_filter=TF, lower=lo, upper=hi, derivative_on="measurement")
    u = np.array([pid.update(ri - yi, measurement=yi) for ri, yi in zip(r, y, strict=True)])
    a, b = TF / (TF + TS), KD / (TF + TS)
    integ, deriv, prev, expected = 0.0, 0.0, y[0], []
    for ri, yi in zip(r, y, strict=True):
        e = ri - yi
        deriv = a * deriv - b * (yi - prev)
        prev = yi
        v = KP * e + integ + deriv
        if not ((v >= hi and KI * e > 0) or (v <= lo and KI * e < 0)):
            integ = integ + KI * TS * e
        expected.append(min(max(KP * e + integ + deriv, lo), hi))
    assert np.max(np.abs(u - np.array(expected))) <= 1e-12
    # 偏差に掛ける既定の PID とは、段差の点で違う（試験が両者を見分けている）
    pid_e = Pid(KP, KI, KD, period=TS, t_filter=TF, lower=lo, upper=hi)
    u_e = np.array([pid_e.update(ri - yi) for ri, yi in zip(r, y, strict=True)])
    assert abs(u_e[200] - u[200]) > 0.1
    assert pid_e.derivative_on == "error"


def test_pid_derivative_on_measurement_needs_the_measurement():
    pid = Pid(KP, KI, KD, period=TS, t_filter=TF, derivative_on="measurement")
    with pytest.raises(ValueError, match="測定値"):
        pid.update(1.0)
    with pytest.raises(ValueError):
        Pid(KP, KI, KD, period=TS, derivative_on="output")


def test_pid_without_filter_is_plain_backward_difference():
    e = error_sequence(50)
    pid = Pid(0.0, 0.0, KD, period=TS)
    u = np.array([pid.update(x) for x in e])
    expected = KD * np.diff(e, prepend=e[0]) / TS
    assert np.max(np.abs(u - expected)) <= 1e-12 * np.abs(expected).max()


def test_pid_reset_restores_initial_state():
    pid = Pid(KP, KI, KD, period=TS, t_filter=TF)
    first = [pid.update(x) for x in error_sequence(30)]
    pid.reset()
    assert pid.integral == 0.0 and pid.derivative == 0.0
    assert [pid.update(x) for x in error_sequence(30)] == first


def slow_plant_response(anti_windup: str, n: int = 6000):
    """試験の中の植物: 1次遅れ y[k+1] = α·y[k] + (1 − α)·G·u[k]（時定数 2 ms、G = 24）。

    指令値 12 の段差で、PI（遅れに対して積分が強い）の出力は上限 0.6（出せる最大 14.4）に長く張り付く。
    戻り値: 出力 y、操作量 u、上下限で止める前の操作量 P + I + D（= Kp·e + 積分）の列。
    """
    alpha, gain, kp = math.exp(-TS / 2e-3), 24.0, 0.02
    pid = Pid(kp, 400.0, 0.0, period=TS, lower=0.0, upper=0.6, anti_windup=anti_windup)
    y, ys, us, raw, u_prev = 0.0, [], [], [], 0.0
    for _ in range(n):
        ys.append(y)
        u = pid.update(12.0 - y)
        us.append(u)
        raw.append(kp * (12.0 - y) + pid.integral)
        y = alpha * y + (1 - alpha) * gain * u_prev       # 演算1サンプル遅れ
        u_prev = u
    return np.array(ys), np.array(us), np.array(raw)


def test_c2_anti_windup_stops_integral_and_halves_overshoot():
    """基準2: 上限に張り付く間、積分が増え続けない。外れた後のオーバーシュートが、処理なしの半分以下。

    「増え続けない」は、上下限で止める前の操作量 P + I + D が、上限を積分1刻み分（Ki·Ts·e）より
    超えないことで確かめる（比例の項が減る分だけ積分が増えて出力は上限に留まるが、上限の外へは積み上がらない）。
    処理なしでは、張り付いている間に上限の外へ積み上がり続ける。
    """
    y, u, raw = slow_plant_response("clamp")
    y0, u0, raw0 = slow_plant_response("none")
    held = np.nonzero(u >= 0.6)[0]
    assert held.size > 100                               # 長く張り付く場面になっている
    one_step = 400.0 * TS * 12.0
    assert raw[held].max() <= 0.6 + one_step
    held0 = np.nonzero(u0 >= 0.6)[0]
    assert raw0[held0].max() > 0.6 + 100 * one_step      # 処理なしでは積み上がる（失敗すべきときに失敗する）
    over, over0 = y.max() - 12.0, y0.max() - 12.0
    assert over0 > 0.5                                   # 処理なしでは大きく行き過ぎる
    assert over <= 0.5 * over0


def test_limiter_and_pid_validation():
    lim = Limiter(-1.0, 2.0)
    assert lim(5.0) == 2.0 and lim(-3.0) == -1.0 and lim(0.5) == 0.5
    assert (lim.saturation(3.0), lim.saturation(-2.0), lim.saturation(0.0)) == (1, -1, 0)
    with pytest.raises(ValueError):
        Limiter(1.0, 0.0)
    with pytest.raises(ValueError):
        Pid(1.0, 1.0, period=0.0)
    with pytest.raises(ValueError):
        Pid(1.0, math.nan, period=TS)
    with pytest.raises(ValueError):
        Pid(1.0, 1.0, period=TS, t_filter=-1.0)
    with pytest.raises(ValueError):
        Pid(1.0, 1.0, period=TS, anti_windup="back")


def test_schedule_values_and_validation():
    s = Schedule(12.0, ((5e-3, 10.0), (8e-3, 11.0)))
    assert [s.at(t) for t in (0.0, 4.999e-3, 5e-3, 7e-3, 9e-3)] == [12.0, 12.0, 10.0, 10.0, 11.0]
    assert s.at(5e-3 - 1e-12, tol=1e-11) == 10.0
    with pytest.raises(ValueError):
        Schedule(1.0, ((2.0, 1.0), (1.0, 2.0)))
    with pytest.raises(ValueError):
        Schedule(math.inf)


def test_schedule_controller_looks_one_period_ahead():
    """指令は1周期遅れて使われるので、時刻 t の演算では t + period の値を返す。"""
    ctrl = ScheduleController(Schedule(0.0, ((3 * TS, 1.0),)), "load", period=TS)
    assert ctrl.reset() == [0.0]
    assert ctrl.inputs == () and ctrl.outputs == ("load",)
    outs = [ctrl.step(j * TS, np.zeros(0))[0] for j in range(5)]
    assert outs == [0.0, 0.0, 1.0, 1.0, 1.0]             # t = 2Ts の演算 → 3Ts から 1


def test_pid_controller_uses_reference_at_measurement_time():
    pid = Pid(1.0, 0.0, period=TS)
    ctrl = PidController(pid, "v(out)", "duty", Schedule(12.0, ((2 * TS, 10.0),)), initial_output=0.3)
    assert ctrl.reset() == [0.3]
    assert ctrl.step(0.0, np.array([11.0])) == [1.0]
    assert ctrl.step(2 * TS, np.array([11.0])) == [-1.0]
