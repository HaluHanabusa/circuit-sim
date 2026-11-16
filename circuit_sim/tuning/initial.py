"""ゲインの初期値を解析式で決める（最適化 E2 の出発点）。

降圧コンバータの電圧モード（buck_voltage_mode）のPID。設計の手順:
  1. 平均モデルの小信号の伝達関数  Gvd(s) = Vin / (1 + s/(Q·ω0) + s²/ω0²)、ω0 = 1/√(LC)、Q = R·√(C/L)。
  2. 遅れ: 演算1サンプル（z⁻¹）と、PWMの保持（零次ホールド。約0.5サンプル）。Gvd を零次ホールドで
     厳密に離散化し、z⁻¹ を掛ける。PIDも実装（control/blocks.py）と同じ差分方程式の z 変換を使う（微分の
     後退差分の 0.5 サンプル分の遅れも入る）。
     微分を偏差に掛けても測定値に掛けても、帰還の経路は同じなので、開ループと余裕は変わらない。
  3. PIDの零点を、LC の共振の下（ωz = κ·ω0、既定 κ = 0.5。1e-9 ≤ κ ≤ 1）に減衰係数 ζz（既定 0.3。
     0 < ζz ≤ 2。1 未満で複素数の組、1 で重根、1 より上で2つの実根）で置く:
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
     （最良の候補つき）。設計の引数が範囲（ARGUMENT_RANGES と、組み合わせの Q_MIN・F_RATIO_MAX）の外、
     NaN のときも GainDesignError（理由 invalid_argument）。

尺度をそろえる（正規化）: ループは Vin·Kp、Vin·Ki·Ts、Vin·Kd/Ts、Tf/Ts と、2つの無次元の量
a = ω0·Ts（= 2π·f0/f_ctrl）、q = 1/Q = √(L/C)/R だけで決まる。設計の計算はすべてこの形で行う（Vin = 1、
時間は制御周期を単位、制御対象の状態は [iL·√(L/C), vC]。状態方程式は A' = a·[[0, −1], [1, −q]]、
B' = [a, 0]）。物理の単位のまま組むと、Vin や Ts の大きさで行列の尺度がそろわず、閉ループの極の丸めが
Vin とともに増えた（以前の不具合。Vin 4e5 V で半径の誤差 2.7e-9 が安定の余白 1e-9 を超え、安定の余裕が
無い設計を成功として返した）。ゲインは最後に物理の単位に戻す（Kp = Kp'/Vin、Ki = Ki'/(Vin·Ts)、
Kd = Kd'·Ts/Vin）。無負荷は r_load = math.inf で受け、q = 0（無損失の LC。R → ∞ の極限）で計算する。

制御対象の零次ホールドの量（極、分母、分子）は、A' の固有値 λ = a·(−q/2 ± √(q²/4 − 1)) から解析式で
求める（_plant_n）: 極は z − 1 = expm1(λ)、分母 det(xI − Δ) = x² + p1·x + p0 の係数は打ち消しの無い形、
分子の b1 = a²·φ1[λ1, λ2]（φ1(λ) = (e^λ − 1)/λ の差分商。|λ| ≤ 1 は級数、離れた2つの実根は差分商、
臨界減衰の近くは行列指数）。以前は行列指数で組んで極を固有値で求めたので、臨界減衰（Q = 0.5）の近くで
極の相対誤差が 1e-8（重根の固有値の √u）になり、負荷がほぼ短絡（q = 1e9）では分子の相対誤差も 1e-9 になった
（|L| が平らな回路で交差が動いた）。

位相は低い周波数から連続に取る（主値の atan2 を 180° に足すと、−180° を下回った位相が折り返して
大きな正の余裕に見える）。連続な位相は、開ループの有理関数 N(z)/D(z) の根ごとの偏角の和で計算する。
根 r が単位円の内側なら arg(e^{jx} − r) = x + arg(1 − r·e^{−jx})、外側なら arg(−r) + arg(1 − e^{jx}/r)
（根は z = 1 からのずれ r − 1 で持ち、e^{jx} − 1 は expm1 で作る。z = 1 の近くの根でも桁が落ちない）。
どちらも右辺の arg の中身の実部が正なので、x（= ωTs）について折り返さず、格子の細かさによらない。
最後に、低い周波数の端で主値に合わせる（2π の整数倍だけずらす）。
ゲインが 1 を横切る点は、格子の符号の変わり目で探さず、log|L| の値・傾き・曲がりの上限で「交差が無い」
または「交差は高々1つ」と言える区間を決めていく囲い込みで求める（_Loop.crossovers）。近い2つの交差が格子の
1区間に入って見落とす、ということが起きない。単位円にごく近い根（負荷抵抗がとても大きいときの制御対象の零点や
極）の項は、傾きでなく値の上下限で別に抑える。θ の MERGE_TOL 倍より近い交差は、余裕が最小の1つにまとめる。
閉ループの極は、特性多項式を x = z − 1 の多項式で作って求める（_closed_loop_roots。ゲインが負でなければ係数は
みな正で、打ち消しが無い）。根は Aberth 法で、係数の大きさの折れ線（Newton 多角形）から出発して求める
（z = 1 の近くに集まる遅い根も、相対の精度で求まる）。
同じ入力からは同じゲインが出る（乱数を使わない）。
"""
from __future__ import annotations

