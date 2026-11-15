"""ブリッジのレグ（上下のスイッチ）を1つの部品として、モデルの精度を選んで層Bに展開する。

スイッチング: 上のスイッチ（positive → output）と下のスイッチ（output → negative）を、相補のゲート
              （デッドタイム0）で動かす。上のスイッチのデューティが指令。
平均        : 「指令 × 直流電圧」の電圧源（指令は 0〜1 で止める）と、スイッチのオン抵抗 r_on の直列に
              置き換える
                  negative ─ 電圧源（＋が内部の節点 <output>_avg）─ r_on ─ output
              相補のゲートでは、どちらのスイッチが導通していても出力の経路にオン抵抗が1つ入るので、
              周期平均の式は  v(output) = d·Vdc − r_on·i_out（i_out はレグから出る電流）。
              直流電圧は仕様の値で、電源が硬い（インピーダンスが無い）前提。positive にはつながない。
              オフのスイッチのオフ抵抗（既定 1 MΩ）は入れない（漏れ電流は負荷の電流の 1e-6 程度）。

両者の周期平均は、デッドタイム0の相補のゲートなら、どの負荷でも（電流の向きによらず）一致する
（電流が負になる軽負荷でも不連続導通にならない）。オン抵抗を平均モデルに入れたのは、Q の高い過渡で
差が出るため: 基準の降圧（Q = 8.75）をデューティ 0.5 に固定して起動すると、オン抵抗を入れない平均モデルは
スイッチングモデルより減衰が r_on/(2L)（1.3%）遅く、周期平均が Vref の 0.46% 違った。入れると 0.065%。
制御周期が搬送波の周期の整数倍なら、指令の変わる時刻も一致する（PWMは搬送波の周期の頭で指令を取り込む）。

搬送波の既定は三角波（オンの区間が周期の中央）。オンの区間の重心が周期の中央に来るので、周期の中で一定の
電圧を出す平均モデルと時間がそろう。のこぎり波（周期の頭でオン）では、重心が (1 − D)·T/2 早く、
LC が大きく振れる過渡で周期平均が平均モデルより先に進む（固定デューティ 0.5 の起動で Vin·D の約 3%）。
三角波では、デューティの小さな変化が両方の端に分かれて周期の中央の前後に出るので、小信号の遅れも
どのデューティでも保持の 0.5 サンプルになる（ゲインの設計の前提と同じ）。

三角波の測定点のずれ（物理として正しい効果。直さない）: 制御器は周期の頭（三角波ではオフの区間の中央）で
測る。そこではインダクタ電流が周期平均に等しく、コンデンサの電流が 0 から負に変わるので、コンデンサ電圧は
リプルの山の頂点にある。積分のある制御では頂点が指令値に合うので、スイッチングモデルの定常の周期平均は
指令値より低い:
    Vref − v̄ = ΔI·(1 + D) / (24·f·C)      ΔI = (Vin − Vo)·D / (L·f)（電流のリプルの幅）
D = 0.5 では ΔI/(16·f·C) = ΔV/2（ΔV = ΔI/(8·f·C) は電圧のリプルの幅）。基準の回路（D = 0.5）で式は
7.98 mV、実測 7.96 mV。Vref 6 V（D = 0.25）では式 4.99 mV、実測 5.09 mV（刻みを T/800 にすると 4.99 mV。
差はデューティの分解能 1/200 でオンの区間の位置が揺れる分）。平均モデルにはリプルが無いので、ずれない。
実在の制御器でも同じことが起きる（測る時刻をオフの区間の中央に置く限り）。
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
    average_id   : 平均モデルの電圧源の素子ID
    average_r_id : 平均モデルのオン抵抗の素子ID
    r_on, r_off  : スイッチのオン抵抗とオフ抵抗 [Ω]（平均モデルはオン抵抗だけを使う）
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
    average_r_id: str = "Rleg"
    carrier: Literal["sawtooth", "triangle"] = "triangle"
    r_on: float = DEFAULT_R_ON
    r_off: float = DEFAULT_R_OFF

    @property
    def average_node(self) -> str:
        """平均モデルの、電圧源とオン抵抗の間の節点。"""
        return f"{self.output}_avg"

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
            return [
                Component(id=self.average_id, type="V", nodes=(self.average_node, self.negative),
                          waveform=waveform),
                Component(id=self.average_r_id, type="R", nodes=(self.average_node, self.output),
                          value=self.r_on),
            ], []
        raise ValueError(f"未対応のモデルの精度 '{fidelity}'（switching か average）")
