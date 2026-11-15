"""ゲインの初期値を解析式で決める（最適化 E2 の出発点）。

降圧コンバータの電圧モード（buck_voltage_mode）のPID。設計の手順:
  1. 平均モデルの小信号の伝達関数  Gvd(s) = Vin / (1 + s/(Q·ω0) + s²/ω0²)、ω0 = 1/√(LC)、Q = R·√(C/L)。
  2. 遅れ: 演算1サンプル（z⁻¹）と、PWMの保持（零次ホールド。約0.5サンプル）。Gvd を零次ホールドで
     厳密に離散化し（状態 [iL, vC] の行列指数）、z⁻¹ を掛ける。PIDも実装（control/blocks.py）と同じ
     差分方程式の z 変換を使う（微分の後退差分の 0.5 サンプル分の遅れも入る）。
     微分を偏差に掛けても測定値に掛けても、帰還の経路は同じなので、開ループと余裕は変わらない。
  3. PIDの零点を、LC の共振の下（ωz = κ·ω0、既定 κ = 0.5）に減衰係数 ζz（既定 0.3）の複素数の組で置く:
       C(s) ≈ Ki·(1 + 2ζz·s/ωz + s²/ωz²) / (s·(1 + s·Tf))
     分子の係数を比べて  Kp = 2ζz·Ki/ωz − Ki·Tf、Kd = Ki/ωz² − Kp·Tf。Tf = r·Ts（既定 r = 0.25）。
     LC の位相が共振で −180° 近くまで落ちる前に、零点の進み（最大 +180°）を出しておくため。ζz を小さくすると
     交差周波数での進みが増えるが、零点の近くで制御器のゲインの谷が深くなる（0.3 はその間を取った値）。
  4. 交差周波数 ωc は、ループの位相が −180° + PM（既定 PM = 55°。基準の45°に、刻みや部品のばらつきの分の
     10° の余裕）になる周波数。上の形では位相は Ki によらないので、ωc を共振の上で探して二分法で解く。
  5. Ki は |L(e^{jωc·Ts})| = 1 から決まる（ループのゲインは Ki に比例する）。
  6. 返す前に確かめる: ゲインが 1 を横切るすべての点の位相余裕の最小が目標以上、離散時間の閉ループの極の
     半径が 1 未満、Kp と Kd が負でない。どれかが成り立たなければ GainDesignError（最良の候補つき）。

位相は低い周波数から連続に取る（主値の atan2 を 180° に足すと、−180° を下回った位相が折り返して
大きな正の余裕に見える）。連続な位相は、開ループの有理関数 N(z)/D(z) の根ごとの偏角の和で計算する。
根 r が単位円の内側なら arg(e^{jx} − r) = x + arg(1 − r·e^{−jx})、外側なら arg(−r) + arg(1 − e^{jx}/r)。
どちらも右辺の arg の中身の実部が正なので、x（= ωTs）について折り返さず、格子の細かさによらない。
最後に、低い周波数の端で主値に合わせる（2π の整数倍だけずらす）。
同じ入力からは同じゲインが出る（乱数を使わない）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.linalg import expm

DesignFailure = Literal[
    "margin_not_reached",          # 探索の範囲に、目標の位相余裕を取れる交差周波数が無い
    "crossover_at_search_edge",    # 目標以上の区間が探索の上端まで続き、交差周波数を決められない
    "margin_below_target",         # 設計した点のほかに、余裕が目標に届かない交差周波数がある
    "unstable",                    # 離散時間の閉ループの極の半径が 1 以上
    "negative_gain",               # Kp か Kd が負になる
]
GRID_POINTS = 600            # 交差周波数を探す格子の点の数（共振の上から探索の上端まで、等比）
CHECK_POINTS = 4000          # 返す前に、ゲインが 1 を横切る点を探す格子の点の数（低い周波数から
#                              ナイキスト周波数まで）
TARGET_TOL_DEG = 1e-6        # 設計した点の位相余裕は二分法で目標に合わせるので、この差は丸めの分


@dataclass(frozen=True)
class PidGains:
    kp: float          # [1/V]
    ki: float          # [1/(V·s)]
    kd: float          # [s/V]
    t_filter: float    # [s]
    f_cross: float     # 設計した交差周波数 [Hz]
    phase_margin_deg: float   # ゲインが 1 を横切るすべての点の位相余裕の最小 [deg]
    #                           （位相は低い周波数から連続に取る）
    pole_radius: float        # 離散時間の閉ループの極の絶対値の最大（1 未満で安定）


class GainDesignError(ValueError):
    """目標を満たすゲインが見つからない。

    reason     : 理由（DesignFailure のどれか）
    target_deg : 目標の位相余裕 [deg]
    best       : 最良の候補（交差周波数を決められなかったときは、余裕が最大になる点で決めたもの）。
                 無ければ None
    """

    def __init__(self, reason: DesignFailure, message: str, target_deg: float, best: PidGains | None):
        self.reason = reason
        self.target_deg = target_deg
        self.best = best
        if best is not None:
            short = self.shortfall_deg
            versus = f"目標まで {short:.1f}° 足りない" if short > 0 else f"目標の {target_deg:g}° は満たす"
            message += (
                f" 最良の候補: Kp {best.kp:.4g} 1/V、Ki {best.ki:.4g} 1/(V·s)、Kd {best.kd:.4g} s/V、"
                f"位相余裕 {best.phase_margin_deg:.1f}°（{versus}）、"
                f"閉ループの極の半径 {best.pole_radius:.4f}（1 未満で安定）。"
            )
        super().__init__(message)

    @property
    def shortfall_deg(self) -> float | None:
        """最良の候補の位相余裕が、目標に何度足りないか（候補が無ければ None）。"""
        return None if self.best is None else self.target_deg - self.best.phase_margin_deg


def _plant_zoh(v_in: float, inductance: float, capacitance: float, r_load: float, ts: float):
    """Gvd を零次ホールドで離散化した状態方程式 (Ad, Bd)。出力は vC（状態の2番目）。"""
    a = np.array([[0.0, -1.0 / inductance], [1.0 / capacitance, -1.0 / (r_load * capacitance)]])
    b = np.array([[v_in / inductance], [0.0]])
    m = np.zeros((3, 3))
    m[:2, :2], m[:2, 2:] = a * ts, b * ts
    e = expm(m)
    return e[:2, :2], e[:2, 2]


def loop_polynomials(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> tuple[np.ndarray, np.ndarray]:
    """開ループ L(z) = C(z)·P(z)·z⁻¹ の分子と分母（z の降べきの係数）。

    P(z) = [0 1]·(zI − Ad)⁻¹·Bd（零次ホールドの平均モデル）。
    C(z) = Kp + Ki·Ts·z/(z − 1) + Kd·(z − 1)/(g·z − Tf)、g = Tf + Ts（control/blocks.py の差分方程式）。
    """
    ad, bd = _plant_zoh(v_in, inductance, capacitance, r_load, ts)
    (a11, a12), (a21, a22) = ad
    p_num = np.array([bd[1], a21 * bd[0] - a11 * bd[1]])
    p_den = np.array([1.0, -(a11 + a22), a11 * a22 - a12 * a21])
    g = t_filter + ts
    filt = np.array([g, -t_filter])
    c_num = np.polyadd(
        np.polyadd(kp * np.polymul([1.0, -1.0], filt), ki * ts * np.polymul([1.0, 0.0], filt)),
        kd * np.polymul([1.0, -1.0], [1.0, -1.0]),
    )
    c_den = np.polymul([1.0, -1.0], filt)
    num = np.trim_zeros(np.polymul(c_num, p_num), "f")
    den = np.polymul(np.polymul(c_den, p_den), [1.0, 0.0])
    return num, den


class _Loop:
    """開ループの周波数応答と、低い周波数から連続な位相。"""

    def __init__(self, num: np.ndarray, den: np.ndarray, ts: float, w_low: float):
        self.num, self.den, self.ts = num, den, ts
        self._zeros, self._poles = np.roots(num), np.roots(den)
        self._sign = math.atan2(0.0, num[0] / den[0])          # 最高次の係数の比の偏角（0 か π）
        self._offset = 0.0
        principal = float(np.angle(self.response(np.array([w_low]))[0]))
        raw = float(self._raw_phase(np.array([w_low]))[0])
        self._offset = 2.0 * math.pi * round((principal - raw) / (2.0 * math.pi))

    def response(self, w: np.ndarray) -> np.ndarray:
        z = np.exp(1j * np.asarray(w, dtype=float) * self.ts)
        return np.polyval(self.num, z) / np.polyval(self.den, z)

    @staticmethod
    def _arg_sum(roots: np.ndarray, x: np.ndarray) -> np.ndarray:
        total = np.zeros_like(x)
        for r in roots:
            if abs(r) <= 1.0:
                total += x + np.angle(1.0 - r * np.exp(-1j * x))
            else:
                total += np.angle(-r) + np.angle(1.0 - np.exp(1j * x) / r)
        return total

    def _raw_phase(self, w: np.ndarray) -> np.ndarray:
        x = np.asarray(w, dtype=float) * self.ts
        return self._sign + self._arg_sum(self._zeros, x) - self._arg_sum(self._poles, x)

    def phase_deg(self, w: np.ndarray) -> np.ndarray:
        """位相 [deg]。低い周波数の端（w_low）で主値と一致し、そこから連続。"""
        return np.degrees(self._raw_phase(w) + self._offset)

    def crossovers(self, w_lo: float, w_hi: float, n: int) -> np.ndarray:
        """|L| が 1 を横切る角周波数（等比の格子で符号の変わる区間を見つけ、二分法で詰める）。"""
        grid = np.geomspace(w_lo, w_hi, n)
        s = np.log(np.abs(self.response(grid)))
        found = []
        for i in np.nonzero(np.sign(s[:-1]) != np.sign(s[1:]))[0]:
            lo, hi, s_lo = grid[i], grid[i + 1], s[i]
            for _ in range(60):
                mid = math.sqrt(lo * hi)
                s_mid = math.log(abs(self.response(np.array([mid]))[0]))
                if (s_mid > 0) == (s_lo > 0):
                    lo, s_lo = mid, s_mid
                else:
                    hi = mid
            found.append(math.sqrt(lo * hi))
        return np.array(found)


def loop_response(
    omega: np.ndarray, kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> np.ndarray:
    """開ループ L(e^{jωTs}) = C(z)·P(z)·z⁻¹ の周波数応答（P は零次ホールドの平均モデル）。"""
    num, den = loop_polynomials(kp, ki, kd, t_filter, v_in, inductance, capacitance, r_load, ts)
    z = np.exp(1j * np.atleast_1d(np.asarray(omega, dtype=float)) * ts)
    return np.polyval(num, z) / np.polyval(den, z)


def closed_loop_pole_radius(num: np.ndarray, den: np.ndarray) -> float:
    """負帰還の閉ループ（特性多項式 D(z) + N(z)）の極の絶対値の最大。"""
    return float(np.max(np.abs(np.roots(np.polyadd(den, num)))))


def _shape_gains(ki: float, wz: float, zeta: float, t_filter: float) -> tuple[float, float]:
    kp = 2.0 * zeta * ki / wz - ki * t_filter
    kd = ki / wz**2 - kp * t_filter
    return kp, kd


def buck_voltage_mode_pid(
    v_in: float, inductance: float, capacitance: float, r_load: float, f_ctrl: float, *,
    zero_ratio: float = 0.5, zero_damping: float = 0.3, filter_ratio: float = 0.25,
    phase_margin_deg: float = 55.0, f_cross_max: float | None = None,
) -> PidGains:
    """電圧モードの降圧コンバータのPIDの初期値（モジュールの説明の手順）。

    f_cross_max: 交差周波数を探す範囲の上端 [Hz]（既定は f_ctrl/4。ナイキスト周波数の半分）。
    目標を満たせなければ GainDesignError（理由と最良の候補つき。ValueError の一種）。
    """
    for name, v in (("v_in", v_in), ("inductance", inductance), ("capacitance", capacitance),
                    ("r_load", r_load), ("f_ctrl", f_ctrl)):
        if not (math.isfinite(v) and v > 0):
            raise ValueError(f"{name} は正の有限値にする（現在 {v}）")
    ts = 1.0 / f_ctrl
    w0 = 1.0 / math.sqrt(inductance * capacitance)
    wz, t_filter = zero_ratio * w0, filter_ratio * ts
    w_low = 1e-3 * min(w0, wz)
    w_nyquist = math.pi / ts
    w_hi = 2.0 * math.pi * f_cross_max if f_cross_max is not None else 0.5 * w_nyquist
    w_hi = min(w_hi, 0.9999 * w_nyquist)
    f0, f_hi = w0 / (2.0 * math.pi), w_hi / (2.0 * math.pi)
    if w_hi <= 1.05 * w0:
        raise GainDesignError(
            "margin_not_reached",
            f"LC の共振（{f0:.4g} Hz）が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）より上にあるので、"
            "共振の上に交差周波数を置けない。制御周波数を上げる。",
            phase_margin_deg, None,
        )

    kp1, kd1 = _shape_gains(1.0, wz, zero_damping, t_filter)
    shape = _Loop(*loop_polynomials(kp1, 1.0, kd1, t_filter, v_in, inductance, capacitance, r_load, ts),
                  ts, w_low)

    def margin(w: np.ndarray) -> np.ndarray:
        return 180.0 + shape.phase_deg(w)       # Ki によらない

    def candidate(wc: float) -> PidGains:
        """交差周波数を wc に置いたゲインと、その確かめ（すべての交差周波数の余裕の最小、閉ループの極）。"""
        ki = 1.0 / abs(shape.response(np.array([wc]))[0])
        kp, kd = _shape_gains(ki, wz, zero_damping, t_filter)
        num, den = loop_polynomials(kp, ki, kd, t_filter, v_in, inductance, capacitance, r_load, ts)
        loop = _Loop(num, den, ts, w_low)
        cross = loop.crossovers(w_low, 0.9999 * w_nyquist, CHECK_POINTS)
        pm = float(np.min(180.0 + loop.phase_deg(cross))) if cross.size else math.inf
        return PidGains(float(kp), float(ki), float(kd), t_filter, wc / (2.0 * math.pi), pm,
                        closed_loop_pole_radius(num, den))

    # 共振の上から探索の上端まで、位相余裕が目標以上の最初の区間を探し、その上の端を二分法で詰める
    grid = np.geomspace(1.05 * w0, w_hi, GRID_POINTS)
    values = margin(grid)
    above = np.nonzero(values >= phase_margin_deg)[0]
    if above.size == 0:
        best = candidate(float(grid[int(np.argmax(values))]))
        raise GainDesignError(
            "margin_not_reached",
            f"制御周波数 {f_ctrl:g} Hz では、LC の共振（{f0:.4g} Hz）より上の "
            f"{1.05 * f0:.4g}〜{f_hi:.4g} Hz に、"
            f"位相余裕 {phase_margin_deg:g}° を取れる交差周波数が無い（取れるのは最大 {values.max():.1f}°）。"
            "制御周波数を上げるか、目標の位相余裕を下げる。",
            phase_margin_deg, best,
        )
    i = int(above[0])
    while i + 1 < len(grid) and values[i + 1] >= phase_margin_deg:
        i += 1
    if i == len(grid) - 1:
        best = candidate(float(grid[-1]))
        raise GainDesignError(
            "crossover_at_search_edge",
            f"位相余裕が {phase_margin_deg:g}° 以上の区間が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）まで"
            "続くので、交差周波数を決められない。",
            phase_margin_deg, best,
        )
    lo, hi = float(grid[i]), float(grid[i + 1])
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if margin(np.array([mid]))[0] >= phase_margin_deg else (lo, mid)
    gains = candidate(lo)

    if gains.kp < 0.0 or gains.kd < 0.0:
        raise GainDesignError(
            "negative_gain",
            f"Kp（{gains.kp:.4g}）か Kd（{gains.kd:.4g}）が負になる。微分のフィルタの時定数 Tf が、"
            "PID の零点の時定数に比べて長すぎる（制御周期に比べて LC の共振が速すぎる）。",
            phase_margin_deg, gains,
        )
    if gains.phase_margin_deg < phase_margin_deg - TARGET_TOL_DEG:
        raise GainDesignError(
            "margin_below_target",
            f"設計した交差周波数（{gains.f_cross:.4g} Hz）のほかに、位相余裕が目標の {phase_margin_deg:g}° に"
            "届かない交差周波数がある。",
            phase_margin_deg, gains,
        )
    if gains.pole_radius >= 1.0:
        raise GainDesignError(
            "unstable",
            f"位相余裕は目標を満たすが、離散時間の閉ループの極の半径が {gains.pole_radius:.4f}（1 以上）で、"
            "閉ループが不安定になる。",
            phase_margin_deg, gains,
        )
    return gains