import cmath
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
TARGET_TOL_DEG = 1e-6        # 設計した点の位相余裕は二分法で目標に合わせるので、この差は丸めの分。
#                              受け付ける範囲の全体（14,649 通り）で、150 桁の参照との差は、
#                              目標から 1e-3° 以内の候補で最大 5.1e-10°、返したゲインで最大 4.7e-11°
#                              （この余白の 1/2000 以下）
NYQUIST_LIMIT = 0.9999       # 交差周波数を探す上端は、ナイキスト周波数のこの倍より下に制限する
STABILITY_EPS = 1e-9         # 安定と言うのは、閉ループの極の半径が 1 − STABILITY_EPS 以下のときだけ。
#                              受け付ける範囲の全体（14,649 通り）で、150 桁の参照との差は、
#                              半径が 1 から 1e-6 以内の候補で最大 1.7e-12（負のゲインの候補。負で
#                              なければ 2.4e-14）、返したゲインで最大 2.4e-14（この余白の 1/600 以下）。
#                              半径 1 − 1e-9 のモードの時定数は制御周期の 1e9 倍（100 kHz で 1e4 s）で、
#                              それより遅いモードは安定の余裕が無いのと同じ
ZERO_RATIO_MIN = 1e-9        # 零点の比 κ の下限（それより小さいと、閉ループの極の半径が 1 との差 1e-9 の中に
#                              来ることしか起きない）
ZERO_RATIO_MAX = 1.0         # 零点の比 κ の上限（ωz ≤ ω0。理由は buck_voltage_mode_pid の説明）
ZERO_DAMPING_MAX = 2.0       # 零点の減衰 ζz の上限（2 で零点が2つの実根になり、その比は約 14）
Q_MIN = 1e-3                 # 負荷の Q = R·√(C/L) の下限（組み合わせの範囲。ARGUMENT_RANGES の後に
#                              確かめる）。Q が小さい（負荷がほぼ短絡）と、制御対象は Q·ω0〜ω0/Q で積分に
#                              近く、微分と打ち消して |L| が平らになる（傾きは最悪で Q² 程度）。交差の位置が
#                              ゲインの丸めで動き、余裕の誤差は Q が 1e-5〜1e-4 で最大 2.9e-6°（判定が
#                              入れ替わる）、1e-3〜1e-2 で 6e-11°
F_RATIO_MAX = 1e11           # 制御周波数と LC の共振周波数の比 f_ctrl/f0 の上限（同上）。実用の値の
#                              組み合わせの上端（L 10 H、C 5 kF、10 MHz で 1.4e10）の外側。約 2.5e10 より上
#                              では、どの設計も閉ループの遅い極が 1 から 1e-9 の中に来て、安定の余裕が無い
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


# ---------------------------------------------------------------------------------------------------
# 文の数字の書き方
# ---------------------------------------------------------------------------------------------------

def _number_text(value: float) -> str:
    """数を、倍精度の値をちょうど表す最短の小数で書く（55.0 → 55、55.04 → 55.04、29.99773 → 29.99773）。
    書いた文字列を読み戻すと同じ値になる。"""
    return np.format_float_positional(float(value), unique=True, trim="-")


def _compare_text(value: float, reference: float, relation: Literal["<", ">", ">="], min_decimals: int = 1,
                  nonzero: float | None = None) -> tuple[str, str, str | None]:
    """文の中で値を基準と比べるときの書き方（余裕と目標、最大の余裕と目標、半径と 1）。すべての文で
    これを使う。

    基準は _number_text で書く（ちょうどの値）。値は、小数 min_decimals 桁から桁を足し、書いた数字どうしでも
    relation（判定の結論）が成り立つ最初の桁で書く（「55.0°（目標の 55.04° は満たす）」「29.9977° … 最大
    29.9977°」「半径 1.0000（1 未満で安定）」と書かない）。nonzero を渡すと、それ（目標までの不足など）も
    同じ桁で 0 に見えないところまで桁を足す。
    relation が ">=" で、値が丸めの範囲（TARGET_TOL_DEG）で基準を下回り、どの桁でも基準以上に見えないときは、
    基準と同じ数字で書く（判定が「満たす」なので、書いた数字もそれと合わせる）。
    戻り値は（値、基準、nonzero の書き方。nonzero を渡さなければ None）。
    """
    ref_text = _number_text(reference)
    ref = float(ref_text)
    for digits in range(min_decimals, 18):
        text = f"{value:.{digits}f}"
        shown = float(text)
        holds = shown < ref if relation == "<" else (shown > ref if relation == ">" else shown >= ref)
        extra = None if nonzero is None else f"{nonzero:.{digits}f}"
        if holds and (extra is None or float(extra) != 0.0):
            return text, ref_text, extra
    if relation == ">=":
        return ref_text, ref_text, None
    return f"{value:.17g}", ref_text, (None if nonzero is None else f"{nonzero:.17g}")


def _radius_text(radius: float) -> str:
    """閉ループの極の半径の書き方。安定なら 1 より小さく、不安定なら 1 より大きく見える桁まで（小数4桁から
    足す。0.99999998616 を「1.0000」と書かない）。安定の余裕が無い（1 との差が ε 未満）なら 17 桁。"""
    stability = stability_of(radius)
    if stability == "stable":
        return _compare_text(radius, 1.0, "<", min_decimals=4)[0]
    if stability == "unstable":
        return _compare_text(radius, 1.0, ">", min_decimals=4)[0]
    return f"{radius:.17g}"


def _margin_text(margin_deg: float, target_deg: float) -> str:
    """最良の候補の位相余裕の書き方（「満たす」は meets_target と同じ判定）。"""
    if not math.isfinite(margin_deg):
        return "位相余裕は求められない"
    if meets_target(margin_deg, target_deg):
        shown, target, _ = _compare_text(margin_deg, target_deg, ">=")
        return f"位相余裕 {shown}°（目標の {target}° は満たす）"
    shown, _, short = _compare_text(margin_deg, target_deg, "<", nonzero=target_deg - margin_deg)
    return f"位相余裕 {shown}°（目標まで {short}° 足りない）"


def _format_hz(value: float) -> str:
    """周波数を SI の接頭語つきで書く（1e9 → 1 GHz。1e15 以上は 1×10^300 Hz の形）。"""
    if value >= 1e15:
        exponent = math.floor(math.log10(value))
        return f"{value / 10.0 ** exponent:.4g}×10^{exponent} Hz"
    for scale, prefix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if value >= scale:
            return f"{value / scale:.4g} {prefix}Hz"
    return f"{value:.4g} Hz"


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
            margin = _margin_text(best.phase_margin_deg, target_deg)       # 受け入れと同じ判定
            radius = _radius_text(best.pole_radius)
            stability = {
                "stable": f"閉ループの極の半径 {radius}（1 未満で安定）",
                "no_margin": (f"閉ループの極の半径 {radius}（1 との差が {STABILITY_EPS:g} より小さく、"
                              "安定の余裕が無い）"),
                "unstable": f"閉ループの極の半径 {radius}（1 以上で不安定）",
            }[stability_of(best.pole_radius)]
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


