"""T0: 基礎回路（ソルバの検証用）。仕様（層A）をネットリスト（層B）に展開する。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..ir import Component, Netlist, Spec, Step
from ..units import DIMENSION_LABEL, UnitError, parse_unit, to_si

DEFAULT_MAX_STEPS = 20_000    # 既定の刻みと計算時間で出すステップ数の上限（画面の横1列あたり約10点）
PHASE_NOTE = 0.008            # 振動の位相のずれの見積もりが入力のこの比を超えたら知らせる。
#                               基準は解析解との差 1% で、
#                               見積もりと実測の差（rlc_series で 0〜7%）を見込んで 0.8% にした
PHASE_TARGET = 0.005          # 注意で勧める刻みで見込む、位相のずれ
DIMENSIONS = {"Vin": "voltage", "R": "resistance", "L": "inductance", "C": "capacitance"}


def _readable(unit: str) -> bool:
    try:
        parse_unit(unit)
    except UnitError:
        return False
    return True


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
    kinds = set()      # "読めない" / "量の種類が違う"
    for n in names:
        q = spec.plant[n]
        try:
            values[n] = to_si(float(q.value), q.unit, DIMENSIONS[n])
        except UnitError as exc:
            wrong_units.append(f"{n}: {exc}")
            kinds.add("読めない" if not _readable(q.unit) else "量の種類が違う")
            continue
        if values[n] != q.value:
            si = DIMENSION_LABEL[DIMENSIONS[n]][1]
            notes.append(f"{n}: {q.value:g} {q.unit.strip()} を {values[n]:g} {si} に換算した")
    if wrong_units:
        heading = {
            frozenset({"読めない"}): "単位を読めない項目がある。",
            frozenset({"量の種類が違う"}): "単位の量の種類が違う項目がある。",
        }.get(frozenset(kinds), "単位を読めない、または量の種類が違う項目がある。")
        raise UnitError(heading + "。".join(wrong_units))
    bad = [n for n in names if n != "Vin" and values[n] <= 0]
    if bad:
        raise ValueError(f"正の値が必要なパラメータ: {bad}")
    unused = [n for n in spec.plant if n not in names]
    if unused:
        notes.append(f"このテンプレートが使わない項目を無視した: {unused}")
    return values


def _phase_error(poles: np.ndarray, t_end: float, h: float) -> tuple[float, float, complex | None]:
    """台形則の位相のずれで、振動の成分が入力に対してどれだけずれるかの見積もり（比）。

    戻り値: （見積もり、頭打ちにする前の見積もり、その極）。
    台形則の振動の角周波数は (2/h)·atan(ωh/2) で、相対的に約 (ωh)² / 12 遅れる。時刻 t までに
    位相が d·t（d = ω·(ωh)² / 12）ずれ、振幅 e^(−αt) の成分（ステップ応答では入力と同じ大きさ）が
    その分ずれる。ずれは振れ幅の2倍を超えないので、見積もりは計算時間の中での
    e^(−αt)·min(d·t, 2) の最大（d·t = 2 で頭打ちになるなら、その時刻での値）。
    頭打ちにする前の値 e^(−αt)·d·t の最大は t = min(1/α, t_end) にあり、h² に比例する。
    勧める刻みはこちらから逆算する（頭打ちの値からだと、ずれを小さく見て刻みを粗く勧めてしまう）。
    """
    worst, worst_raw, which = 0.0, 0.0, None
    for p in poles:
        omega, alpha = abs(p.imag), abs(p.real)
        if omega <= alpha:          # 振動しない（または減衰の方が速い）成分は数えない
            continue
        drift = omega * (omega * h) ** 2 / 12.0
        t_peak = t_end if alpha * t_end <= 1.0 else 1.0 / alpha
        raw = math.exp(-alpha * t_peak) * drift * t_peak
        t_cap = 2.0 / drift                         # この時刻でずれが振れ幅の2倍に届く
        error = 2.0 * math.exp(-alpha * t_cap) if t_cap < t_peak else raw
        if raw > worst_raw:
            worst, worst_raw, which = error, raw, p
    return worst, worst_raw, which


def _timing(spec: Spec, poles: np.ndarray, notes: list[str]) -> tuple[float, float]:
    """極から計算時間と刻みを決める。仕様に指定があればそれを使う。

    既定: 刻みは最も速い極の周期の1/200、計算時間は最も遅い減衰の時定数の5倍。
    極が大きく離れていてステップ数が DEFAULT_MAX_STEPS を超えるときは、次のどれかにして知らせる。
      - 最も速い極が振動（複素数）で、計算時間の指定が無い: 刻みを保ち、計算時間を短くする
        （刻みを粗くすると振動が折り返す）。
      - 最も速い極が振動で、計算時間の指定がある: 指定を守り、刻みを粗くする。
      - 実数（過減衰）: 計算時間を保ち、刻みを粗くする。刻みより速い成分は、計算の先頭の
        減衰ステップが振動させずに処理する（遅い側の応答は正しく出る）。
    どの場合も（simulation.h の指定があっても）、振動の位相のずれ（_phase_error）が
    入力の PHASE_NOTE を超える見込みなら、
    精度が落ちることと直し方を知らせる。
    既定の刻みは、計算時間がちょうど整数ステップになるように合わせる。
    """
    fastest = poles[int(np.argmax(np.abs(poles)))]
    slowest_decay = float(np.min(np.abs(poles.real)))
    t_full = 5.0 / slowest_decay
    t_end = spec.simulation.t_end or t_full
    if spec.simulation.h:
        h = spec.simulation.h
    else:
        h = (2.0 * math.pi / abs(fastest)) / 200.0
        if t_end / h > DEFAULT_MAX_STEPS:
            oscillating = abs(fastest.imag) > abs(fastest.real)
            if oscillating and spec.simulation.t_end is None:
                t_end = DEFAULT_MAX_STEPS * h
                notes.append(
                    f"減衰が遅い（収まるまで約 {t_full:.3g} s）ので、既定の計算時間を {t_end:.3g} s"
                    f"（{DEFAULT_MAX_STEPS} ステップ）に短くした。全体を見るには simulation.t_end を指定する"
                )
            elif oscillating:
                period = 2.0 * math.pi / abs(fastest.imag)
                h = t_end / DEFAULT_MAX_STEPS
                notes.append(
                    f"指定の t_end={t_end:.6g} s は回路の振動の周期 {period:.3g} s の"
                    f" {t_end / period:.3g} 倍あり、"
                    f"既定の刻み（1周期 200 点）では {DEFAULT_MAX_STEPS} ステップを超えるので、"
                    f"刻みを {h:.3g} s（1周期 {period / h:.3g} 点、{DEFAULT_MAX_STEPS} ステップ）に粗くした"
                )
            else:
                h = t_end / DEFAULT_MAX_STEPS
                notes.append(
                    f"回路の速い成分（{1.0 / abs(fastest):.3g} s）と遅い成分（{1.0 / slowest_decay:.3g} s）が"
                    f"大きく離れているので、既定の刻みを遅い側に合わせて {h:.3g} s"
                    f"（{DEFAULT_MAX_STEPS} ステップ）にした。"
                    "速い成分の変化を見るには simulation.h と simulation.t_end を指定する"
                )
        h = t_end / max(1, round(t_end / h))
    error, raw, pole = _phase_error(poles, t_end, h)
    if error > PHASE_NOTE and pole is not None:
        period = 2.0 * math.pi / abs(pole.imag)
        # 頭打ちにする前のずれは (ωh)² に比例するので、ずれを PHASE_TARGET にする刻み
        h_fine = h * math.sqrt(PHASE_TARGET / raw)
        notes.append(
            f"回路の振動（周期 {period:.3g} s）を1周期 {period / h:.3g} 点で計算するので、"
            "台形則の位相のずれが"
            f"積み上がり、計算の精度が落ちる（振動の波形が入力の約 {error:.1%} ずれる見込み）。"
            f"ずれを入力の {PHASE_TARGET:.1%} 程度にするには、simulation.h を {h_fine:.3g} s 以下"
            f"（1周期 {period / h_fine:.3g} 点以上）にするか、simulation.t_end を短くする"
        )
    return t_end, h


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
