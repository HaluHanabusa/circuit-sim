"""T0: 基礎回路（ソルバの検証用）。仕様（層A）をネットリスト（層B）に展開する。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..ir import Component, Netlist, Spec, Step
from ..units import DIMENSION_LABEL, UnitError, to_si

DEFAULT_MAX_STEPS = 20_000    # 既定の刻みと計算時間で出すステップ数の上限（画面の横1列あたり約10点）
DIMENSIONS = {"Vin": "voltage", "R": "resistance", "L": "inductance", "C": "capacitance"}


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
    notes: list[str] = field(default_factory=list)    # 単位の換算、既定の刻みの調整など、利用者に伝えること


def _values(spec: Spec, names: list[str], notes: list[str]) -> dict[str, float]:
    """必要な値をまとめて取り出し、SI単位に換算する。足りないものは一度に全部報告する。"""
    missing = [n for n in names if n not in spec.plant or spec.plant[n].value is None]
    if missing:
        raise MissingValueError(missing)
    values: dict[str, float] = {}
    wrong_units = []
    for n in names:
        q = spec.plant[n]
        try:
            values[n] = to_si(float(q.value), q.unit, DIMENSIONS[n])
        except UnitError as exc:
            wrong_units.append(f"{n}: {exc}")
            continue
        if values[n] != q.value:
            si = DIMENSION_LABEL[DIMENSIONS[n]][1]
            notes.append(f"{n}: {q.value:g} {q.unit.strip()} を {values[n]:g} {si} に換算した")
    if wrong_units:
        raise UnitError("単位を読めない項目がある。" + "。".join(wrong_units))
    bad = [n for n in names if n != "Vin" and values[n] <= 0]
    if bad:
        raise ValueError(f"正の値が必要なパラメータ: {bad}")
    unused = [n for n in spec.plant if n not in names]
    if unused:
        notes.append(f"このテンプレートが使わない項目を無視した: {unused}")
    return values


def _timing(spec: Spec, poles: np.ndarray, notes: list[str]) -> tuple[float, float]:
    """極から計算時間と刻みを決める。仕様に指定があればそれを使う。

    既定: 刻みは最も速い極の周期の1/200、計算時間は最も遅い減衰の時定数の5倍。
    極が大きく離れていてステップ数が DEFAULT_MAX_STEPS を超えるときは、次のどちらかにして知らせる。
      - 最も速い極が振動（複素数）: 刻みを保ち、計算時間を短くする（刻みを粗くすると振動が折り返す）。
      - 実数（過減衰）: 計算時間を保ち、刻みを粗くする。刻みより速い成分は、計算の先頭の
        減衰ステップが振動させずに処理する（遅い側の応答は正しく出る）。
    既定の刻みは、計算時間がちょうど整数ステップになるように合わせる。
    """
    fastest = poles[int(np.argmax(np.abs(poles)))]
    slowest_decay = float(np.min(np.abs(poles.real)))
    t_full = 5.0 / slowest_decay
    t_end = spec.simulation.t_end or t_full
    if spec.simulation.h:
        return t_end, spec.simulation.h

    h = (2.0 * math.pi / abs(fastest)) / 200.0
    if t_end / h > DEFAULT_MAX_STEPS:
        if abs(fastest.imag) > abs(fastest.real) and spec.simulation.t_end is None:
            t_end = DEFAULT_MAX_STEPS * h
            notes.append(
                f"減衰が遅い（収まるまで約 {t_full:.3g} s）ので、既定の計算時間を {t_end:.3g} s"
                f"（{DEFAULT_MAX_STEPS} ステップ）に短くした。全体を見るには simulation.t_end を指定する"
            )
        else:
            h = t_end / DEFAULT_MAX_STEPS
            notes.append(
                f"回路の速い成分（{1.0 / abs(fastest):.3g} s）と遅い成分（{1.0 / slowest_decay:.3g} s）が"
                f"大きく離れているので、既定の刻みを遅い側に合わせて {h:.3g} s"
                f"（{DEFAULT_MAX_STEPS} ステップ）にした。"
                "速い成分の変化を見るには simulation.h と simulation.t_end を指定する"
            )
    n_steps = max(1, round(t_end / h))
    return t_end, t_end / n_steps


def rc_lowpass(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - C。出力はコンデンサ電圧 v(out)。"""
    notes: list[str] = []
    p = _values(spec, ["Vin", "R", "C"], notes)
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=p["Vin"])),
        Component(id="R1", type="R", nodes=("in", "out"), value=p["R"]),
        Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]),
    ])
    poles = np.array([-1.0 / (p["R"] * p["C"])], dtype=complex)
    t_end, h = _timing(spec, poles, notes)
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(V1)"], notes=notes)


def rlc_series(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - L - C の直列。出力はコンデンサ電圧 v(out)。"""
    notes: list[str] = []
    p = _values(spec, ["Vin", "R", "L", "C"], notes)
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=p["Vin"])),
        Component(id="R1", type="R", nodes=("in", "a"), value=p["R"]),
        Component(id="L1", type="L", nodes=("a", "out"), value=p["L"]),
        Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]),
    ])
    # s^2 + (R/L) s + 1/(LC) = 0
    poles = np.roots([1.0, p["R"] / p["L"], 1.0 / (p["L"] * p["C"])]).astype(complex)
    t_end, h = _timing(spec, poles, notes)
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(L1)"], notes=notes)