# ---------------------------------------------------------------------------------------------------
# 正規化した制御対象（Vin = 1、時間は Ts 単位、状態 [iL·√(L/C), vC]）
# ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Plant:
    """零次ホールドで離散化した、正規化した制御対象。P(z) = (b1·z + b0)/(z² + …)、z − 1 = x と置いて
    分母 x² + p1·x + p0（= det(xI − Δ)、Δ = Ad − I）、分子 b1·x + p0（直流のゲインが 1 なので
    b0 + b1 = p0）。"""
    poles: np.ndarray     # 極 − 1（2つ。複素数の組か、2つの実根）
    excess: np.ndarray    # 極ごとの |z|² − 1（複素数の組は expm1(−a·q)、実根は expm1(2λ)）
    p1: float
    p0: float
    b1: float


def _phi1_dd_series(s: float, d: float) -> float:
    """φ1(λ) = (e^λ − 1)/λ の差分商 φ1[λ1, λ2] の級数（λ1 + λ2 = s、λ1·λ2 = d、|λ| ≤ 1 で使う）。

    φ1(X) = Σ X^k/(k + 1)! を Cayley–Hamilton（X² = s·X − d·I）で αI + βX にしたときの β。
    X^k = u_k·X + v_k·I、u_{k+1} = s·u_k + v_k、v_{k+1} = −d·u_k。|u_k| ≤ k なので 30 項で倍精度に足りる。"""
    u, v, total, fact = 1.0, 0.0, 0.5, 2.0
    for k in range(2, 30):
        u, v = s * u + v, -d * u
        fact *= k + 1
        total += u / fact
    return total


def _plant_n(a: float, q: float) -> _Plant:
    """正規化した制御対象 A' = a·[[0, −1], [1, −q]]、B' = [a, 0]、出力 vC の零次ホールドの量（解析式）。

    a = ω0·Ts、q = 1/Q（無負荷は 0）。A' の固有値 λ = a·(−q/2 ± √(q²/4 − 1))。
    - q < 2（共振、複素数の組 σ ± jω）: 極 − 1 = (expm1(σ)·cos ω − 2 sin²(ω/2)) ± j·e^σ·sin ω、
      p1 = −2·Re、p0 = expm1(σ)² + 4·e^σ·sin²(ω/2)（どれも打ち消しの無い形）。
    - q ≥ 2（2つの実根 λs、λf。λf = −a·q/2 − ν、λs = a²/λf）: 極 − 1 = expm1(λ)、p1 = −Σ、p0 = 積。
    分子 b1 = (φ1(A')·B') の2番目 = a²·φ1[λ1, λ2]: |λ| ≤ 1 は級数、複素数の組は Im φ1(λ)/ω、離れた実根
    （|λf| ≥ 4|λs|）は差分商、臨界減衰の近くで |λ| > 1 は 3×3 の行列指数（どの量も大きさ 0.1〜1 で、
    絶対の精度で足りる）。60 桁の参照との相対差は 3e-15 以下（a = 1e-12〜3、q = 0〜1e9）。
    """
    if q < 2.0:
        sigma = -0.5 * a * q
        omega = a * math.sqrt((1.0 - 0.5 * q) * (1.0 + 0.5 * q))
        es, em = math.exp(sigma), math.expm1(sigma)
        s2 = math.sin(0.5 * omega) ** 2
        re = em * math.cos(omega) - 2.0 * s2
        im = es * math.sin(omega)
        poles = np.array([complex(re, im), complex(re, -im)])
        excess = np.full(2, math.expm1(-a * q))
        p1, p0 = -2.0 * re, em * em + 4.0 * es * s2
        if a <= 1.0:
            beta = _phi1_dd_series(-a * q, a * a)
        else:
            beta = (im * sigma - re * omega) / (a * a * omega)
    else:
        nu = a * math.sqrt((0.5 * q - 1.0) * (0.5 * q + 1.0))
        lf = -0.5 * a * q - nu
        ls = a * a / lf
        xs, xf = math.expm1(ls), math.expm1(lf)
        poles = np.array([complex(xs), complex(xf)])
        excess = np.array([math.expm1(2.0 * ls), math.expm1(2.0 * lf)])
        p1, p0 = -(xs + xf), xs * xf
        if -lf <= 1.0:
            beta = _phi1_dd_series(-a * q, a * a)
        elif lf <= 4.0 * ls:
            beta = (xs / ls - xf / lf) / (ls - lf)
        else:
            m = np.array([[0.0, -a, a], [a, -a * q, 0.0], [0.0, 0.0, 0.0]])
            beta = float(expm(m)[1, 2]) / (a * a)
    return _Plant(poles, excess, float(p1), float(p0), float(a * a * beta))


def _normalize(inductance: float, capacitance: float, r_load: float, ts: float) -> tuple[float, float]:
    """(a, q) = (ω0·Ts, √(L/C)/R)。r_load = inf（無負荷）は q = 0。"""
    sl, sc = math.sqrt(inductance), math.sqrt(capacitance)
    return ts / (sl * sc), (0.0 if math.isinf(r_load) else sl / (sc * r_load))


def _normalized_gains(kp: float, ki: float, kd: float, t_filter: float, v_in: float,
                      ts: float) -> tuple[float, float, float, float]:
    """物理の単位のゲイン → (Vin·Kp, Vin·Ki·Ts, Vin·Kd/Ts, Tf/Ts)。"""
    return v_in * kp, v_in * ki * ts, v_in * kd / ts, t_filter / ts


def _pid_numerator(kp: float, ki: float, kd: float, r: float) -> tuple[float, float, float, float]:
    """PID の分子を x = z − 1 で書いた a2·x² + a1·x + a0 の係数と g = r + 1（正規化したゲイン）。

    C(z) = Kp + Ki·z/(z − 1) + Kd·(z − 1)/(g·z − r) の分子 Kp·x·(1 + g·x) + Ki·(1 + x)·(1 + g·x) + Kd·x²。
    Kp, Ki, Kd ≥ 0 なら係数がみな正で打ち消しが無い。"""
    g = r + 1.0
    return g * (kp + ki) + kd, kp + ki * (g + 1.0), ki, g


