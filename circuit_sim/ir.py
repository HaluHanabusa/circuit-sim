"""仕様JSON（層A）とネットリスト（層B）のスキーマ。

層A (Spec)    : テンプレート名＋パラメータ＋目標値。AIが出力するのはここだけ。
層B (Netlist) : 素子と接続の一覧。層Aからコードが展開し、ソルバが読む。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

SCHEMA_VERSION = "0.2"
SUPPORTED_SCHEMA_VERSIONS = ("0.1", "0.2")
LEGACY_SOURCE = "記載なし"     # 版 0.1 の control の数値を Quantity にしたときの出どころ


# ---------------------------------------------------------------- 層A: 仕様

class Quantity(BaseModel):
    """設計書から取り出した1つの値。

    value が None のときは「設計書に無かった」ことを表す。AIに値を埋めさせない。
    source は設計書内の位置ラベル（例: "p.3 表2"、"仕様!B4"）。
    """

    model_config = ConfigDict(extra="forbid")

    value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str
    source: str | None = None


class SimulationSettings(BaseModel):
    """計算時間と刻み、モデルの精度。省略するとテンプレートが決める。

    fidelity: "switching"（スイッチングモデル。スイッチを理想スイッチで計算する）、
              "average"（平均モデル。ブリッジのレグを「指令 × 直流電圧」の電源に置き換える）。
              省略するとテンプレートの既定（スイッチを含むテンプレートでは switching）。
    """

    model_config = ConfigDict(extra="forbid")

    t_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    h: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    fidelity: Literal["switching", "average"] | None = None


class Spec(BaseModel):
    """仕様（層A）。

    版 0.2: control の値も plant と同じ Quantity（値、単位、出どころ）。
    版 0.1: control の値は単位の無い数値だった。読むときは、数値（と null）を単位 ""、出どころ
            「記載なし」の Quantity にする（legacy_control() で分かる。単位はテンプレートが SI とみなす）。
    版の欄（schema_version）が無いファイルは、中身から決める: control に Quantity の形でない値
            （数値や null）があれば 0.1、無ければ 0.2。version_inferred が True になる
            （0.1 と決めたときはテンプレートが注意を出す）。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1", "0.2"] = SCHEMA_VERSION
    template: str
    plant: dict[str, Quantity]
    control: dict[str, Quantity] | None = None
    targets: dict[str, float] = Field(default_factory=dict)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    missing: list[str] = Field(default_factory=list)

    _version_inferred: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def _read_versions(cls, data: Any, handler: Any) -> Spec:
        """版の欄が無ければ中身から決め、版 0.1 の control の数値を Quantity の形にする。

        版 0.1 の control の値が、数値でも null でも Quantity の形でもなければエラー。
        """
        if not isinstance(data, dict):
            return handler(data)
        inferred = "schema_version" not in data
        if inferred:
            control = data.get("control")
            bare = isinstance(control, dict) and any(
                not isinstance(v, dict | Quantity) for v in control.values())
            data = {**data, "schema_version": "0.1" if bare else SCHEMA_VERSION}
        if data.get("schema_version") == "0.1" and isinstance(data.get("control"), dict):
            converted = {}
            for name, value in data["control"].items():
                if value is None or (isinstance(value, int | float) and not isinstance(value, bool)):
                    converted[name] = {"value": value, "unit": "", "source": LEGACY_SOURCE}
                elif isinstance(value, dict | Quantity):
                    converted[name] = value
                else:
                    raise ValueError(
                        f"仕様の版 0.1 の control.{name} は数値でないので読めない（{value!r}）。"
                        "版 0.2 の形（value、unit、source）で書く"
                    )
            data = {**data, "control": converted}
        spec = handler(data)
        spec._version_inferred = inferred
        return spec

    @property
    def version_inferred(self) -> bool:
        """読んだファイルに版の欄が無く、中身から版を決めたなら True。"""
        return self._version_inferred

    def null_fields(self) -> list[str]:
        """値が入っていないパラメータ名の一覧。"""
        return [name for name, q in self.plant.items() if q.value is None]

    def legacy_control(self) -> list[str]:
        """版 0.1 の数値から作った control の項目（単位 ""、出どころ「記載なし」）の名前。"""
        if self.schema_version != "0.1" or not self.control:
            return []
        return [n for n, q in self.control.items() if q.unit == "" and q.source == LEGACY_SOURCE]


# ---------------------------------------------------------- 層B: ネットリスト

class DC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["dc"] = "dc"
    value: float = Field(allow_inf_nan=False)


class Step(BaseModel):
    """t_step で initial から final に切り替わる。t_step は時間刻みの格子に丸められる。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["step"] = "step"
    initial: float = Field(default=0.0, allow_inf_nan=False)
    final: float = Field(allow_inf_nan=False)
    t_step: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class Sine(BaseModel):
    """offset + amplitude * sin(2*pi*frequency*t + phase)"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["sine"] = "sine"
    amplitude: float = Field(allow_inf_nan=False)
    frequency: float = Field(gt=0, allow_inf_nan=False)
    phase_deg: float = Field(default=0.0, allow_inf_nan=False)
    offset: float = Field(default=0.0, allow_inf_nan=False)


