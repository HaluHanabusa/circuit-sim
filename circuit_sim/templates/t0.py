"""T0: 基礎回路（ソルバの検証用）。仕様（層A）をネットリスト（層B）に展開する。"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..ir import Component, Netlist, Spec, Step


class MissingValueError(ValueError):
    """仕様に値が入っていないパラメータがある。"""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"値が入っていないパラメータ: {names}")


@dataclass
class Expanded:
    netlist: Netlist
    t_end: float
    h: float
    probes: list[str]


def _values(spec: Spec, names: list[str]) -> dict[str, float]:
    """必要な値をまとめて取り出す。足りないものは一度に全部報告する。"""
    missing = [n for n in names if n not in spec.plant or spec.plant[n].value is None]
    if missing:
        raise MissingValueError(missing)
    values = {n: float(spec.plant[n].value) for n in names}
    bad = [n for n in names if n != "Vin" and values[n] <= 0]
    if bad:
        raise ValueError(f"正の値が必要なパラメータ: {bad}")
    return values


def _timing(spec: Spec, poles: np.ndarray) -> tuple[float, float]:
    """極から計算時間と刻みを決める。仕様に指定があればそれを使う。

    刻み: 最も速い極の周期の1/200。計算時間: 最も遅い減衰の時定数の5倍。
    """
    fastest = float(np.max(np.abs(poles)))
    slowest_decay = float(np.min(np.abs(poles.real)))
    h = spec.simulation.h or (2.0 * math.pi / fastest) / 200.0
    t_end = spec.simulation.t_end or 5.0 / slowest_decay
    return t_end, h


def rc_lowpass(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - C。出力はコンデンサ電圧 v(out)。"""
    p = _values(spec, ["Vin", "R", "C"])
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=p["Vin"])),
        Component(id="R1", type="R", nodes=("in", "out"), value=p["R"]),
        Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]),
    ])
    poles = np.array([-1.0 / (p["R"] * p["C"])], dtype=complex)
    t_end, h = _timing(spec, poles)
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(V1)"])


def rlc_series(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - L - C の直列。出力はコンデンサ電圧 v(out)。"""
    p = _values(spec, ["Vin", "R", "L", "C"])
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=p["Vin"])),
        Component(id="R1", type="R", nodes=("in", "a"), value=p["R"]),
        Component(id="L1", type="L", nodes=("a", "out"), value=p["L"]),
        Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]),
    ])
    # s^2 + (R/L) s + 1/(LC) = 0
    poles = np.roots([1.0, p["R"] / p["L"], 1.0 / (p["L"] * p["C"])]).astype(complex)
    t_end, h = _timing(spec, poles)
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(L1)"])
