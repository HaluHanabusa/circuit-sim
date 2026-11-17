"""T0: 基礎回路（ソルバの検証用）。仕様（層A）をネットリスト（層B）に展開する。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..control import Controller, Pwm
from ..ir import Component, Netlist, Spec, Step
from ..messages import CodedError, Message, Names, Value
from ..signals import SignalInfo, merge_signal_info
from ..simulate import Result, simulate
from ..units import DIMENSION_LABEL, UnitError, parse_unit, to_si
from .items import MissingValueError, units_error

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


@dataclass
class Expanded:
    """テンプレートを展開した結果（層Bと、計算に渡すもの）。

    probes      : 要約に出す信号の名前
    notes       : 単位の換算、既定の刻みの調整など、利用者に伝えること（番号つき。messages.Message）
    gates, controllers : simulate() に渡すゲート信号と制御器（スイッチや制御の無い回路では空）
    signals     : 結果の信号の構造化した情報（signals.SignalInfo: 名前、単位、既定で表示するか、種類、
                  節点・素子・正の向き、内部の印）。表示する信号（画面仕様 付録A の信号の表の順）と、
                  画面に出さない内部の信号（internal=True）の両方を、結果の名前に漏れなく並べる。
                  説明の文言は画面の辞書が持つ
    fidelity    : モデルの種類（"switching" / "average"。モデルを選べないテンプレートでは None）
    """

    netlist: Netlist
    t_end: float
    h: float
    probes: list[str]
    notes: list[Message] = field(default_factory=list)
    gates: list[Pwm] = field(default_factory=list)
    controllers: list[Controller] = field(default_factory=list)
    signals: list[SignalInfo] = field(default_factory=list)
    fidelity: str | None = None

    def run(self, **kwargs) -> Result:
        """展開した回路を計算する（simulate() に、刻み・ゲート信号・制御器を渡す）。

        kwargs は simulate() にそのまま渡す（progress、cancel、max_steps など）。
        結果の notes の先頭には、展開で出た注意（self.notes）が入る。結果の signal_info には、
        テンプレートが決めた表示する信号と内部の信号の区別が入る（決めていない名前は内部とみなす）。
        """
        res = simulate(
            self.netlist, self.t_end, self.h, gates=self.gates, controllers=self.controllers, **kwargs
        )
        res.notes[:0] = self.notes
        res.signal_info = merge_signal_info(res.signal_info, self.signals)
        return res


def _values(spec: Spec, names: list[str], notes: list[Message]) -> dict[str, float]:
    """必要な値をまとめて取り出し、SI単位に換算する。足りないものは一度に全部報告する。"""
    missing = [n for n in names if n not in spec.plant or spec.plant[n].value is None]
    if missing:
        raise MissingValueError(missing)
    values: dict[str, float] = {}
    problems = []      # （項目、書かれた単位、項目の単位、読めないか、UnitError）
    for n in names:
        q = spec.plant[n]
        si = DIMENSION_LABEL[DIMENSIONS[n]][1]
        try:
            values[n] = to_si(float(q.value), q.unit, DIMENSIONS[n])
        except UnitError as exc:
            problems.append((n, q.unit, si, not _readable(q.unit), exc))
            continue
        if values[n] != q.value:
            notes.append(Message("UNIT_CONVERTED", key=n, value=Value(q.value, q.unit.strip()),
                                 si=Value(values[n], si)))
    if problems:
        raise units_error(problems)
    bad = [n for n in names if n != "Vin" and values[n] <= 0]
    if bad:
        raise CodedError("VALUES_NOT_POSITIVE", names=Names(bad))
    unused = [n for n in spec.plant if n not in names]
    if unused:
        notes.append(Message("UNUSED_ITEMS", items=Names(unused)))
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


def _timing(spec: Spec, poles: np.ndarray, notes: list[Message]) -> tuple[float, float]:
    """極から終了時刻と刻みを決める。仕様に指定があればそれを使う。

    既定: 刻みは最も速い極の周期の1/200、終了時刻は最も遅い減衰の時定数の5倍。
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
                notes.append(Message("DEFAULT_T_END_SHORTENED", t_full=Value(t_full, "s"),
                                     t_end=Value(t_end, "s"), n=DEFAULT_MAX_STEPS))
            elif oscillating:
                period = 2.0 * math.pi / abs(fastest.imag)
                h = t_end / DEFAULT_MAX_STEPS
                notes.append(Message(
                    "DEFAULT_H_COARSENED", t_end=Value(t_end, "s"), period=Value(period, "s"),
                    ratio=Value(t_end / period), n=DEFAULT_MAX_STEPS, h=Value(h, "s"),
                    points=Value(period / h),
                ))
            else:
                h = t_end / DEFAULT_MAX_STEPS
                notes.append(Message(
                    "DEFAULT_H_STIFF", fast=Value(1.0 / abs(fastest), "s"),
                    slow=Value(1.0 / slowest_decay, "s"),
                    h=Value(h, "s"), n=DEFAULT_MAX_STEPS,
                ))
        h = t_end / max(1, round(t_end / h))
    error, raw, pole = _phase_error(poles, t_end, h)
    if error > PHASE_NOTE and pole is not None:
        period = 2.0 * math.pi / abs(pole.imag)
        # 頭打ちにする前のずれは (ωh)² に比例するので、ずれを PHASE_TARGET にする刻み
        h_fine = h * math.sqrt(PHASE_TARGET / raw)
        notes.append(Message(
            "PHASE_DRIFT", period=Value(period, "s"), points=Value(period / h), error=Value(error),
            target=Value(PHASE_TARGET), h_fine=Value(h_fine, "s"), points_fine=Value(period / h_fine),
            h=Value(h, "s"),
        ))
    return t_end, h