class Command(BaseModel):
    """制御器の指令で値が決まる電源: 値 = gain × 指令（lower〜upper で止める）。

    指令は制御器の出力の名前（command）。制御周期の頭で変わり、次に変わるまで一定（区分一定）。
    制御器（simulate の controllers）が無いと計算できない。平均モデルでブリッジのレグを置き換えるのに使う
    （指令 = デューティ、gain = 直流電圧、lower = 0、upper = 1）。
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["command"] = "command"
    command: str = Field(min_length=1)
    gain: float = Field(allow_inf_nan=False)
    lower: float | None = Field(default=None, allow_inf_nan=False)
    upper: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _check(self) -> Command:
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError(
                f"指令 '{self.command}': lower（{self.lower}）は upper（{self.upper}）以下にする"
            )
        return self

    def value(self, command: float) -> float:
        """指令の値から電源の値を作る。"""
        if self.lower is not None:
            command = max(command, self.lower)
        if self.upper is not None:
            command = min(command, self.upper)
        return self.gain * command


Waveform = Annotated[DC | Step | Sine | Command, Field(discriminator="kind")]


DEFAULT_R_ON = 1e-3    # S と D のオン抵抗の既定値 [ohm]
DEFAULT_R_OFF = 1e6    # S と D のオフ抵抗の既定値 [ohm]


class Component(BaseModel):
    """2端子素子。

    type  : R [ohm], L [H], C [F], V = 電圧源 [V], I = 電流源 [A]（波形は DC / Step / Sine / Command）,
            S = 制御スイッチ, D = ダイオード（順方向電圧は 0）
    nodes : (n+, n-)。電流の正方向は素子の中を n+ から n- へ（SPICEと同じ）。
            D はアノードが n+、カソードが n-。
    ic    : 初期値。C は n+ 側を正とした電圧 [V]、L は n+ → n- の電流 [A]。
    r_on  : S と D のオン抵抗 [ohm]。省略すると 1 mΩ。
    r_off : S と D のオフ抵抗 [ohm]。省略すると 1 MΩ。
    gate  : S を動かすゲート信号の名前（信号そのものは計算の入口に渡す）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: Literal["R", "L", "C", "V", "I", "S", "D"]
    nodes: tuple[str, str]
    value: float | None = None
    waveform: Waveform | None = None
    ic: float = Field(default=0.0, allow_inf_nan=False)
    r_on: float | None = None
    r_off: float | None = None
    gate: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Component:
        if self.nodes[0] == self.nodes[1]:
            raise ValueError(f"{self.id}: 両端が同じ節点 '{self.nodes[0]}' につながっている")
        if self.type in ("R", "L", "C"):
            if self.value is None or not math.isfinite(self.value) or self.value <= 0:
                raise ValueError(f"{self.id}: value は正の有限値が必要（現在 {self.value}）")
            if self.waveform is not None:
                raise ValueError(f"{self.id}: {self.type} に waveform は指定できない")
        elif self.type in ("V", "I"):
            if self.waveform is None:
                raise ValueError(f"{self.id}: 電源には waveform が必要")
            if self.value is not None:
                raise ValueError(f"{self.id}: 電源の値は waveform で指定する")
        else:
            self._check_switching()
        if self.type not in ("S", "D") and (self.r_on is not None or self.r_off is not None):
            raise ValueError(f"{self.id}: r_on / r_off を指定できるのは S と D だけ")
        if self.type != "S" and self.gate is not None:
            raise ValueError(f"{self.id}: gate を指定できるのは S だけ")
        if self.ic != 0.0 and self.type not in ("L", "C"):
            raise ValueError(f"{self.id}: ic を指定できるのは L と C だけ")
        return self

    def _check_switching(self) -> None:
        """S と D の検証。オン抵抗とオフ抵抗の既定値もここで入れる。"""
        if self.value is not None or self.waveform is not None:
            raise ValueError(
                f"{self.id}: {self.type} に value / waveform は指定できない（r_on / r_off を使う）"
            )
        if self.r_on is None:
            self.r_on = DEFAULT_R_ON
        if self.r_off is None:
            self.r_off = DEFAULT_R_OFF
        for name, r in (("r_on", self.r_on), ("r_off", self.r_off)):
            if not math.isfinite(r) or r <= 0:
                raise ValueError(f"{self.id}: {name} は正の有限値が必要（現在 {r}）")
        if self.r_on >= self.r_off:
            raise ValueError(
                f"{self.id}: r_on（{self.r_on}）は r_off（{self.r_off}）より小さくする"
            )
        if self.type == "S" and not self.gate:
            raise ValueError(f"{self.id}: スイッチにはゲート信号の名前 gate が必要")


class Netlist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ground: str = "0"
    components: list[Component] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Netlist:
        ids = Counter(c.id for c in self.components)
        dup = sorted(i for i, n in ids.items() if n > 1)
        if dup:
            raise ValueError(f"素子IDが重複している: {dup}")

        degree = Counter(n for c in self.components for n in c.nodes)
        if self.ground not in degree:
            raise ValueError(f"基準節点 '{self.ground}' につながる素子が無い")
        dangling = sorted(n for n, d in degree.items() if d < 2 and n != self.ground)
        if dangling:
            raise ValueError(f"素子が1つしかつながっていない節点がある: {dangling}")
        return self

    def nodes(self) -> list[str]:
        """基準節点を除いた節点名（並びは固定）。"""
        seen: dict[str, None] = {}
        for c in self.components:
            for n in c.nodes:
                if n != self.ground:
                    seen.setdefault(n)
        return list(seen)

    def component(self, comp_id: str) -> Component:
        for c in self.components:
            if c.id == comp_id:
                return c
        raise KeyError(comp_id)
