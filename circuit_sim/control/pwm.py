"""PWMのゲート信号。

切り替えの時刻は時間刻みの格子点に丸める。キャリアの1周期を N ステップに分けたとき、
オンにするステップ数は整数なので、1周期のデューティの分解能は 1 / N になる。
丸めの余りは次の周期に持ち越す（既定）。こうすると、数周期の平均では指令どおりのデューティになり、
閉ループで積分器が隣り合う2つの値を行き来して出力が振れるのを防げる。
"""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..messages import CodedError, Value


class Pwm(BaseModel):
    """キャリアとデューティ指令を比べて作るゲート信号。

    name      : ゲート信号の名前。スイッチ（S）の gate から参照する。
    frequency : キャリアの周波数 [Hz]。周期は時間刻みの整数倍にする。
    duty      : デューティ指令。固定値（0〜1）、または制御器が出す指令の名前。
                指令は 0〜1 の外に出たら 0 と 1 で止める。指令はキャリアの周期の先頭で取り込み、
                その周期のあいだは変えない。
    carrier   : "sawtooth" = のこぎり波（周期の先頭でオン、duty * 周期 が経ったらオフ）、
                "triangle" = 三角波（オンの区間が周期の中央に来る）。
    carry_remainder : 格子点に丸めた余りを次の周期に持ち越す（既定 True）。False にすると、
                毎周期 round(duty * N) ステップになる。
    complement : 相補のゲート信号の名前（省略できる）。指定すると、name がオフの区間だけオンになる
                ゲート信号をこの名前で出す。ブリッジのレグの上下のスイッチを動かすのに使う
                （上のスイッチが name、下のスイッチが complement）。
    dead_time : 上下のスイッチを両方オフにする時間 [s]。いまは 0 だけ（0 以外はエラー）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    frequency: float = Field(gt=0)
    duty: float | str
    carrier: Literal["sawtooth", "triangle"] = "sawtooth"
    carry_remainder: bool = True
    complement: str | None = None
    dead_time: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> Pwm:
        if not math.isfinite(self.frequency):
            raise CodedError("PWM_FREQUENCY_NOT_FINITE", name=self.name)
        if self.complement is not None and (not self.complement or self.complement == self.name):
            raise CodedError("PWM_COMPLEMENT_NAME", name=self.name)
        if self.dead_time != 0.0:
            raise CodedError("PWM_DEAD_TIME", name=self.name, dead_time=Value(self.dead_time, "s"))
        if isinstance(self.duty, str):
            if not self.duty:
                raise CodedError("PWM_DUTY_NAME_EMPTY", name=self.name)
        elif not (math.isfinite(self.duty) and 0.0 <= self.duty <= 1.0):
            raise CodedError("PWM_DUTY_RANGE", name=self.name, duty=Value(self.duty))
        return self

    def on_counts(
        self, duty: float, steps_per_period: int, n_periods: int, carry: float
    ) -> tuple[np.ndarray, float]:
        """続く n_periods 周期の、オンにするステップ数（整数の配列）と、次に持ち越す余り。

        duty はその間のデューティ指令（一定）、carry は前の周期から持ち越した余り（ステップ単位）。
        余りを持ち越すと、j 周期目までのオンの合計が round(j * duty * N + carry) になる。
        """
        if not self.carry_remainder:
            return np.full(n_periods, on_steps(duty, steps_per_period), dtype=np.int64), 0.0
        exact = min(max(duty, 0.0), 1.0) * steps_per_period
        edges = np.floor(np.arange(1, n_periods + 1) * exact + carry + 0.5)
        counts = np.diff(edges, prepend=0.0)
        counts = np.clip(counts, 0, steps_per_period).astype(np.int64)
        left = carry + n_periods * exact - float(counts.sum())
        return counts, min(max(left, -0.5), 0.5)

    @property
    def outputs(self) -> list[str]:
        """このPWMが出すゲート信号の名前（相補のゲートがあれば2つ）。"""
        return [self.name] if self.complement is None else [self.name, self.complement]

    def pattern(self, k: np.ndarray, steps_per_period: int, n_on: np.ndarray | int) -> np.ndarray:
        """ステップ番号 k（配列）の区間 [t[k], t[k+1]) でオンかどうか。

        n_on は、k が属する周期のオンのステップ数（on_counts の結果を k ごとに並べたもの）。
        """
        return gate_pattern(self.carrier, k, steps_per_period, n_on)


def on_steps(duty: float, steps_per_period: int) -> int:
    """1周期 steps_per_period ステップのうち、オンにするステップ数（四捨五入）。"""
    d = min(max(duty, 0.0), 1.0)
    return int(math.floor(d * steps_per_period + 0.5))


def gate_pattern(
    carrier: str, k: np.ndarray, steps_per_period: int, n_on: np.ndarray | int
) -> np.ndarray:
    """ステップ番号 k（配列）の区間 [t[k], t[k+1]) でゲートがオンかどうか。n_on は周期ごとに変えてよい。

    のこぎり波: 周期の先頭から n_on ステップがオン。
    三角波    : 周期の中央に置いた n_on ステップがオン（残りが奇数ステップなら半ステップ前に寄る）。
    """
    phase = k % steps_per_period
    if carrier == "sawtooth":
        return phase < n_on
    if carrier == "triangle":
        first = (steps_per_period - n_on) // 2
        return (phase >= first) & (phase < first + n_on)
    raise CodedError("CARRIER_UNKNOWN", carrier=str(carrier))