def _quadratic_roots(a2: float, a1: float, a0: float) -> list[complex]:
    """a2·x² + a1·x + a0 = 0 の根（桁落ちしない形）。"""
    disc = a1 * a1 - 4.0 * a2 * a0
    if disc < 0.0:
        re, im = -a1 / (2.0 * a2), math.sqrt(-disc) / (2.0 * abs(a2))
        return [complex(re, im), complex(re, -im)]
    q = -0.5 * (a1 + math.copysign(math.sqrt(disc), a1))
    return [complex(q / a2), complex(a0 / q)] if q != 0.0 else [0j, 0j]


def _real_excess(x: complex) -> float:
    """|1 + x|² − 1。実数の x は x·(2 + x)（打ち消しが無い）、複素数は 2·Re x + |x|²。"""
    x = complex(x)
    if x.imag == 0.0:
        return x.real * (2.0 + x.real)
    return 2.0 * x.real + abs(x) ** 2


def _factors_n(kp: float, ki: float, kd: float, r: float,
               plant: _Plant) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """開ループ L(z) = C(z)·P(z)·z⁻¹ = k·Π(z − zi)/Π(z − pk) の（k、零点 − 1、極 − 1、根ごとの |r|² − 1
    （零点、極の順））。正規化したゲイン（_normalized_gains）と制御対象（_plant_n）から。

    根は z = 1 からのずれ x = r − 1 で持つ（LC の共振や PID の零点が制御周波数よりずっと低いと、根が z = 1 に
    集まる。z で持つと 1 との差の桁が落ちる）。多項式を掛け合わせず因子ごとに求める:
      極: 0（積分 z = 1）、r/(r + 1) − 1 = −1/g（微分のフィルタ）、制御対象の極、−1（演算の遅れ z⁻¹ の
          z = 0）。
      零点: PID の分子 a2·x² + a1·x + a0 の根（_pid_numerator）と、制御対象の分子 b1·x + p0 の根 −p0/b1。
    |r|² − 1（単位円の内外と、単位円までの距離を決める）は、複素数の組は積から与える（x から 2·Re x + |x|² で
    作ると、根が単位円に丸めより近いとき符号まで埋もれる）:
      制御対象の極の組: |r|² = det Ad = e^{−a·q} → expm1(−a·q)（q > 0 なら必ず内側、無負荷 q = 0 は円の上）。
      PID の零点の組: |r|² = （z の多項式の定数項）/（最高次の係数）= (Kp·r + Kd)/a2 → −(Kp + Ki·g)/a2。
    実根は x·(2 + x)（打ち消しが無い）。
    """
    a2, a1, a0, g = _pid_numerator(kp, ki, kd, r)
    if a2 != 0.0:                                        # PID の分子の次数と、z の最高次の係数
        zeros, lead = _quadratic_roots(a2, a1, a0), a2
    elif a1 != 0.0:
        zeros, lead = [complex(-a0 / a1)], a1
    else:
        zeros, lead = [], a0
    zero_excess = [_real_excess(x) for x in zeros]
    if len(zeros) == 2 and zeros[0].imag != 0.0:          # PID の零点が複素数の組
        zero_excess = [-(kp + ki * g) / a2] * 2
    zeros.append(complex(-plant.p0 / plant.b1))           # 制御対象の分子 b1·x + p0 の根（z ≈ −1）
    zero_excess.append(_real_excess(zeros[-1]))
    gain = lead * plant.b1 / g
    poles = [0j, complex(-1.0 / g), *plant.poles, complex(-1.0)]
    pole_excess = [0.0, _real_excess(-1.0 / g), *plant.excess, -1.0]
    return (float(gain), np.array(zeros, dtype=complex), np.array(poles, dtype=complex),
            np.array(zero_excess + pole_excess, dtype=float))


