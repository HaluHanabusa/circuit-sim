"""離散時間の制御ブロック（リミッタ、PID）。

ブロックは1回の update() が1サンプル。時刻や信号の名前は知らない（それは controllers.py の役目）。
カスケード制御（電圧の外側のループと電流の内側のループなど）は、ブロックを組み合わせて作る。
"""
from __future__ import annotations

import math
from typing import Literal

AntiWindup = Literal["clamp", "none"]
DerivativeOn = Literal["error", "measurement"]


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
    微分を掛けるもの（derivative_on）:
      "error"      : 偏差 e に掛ける（上の式。既定）。
      "measurement": 測定値 y に掛ける。D[k] = a·D[k−1] − b·(y[k] − y[k−1])（y[−1] = y[0]）。
                     偏差 e = r − y の微分から指令値 r の変化を除いたもので、指令値の段差で出力が跳ねない。
                     帰還の経路（y から u）は "error" と同じなので、開ループと安定余裕は変わらない。
                     update() に測定値を渡す。
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
        derivative_on: DerivativeOn = "error",
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
        if derivative_on not in ("error", "measurement"):
            raise ValueError(f"derivative_on は 'error' か 'measurement'（現在 {derivative_on!r}）")
        self.kp, self.ki, self.kd = float(kp), float(ki), float(kd)
        self.period = float(period)
        self.t_filter = float(t_filter)
        self.limiter = Limiter(lower, upper)
        self.anti_windup = anti_windup
        self.derivative_on = derivative_on
        self._a = self.t_filter / (self.t_filter + self.period)
        self._b = self.kd / (self.t_filter + self.period)
        self.reset()

    def reset(self) -> None:
        """内部状態（積分、微分、前回の偏差）を初期値に戻す。"""
        self.integral = 0.0
        self.derivative = 0.0
        self._d_prev: float | None = None     # 前回の、微分を掛けるもの（−y か e）

    def update(self, e: float, measurement: float | None = None) -> float:
        """偏差 e[k]（と、微分を測定値に掛けるときは測定値 y[k]）から出力 u[k] を計算し、内部状態を進める。"""
        e = float(e)
        if self.derivative_on == "measurement":
            if measurement is None:
                raise ValueError("微分を測定値に掛けるPIDには、update() に測定値を渡す")
            d_in = -float(measurement)
        else:
            d_in = e
        d_prev = d_in if self._d_prev is None else self._d_prev
        self.derivative = self._a * self.derivative + self._b * (d_in - d_prev)
        self._d_prev = d_in
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
