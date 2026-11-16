"""時刻で決まる区分一定の値（指令値の段差、負荷の段差など）と、それを指令として出す制御器。"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

TIME_TOL = 1e-6          # 時刻の比較の許容（制御周期に対する比）。格子の時刻 k·h の丸め誤差を吸収する


@dataclass(frozen=True)
class Schedule:
    """initial から始まり、changes の（時刻 [s]、値）で切り替わる区分一定の値。時刻は昇順。"""

    initial: float
    changes: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        times = [t for t, _ in self.changes]
        values = [self.initial] + [v for _, v in self.changes]
        if not all(math.isfinite(v) for v in values) or not all(math.isfinite(t) and t >= 0 for t in times):
            raise ValueError(f"区分一定の値の時刻と値は有限値にする（時刻は 0 以上）: {self}")
        if any(b < a for a, b in zip(times, times[1:], strict=False)):
            raise ValueError(f"区分一定の値の切り替えの時刻は昇順に並べる: {times}")

    @classmethod
    def constant(cls, value: float) -> Schedule:
        return cls(float(value))

    def at(self, t: float, tol: float = 0.0) -> float:
        """時刻 t の値（t + tol までに切り替わったものを含める）。"""
        value = self.initial
        for time, v in self.changes:
            if time <= t + tol:
                value = v
            else:
                break
        return value


class ScheduleController:
    """時刻で決まる指令を出す制御器（Controller の口。測る信号は無い）。

    指令は次の制御周期の頭から使われる（1サンプル遅れ）ので、時刻 t の演算では t + period の値を返す。
    こうすると、指令は予定の時刻（制御周期の格子に切り上げた時刻）ちょうどに変わる。
    """

    inputs: Sequence[str] = ()

    def __init__(self, schedule: Schedule, output: str, period: float):
        self.schedule = schedule
        self.outputs = (output,)
        self.period = float(period)

    def reset(self) -> list[float]:
        return [self.schedule.at(0.0, TIME_TOL * self.period)]

    def step(self, t: float, measured: np.ndarray) -> list[float]:
        return [self.schedule.at(t + self.period, TIME_TOL * self.period)]
