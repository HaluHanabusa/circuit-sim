"""ゲインの初期値を解析式で決める（最適化 E2 の出発点）。

降圧コンバータの電圧モード（buck_voltage_mode）のPID。設計の手順:
  1. 平均モデルの小信号の伝達関数  Gvd(s) = Vin / (1 + s/(Q·ω0) + s²/ω0²)、ω0 = 1/√(LC)、Q = R·√(C/L)。
  2. 遅れ: 演算1サンプル（z⁻¹）と、PWMの保持（零次ホールド。約0.5サンプル）。Gvd を零次ホールドで
     厳密に離散化し（状態 [iL, vC] の行列指数）、z⁻¹ を掛ける。PIDも実装（control/blocks.py）と同じ
     差分方程式の z 変換を使う（微分の後退差分の 0.5 サンプル分の遅れも入る）。
  3. PIDの零点を、LC の共振の下（ωz = κ·ω0、既定 κ = 0.5）に減衰係数 ζz（既定 0.3）の複素数の組で置く:
       C(s) ≈ Ki·(1 + 2ζz·s/ωz + s²/ωz²) / (s·(1 + s·Tf))
     分子の係数を比べて  Kp = 2ζz·Ki/ωz − Ki·Tf、Kd = Ki/ωz² − Kp·Tf。Tf = r·Ts（既定 r = 0.25）。
     LC の位相が共振で −180° 近くまで落ちる前に、零点の進み（最大 +180°）を出しておくため。ζz を小さくすると
     交差周波数での進みが増えるが、零点の近くで制御器のゲインの谷が深くなる（0.3 はその間を取った値）。
  4. 交差周波数 ωc は、ループの位相が −180° + PM（既定 PM = 55°。基準の45°に、刻みや部品のばらつきの分の
     10° の余裕）になる周波数。上の形では位相は Ki によらないので、ωc を共振の上で探して二分法で解く。
  5. Ki は |L(e^{jωc·Ts})| = 1 から決まる（ループのゲインは Ki に比例する）。
同じ入力からは同じゲインが出る（乱数を使わない）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.linalg import expm


@dataclass(frozen=True)
class PidGains:
    kp: float          # [1/V]
    ki: float          # [1/(V·s)]
    kd: float          # [s/V]
    t_filter: float    # [s]
    f_cross: float     # 設計した交差周波数 [Hz]
    phase_margin_deg: float   # 設計した交差周波数での位相余裕 [deg]


def _plant_zoh(v_in: float, inductance: float, capacitance: float, r_load: float, ts: float):
    """Gvd を零次ホールドで離散化した状態方程式 (Ad, Bd)。出力は vC（状態の2番目）。"""
    a = np.array([[0.0, -1.0 / inductance], [1.0 / capacitance, -1.0 / (r_load * capacitance)]])
    b = np.array([[v_in / inductance], [0.0]])
    m = np.zeros((3, 3))
    m[:2, :2], m[:2, 2:] = a * ts, b * ts
    e = expm(m)
    return e[:2, :2], e[:2, 2]


def loop_response(
    omega: np.ndarray, kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> np.ndarray:
    """開ループ L(e^{jωTs}) = C(z)·P(z)·z⁻¹ の周波数応答（P は零次ホールドの平均モデル）。"""
    ad, bd = _plant_zoh(v_in, inductance, capacitance, r_load, ts)
    out = np.empty(omega.shape, dtype=complex)
    for i, w in enumerate(np.atleast_1d(omega)):
        z = np.exp(1j * w * ts)
        p = np.linalg.solve(z * np.eye(2) - ad, bd)[1]
        zi = 1.0 / z
        c = kp + ki * ts / (1.0 - zi) + kd * (1.0 - zi) / ((t_filter + ts) - t_filter * zi)
        out[i] = c * p * zi
    return out


def _shape_gains(ki: float, wz: float, zeta: float, t_filter: float) -> tuple[float, float]:
    kp = 2.0 * zeta * ki / wz - ki * t_filter
    kd = ki / wz**2 - kp * t_filter
    return kp, kd


def buck_voltage_mode_pid(
    v_in: float, inductance: float, capacitance: float, r_load: float, f_ctrl: float, *,
    zero_ratio: float = 0.5, zero_damping: float = 0.3, filter_ratio: float = 0.25,
    phase_margin_deg: float = 55.0,
) -> PidGains:
    """電圧モードの降圧コンバータのPIDの初期値（モジュールの説明の手順）。"""
    ts = 1.0 / f_ctrl
    w0 = 1.0 / math.sqrt(inductance * capacitance)
    wz, t_filter = zero_ratio * w0, filter_ratio * ts
    kp1, kd1 = _shape_gains(1.0, wz, zero_damping, t_filter)

    def margin(w: float) -> float:
        lw = loop_response(np.array([w]), kp1, 1.0, kd1, t_filter, v_in, inductance, capacitance, r_load, ts)
        return 180.0 + math.degrees(math.atan2(lw[0].imag, lw[0].real))

    # 共振の上から、ナイキスト周波数の手前まで、位相余裕が目標を下回る最初の点を探して二分法で詰める
    grid = np.geomspace(1.05 * w0, 0.5 * math.pi / ts, 400)
    values = [margin(w) for w in grid]
    above = [i for i, v in enumerate(values) if v >= phase_margin_deg]
    if not above or above[0] == len(grid) - 1:
        raise ValueError("この回路と制御周期では、目標の位相余裕を満たす交差周波数が見つからない")
    i = above[0]
    while i + 1 < len(grid) and values[i + 1] >= phase_margin_deg:
        i += 1
    lo, hi = grid[i], grid[min(i + 1, len(grid) - 1)]
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if margin(mid) >= phase_margin_deg else (lo, mid)
    wc = lo
    gain1 = abs(loop_response(np.array([wc]), kp1, 1.0, kd1, t_filter, v_in, inductance, capacitance,
                              r_load, ts)[0])
    ki = 1.0 / gain1
    kp, kd = _shape_gains(ki, wz, zero_damping, t_filter)
    return PidGains(float(kp), float(ki), float(kd), t_filter, float(wc / (2.0 * math.pi)), margin(wc))
