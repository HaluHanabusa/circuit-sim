"""ゲインの初期値を解析式で決める（最適化 E2 の出発点）。

降圧コンバータの電圧モード（buck_voltage_mode）のPID。設計の手順:
  1. 平均モデルの小信号の伝達関数  Gvd(s) = Vin / (1 + s/(Q·ω0) + s²/ω0²)、ω0 = 1/√(LC)、Q = R·√(C/L)。
  2. 遅れ: 演算1サンプル（z⁻¹）と、PWMの保持（零次ホールド。約0.5サンプル）。Gvd を零次ホールドで
     厳密に離散化し（状態 [iL, vC] の行列指数）、z⁻¹ を掛ける。PIDも実装（control/blocks.py）と同じ
     差分方程式の z 変換を使う（微分の後退差分の 0.5 サンプル分の遅れも入る）。
     微分を偏差に掛けても測定値に掛けても、帰還の経路は同じなので、開ループと余裕は変わらない。
  3. PIDの零点を、LC の共振の下（ωz = κ·ω0、既定 κ = 0.5。0 < κ ≤ 1）に減衰係数 ζz（既定 0.3。0 < ζz ≤ 2。
     1 未満で複素数の組、1 で重根、1 より上で2つの実根）で置く:
       C(s) ≈ Ki·(1 + 2ζz·s/ωz + s²/ωz²) / (s·(1 + s·Tf))
     分子の係数を比べて  Kp = 2ζz·Ki/ωz − Ki·Tf、Kd = Ki/ωz² − Kp·Tf。Tf = r·Ts（既定 r = 0.25）。
     LC の位相が共振で −180° 近くまで落ちる前に、零点の進み（最大 +180°）を出しておくため。ζz を小さくすると
     交差周波数での進みが増えるが、零点の近くで制御器のゲインの谷が深くなる（0.3 はその間を取った値）。
  4. 交差周波数 ωc は、ループの位相が −180° + PM（既定 PM = 55°。基準の45°に、刻みや部品のばらつきの分の
     10° の余裕）になる周波数。上の形では位相は Ki によらないので、ωc を共振の上で探して二分法で解く。
  5. Ki は |L(e^{jωc·Ts})| = 1 から決まる（ループのゲインは Ki に比例する）。
  6. 返す前に確かめる: ゲインが 1 を横切るすべての点（設計した点を含む）の位相余裕の最小が目標以上
     （meets_target。目標 − TARGET_TOL_DEG まで。文言の「満たす」も同じ判定）、離散時間の閉ループの極の
     半径が 1 − STABILITY_EPS 以下、Kp と Kd が負でない。どれかが成り立たなければ GainDesignError
     （最良の候補つき）。設計の引数が範囲の外、NaN、inf のときも GainDesignError（理由 invalid_argument）。

位相は低い周波数から連続に取る（主値の atan2 を 180° に足すと、−180° を下回った位相が折り返して
大きな正の余裕に見える）。連続な位相は、開ループの有理関数 N(z)/D(z) の根ごとの偏角の和で計算する。
根 r が単位円の内側なら arg(e^{jx} − r) = x + arg(1 − r·e^{−jx})、外側なら arg(−r) + arg(1 − e^{jx}/r)
（根は z = 1 からのずれ r − 1 で持ち、e^{jx} − 1 は expm1 で作る。z = 1 の近くの根でも桁が落ちない）。
どちらも右辺の arg の中身の実部が正なので、x（= ωTs）について折り返さず、格子の細かさによらない。
最後に、低い周波数の端で主値に合わせる（2π の整数倍だけずらす）。
ゲインが 1 を横切る点は、格子の符号の変わり目で探さず、log|L| の値・傾き・曲がりの上限で「交差が無い」
または「交差は高々1つ」と言える区間を決めていく囲い込みで求める（_Loop.crossovers）。近い2つの交差が格子の
1区間に入って見落とす、ということが起きない。単位円にごく近い根（負荷抵抗がとても大きいときの植物の零点や
極）の項は、傾きでなく値の上下限で別に抑える。θ の MERGE_TOL 倍より近い交差は、余裕が最小の1つにまとめる。
開ループは因子の形（零点と極）で計算し、閉ループの極は状態遷移行列の固有値で求める（多項式を掛け合わせると、
LC の共振が制御周波数よりずっと低いときに根が z = 1 の近くに集まり、精度が落ちる）。
同じ入力からは同じゲインが出る（乱数を使わない）。
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from scipy.linalg import expm

DesignFailure = Literal[
    "margin_not_reached",          # 探索の範囲に、目標の位相余裕を取れる交差周波数が無い
    "crossover_at_search_edge",    # 目標以上の区間が探索の上端まで続き、交差周波数を決められない
    "margin_below_target",         # 設計した点のほかに、余裕が目標に届かない交差周波数がある
    "unstable",                    # 離散時間の閉ループの極の半径が 1 以上
    "no_stability_margin",         # 閉ループの極の半径が 1 − STABILITY_EPS より大きく 1 + STABILITY_EPS より
    #                                小さい（丸めで安定か不安定かが変わる範囲。安定とは言わない）
    "negative_gain",               # Kp か Kd が負になる
    "invalid_argument",            # 設計の引数が範囲の外、NaN、inf
]
GRID_POINTS = 600            # 目標の位相余裕になる周波数を探す格子の点の数（共振の上から探索の上端まで、
#                              等比）
INITIAL_CELLS = 2000         # ゲインが 1 を横切る点を囲い込む、最初の区間の数（等比）
CELL_TOL = 1e-12             # 囲い込みで、区間を二分し続ける幅の下限（θ に対する比）
MAX_LEVELS = 80              # 二分の段数の上限（幅 1.5% の区間から CELL_TOL まで約 34 段）
MAX_CELLS = 200_000          # 1段で調べる区間の数の上限（超えたら二分をやめ、残りを交差がありうる区間とする）
MERGE_TOL = 1e-9             # これより近い（θ に対する比）交差は1つにまとめ、余裕が最小の点を残す
H_TOL = 1e-12                # log|L| の丸めの見込み（対数の和の丸め。距離の丸めの分は区間ごとに足す）
TARGET_TOL_DEG = 1e-6        # 設計した点の位相余裕は二分法で目標に合わせるので、この差は丸めの分
NYQUIST_LIMIT = 0.9999       # 交差周波数を探す上端は、ナイキスト周波数のこの倍より下に制限する
STABILITY_EPS = 1e-9         # 安定と言うのは、閉ループの極の半径が 1 − STABILITY_EPS 以下のときだけ。
#                              固有値の丸め（60 桁で求めた半径との差。f_ctrl/f0 が 3e4 までの 3,500 通りで
#                              最大 1.1e-11）の約 100 倍。半径 1 − 1e-9 のモードの時定数は制御周期の 1e9 倍
#                              （100 kHz で 1e4 s）で、それより遅いモードは安定の余裕が無いのと同じ
ZERO_RATIO_MAX = 1.0         # 零点の比 κ の上限（ωz ≤ ω0。理由は buck_voltage_mode_pid の説明）
ZERO_DAMPING_MAX = 2.0       # 零点の減衰 ζz の上限（2 で零点が2つの実根になり、その比は約 14）
_ULP = 2.0 ** -52            # 倍精度の丸めの単位（1 と、その隣の数との差）

Stability = Literal["stable", "no_margin", "unstable"]


def meets_target(margin_deg: float, target_deg: float) -> bool:
    """位相余裕が目標を満たすか。受け入れの判定と文言の「満たす」は、この1つの判定を使う。

    有限で、目標 − TARGET_TOL_DEG 以上のとき満たす（設計した点の余裕は二分法で目標に合わせるので、
    丸めで目標をわずかに下回ることがある。その分だけ許す）。
    """
    return math.isfinite(margin_deg) and margin_deg >= target_deg - TARGET_TOL_DEG


def stability_of(radius: float) -> Stability:
    """閉ループの極の半径から、安定（1 − ε 以下）、安定の余裕が無い（1 との差が ε 未満）、
    不安定（1 + ε 以上）を決める。

    ε = STABILITY_EPS。半径が 1 の丸めの範囲にあるときは、安定とも不安定とも言わない。
    """
    if radius <= 1.0 - STABILITY_EPS:
        return "stable"
    if radius >= 1.0 + STABILITY_EPS:
        return "unstable"
    return "no_margin"


def _format_hz(value: float) -> str:
    """周波数を SI の接頭語つきで書く（1e9 → 1 GHz。1e15 以上は 1×10^300 Hz の形）。"""
    if value >= 1e15:
        exponent = math.floor(math.log10(value))
        return f"{value / 10.0 ** exponent:.4g}×10^{exponent} Hz"
    for scale, prefix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if value >= scale:
            return f"{value / scale:.4g} {prefix}Hz"
    return f"{value:.4g} Hz"


def _format_deg_pair(margin_deg: float, shortfall_deg: float) -> tuple[str, str]:
    """（余裕、不足）の書き方。不足が 0.05° 未満でも「0.0°」にしないよう、0 でない最初の桁まで書く。"""
    digits = 1
    while round(shortfall_deg, digits) == 0.0 and digits < 17:
        digits += 1
    return f"{margin_deg:.{digits}f}", f"{shortfall_deg:.{digits}f}"


@dataclass(frozen=True)
class PidGains:
    kp: float          # [1/V]
    ki: float          # [1/(V·s)]
    kd: float          # [s/V]
    t_filter: float    # [s]
    f_cross: float     # 設計した交差周波数 [Hz]
    phase_margin_deg: float   # ゲインが 1 を横切るすべての点の位相余裕の最小 [deg]
    #                           （位相は低い周波数から連続に取る）
    pole_radius: float        # 離散時間の閉ループの極の絶対値の最大（1 − STABILITY_EPS 以下で安定）
    # 交差周波数を探した範囲の上端 [Hz]（ナイキスト周波数の手前に制限した後の値）
    f_search_max: float = field(default=math.nan, compare=False)
    # 指定した上限 f_cross_max [Hz]。ナイキスト周波数の手前に制限したときだけ値を持つ（制限しなければ None）。
    # この2つは探索の記録で、比べる対象（ゲインと確かめの結果）に入れない
    f_cross_max_limited_from: float | None = field(default=None, compare=False)


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
            elif meets_target(pm, target_deg):          # 受け入れと同じ判定
                margin = f"位相余裕 {pm:.1f}°（目標の {target_deg:g}° は満たす）"
            else:
                shown, short = _format_deg_pair(pm, target_deg - pm)
                margin = f"位相余裕 {shown}°（目標まで {short}° 足りない）"
            radius = best.pole_radius
            stability = {
                "stable": f"閉ループの極の半径 {radius:.4f}（1 未満で安定）",
                "no_margin": (f"閉ループの極の半径 {radius:.17g}（1 との差が {STABILITY_EPS:g} より小さく、"
                              "安定の余裕が無い）"),
                "unstable": f"閉ループの極の半径 {radius:.4f}（1 以上で不安定）",
            }[stability_of(radius)]
            message += (
                f" 最良の候補: Kp {best.kp:.4g} 1/V、Ki {best.ki:.4g} 1/(V·s)、Kd {best.kd:.4g} s/V、"
                f"{margin}、{stability}。"
            )
        super().__init__(message)

    @property
    def shortfall_deg(self) -> float | None:
        """目標 − 最良の候補の位相余裕 [deg]（候補が無ければ None）。満たすかどうかは meets_target で決める
        （丸めの分 TARGET_TOL_DEG だけ正でも満たす）。"""
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


def _plant_delta(v_in: float, inductance: float, capacitance: float, r_load: float, ts: float):
    """零次ホールドの植物の (Δ = Ad − I, Bd)。

    Δ = X·φ1(X)、Bd = φ1(X)·b·Ts（X = A·Ts、φ1(X) = (e^X − I)/X は拡大行列の行列指数から）。Ad を作ってから
    I を引くと、LC の共振が制御周波数よりずっと低い（X が小さい）とき Δ の桁が落ちるので、Δ を直接求める。
    """
    a = np.array([[0.0, -1.0 / inductance], [1.0 / capacitance, -1.0 / (r_load * capacitance)]])
    b = np.array([v_in / inductance, 0.0])
    m = np.zeros((4, 4))
    m[:2, :2], m[:2, 2:] = a * ts, np.eye(2)
    phi1 = expm(m)[:2, 2:]
    return (a * ts) @ phi1, phi1 @ (b * ts)


def _quadratic_roots(a2: float, a1: float, a0: float) -> list[complex]:
    """a2·x² + a1·x + a0 = 0 の根（桁落ちしない形）。"""
    disc = a1 * a1 - 4.0 * a2 * a0
    if disc < 0.0:
        re, im = -a1 / (2.0 * a2), math.sqrt(-disc) / (2.0 * abs(a2))
        return [complex(re, im), complex(re, -im)]
    q = -0.5 * (a1 + math.copysign(math.sqrt(disc), a1))
    return [complex(q / a2), complex(a0 / q)] if q != 0.0 else [0j, 0j]


def _loop_factors(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> tuple[float, np.ndarray, np.ndarray]:
    """開ループ L(z) = k·Π(z − zi)/Π(z − pk) の（k、零点 − 1、極 − 1）。

    根は z = 1 からのずれ x = r − 1 で持つ（LC の共振や PID の零点が制御周波数よりずっと低いと、根が z = 1 に
    集まる。z で持つと 1 との差の桁が落ち、PID の零点が重根に近いと np.roots の誤差 √u ≈ 1e-8 が 1 との差を
    超える）。多項式を掛け合わせず因子ごとに求める:
      極: 0（積分 z = 1）、Tf/(Tf + Ts) − 1 = −Ts/(Tf + Ts)（微分のフィルタ）、Δ = Ad − I の固有値（植物）、
          −1（演算の遅れ z⁻¹ の z = 0）。
      零点: PID の分子を x で書いた a2·x² + a1·x + a0 の根（a2 = Kp·g + Ki·Ts·g + Kd、
          a1 = Kp·Ts + Ki·Ts·(g + Ts)、a0 = Ki·Ts²、g = Tf + Ts。Kp, Ki, Kd ≥ 0 なら係数がみな正で
          打ち消しが無い）と、植物の分子 b1·z + b0 の根 −(b0 + b1)/b1。
    """
    delta, bd = _plant_delta(v_in, inductance, capacitance, r_load, ts)
    (d11, _), (d21, _) = delta
    b1 = bd[1]
    b0_plus_b1 = d21 * bd[0] - d11 * bd[1]              # 植物の分子 b1·z + b0 の z = 1 での値
    g = t_filter + ts
    a2 = kp * g + ki * ts * g + kd
    a1 = kp * ts + ki * ts * (g + ts)
    a0 = ki * ts * ts
    if a2 != 0.0:                                        # PID の分子の次数と、z の最高次の係数
        zeros, lead = _quadratic_roots(a2, a1, a0), a2
    elif a1 != 0.0:
        zeros, lead = [complex(-a0 / a1)], a1
    else:
        zeros, lead = [], a0
    if b1 != 0.0:
        zeros.append(-b0_plus_b1 / b1)
        gain = lead * b1 / g
    else:
        gain = lead * b0_plus_b1 / g
    poles = [0.0, -ts / g, *np.linalg.eigvals(delta), -1.0]
    return float(gain), np.array(zeros, dtype=complex), np.array(poles, dtype=complex)


class _Loop:
    """開ループの周波数応答、低い周波数から連続な位相、ゲインが 1 を横切る点（因子の形で計算する）。

    零点と極は z = 1 からのずれ x = r − 1 で受け取る。e^{jθ} − r = (e^{jθ} − 1) − x を expm1(jθ) − x で
    計算するので、θ と x が小さくても距離を相対の精度で測れる。
    """

    def __init__(self, gain: float, zeros: np.ndarray, poles: np.ndarray, ts: float, w_low: float):
        self.gain, self.ts = gain, ts
        self._zeros, self._poles = zeros, poles
        self._x = np.concatenate([zeros, poles])
        self._signs = np.concatenate([np.ones(len(zeros)), -np.ones(len(poles))])   # 零点 +1、極 −1
        self._abs = np.abs(1.0 + self._x)                       # |r|
        excess = 2.0 * self._x.real + np.abs(self._x) ** 2      # |r|² − 1（桁落ちしない形）
        self._gap = np.abs(excess) / (self._abs + 1.0)          # 根と単位円の距離 ||r| − 1|
        self._log_k = math.log(abs(gain))
        self._sign = math.atan2(0.0, gain)                     # k の偏角（0 か π）
        self._offset = 0.0
        principal = float(np.angle(self.response(np.array([w_low]))[0]))
        raw = float(self._raw_phase(np.array([w_low]))[0])
        self._offset = 2.0 * math.pi * round((principal - raw) / (2.0 * math.pi))

    def _distance(self, theta: np.ndarray) -> np.ndarray:
        """e^{jθ} − r（行は θ、列は根）。"""
        return np.expm1(1j * np.asarray(theta, dtype=float))[:, None] - self._x[None, :]

    def response(self, w: np.ndarray) -> np.ndarray:
        theta = np.atleast_1d(np.asarray(w, dtype=float)) * self.ts
        d = self._distance(theta)
        n = len(self._zeros)
        return self.gain * np.prod(d[:, :n], axis=1) / np.prod(d[:, n:], axis=1)

    def _log_gain(self, theta: np.ndarray) -> np.ndarray:
        """log|L(e^{jθ})|（因子ごとの対数の和。z = 1 の近くでも桁が落ちない）。"""
        with np.errstate(divide="ignore"):
            return self._log_k + (self._signs[None, :] * np.log(np.abs(self._distance(theta)))).sum(axis=1)

    @staticmethod
    def _arg_sum(x_roots: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """Σ arg(e^{jθ} − r) を、θ について折り返さない形で（r = 1 + x）。
        |r| ≤ 1: θ + arg(1 − r·e^{−jθ})、1 − r·e^{−jθ} = −expm1(−jθ) − x·e^{−jθ}（実部が正）。
        |r| > 1: arg(−r) + arg(1 − e^{jθ}/r)、1 − e^{jθ}/r = (x − expm1(jθ))/r（実部が正）。"""
        total = np.zeros_like(theta)
        for x in x_roots:
            if 2.0 * x.real + abs(x) ** 2 <= 0.0:
                total += theta + np.angle(-np.expm1(-1j * theta) - x * np.exp(-1j * theta))
            else:
                r = 1.0 + x
                total += np.angle(-r) + np.angle((x - np.expm1(1j * theta)) / r)
        return total

    def _raw_phase(self, w: np.ndarray) -> np.ndarray:
        theta = np.asarray(w, dtype=float) * self.ts
        return self._sign + self._arg_sum(self._zeros, theta) - self._arg_sum(self._poles, theta)

    def phase_deg(self, w: np.ndarray) -> np.ndarray:
        """位相 [deg]。低い周波数の端（w_low）で主値と一致し、そこから連続。"""
        return np.degrees(self._raw_phase(w) + self._offset)

    @staticmethod
    def _rounding(theta: np.ndarray, dm: np.ndarray) -> np.ndarray:
        """|e^{jθ} − r| の丸めの見込み: expm1(jθ) の誤差（成分ごとに相対 u）と、差の丸め（相対 u）。
        2 倍の余裕を取る。"""
        return 4.0 * _ULP * (2.0 * np.abs(np.sin(0.5 * theta))[:, None] + dm)

    def _log_gain_tol(self, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """（log|L(e^{jθ})|、その丸めの見込み H_TOL + Σ（距離の丸め / 距離 + 対数の丸め））。"""
        d = np.abs(self._distance(theta))
        with np.errstate(divide="ignore"):
            h = self._log_k + (self._signs[None, :] * np.log(d)).sum(axis=1)
            tol = H_TOL + (self._rounding(theta, d) / d + 2.0 * _ULP).sum(axis=1)
        return h, tol

    def _cell_tests(self, m: np.ndarray, half: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """区間 [m − half, m + half] ごとに（交差が無いと言える、h が単調と言える）。

        h(θ) = log|k| + Σ s·log|e^{jθ} − r|（零点は s = +1、極は s = −1）。区間の上の点と e^{jm} の距離は
        half 以下なので、根 r との距離は [ρ_lo, ρ_hi] = [max(|e^{jm} − r| − half, ||r| − 1|),
        |e^{jm} − r| + half] に入る（どちらも丸めの分だけ広げる）。
        - 近い根（|e^{jm} − r| ≤ 2·half）: その項は log ρ_lo〜log ρ_hi で上下から抑える（傾きは使わない）。
          根が単位円にごく近くても、|L| が 1 になりえない区間を捨てられる。
        - 遠い根: その項の傾きは 1/ρ_lo 以下、曲がり（2階微分）は |r|/ρ_lo² 以下。
        交差が無い: 区間での h の下限 > 0 か、上限 < 0。近い根が無ければ、2次の展開
        |h(m)| − |h'(m)|·half − half²/2·Σ|r|/ρ_lo² > 0 でも言える（傾きの上限だけより、ずっと狭く
        捨てられる）。
        単調: 近い根が無く、|h'(m)| > half·Σ|r|/ρ_lo²（h' が区間の中で 0 にならない）。交差は高々1つ。
        """
        e = np.exp(1j * m)[:, None]
        d = self._distance(m)
        dm = np.abs(d)
        err = self._rounding(m, dm)                                # |e^{jm} − r| の丸めの見込み
        hh = half[:, None]
        near = dm <= 2.0 * hh + err
        far = ~near
        rho_lo = np.maximum(np.maximum(dm - hh, self._gap[None, :]) - err, 0.0)
        rho_hi = dm + hh + err
        sg = self._signs[None, :]
        with np.errstate(divide="ignore"):
            log_lo, log_hi = np.log(rho_lo), np.log(rho_hi)
        lo_far = np.where(far, rho_lo, 1.0)
        h_far = self._log_k + np.where(far, sg * np.log(np.where(far, dm, 1.0)), 0.0).sum(axis=1)
        slope = np.where(far, 1.0 / lo_far, 0.0).sum(axis=1)
        tol = H_TOL + np.where(far, err / lo_far, 0.0).sum(axis=1)
        near_lo = np.where(near, np.where(sg > 0, log_lo, -log_hi), 0.0).sum(axis=1)
        near_hi = np.where(near, np.where(sg > 0, log_hi, -log_lo), 0.0).sum(axis=1)
        excluded = ((h_far - half * slope + near_lo - tol > 0.0)
                    | (h_far + half * slope + near_hi + tol < 0.0))
        clean = ~near.any(axis=1)
        dh = np.where(far, sg * np.real(1j * e / np.where(far, d, 1.0)), 0.0).sum(axis=1)     # h'(m)
        curv = np.where(far, self._abs[None, :] / lo_far**2, 0.0).sum(axis=1)
        dtol = 2.0 * np.where(far, err / lo_far**2 + 2.0 * _ULP / lo_far, 0.0).sum(axis=1)
        taylor = np.abs(h_far) - (np.abs(dh) + dtol) * half - 0.5 * half**2 * curv - tol
        excluded |= clean & (taylor > 0.0)
        monotone = clean & ~excluded & (np.abs(dh) - dtol > half * curv)
        return excluded, monotone

    def _monotone_crossings(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """h が単調な区間 [a, b] の交差（高々1つ）。端の符号が変われば二分法で詰める。端の |h| が丸めの
        見込み以下なら、その端も交差として入れる（隣の区間との境にある交差を、丸めで両方から落とさない）。"""
        ha, ta = self._log_gain_tol(a)
        hb, tb = self._log_gain_tol(b)
        cross = (ha > 0.0) != (hb > 0.0)
        out = [a[np.abs(ha) <= ta], b[np.abs(hb) <= tb]]
        lo, hi, positive = a[cross], b[cross], ha[cross] > 0.0
        for _ in range(64):
            if lo.size == 0:
                break
            mid = 0.5 * (lo + hi)
            same = (self._log_gain(mid) > 0.0) == positive
            lo, hi = np.where(same, mid, lo), np.where(same, hi, mid)
        out.append(0.5 * (lo + hi))
        return np.concatenate(out)

    def crossovers(self, theta_low: float) -> np.ndarray:
        """|L(e^{jθ})| = 1 になる角周波数のすべて（θ = ωTs が theta_low〜π）。格子の細かさによらず落とさない。

        区間ごとに _cell_tests で「交差が無い」区間を捨て、「h が単調」な区間は端の符号で交差の有無を決めて
        二分法で詰める。どちらとも言えない区間を、幅が θ の CELL_TOL 倍になるまで二分する。そこまで残った
        区間のつながりごとに、端の h の符号が変わる点を交差とし、符号が変わらないつながり（|L| が 1 に接する、
        または見分けられないほど近い2つの交差）は |h| が最小の点を入れる（余裕を小さく見る側に倒す）。
        最後に、θ の MERGE_TOL 倍より近い交差（丸めで符号が揺れる点など）を1つにまとめ、余裕が最小の点を
        残す（余裕の最小は変わらない）。
        """
        a = np.geomspace(theta_low, math.pi, INITIAL_CELLS + 1)
        a, b = a[:-1], a[1:]
        found, kept_a, kept_b = [], [], []
        for _ in range(MAX_LEVELS):
            if a.size == 0:
                break
            m, half = 0.5 * (a + b), 0.5 * (b - a)
            excluded, monotone = self._cell_tests(m, half)
            if monotone.any():
                found.append(self._monotone_crossings(a[monotone], b[monotone]))
            rest = ~excluded & ~monotone
            small = (half <= CELL_TOL * m) | (a.size > MAX_CELLS)  # 数が多すぎれば、そこで止めて残す
            kept_a.append(a[rest & small])
            kept_b.append(b[rest & small])
            split = rest & ~small
            a, b = np.concatenate([a[split], m[split]]), np.concatenate([m[split], b[split]])
        kept_a.append(a)                                         # 段数の上限まで残ったもの（普通は無い）
        kept_b.append(b)
        ka, kb = np.concatenate(kept_a), np.concatenate(kept_b)
        order = np.argsort(ka)
        ka, kb = ka[order], kb[order]
        start = 0
        for i in range(1, ka.size + 1):                          # 残った区間の、つながりごと
            if i < ka.size and ka[i] <= kb[i - 1] * (1.0 + 1e-15):
                continue
            edges = np.append(ka[start:i], kb[i - 1])
            h = self._log_gain(edges)
            change = np.nonzero(np.sign(h[:-1]) != np.sign(h[1:]))[0]
            if change.size:
                found.append(0.5 * (edges[change] + edges[change + 1]))
            else:
                found.append(edges[[int(np.argmin(np.abs(h)))]])
            start = i
        theta = np.sort(np.concatenate(found)) if found else np.zeros(0)
        if theta.size == 0:
            return theta
        w = theta / self.ts
        return w[_merge_close(theta, self.phase_deg(w), MERGE_TOL)]


def _merge_close(theta: np.ndarray, phase: np.ndarray, tol: float) -> list[int]:
    """昇順の θ のうち、隣との差が θ の tol 倍以下のものを1つのまとまりにし、まとまりごとに位相が最小
    （余裕が最小）の点の番号を返す（まとめても余裕の最小は変わらない）。"""
    group = np.concatenate([[0], np.cumsum(theta[1:] > theta[:-1] * (1.0 + tol))])
    order = np.lexsort((phase, group))                   # まとまりの順、その中は位相の小さい順
    first = np.concatenate([[True], group[order][1:] != group[order][:-1]])
    return sorted(int(i) for i in order[first])


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


def _is_real(v) -> bool:
    """有限の実数か（Python の int・float、numpy の整数・浮動小数点数など numbers.Real）。
    真偽値は数えない。"""
    return (isinstance(v, numbers.Real) and not isinstance(v, bool | np.bool_)
            and math.isfinite(float(v)))


def _check_arguments(target_deg, *, positive: dict, ratio: dict, non_negative: dict) -> None:
    """設計の引数を確かめる。範囲の外、NaN、inf、数でない値は GainDesignError（理由 invalid_argument）。

    ratio は零点の比と減衰（上限は ZERO_RATIO_MAX、ZERO_DAMPING_MAX）: {名前: (値, 上限)}。
    """
    rules = [(k, v, "0 より大きい有限値", _is_real(v) and v > 0) for k, v in positive.items()]
    rules += [(k, v, f"0 より大きく {upper:g} 以下の値", _is_real(v) and 0 < v <= upper)
              for k, (v, upper) in ratio.items()]
    rules += [(k, v, "0 以上の有限値", _is_real(v) and v >= 0) for k, v in non_negative.items()]
    rules.append(("phase_margin_deg", target_deg, "0° より大きく 180° より小さい値",
                  _is_real(target_deg) and 0 < target_deg < 180))
    bad = [f"{k} は {rule}にする（現在 {v!r}）" for k, v, rule, ok in rules if not ok]
    if bad:
        raise GainDesignError("invalid_argument", "設計の引数が範囲の外。" + "。".join(bad) + "。",
                              float(target_deg) if _is_real(target_deg) else math.nan, None)


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

    zero_ratio   : PID の零点の角周波数の、LC の共振に対する比 κ（0 < κ ≤ 1）
    zero_damping : PID の零点の減衰係数 ζz（0 < ζz ≤ 2。1 未満で複素数の組、1 で重根、1 より上で2つの実根）
    filter_ratio : 微分のフィルタの時定数の、制御周期に対する比（0 以上）
    phase_margin_deg : 目標の位相余裕 [deg]（0 < PM < 180）
    f_cross_max  : 交差周波数を探す範囲の上端 [Hz]（既定は f_ctrl/4。ナイキスト周波数の半分）。
                   ナイキスト周波数の NYQUIST_LIMIT 倍より上を指定したら、そこまでに制限し、結果
                   （PidGains.f_cross_max_limited_from）と失敗の文に残す。
    数の引数は、Python の int・float のほか numpy の数（np.int64、np.float32 など）も受け、倍精度に
    直して使う。
    目標を満たせなければ GainDesignError（理由と最良の候補つき。ValueError の一種）。

    零点の範囲（0 < κ ≤ 1、0 < ζz ≤ 2）: 零点は、LC の共振で位相が −180° 近くまで落ちる分を進みで
    取り戻すためのもの。電圧モードの PID（タイプ III）では、2つの零点を共振の少し下〜共振に置く
    （κ = 0.5〜1。ζz = 1 の重根も普通）。κ が 1 を超えると零点が共振の上に来て、共振と零点の間で
    |L| > 1 のまま位相が −180° を下回る（条件つき安定。起動やデューティの飽和でループのゲインが下がると
    不安定になりうる）。初期値の設計としての意味が無いので断る。ζz が 1 を超えると零点は2つの実根
    ωz·(ζz ∓ √(ζz² − 1)) に分かれ、ζz = 2 で 0.27·ωz と 3.7·ωz（比 14。積分の零点を低く、微分の零点を
    共振の近くに置く設計）。それより大きいと上の零点が交差周波数の近くまで上がり、1つの ωz で2つの零点を
    表す意味が無くなる。
    """
    _check_arguments(
        phase_margin_deg,
        positive={"v_in": v_in, "inductance": inductance, "capacitance": capacitance, "r_load": r_load,
                  "f_ctrl": f_ctrl, **({} if f_cross_max is None else {"f_cross_max": f_cross_max})},
        ratio={"zero_ratio": (zero_ratio, ZERO_RATIO_MAX), "zero_damping": (zero_damping, ZERO_DAMPING_MAX)},
        non_negative={"filter_ratio": filter_ratio},
    )
    v_in, inductance, capacitance, r_load = map(float, (v_in, inductance, capacitance, r_load))
    f_ctrl = float(f_ctrl)
    zero_ratio, zero_damping = float(zero_ratio), float(zero_damping)
    filter_ratio, phase_margin_deg = float(filter_ratio), float(phase_margin_deg)
    f_cross_max = None if f_cross_max is None else float(f_cross_max)
    ts = 1.0 / f_ctrl
    w0 = 1.0 / math.sqrt(inductance * capacitance)
    wz, t_filter = zero_ratio * w0, filter_ratio * ts
    w_low = 1e-3 * min(w0, wz)
    w_nyquist = math.pi / ts
    w_hi = 2.0 * math.pi * f_cross_max if f_cross_max is not None else 0.5 * w_nyquist
    limited_from = None
    if w_hi > NYQUIST_LIMIT * w_nyquist:
        w_hi, limited_from = NYQUIST_LIMIT * w_nyquist, f_cross_max
    f0, f_hi = w0 / (2.0 * math.pi), w_hi / (2.0 * math.pi)
    limit_note = ("" if limited_from is None else
                  f"（指定した上限 {_format_hz(limited_from)} を、ナイキスト周波数の手前の {f_hi:.6g} Hz に"
                  "制限した）")

    def fail(reason: DesignFailure, text: str, best: PidGains | None) -> GainDesignError:
        return GainDesignError(reason, text + limit_note, phase_margin_deg, best)

    if w_hi <= 1.05 * w0:
        raise fail(
            "margin_not_reached",
            f"LC の共振（{f0:.4g} Hz）が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）より上にあるので、"
            "共振の上に交差周波数を置けない。制御周波数を上げる。",
            None,
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
                        closed_loop_pole_radius(kp, ki, kd, t_filter, *plant),
                        f_search_max=f_hi, f_cross_max_limited_from=limited_from)

    # 共振の上から探索の上端まで、位相余裕が目標以上の最初の区間を探し、その上の端を二分法で詰める
    grid = np.geomspace(1.05 * w0, w_hi, GRID_POINTS)
    values = margin(grid)
    above = np.nonzero(values >= phase_margin_deg)[0]
    if above.size == 0:
        best = candidate(float(grid[int(np.argmax(values))]))
        raise fail(
            "margin_not_reached",
            f"制御周波数 {f_ctrl:g} Hz では、LC の共振（{f0:.4g} Hz）より上の "
            f"{1.05 * f0:.4g}〜{f_hi:.4g} Hz に、"
            f"位相余裕 {phase_margin_deg:g}° を取れる交差周波数が無い（取れるのは最大 {values.max():.1f}°）。"
            "制御周波数を上げるか、目標の位相余裕を下げる。",
            best,
        )
    i = int(above[0])
    while i + 1 < len(grid) and values[i + 1] >= phase_margin_deg:
        i += 1
    if i == len(grid) - 1:
        best = candidate(float(grid[-1]))
        raise fail(
            "crossover_at_search_edge",
            f"位相余裕が {phase_margin_deg:g}° 以上の区間が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）まで"
            "続くので、交差周波数を決められない。",
            best,
        )
    lo, hi = float(grid[i]), float(grid[i + 1])
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if margin(np.array([mid]))[0] >= phase_margin_deg else (lo, mid)
    gains = candidate(lo)

    if gains.kp < 0.0 or gains.kd < 0.0:
        raise fail(
            "negative_gain",
            f"Kp（{gains.kp:.4g}）か Kd（{gains.kd:.4g}）が負になる。微分のフィルタの時定数 Tf が、"
            "PID の零点の時定数に比べて長すぎる（制御周期に比べて LC の共振が速すぎる）。",
            gains,
        )
    if not meets_target(gains.phase_margin_deg, phase_margin_deg):
        raise fail(
            "margin_below_target",
            f"設計した交差周波数（{gains.f_cross:.4g} Hz）のほかに、位相余裕が目標の {phase_margin_deg:g}° に"
            "届かない交差周波数がある。",
            gains,
        )
    stability = stability_of(gains.pole_radius)
    if stability == "unstable":
        raise fail(
            "unstable",
            f"位相余裕は目標を満たすが、離散時間の閉ループの極の半径が {gains.pole_radius:.4f}（1 以上）で、"
            "閉ループが不安定になる。",
            gains,
        )
    if stability == "no_margin":
        raise fail(
            "no_stability_margin",
            f"位相余裕は目標を満たすが、離散時間の閉ループの極の半径が {gains.pole_radius:.17g} で、"
            f"1 との差が {STABILITY_EPS:g} より小さい（いちばん遅い応答の時定数が、制御周期の "
            f"{1 / STABILITY_EPS:,.0f} 倍を超える）。丸めで安定か不安定かが変わる範囲なので、安定とは"
            "言えない（安定の余裕が無い）。"
            "PID の零点（zero_ratio）が LC の共振よりずっと低いと、こうなる。",
            gains,
        )
    return gains
