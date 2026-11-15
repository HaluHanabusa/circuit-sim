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
  6. 返す前に確かめる: ゲインが 1 を横切るすべての点（設計した点を含む）の位相余裕の最小が目標以上、
     離散時間の閉ループの極の半径が 1 未満、Kp と Kd が負でない。どれかが成り立たなければ GainDesignError
     （最良の候補つき）。設計の引数が範囲の外、NaN、inf のときも GainDesignError（理由 invalid_argument）。

位相は低い周波数から連続に取る（主値の atan2 を 180° に足すと、−180° を下回った位相が折り返して
大きな正の余裕に見える）。連続な位相は、開ループの有理関数 N(z)/D(z) の根ごとの偏角の和で計算する。
根 r が単位円の内側なら arg(e^{jx} − r) = x + arg(1 − r·e^{−jx})、外側なら arg(−r) + arg(1 − e^{jx}/r)。
どちらも右辺の arg の中身の実部が正なので、x（= ωTs）について折り返さず、格子の細かさによらない。
最後に、低い周波数の端で主値に合わせる（2π の整数倍だけずらす）。
ゲインが 1 を横切る点は、格子の符号の変わり目で探さず、log|L| の傾きの上限で「交差が無い」と言える区間を
捨てていく囲い込みで求める（_Loop.crossovers）。近い2つの交差が格子の1区間に入って見落とす、ということが
起きない。開ループは因子の形（零点と極）で計算し、閉ループの極は状態遷移行列の固有値で求める
（多項式を掛け合わせると、LC の共振が制御周波数よりずっと低いときに根が z = 1 の近くに集まり、精度が落ちる）。
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
    "invalid_argument",            # 設計の引数が範囲の外、NaN、inf
]
GRID_POINTS = 600            # 目標の位相余裕になる周波数を探す格子の点の数（共振の上から探索の上端まで、
#                              等比）
INITIAL_CELLS = 2000         # ゲインが 1 を横切る点を囲い込む、最初の区間の数（等比）
CELL_TOL = 1e-12             # 囲い込みで、区間を二分し続ける幅の下限（θ に対する比）
MAX_LEVELS = 80              # 二分の段数の上限（幅 1.5% の区間から CELL_TOL まで約 34 段）
TARGET_TOL_DEG = 1e-6        # 設計した点の位相余裕は二分法で目標に合わせるので、この差は丸めの分
NYQUIST_LIMIT = 0.9999       # 交差周波数を探す上端は、ナイキスト周波数のこの倍より下に制限する


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
            pm = best.phase_margin_deg
            if not math.isfinite(pm):
                margin = "位相余裕は求められない"
            elif pm >= target_deg:                       # 「満たす」は、有限で目標以上のときだけ
                margin = f"位相余裕 {pm:.1f}°（目標の {target_deg:g}° は満たす）"
            else:
                margin = f"位相余裕 {pm:.1f}°（目標まで {target_deg - pm:.1f}° 足りない）"
            radius = best.pole_radius
            stability = "1 未満で安定" if radius < 1.0 else "1 以上で不安定"
            message += (
                f" 最良の候補: Kp {best.kp:.4g} 1/V、Ki {best.ki:.4g} 1/(V·s)、Kd {best.kd:.4g} s/V、"
                f"{margin}、閉ループの極の半径 {radius:.4f}（{stability}）。"
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


def _loop_factors(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> tuple[float, np.ndarray, np.ndarray]:
    """開ループ L(z) = k·Π(z − zi)/Π(z − pk) の（k、零点、極）。

    多項式を掛け合わせてから根を求めると、LC の共振が制御周波数よりずっと低いときに根が z = 1 の近くに
    集まって精度が落ちるので、因子ごとに求める: 極は 1（積分）、Tf/(Tf + Ts)（微分のフィルタ）、
    Ad の固有値（植物）、0（演算の遅れ z⁻¹）。零点は PID の分子（2次）と植物の分子（1次）の根。
    """
    ad, bd = _plant_zoh(v_in, inductance, capacitance, r_load, ts)
    (a11, _), (a21, _) = ad
    b1, b0 = bd[1], a21 * bd[0] - a11 * bd[1]           # 植物の分子 b1·z + b0
    g = t_filter + ts
    filt = np.array([g, -t_filter])
    c_num = np.trim_zeros(np.polyadd(
        np.polyadd(kp * np.polymul([1.0, -1.0], filt), ki * ts * np.polymul([1.0, 0.0], filt)),
        kd * np.polymul([1.0, -1.0], [1.0, -1.0]),
    ), "f")
    zeros = list(np.roots(c_num))
    if b1 != 0.0:
        zeros.append(-b0 / b1)
        gain = c_num[0] * b1 / g
    else:
        gain = c_num[0] * b0 / g
    poles = [1.0, t_filter / g, *np.linalg.eigvals(ad), 0.0]
    return float(gain), np.array(zeros, dtype=complex), np.array(poles, dtype=complex)


class _Loop:
    """開ループの周波数応答、低い周波数から連続な位相、ゲインが 1 を横切る点（因子の形で計算する）。"""

    def __init__(self, gain: float, zeros: np.ndarray, poles: np.ndarray, ts: float, w_low: float):
        self.gain, self.ts = gain, ts
        self._zeros, self._poles = zeros, poles
        self._roots = np.concatenate([zeros, poles])
        self._sign = math.atan2(0.0, gain)                     # k の偏角（0 か π）
        self._offset = 0.0
        principal = float(np.angle(self.response(np.array([w_low]))[0]))
        raw = float(self._raw_phase(np.array([w_low]))[0])
        self._offset = 2.0 * math.pi * round((principal - raw) / (2.0 * math.pi))

    def response(self, w: np.ndarray) -> np.ndarray:
        z = np.exp(1j * np.atleast_1d(np.asarray(w, dtype=float)) * self.ts)[:, None]
        return self.gain * np.prod(z - self._zeros, axis=1) / np.prod(z - self._poles, axis=1)

    def _log_gain(self, theta: np.ndarray) -> np.ndarray:
        """log|L(e^{jθ})|（因子ごとの対数の和。z = 1 の近くでも桁が落ちない）。"""
        z = np.exp(1j * theta)[:, None]
        return (math.log(abs(self.gain)) + np.log(np.abs(z - self._zeros)).sum(axis=1)
                - np.log(np.abs(z - self._poles)).sum(axis=1))

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

    def crossovers(self, theta_low: float) -> np.ndarray:
        """|L(e^{jθ})| = 1 になる角周波数のすべて（θ = ωTs が theta_low〜π）。格子の細かさによらず落とさない。

        h(θ) = log|L(e^{jθ})| の、区間 [a, b] での傾きは Σ 1/|e^{jθ} − r|（r は零点と極）で抑えられ、
        弧の上の点と r の距離は |e^{jm} − r| − (b − a)/2 以上（m は中点）。|h(m)| がその上限 × (b − a)/2 を
        超える区間には交差が無いので捨て、捨てられない区間を幅が θ の CELL_TOL 倍になるまで二分する。
        残った区間のつながりごとに、端の h の符号が変わる点を交差とする。符号が変わらないつながりは、
        |L| が 1 に接する（または、見分けられないほど近い2つの交差の）点として、|h| が最小の点を入れる
        （余裕を小さく見る側に倒す）。
        """
        a = np.geomspace(theta_low, math.pi, INITIAL_CELLS + 1)
        a, b = a[:-1], a[1:]
        kept_a, kept_b = [], []
        for _ in range(MAX_LEVELS):
            if a.size == 0:
                break
            m, half = 0.5 * (a + b), 0.5 * (b - a)
            dist = np.abs(np.exp(1j * m)[:, None] - self._roots[None, :]) - half[:, None]
            slope = (1.0 / np.where(dist > 0, dist, 1.0)).sum(axis=1)
            bound = np.where((dist > 0).all(axis=1), slope, np.inf)
            maybe = ~(np.abs(self._log_gain(m)) > half * bound + 1e-12)
            small = half <= CELL_TOL * m
            kept_a.append(a[maybe & small])
            kept_b.append(b[maybe & small])
            split = maybe & ~small
            a, b = np.concatenate([a[split], m[split]]), np.concatenate([m[split], b[split]])
        kept_a.append(a)                                         # 段数の上限まで残ったもの（普通は無い）
        kept_b.append(b)
        ka, kb = np.concatenate(kept_a), np.concatenate(kept_b)
        if ka.size == 0:
            return np.zeros(0)
        order = np.argsort(ka)
        ka, kb = ka[order], kb[order]
        found = []
        start = 0
        for i in range(1, ka.size + 1):                          # つながった区間ごと
            if i < ka.size and ka[i] <= kb[i - 1] * (1.0 + 1e-15):
                continue
            edges = np.append(ka[start:i], kb[i - 1])
            h = self._log_gain(edges)
            change = np.nonzero(np.sign(h[:-1]) != np.sign(h[1:]))[0]
            if change.size:
                found.extend(0.5 * (edges[change] + edges[change + 1]))
            else:
                found.append(edges[int(np.argmin(np.abs(h)))])
            start = i
        return np.array(found) / self.ts


def _closed_loop_matrix(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> np.ndarray:
    """離散時間の閉ループの状態遷移行列（指令値 0、上下限なし）。

    状態は [iL, vC, 前回の指令, I, D, 前回の偏差]。

    e[k] = −vC[k]、I[k] = I[k−1] + Ki·Ts·e[k]、D[k] = a·D[k−1] + b·(e[k] − e[k−1])、
    u[k] = Kp·e[k] + I[k] + D[k] は次の周期に使う（x[k+1] = Ad·x[k] + Bd·u[k−1]）。
    """
    ad, bd = _plant_zoh(v_in, inductance, capacitance, r_load, ts)
    a, b = t_filter / (t_filter + ts), kd / (t_filter + ts)
    m = np.zeros((6, 6))
    m[0:2, 0:2], m[0:2, 2] = ad, bd
    m[3, 1], m[3, 3] = -ki * ts, 1.0                             # I
    m[4, 1], m[4, 4], m[4, 5] = -b, a, -b                        # D
    m[5, 1] = -1.0                                               # 前回の偏差
    m[2] = -kp * np.eye(6)[1] + m[3] + m[4]                      # u = Kp·e + I + D
    return m


def loop_response(
    omega: np.ndarray, kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> np.ndarray:
    """開ループ L(e^{jωTs}) = C(z)·P(z)·z⁻¹ の周波数応答（P は零次ホールドの平均モデル）。"""
    num, den = loop_polynomials(kp, ki, kd, t_filter, v_in, inductance, capacitance, r_load, ts)
    z = np.exp(1j * np.atleast_1d(np.asarray(omega, dtype=float)) * ts)
    return np.polyval(num, z) / np.polyval(den, z)


def closed_loop_pole_radius(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> float:
    """離散時間の閉ループの極の絶対値の最大（状態遷移行列の固有値。1 未満で安定）。"""
    m = _closed_loop_matrix(kp, ki, kd, t_filter, v_in, inductance, capacitance, r_load, ts)
    return float(np.max(np.abs(np.linalg.eigvals(m))))


def _check_arguments(target_deg: float, *, positive: dict[str, float], unit_interval: dict[str, float],
                     non_negative: dict[str, float]) -> None:
    """設計の引数を確かめる。範囲の外、NaN、inf は GainDesignError（理由 invalid_argument）。"""
    def finite(v) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)

    rules = [(k, v, "0 より大きい有限値", finite(v) and v > 0) for k, v in positive.items()]
    rules += [(k, v, "0 より大きく 1 より小さい値", finite(v) and 0 < v < 1)
              for k, v in unit_interval.items()]
    rules += [(k, v, "0 以上の有限値", finite(v) and v >= 0) for k, v in non_negative.items()]
    rules.append(("phase_margin_deg", target_deg, "0° より大きく 180° より小さい値",
                  finite(target_deg) and 0 < target_deg < 180))
    bad = [f"{k} は {rule}にする（現在 {v!r}）" for k, v, rule, ok in rules if not ok]
    if bad:
        raise GainDesignError("invalid_argument", "設計の引数が範囲の外。" + "。".join(bad) + "。",
                              target_deg if finite(target_deg) else math.nan, None)


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

    zero_ratio   : PID の零点の角周波数の、LC の共振に対する比 κ（0 < κ < 1）
    zero_damping : PID の零点の減衰係数 ζz（0 < ζz < 1。複素数の組）
    filter_ratio : 微分のフィルタの時定数の、制御周期に対する比（0 以上）
    phase_margin_deg : 目標の位相余裕 [deg]（0 < PM < 180）
    f_cross_max  : 交差周波数を探す範囲の上端 [Hz]（既定は f_ctrl/4。ナイキスト周波数の半分）。
                   ナイキスト周波数の NYQUIST_LIMIT 倍より上を指定したら、そこまでに制限する。
    目標を満たせなければ GainDesignError（理由と最良の候補つき。ValueError の一種）。
    """
    _check_arguments(
        phase_margin_deg,
        positive={"v_in": v_in, "inductance": inductance, "capacitance": capacitance, "r_load": r_load,
                  "f_ctrl": f_ctrl, **({} if f_cross_max is None else {"f_cross_max": f_cross_max})},
        unit_interval={"zero_ratio": zero_ratio, "zero_damping": zero_damping},
        non_negative={"filter_ratio": filter_ratio},
    )
    ts = 1.0 / f_ctrl
    w0 = 1.0 / math.sqrt(inductance * capacitance)
    wz, t_filter = zero_ratio * w0, filter_ratio * ts
    w_low = 1e-3 * min(w0, wz)
    w_nyquist = math.pi / ts
    w_hi = 2.0 * math.pi * f_cross_max if f_cross_max is not None else 0.5 * w_nyquist
    w_hi = min(w_hi, NYQUIST_LIMIT * w_nyquist)
    f0, f_hi = w0 / (2.0 * math.pi), w_hi / (2.0 * math.pi)
    if w_hi <= 1.05 * w0:
        raise GainDesignError(
            "margin_not_reached",
            f"LC の共振（{f0:.4g} Hz）が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）より上にあるので、"
            "共振の上に交差周波数を置けない。制御周波数を上げる。",
            phase_margin_deg, None,
        )

    kp1, kd1 = _shape_gains(1.0, wz, zero_damping, t_filter)
    shape = _Loop(*_loop_factors(kp1, 1.0, kd1, t_filter, v_in, inductance, capacitance, r_load, ts),
                  ts, w_low)

    def margin(w: np.ndarray) -> np.ndarray:
        return 180.0 + shape.phase_deg(w)       # Ki によらない

    def candidate(wc: float) -> PidGains:
        """交差周波数を wc に置いたゲインと、その確かめ（すべての交差周波数の余裕の最小、閉ループの極）。"""
        ki = 1.0 / abs(shape.response(np.array([wc]))[0])
        kp, kd = _shape_gains(ki, wz, zero_damping, t_filter)
        plant = (v_in, inductance, capacitance, r_load, ts)
        loop = _Loop(*_loop_factors(kp, ki, kd, t_filter, *plant), ts, w_low)
        theta_low = w_low * ts
        for _ in range(10):        # 下の端では |L| > 1（積分）。そうでなければ、さらに下から囲い込む
            if loop._log_gain(np.array([theta_low]))[0] > 0.0:
                break
            theta_low *= 1e-3
        cross = np.append(loop.crossovers(theta_low), wc)        # 設計した点 wc は必ず入れる
        pm = float(np.min(180.0 + loop.phase_deg(cross)))
        return PidGains(float(kp), float(ki), float(kd), t_filter, wc / (2.0 * math.pi), pm,
                        closed_loop_pole_radius(kp, ki, kd, t_filter, *plant))

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
