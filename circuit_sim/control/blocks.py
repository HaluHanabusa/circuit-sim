"""離散時間の制御ブロック（リミッタ、PID）。

ブロックは1回の update() が1サンプル。時刻や信号の名前は知らない（それは controllers.py の役目）。
カスケード制御（電圧の外側のループと電流の内側のループなど）は、ブロックを組み合わせて作る。
"""
from __future__ import annotations

import math
from typing import Literal

AntiWindup = Literal["clamp", "none"]


class Limiter:
    """値を lower〜upper で止める。片側だけなら、もう一方を ±inf にする。"""

    def __init__(self, lower: float = -math.inf, upper: float = math.inf):
        if math.isnan(lower) or math.isnan(upper) or lower > upper:
            raise ValueError(f"リミッタの下限（{lower}）は上限（{upper}）以下にする")
        self.lower = float(lower)
        self.upper = float(upper)

    def __call__(self, x: float) -> float:
        return min(max(x, self.lower), self.upper)

    def saturation(self, x: float) -> int:
        """x が上限を超えるなら +1、下限を下回るなら -1、範囲の中なら 0。"""
        return 1 if x > self.upper else -1 if x < self.lower else 0


class Pid:
    """離散時間のPID（並列形）。出力の上下限と、積分の飽和を防ぐ処理つき。

    1回の update(e) が1サンプル（周期 Ts = period）。差分方程式:
      P[k] = Kp·e[k]
      I[k] = I[k−1] + Ki·Ts·e[k]                   （= Ki·Ts·Σe。I[−1] = 0）
      D[k] = a·D[k−1] + b·(e[k] − e[k−1])           a = Tf / (Tf + Ts)、b = Kd / (Tf + Ts)
             Kd·s / (1 + s·Tf) を後退差分で離散化したもの（Tf = 0 なら Kd·(e[k] − e[k−1]) / Ts）。
             reset の直後の最初のサンプルは e[−1] = e[0]、D[−1] = 0（微分の項は 0。起動で跳ねない）
      u[k] = clip(P[k] + I[k] + D[k], lower, upper)
    積分の飽和を防ぐ処理（anti_windup）:
      "clamp": 積分を進める前の出力 P[k] + I[k−1] + D[k] が上限以上、かつ Ki·e[k] > 0
               （下限以下、かつ Ki·e[k] < 0）のときは、積分を進めない（I[k] = I[k−1]）。
               出力が上下限に張り付いている間、積分は張り付く向きには増えない。
      "none" : 処理なし（比較用）。
    偏差 e の単位を V、出力をデューティ（無次元）とすると、Kp [1/V]、Ki [1/(V·s)]、Kd [s/V]。
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float = 0.0,
        *,
        period: float,
        t_filter: float = 0.0,
        lower: float = -math.inf,
        upper: float = math.inf,
        anti_windup: AntiWindup = "clamp",
    ):
        for name, v in (("kp", kp), ("ki", ki), ("kd", kd)):
            if not math.isfinite(v):
                raise ValueError(f"PIDの {name} は有限値が必要（現在 {v}）")
        if not (math.isfinite(period) and period > 0):
            raise ValueError(f"PIDの周期 period は正の有限値が必要（現在 {period}）")
        if not (math.isfinite(t_filter) and t_filter >= 0):
            raise ValueError(f"微分のフィルタの時定数 t_filter は 0 以上の有限値にする（現在 {t_filter}）")
        if anti_windup not in ("clamp", "none"):
            raise ValueError(f"anti_windup は 'clamp' か 'none'（現在 {anti_windup!r}）")
        self.kp, self.ki, self.kd = float(kp), float(ki), float(kd)
        self.period = float(period)
        self.t_filter = float(t_filter)
        self.limiter = Limiter(lower, upper)
        self.anti_windup = anti_windup
        self._a = self.t_filter / (self.t_filter + self.period)
        self._b = self.kd / (self.t_filter + self.period)
        self.reset()

    def reset(self) -> None:
        """内部状態（積分、微分、前回の偏差）を初期値に戻す。"""
        self.integral = 0.0
        self.derivative = 0.0
        self._e_prev: float | None = None

    def update(self, e: float) -> float:
        """偏差 e[k] から出力 u[k] を計算し、内部状態を進める。"""
        e = float(e)
        e_prev = e if self._e_prev is None else self._e_prev
        self.derivative = self._a * self.derivative + self._b * (e - e_prev)
        self._e_prev = e
        proportional = self.kp * e
        if self.anti_windup == "clamp":
            v = proportional + self.integral + self.derivative
            pushing = 1 if v >= self.limiter.upper else -1 if v <= self.limiter.lower else 0
            if pushing == 0 or pushing * self.ki * e <= 0:
                self.integral += self.ki * self.period * e
            # 張り付いていて、偏差がさらに張り付く向きなら積分しない
        else:
            self.integral += self.ki * self.period * e
        return self.limiter(proportional + self.integral + self.derivative)