def _loop_factors(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """_factors_n の、物理の単位の入口（ゲインと回路の値を正規化してから計算する）。"""
    a, q = _normalize(inductance, capacitance, r_load, ts)
    kp_n, ki_n, kd_n, r = _normalized_gains(kp, ki, kd, t_filter, v_in, ts)
    return _factors_n(kp_n, ki_n, kd_n, r, _plant_n(a, q))


class _Loop:
    """開ループの周波数応答、低い周波数から連続な位相、ゲインが 1 を横切る点（因子の形で計算する）。

    零点と極は z = 1 からのずれ x = r − 1 で受け取る。e^{jθ} − r = (e^{jθ} − 1) − x を expm1(jθ) − x で
    計算するので、θ と x が小さくても距離を相対の精度で測れる。excess は根ごとの |r|² − 1（零点、極の順。
    _factors_n が丸めに頼らない形で与える）で、単位円の内外（位相の数え方）と単位円までの距離に使う。
    周波数は ω（θ = ω·ts）。設計の計算は正規化した形（ts = 1、ω は θ そのもの）で使う。
    """

    def __init__(self, gain: float, zeros: np.ndarray, poles: np.ndarray, excess: np.ndarray, ts: float,
                 w_low: float):
        self.gain, self.ts = gain, ts
        self._zeros, self._poles = zeros, poles
        self._x = np.concatenate([zeros, poles])
        self._signs = np.concatenate([np.ones(len(zeros)), -np.ones(len(poles))])   # 零点 +1、極 −1
        self._abs = np.abs(1.0 + self._x)                       # |r|
        self._inside = np.asarray(excess) <= 0.0                # |r| ≤ 1
        self._gap = np.abs(excess) / (self._abs + 1.0)          # 根と単位円の距離 ||r| − 1|
        self._log_k = math.log(abs(gain))
        self._sign = math.atan2(0.0, gain)                     # k の偏角（0 か π）
        self._offset = 0.0
        # 主値に合わせる点は、w_low と、0 でないどの根よりも 3 桁下（そこでは位相が積分の −90° の近くで、
        # 主値の枝を取り違えない。根より上で合わせると、位相が ±180° の境目に来て 360° ずれることがある:
        # 負荷がほぼ短絡で制御対象の遅い極 a·Q が w_low より下に来たとき。以前の不具合）
        nonzero = np.abs(self._x[self._x != 0.0])
        anchor = min(w_low, 1e-3 * float(nonzero.min()) / ts) if nonzero.size else w_low
        principal = float(np.angle(self.response(np.array([anchor]))[0]))
        raw = float(self._raw_phase(np.array([anchor]))[0])
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
    def _arg_sum(x_roots: np.ndarray, inside: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """Σ arg(e^{jθ} − r) を、θ について折り返さない形で（r = 1 + x。inside は |r| ≤ 1 か）。
        |r| ≤ 1: θ + arg(1 − r·e^{−jθ})、1 − r·e^{−jθ} = −expm1(−jθ) − x·e^{−jθ}（実部が正）。
        |r| > 1: arg(−r) + arg(1 − e^{jθ}/r)、1 − e^{jθ}/r = (x − expm1(jθ))/r（実部が正）。"""
        total = np.zeros_like(theta)
        for x, is_inside in zip(x_roots, inside, strict=True):
            if is_inside:
                total += theta + np.angle(-np.expm1(-1j * theta) - x * np.exp(-1j * theta))
            else:
                r = 1.0 + x
                total += np.angle(-r) + np.angle((x - np.expm1(1j * theta)) / r)
        return total

    def _raw_phase(self, w: np.ndarray) -> np.ndarray:
        theta = np.asarray(w, dtype=float) * self.ts
        n = len(self._zeros)
        return (self._sign + self._arg_sum(self._zeros, self._inside[:n], theta)
                - self._arg_sum(self._poles, self._inside[n:], theta))

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


# ---------------------------------------------------------------------------------------------------
# 閉ループの極（特性多項式を x = z − 1 で作り、Aberth 法で解く）
# ---------------------------------------------------------------------------------------------------

def _closed_loop_polynomial(kp: float, ki: float, kd: float, r: float, plant: _Plant) -> np.ndarray:
    """閉ループの特性多項式 D(x) + N(x)（x = z − 1 の降べきの係数。正規化したゲイン）。

    D = (1 + x)·x·(1 + g·x)·(x² + p1·x + p0)（演算の遅れ、積分、微分のフィルタ、制御対象の分母）、
    N = (a2·x² + a1·x + a0)·(b1·x + p0)（PID と制御対象の分子）。ゲインが負でなければ、どの因子の係数も正
    なので、掛けても足しても打ち消しが無く、係数は相対の精度で求まる（z の多項式で組むと、根が z = 1 に
    集まるとき係数の打ち消しで遅い根の桁が落ちる）。指令値 0、上下限なしの線形の閉ループ。"""
    a2, a1, a0, g = _pid_numerator(kp, ki, kd, r)
    den = np.polymul(np.polymul([1.0, 1.0, 0.0], [g, 1.0]), [1.0, plant.p1, plant.p0])
    num = np.polymul([a2, a1, a0], [plant.b1, plant.p0])
    out = den.astype(float)
    out[-num.size:] += num
    return out


def _poly_roots(coefficients: np.ndarray) -> np.ndarray:
    """実係数の多項式（降べき）の根を Aberth 法で（Gauss–Seidel の順に更新）。

    出発点は Newton 多角形（係数の対数の上側の凸包）の辺ごとの半径の円の上（根の大きさが 1e-30 から 1 まで
    散らばっても、大きさの順に分かれて出発する）。根ごとに、修正が |z| の 2u 以下か、値が評価の丸め
    （(n + 1)·4u·Σ|c_i||z|^i）以下になったら止める。最後の修正は入れる。"""
    c = [float(v) for v in np.asarray(coefficients, dtype=float)[::-1]]       # 昇べき
    while len(c) > 1 and c[-1] == 0.0:
        c.pop()
    n_zero = 0
    while len(c) > 1 and c[0] == 0.0:
        c.pop(0)
        n_zero += 1
    n = len(c) - 1
    if n <= 0:
        return np.zeros(n_zero, dtype=complex)
    hull: list[tuple[int, float]] = []
    for point in [(i, math.log(abs(v))) for i, v in enumerate(c) if v != 0.0]:
        while len(hull) >= 2 and ((hull[-1][1] - hull[-2][1]) * (point[0] - hull[-2][0])
                                  <= (point[1] - hull[-2][1]) * (hull[-1][0] - hull[-2][0])):
            hull.pop()
        hull.append(point)
    z: list[complex] = []
    for (i, yi), (j, yj) in zip(hull, hull[1:], strict=False):
        m = j - i
        radius = math.exp((yi - yj) / m)
        for k in range(m):
            z.append(radius * cmath.exp(1j * (2.0 * math.pi * (k + 0.25) / m + 2.0 * math.pi * i / n + 0.4)))
    done = [False] * n
    for _ in range(300):
        for k in range(n):
            if done[k]:
                continue
            zk = z[k]
            p, dp, mag, az = complex(c[n]), 0j, abs(c[n]), abs(zk)
            for i in range(n - 1, -1, -1):
                dp = dp * zk + p
                p = p * zk + c[i]
                mag = mag * az + abs(c[i])
            if p == 0.0:
                done[k] = True
                continue
            if dp == 0.0:
                z[k] = zk * (1.0 + 1e-8) + 1e-300
                continue
            ratio = p / dp
            s = sum(1.0 / (zk - z[j]) for j in range(n) if j != k)
            step = ratio / (1.0 - ratio * s)
            z[k] = zk - step
            if abs(step) <= 2.0 * _ULP * abs(z[k]) or abs(p) <= 4.0 * (n + 1) * _ULP * mag:
                done[k] = True
        if all(done):
            break
    return np.array(z + [0j] * n_zero, dtype=complex)


def _closed_loop_roots(kp: float, ki: float, kd: float, r: float, plant: _Plant) -> np.ndarray:
    """閉ループの極 − 1（x = z − 1。正規化したゲイン）。"""
    return _poly_roots(_closed_loop_polynomial(kp, ki, kd, r, plant))


def _radius_n(kp: float, ki: float, kd: float, r: float, plant: _Plant) -> float:
    """閉ループの極の絶対値の最大。|1 + x| − 1 = (2·Re x + |x|²)/(|1 + x| + 1) で求めてから 1 を足す
    （z = 1 の近くの遅い極でも、1 との差を相対の精度で持つ）。"""
    x = _closed_loop_roots(kp, ki, kd, r, plant)
    excess = (2.0 * x.real + np.abs(x) ** 2) / (np.abs(1.0 + x) + 1.0)
    return float(1.0 + np.max(excess))


# ---------------------------------------------------------------------------------------------------
# 物理の単位の入口（試験と解析用。中では正規化して計算する）
# ---------------------------------------------------------------------------------------------------

def loop_polynomials(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> tuple[np.ndarray, np.ndarray]:
    """開ループ L(z) = C(z)·P(z)·z⁻¹ の分子と分母（z の降べきの係数。比が L(z)）。

    P(z) は零次ホールドの平均モデル、C(z) = Kp + Ki·Ts·z/(z − 1) + Kd·(z − 1)/(g·z − Tf)、g = Tf + Ts
    （control/blocks.py の差分方程式）。正規化した形（_plant_n、_pid_numerator）の x の多項式を z に
    直したもの。
    z の係数は、根が z = 1 に集まると打ち消しで桁が落ちる（設計の計算には使わない）。"""
    a, q = _normalize(inductance, capacitance, r_load, ts)
    kp_n, ki_n, kd_n, r = _normalized_gains(kp, ki, kd, t_filter, v_in, ts)
    plant = _plant_n(a, q)
    a2, a1, a0, g = _pid_numerator(kp_n, ki_n, kd_n, r)
    c_num = np.array([a2, a1 - 2.0 * a2, a2 - a1 + a0])                     # x = z − 1 を z に
    p_num = np.array([plant.b1, plant.p0 - plant.b1])
    p_den = np.array([1.0, plant.p1 - 2.0, 1.0 - plant.p1 + plant.p0])
    num = np.trim_zeros(np.polymul(c_num, p_num), "f")
    den = np.polymul(np.polymul(np.polymul([1.0, -1.0], [g, -r]), p_den), [1.0, 0.0])
    return num, den


def loop_response(
    omega: np.ndarray, kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> np.ndarray:
    """開ループ L(e^{jωTs}) = C(z)·P(z)·z⁻¹ の周波数応答（P は零次ホールドの平均モデル。因子の形で
    計算する）。"""
    gain, zeros, poles, _ = _loop_factors(kp, ki, kd, t_filter, v_in, inductance, capacitance, r_load, ts)
    x = np.expm1(1j * np.atleast_1d(np.asarray(omega, dtype=float)) * ts)[:, None]
    return gain * np.prod(x - zeros[None, :], axis=1) / np.prod(x - poles[None, :], axis=1)


def closed_loop_pole_radius(
    kp: float, ki: float, kd: float, t_filter: float,
    v_in: float, inductance: float, capacitance: float, r_load: float, ts: float,
) -> float:
    """離散時間の閉ループの極の絶対値の最大（1 未満で安定。正規化した特性多項式の根）。"""
    a, q = _normalize(inductance, capacitance, r_load, ts)
    kp_n, ki_n, kd_n, r = _normalized_gains(kp, ki, kd, t_filter, v_in, ts)
    return _radius_n(kp_n, ki_n, kd_n, r, _plant_n(a, q))


# ---------------------------------------------------------------------------------------------------
# 引数の範囲
# ---------------------------------------------------------------------------------------------------

# 設計の引数の範囲（SI）: 名前 → (下限, 下限を含むか, 上限, 上限を含むか, 単位)。外は invalid_argument。
# 計算は正規化した形（a = ω0·Ts、q = 1/Q）で行うので、回路の値そのものは、a と q と最後のゲインの換算が
# 倍精度で 0 割りやあふれを起こさない範囲であればよい。組み合わせで効く量は Q_MIN と F_RATIO_MAX で縛る。
# r_load は、このほかに math.inf（無負荷）を受ける。零点の比の下限は ZERO_RATIO_MIN。微分のフィルタの比の
# 上限 1e3: 時定数が制御周期の 1000 倍を超えると微分は交差周波数より 3 桁下で切れる。
ARGUMENT_RANGES: dict[str, tuple[float, bool, float, bool, str]] = {
    "v_in": (1e-6, True, 1e9, True, " V"),
    "inductance": (1e-12, True, 1e4, True, " H"),
    "capacitance": (1e-15, True, 1e6, True, " F"),
    "r_load": (1e-6, True, 1e30, True, " Ω"),
    "f_ctrl": (1e-3, True, 1e12, True, " Hz"),
    "f_cross_max": (0.0, False, math.inf, False, " Hz"),
    "zero_ratio": (ZERO_RATIO_MIN, True, ZERO_RATIO_MAX, True, ""),
    "zero_damping": (0.0, False, ZERO_DAMPING_MAX, True, ""),
    "filter_ratio": (0.0, True, 1e3, True, ""),
    "phase_margin_deg": (0.0, False, 180.0, False, "°"),
}


def _as_real(v) -> float | None:
    """有限の実数なら倍精度の値（Python の int・float、numpy の整数・浮動小数点数など numbers.Real）。
    真偽値、倍精度にできない値（10**400 など）、NaN、inf は None。"""
    if not isinstance(v, numbers.Real) or isinstance(v, bool | np.bool_):
        return None
    try:
        x = float(v)
    except (OverflowError, ValueError, TypeError):
        return None
    return x if math.isfinite(x) else None


def _is_no_load(v) -> bool:
    """r_load の無負荷（正の無限大の実数。math.inf、np.inf、float('inf')）。"""
    if not isinstance(v, numbers.Real) or isinstance(v, bool | np.bool_):
        return False
    try:
        return float(v) == math.inf
    except (OverflowError, ValueError, TypeError):
        return False


def _range_text(lo: float, lo_closed: bool, hi: float, hi_closed: bool, unit: str) -> str:
    low = f"{lo:g}{unit} {'以上' if lo_closed else 'より大きく'}"
    if math.isinf(hi):
        return f"{lo:g}{unit} より大きい有限値" if not lo_closed else f"{low}の有限値"
    return f"{low} {hi:g}{unit} {'以下の値' if hi_closed else 'より小さい値'}"


def _check_arguments(values: dict) -> dict[str, float]:
    """設計の引数を ARGUMENT_RANGES で確かめ、倍精度の値にして返す。範囲の外、NaN、inf（r_load の無負荷を
    除く）、数でない値は GainDesignError（理由 invalid_argument。すべての誤りを1つの文に書く）。範囲は倍精度に
    直した後の値で比べる（np.longdouble('1e-4000') は 0 になるので断る）。
    1つずつの値が範囲の中なら、組み合わせで効く量（負荷の Q と、f_ctrl/f0）を Q_MIN と F_RATIO_MAX で
    確かめる。"""
    out, bad = {}, []
    for name, v in values.items():
        lo, lo_closed, hi, hi_closed, unit = ARGUMENT_RANGES[name]
        if name == "r_load" and _is_no_load(v):
            out[name] = math.inf
            continue
        x = _as_real(v)
        ok = (x is not None and (x >= lo if lo_closed else x > lo) and (x <= hi if hi_closed else x < hi))
        if ok:
            out[name] = x
        else:
            text = _range_text(lo, lo_closed, hi, hi_closed, unit)
            if name == "r_load":
                text += "（無負荷は math.inf）"
            bad.append(f"{name} は {text}にする（現在 {v!r}）")
    head = "設計の引数が範囲の外。"
    if not bad:
        sl, sc = math.sqrt(out["inductance"]), math.sqrt(out["capacitance"])
        q_load = out["r_load"] * sc / sl
        f_ratio = 2.0 * math.pi * out["f_ctrl"] * sl * sc
        if q_load < Q_MIN:
            bad.append(f"負荷の Q = r_load·√(capacitance/inductance) は {Q_MIN:g} 以上にする"
                       f"（現在 {q_load:.3g}。負荷がほぼ短絡で、ループのゲインが交差周波数のまわりで"
                       "平らになり、交差周波数が丸めで動く）")
        if f_ratio > F_RATIO_MAX:
            bad.append(f"制御周波数と LC の共振周波数の比 f_ctrl/f0 = 2π·f_ctrl·√(inductance·capacitance) は "
                       f"{F_RATIO_MAX:g} 以下にする（現在 {f_ratio:.3g}）")
        head = "設計の引数の組み合わせが範囲の外。"
    if bad:
        target = _as_real(values.get("phase_margin_deg"))
        raise GainDesignError("invalid_argument", head + "。".join(bad) + "。",
                              math.nan if target is None else target, None)
    return out


def _shape_gains(ki: float, wz: float, zeta: float, t_filter: float) -> tuple[float, float]:
    """零点の形から (Kp, Kd)（物理の単位でも、正規化した形 wz → ωz·Ts、t_filter → Tf/Ts でも同じ式）。"""
    kp = 2.0 * zeta * ki / wz - ki * t_filter
    kd = ki / wz**2 - kp * t_filter
    return kp, kd


def buck_voltage_mode_pid(
    v_in: float, inductance: float, capacitance: float, r_load: float, f_ctrl: float, *,
    zero_ratio: float = 0.5, zero_damping: float = 0.3, filter_ratio: float = 0.25,
    phase_margin_deg: float = 55.0, f_cross_max: float | None = None,
) -> PidGains:
    """電圧モードの降圧コンバータのPIDの初期値（モジュールの説明の手順）。

    v_in, inductance, capacitance, r_load, f_ctrl : 回路の値と制御周波数（SI。範囲は ARGUMENT_RANGES と、
                   組み合わせの Q_MIN・F_RATIO_MAX）。r_load = math.inf は無負荷
    zero_ratio   : PID の零点の角周波数の、LC の共振に対する比 κ（1e-9 ≤ κ ≤ 1）
    zero_damping : PID の零点の減衰係数 ζz（0 < ζz ≤ 2。1 未満で複素数の組、1 で重根、1 より上で2つの実根）
    filter_ratio : 微分のフィルタの時定数の、制御周期に対する比（0〜1000）
    phase_margin_deg : 目標の位相余裕 [deg]（0 < PM < 180）
    f_cross_max  : 交差周波数を探す範囲の上端 [Hz]（既定は f_ctrl/4。ナイキスト周波数の半分）。
                   ナイキスト周波数の NYQUIST_LIMIT 倍より上を指定したら、そこまでに制限し、結果
                   （PidGains.f_cross_max_limited_from）と失敗の文に残す。
    数の引数は、Python の int・float のほか numpy の数（np.int64、np.float32 など）も受け、倍精度に
    直して使う。
    目標を満たせなければ GainDesignError（理由と最良の候補つき。ValueError の一種）。

    零点の範囲（κ ≤ 1、ζz ≤ 2）: 零点は、LC の共振で位相が −180° 近くまで落ちる分を進みで
    取り戻すためのもの。電圧モードの PID（タイプ III）では、2つの零点を共振の少し下〜共振に置く
    （κ = 0.5〜1。ζz = 1 の重根も普通）。κ が 1 を超えると零点が共振の上に来て、共振と零点の間で
    |L| > 1 のまま位相が −180° を下回る（条件つき安定。起動やデューティの飽和でループのゲインが下がると
    不安定になりうる）。初期値の設計としての意味が無いので断る。ζz が 1 を超えると零点は2つの実根
    ωz·(ζz ∓ √(ζz² − 1)) に分かれ、ζz = 2 で 0.27·ωz と 3.7·ωz（比 14。積分の零点を低く、微分の零点を
    共振の近くに置く設計）。それより大きいと上の零点が交差周波数の近くまで上がり、1つの ωz で2つの零点を
    表す意味が無くなる。
    """
    given = {"v_in": v_in, "inductance": inductance, "capacitance": capacitance, "r_load": r_load,
             "f_ctrl": f_ctrl, "zero_ratio": zero_ratio, "zero_damping": zero_damping,
             "filter_ratio": filter_ratio, "phase_margin_deg": phase_margin_deg}
    if f_cross_max is not None:
        given["f_cross_max"] = f_cross_max
    args = _check_arguments(given)                 # 倍精度の値
    v_in, inductance, capacitance, r_load, f_ctrl = (args[k] for k in ("v_in", "inductance", "capacitance",
                                                                       "r_load", "f_ctrl"))
    zero_ratio, zero_damping, filter_ratio = args["zero_ratio"], args["zero_damping"], args["filter_ratio"]
    phase_margin_deg, f_cross_max = args["phase_margin_deg"], args.get("f_cross_max")
    target_text = _number_text(phase_margin_deg)
    ts = 1.0 / f_ctrl
    a, q = _normalize(inductance, capacitance, r_load, ts)      # a = ω0·Ts、q = 1/Q
    b, r = zero_ratio * a, filter_ratio                          # b = ωz·Ts、r = Tf/Ts
    theta_low = 1e-3 * b                                         # 周波数はすべて θ = ω·Ts
    theta_hi = 2.0 * math.pi * f_cross_max * ts if f_cross_max is not None else 0.5 * math.pi
    limited_from = None
    if theta_hi > NYQUIST_LIMIT * math.pi:
        theta_hi, limited_from = NYQUIST_LIMIT * math.pi, f_cross_max
    to_hz = f_ctrl / (2.0 * math.pi)                             # θ → Hz
    f0, f_hi = 1.0 / (2.0 * math.pi * math.sqrt(inductance) * math.sqrt(capacitance)), theta_hi * to_hz
    limit_note = ("" if limited_from is None else
                  f"（指定した上限 {_format_hz(limited_from)} を、ナイキスト周波数の手前の {f_hi:.6g} Hz に"
                  "制限した）")

    def fail(reason: DesignFailure, text: str, best: PidGains | None) -> GainDesignError:
        return GainDesignError(reason, text + limit_note, phase_margin_deg, best)

    if theta_hi <= 1.05 * a:
        raise fail(
            "margin_not_reached",
            f"LC の共振（{f0:.4g} Hz）が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）より上にあるので、"
            "共振の上に交差周波数を置けない。制御周波数を上げる。",
            None,
        )

    plant = _plant_n(a, q)
    kp1, kd1 = _shape_gains(1.0, b, zero_damping, r)
    shape = _Loop(*_factors_n(kp1, 1.0, kd1, r, plant), 1.0, theta_low)

    def margin(theta: np.ndarray) -> np.ndarray:
        return 180.0 + shape.phase_deg(theta)       # Ki によらない

    def candidate(theta_c: float) -> PidGains:
        """交差周波数を θc に置いたゲインと、その確かめ（すべての交差周波数の余裕の最小、閉ループの極）。"""
        ki = 1.0 / abs(shape.response(np.array([theta_c]))[0])
        kp, kd = _shape_gains(ki, b, zero_damping, r)
        loop = _Loop(*_factors_n(kp, ki, kd, r, plant), 1.0, theta_low)
        low = theta_low
        for _ in range(10):        # 下の端では |L| > 1（積分）。そうでなければ、さらに下から囲い込む
            if loop._log_gain(np.array([low]))[0] > 0.0:
                break
            low *= 1e-3
        cross = np.append(loop.crossovers(low), theta_c)          # 設計した点 θc は必ず入れる
        pm = float(np.min(180.0 + loop.phase_deg(cross)))
        return PidGains(float(kp / v_in), float(ki / (v_in * ts)), float(kd * ts / v_in), r * ts,
                        theta_c * to_hz, pm, _radius_n(kp, ki, kd, r, plant),
                        f_search_max=f_hi, f_cross_max_limited_from=limited_from)

    # 共振の上から探索の上端まで、位相余裕が目標を満たす（meets_target。受け入れと同じ判定）最初の区間を探し、
    # その上の端を二分法で詰める（二分法は目標ちょうどを狙う）
    grid = np.geomspace(1.05 * a, theta_hi, GRID_POINTS)
    values = margin(grid)
    meets = values >= phase_margin_deg - TARGET_TOL_DEG
    above = np.nonzero(meets)[0]
    if above.size == 0:
        best = candidate(float(grid[int(np.argmax(values))]))
        largest, shown_target, _ = _compare_text(float(values.max()), phase_margin_deg, "<")
        raise fail(
            "margin_not_reached",
            f"制御周波数 {f_ctrl:g} Hz では、LC の共振（{f0:.4g} Hz）より上の "
            f"{1.05 * f0:.4g}〜{f_hi:.4g} Hz に、"
            f"位相余裕 {shown_target}° を取れる交差周波数が無い（取れるのは最大 {largest}°）。"
            "制御周波数を上げるか、目標の位相余裕を下げる。",
            best,
        )
    i = int(above[0])
    while i + 1 < len(grid) and meets[i + 1]:
        i += 1
    if i == len(grid) - 1:
        best = candidate(float(grid[-1]))
        raise fail(
            "crossover_at_search_edge",
            f"位相余裕が {target_text}° 以上の区間が、交差周波数を探す範囲の上端（{f_hi:.4g} Hz）まで"
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
            f"設計した交差周波数（{gains.f_cross:.4g} Hz）のほかに、位相余裕が目標の {target_text}° に"
            "届かない交差周波数がある。",
            gains,
        )
    stability = stability_of(gains.pole_radius)
    if stability == "unstable":
        raise fail(
            "unstable",
            f"位相余裕は目標を満たすが、離散時間の閉ループの極の半径が {_radius_text(gains.pole_radius)}"
            "（1 以上）で、閉ループが不安定になる。",
            gains,
        )
    if stability == "no_margin":
        raise fail(
            "no_stability_margin",
            f"位相余裕は目標を満たすが、離散時間の閉ループの極の半径が {_radius_text(gains.pole_radius)} で、"
            f"1 との差が {STABILITY_EPS:g} より小さい（いちばん遅い応答の時定数が、制御周期の "
            f"{1 / STABILITY_EPS:,.0f} 倍を超える）。丸めで安定か不安定かが変わる範囲なので、安定とは"
            "言えない（安定の余裕が無い）。"
            "PID の零点（zero_ratio）が LC の共振よりずっと低いか、制御周波数が LC の共振よりずっと高い"
            "（f_ctrl/f0 が大きい）と、こうなる。",
            gains,
        )
    return gains