def _fidelity_note(spec: Spec, notes: list[Message]) -> None:
    """スイッチの無いテンプレートはモデルを選べない。simulation.fidelity の指定は使わず、知らせる。"""
    if spec.simulation.fidelity is not None:
        notes.append(Message("FIDELITY_IGNORED", fidelity=spec.simulation.fidelity))


# 結果の信号（画面仕様 付録A.2 の信号の表と同じ順。説明の文言は画面の辞書が持つ）
RC_SIGNALS = (
    SignalInfo("v(out)", "V", True, "node_voltage", node="out"),
    SignalInfo("i(V1)", "A", True, "element_current", element="V1", positive=("in", "0")),
    SignalInfo("v(in)", "V", False, "node_voltage", node="in"),
)
# 付録A.3 の信号の表と同じ順
RLC_SIGNALS = (
    SignalInfo("v(out)", "V", True, "node_voltage", node="out"),
    SignalInfo("i(L1)", "A", True, "element_current", element="L1", positive=("a", "out")),
    SignalInfo("v(in)", "V", False, "node_voltage", node="in"),
    SignalInfo("v(a)", "V", False, "node_voltage", node="a"),
    SignalInfo("i(V1)", "A", False, "element_current", element="V1", positive=("in", "0")),
)


def rc_lowpass(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - C。出力はコンデンサ電圧 v(out)。"""
    notes: list[Message] = []
    _fidelity_note(spec, notes)
    p = _values(spec, ["Vin", "R", "C"], notes)
    netlist = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=p["Vin"])),
        Component(id="R1", type="R", nodes=("in", "out"), value=p["R"]),
        Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]),
    ])
    poles = np.array([-1.0 / (p["R"] * p["C"])], dtype=complex)
    t_end, h = _timing(spec, poles, notes)
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(V1)"], notes=notes, signals=list(RC_SIGNALS))


def rlc_series(spec: Spec) -> Expanded:
    """電圧源(ステップ) - R - L - C の直列。出力はコンデンサ電圧 v(out)。"""
    notes: list[Message] = []
    _fidelity_note(spec, notes)
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
    return Expanded(netlist, t_end, h, probes=["v(out)", "i(L1)"], notes=notes, signals=list(RLC_SIGNALS))
