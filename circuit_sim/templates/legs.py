"""ブリッジのレグ（上下のスイッチ）を1つの部品として、モデルの精度を選んで層Bに展開する。

スイッチング: 上のスイッチ（positive → output）と下のスイッチ（output → negative）を、相補のゲート
              （デッドタイム0）で動かす。上のスイッチのデューティが指令。
平均        : output と negative の間を「指令 × 直流電圧」の電圧源に置き換える（指令は 0〜1 で止める）。
              直流電圧は仕様の値で、電源が硬い（インピーダンスが無い）前提。positive にはつながない。

両者の周期平均は、デッドタイム0の相補のゲートなら、どの負荷でも（電流の向きによらず）一致する。
制御周期が搬送波の周期の整数倍なら、指令の変わる時刻も一致する（PWMは搬送波の周期の頭で指令を取り込む）。

搬送波の既定は三角波（オンの区間が周期の中央）。オンの区間の重心が周期の中央に来るので、周期の中で一定の
電圧を出す平均モデルと時間がそろう。のこぎり波（周期の頭でオン）では、重心が (1 − D)·T/2 早く、
LC が大きく振れる過渡で周期平均が平均モデルより先に進む（固定デューティ 0.5 の起動で Vin·D の約 3%）。
三角波では、デューティの小さな変化が両方の端に分かれて周期の中央の前後に出るので、小信号の遅れも
どのデューティでも保持の 0.5 サンプルになる（ゲインの設計の前提と同じ）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..control import Pwm
from ..ir import DC, DEFAULT_R_OFF, DEFAULT_R_ON, Command, Component

Fidelity = Literal["switching", "average"]


@dataclass(frozen=True)
class HalfBridgeLeg:
    """ブリッジのレグ1つ。

    positive, negative : 直流の＋側と−側の節点
    output   : レグの出力の節点（スイッチの間）
    duty     : 上のスイッチのデューティ。指令の名前（制御器の出力）、または固定値（0〜1）
    frequency: 搬送波の周波数 [Hz]
    v_dc     : 直流電圧 [V]（平均モデルで使う）
    high, low: 上下のスイッチの素子ID。gate、gate_low: そのゲート信号の名前
    average_id : 平均モデルの電圧源の素子ID
    """

    positive: str
    negative: str
    output: str
    duty: str | float
    frequency: float
    v_dc: float
    high: str = "S1"
    low: str = "S2"
    gate: str = "q1"
    gate_low: str = "q2"
    average_id: str = "Vleg"
    carrier: Literal["sawtooth", "triangle"] = "triangle"
    r_on: float = DEFAULT_R_ON
    r_off: float = DEFAULT_R_OFF

    def expand(self, fidelity: Fidelity) -> tuple[list[Component], list[Pwm]]:
        """層Bの素子と、ゲート信号（平均モデルでは無し）。"""
        if fidelity == "switching":
            comps = [
                Component(id=self.high, type="S", nodes=(self.positive, self.output), gate=self.gate,
                          r_on=self.r_on, r_off=self.r_off),
                Component(id=self.low, type="S", nodes=(self.output, self.negative), gate=self.gate_low,
                          r_on=self.r_on, r_off=self.r_off),
            ]
            pwm = Pwm(name=self.gate, complement=self.gate_low, frequency=self.frequency,
                      duty=self.duty, carrier=self.carrier)
            return comps, [pwm]
        if fidelity == "average":
            if isinstance(self.duty, str):
                waveform = Command(command=self.duty, gain=self.v_dc, lower=0.0, upper=1.0)
            else:
                waveform = DC(value=min(max(float(self.duty), 0.0), 1.0) * self.v_dc)
            return [Component(id=self.average_id, type="V", nodes=(self.output, self.negative),
                              waveform=waveform)], []
        raise ValueError(f"未対応のモデルの精度 '{fidelity}'（switching か average）")

    def signals(self, fidelity: Fidelity) -> list[str]:
        """このレグが結果に足す信号の名前。"""
        if fidelity == "switching":
            return [f"gate({self.gate})", f"gate({self.gate_low})", f"on({self.high})", f"on({self.low})"]
        return [f"i({self.average_id})"]
