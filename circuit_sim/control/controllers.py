"""制御ブロックを Controller の口（period、inputs、outputs、reset()、step()）に合わせた制御器。"""
from __future__ import annotations

import numpy as np

from .blocks import Pid
from .schedule import TIME_TOL, Schedule


class PidController:
    """偏差 = 指令値 − 測定値 にPIDを掛けて、指令を1つ出す制御器。

    pid       : PIDのブロック。制御周期は pid.period。
                微分を偏差と測定値のどちらに掛けるかは pid.derivative_on。
    measure   : 測る信号の名前（"v(out)" など）。
    output    : 出す指令の名前（PWMの duty や、指令で決まる電源から名前で参照する）。
    reference : 指令値。数値、または時刻で決まる区分一定の値（Schedule）。測定の時刻 t の値を使う。
    initial_output : 最初の制御周期（演算の結果がまだ無い間）に使う指令。上下限で止める（既定 0）。

    遅れ: 時刻 t に測った値から計算した指令は、t + period から1周期使われる（simulate() の決まり）。
    PWMはその指令を周期のあいだ保持するので、小信号の遅れは演算1サンプル＋保持0.5サンプル。
    """

    def __init__(
        self,
        pid: Pid,
        measure: str,
        output: str,
        reference: float | Schedule,
        initial_output: float = 0.0,
    ):
        self.pid = pid
        self.period = pid.period
        self.inputs = (measure,)
        self.outputs = (output,)
        self.reference = reference if isinstance(reference, Schedule) else Schedule.constant(reference)
        self.initial_output = float(initial_output)

    def reset(self) -> list[float]:
        self.pid.reset()
        return [self.pid.limiter(self.initial_output)]

    def step(self, t: float, measured: np.ndarray) -> list[float]:
        r = self.reference.at(t, TIME_TOL * self.period)
        y = float(measured[0])
        if self.pid.derivative_on == "measurement":
            return [self.pid.update(r - y, measurement=y)]
        return [self.pid.update(r - y)]
