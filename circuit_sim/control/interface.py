"""制御器の口。

simulate() は、この形をしたものを制御周期ごとに呼ぶ。
  - 制御周期 period は時間刻みの整数倍にする。
  - 時刻 j * period（j = 0, 1, 2, ...）に inputs の信号を測り、step() に渡す。
    渡す値は、結果に記録されるその時刻の値と同じ（その時刻の切り替えの直前の値）。
  - step() が返した指令は、次の制御周期の頭（(j + 1) * period）から1周期のあいだ使われる
    （1サンプル遅れ）。
  - 最初の制御周期（0 <= t < period）には、reset() が返した指令を使う。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Controller(Protocol):
    """離散時間の制御器。

    period  : 制御周期 [s]
    inputs  : 測る信号の名前（"v(out)" や "i(L1)"。回路の変数の名前）
    outputs : 出す指令の名前（PWMの duty などから名前で参照する）
    """

    period: float
    inputs: Sequence[str]
    outputs: Sequence[str]

    def reset(self) -> Sequence[float]:
        """内部状態を初期化し、最初の制御周期に使う指令を outputs の並びで返す。

        計算のたびに最初に1回呼ばれる。同じ入力から同じ結果を出すために、
        積分器などの内部状態はここで必ず初期値に戻す。
        """
        ...

    def step(self, t: float, measured: np.ndarray) -> Sequence[float]:
        """時刻 t の測定値（inputs の並び）から、次の制御周期に使う指令を outputs の並びで返す。"""
        ...
