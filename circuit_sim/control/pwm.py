"""PWMのゲート信号。

切り替えの時刻は時間刻みの格子点に丸める。搬送波の1周期を N ステップに分けたとき、
オンにするステップ数は round(duty * N) で、デューティの分解能は 1 / N になる。
"""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Pwm(BaseModel):
    """搬送波とデューティ指令を比べて作るゲート信号。

    name      : ゲート信号の名前。スイッチ（S）の gate から参照する。
    frequency : 搬送波の周波数 [Hz]。周期は時間刻みの整数倍にする。
    duty      : デューティ指令。固定値（0〜1）、または制御器が出す指令の名前。
                指令は 0〜1 の外に出たら 0 と 1 で止める。指令が周期の途中で変わったら、
                その時点から新しい値で比べる。
    carrier   : "sawtooth" = のこぎり波（周期の頭でオン、duty * 周期 が経ったらオフ）、
                "triangle" = 三角波（オンの区間が周期の中央に来る）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    frequency: float = Field(gt=0)
    duty: float | str
    carrier: Literal["sawtooth", "triangle"] = "sawtooth"

    @model_validator(mode="after")
    def _check(self) -> Pwm:
        if not math.isfinite(self.frequency):
            raise ValueError(f"{self.name}: frequency は有限値が必要")
        if isinstance(self.duty, str):
            if not self.duty:
                raise ValueError(f"{self.name}: duty に指令の名前を使うなら空にしない")
        elif not (math.isfinite(self.duty) and 0.0 <= self.duty <= 1.0):
            raise ValueError(f"{self.name}: duty の固定値は 0〜1 にする（現在 {self.duty}）")
        return self

    def pattern(self, k: np.ndarray, steps_per_period: int, duty: float) -> np.ndarray:
        """ステップ番号 k（配列）の区間 [t[k], t[k+1]) でオンかどうか。計算の入口はこれだけを呼ぶ。

        duty は、その区間で使うデューティ指令（固定値、または制御器の指令のいまの値）。
        """
        return gate_pattern(self.carrier, k, steps_per_period, on_steps(duty, steps_per_period))


def on_steps(duty: float, steps_per_period: int) -> int:
    """1周期 steps_per_period ステップのうち、オンにするステップ数（四捨五入）。"""
    d = min(max(duty, 0.0), 1.0)
    return int(math.floor(d * steps_per_period + 0.5))


def gate_pattern(carrier: str, k: np.ndarray, steps_per_period: int, n_on: int) -> np.ndarray:
    """ステップ番号 k（配列）の区間 [t[k], t[k+1]) でゲートがオンかどうか。

    のこぎり波: 周期の頭から n_on ステップがオン。
    三角波    : 周期の中央に置いた n_on ステップがオン（残りが奇数ステップなら半ステップ前に寄る）。
    """
    phase = k % steps_per_period
    if carrier == "sawtooth":
        return phase < n_on
    if carrier == "triangle":
        first = (steps_per_period - n_on) // 2
        return (phase >= first) & (phase < first + n_on)
    raise ValueError(f"未対応の搬送波 '{carrier}'")
