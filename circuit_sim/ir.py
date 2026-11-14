"""仕様JSON（層A）とネットリスト（層B）のスキーマ。

層A (Spec)    : テンプレート名＋パラメータ＋目標値。AIが出力するのはここだけ。
層B (Netlist) : 素子と接続の一覧。層Aからコードが展開し、ソルバが読む。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1"


# ---------------------------------------------------------------- 層A: 仕様

class Quantity(BaseModel):
    """設計書から取り出した1つの値。

    value が None のときは「設計書に無かった」ことを表す。AIに値を埋めさせない。
    source は設計書内の位置ラベル（例: "p.3 表2"、"仕様!B4"）。
    """

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    unit: str
    source: str | None = None


class SimulationSettings(BaseModel):
    """計算時間と刻み。省略するとテンプレートが回路定数から決める。"""

    model_config = ConfigDict(extra="forbid")

    t_end: float | None = Field(default=None, gt=0)
    h: float | None = Field(default=None, gt=0)


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    template: str
    plant: dict[str, Quantity]
    control: dict[str, Any] | None = None
    targets: dict[str, float] = Field(default_factory=dict)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    missing: list[str] = Field(default_factory=list)

    def null_fields(self) -> list[str]:
        """値が入っていないパラメータ名の一覧。"""
        return [name for name, q in self.plant.items() if q.value is None]


# ---------------------------------------------------------- 層B: ネットリスト

class DC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["dc"] = "dc"
    value: float


class Step(BaseModel):
    """t_step で initial から final に切り替わる。t_step は時間刻みの格子に丸められる。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["step"] = "step"
    initial: float = 0.0
    final: float
    t_step: float = Field(default=0.0, ge=0)


class Sine(BaseModel):
    """offset + amplitude * sin(2*pi*frequency*t + phase)"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["sine"] = "sine"
    amplitude: float
    frequency: float = Field(gt=0)
    phase_deg: float = 0.0
    offset: float = 0.0


Waveform = Annotated[DC | Step | Sine, Field(discriminator="kind")]


class Component(BaseModel):
    """2端子素子。

    type  : R [ohm], L [H], C [F], V = 電圧源 [V], I = 電流源 [A]
    nodes : (n+, n-)。電流の正方向は素子の中を n+ から n- へ（SPICEと同じ）。
    ic    : 初期値。C は n+ 側を正とした電圧 [V]、L は n+ → n- の電流 [A]。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: Literal["R", "L", "C", "V", "I"]
    nodes: tuple[str, str]
    value: float | None = None
    waveform: Waveform | None = None
    ic: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> Component:
        if self.nodes[0] == self.nodes[1]:
            raise ValueError(f"{self.id}: 両端が同じ節点 '{self.nodes[0]}' につながっている")
        if self.type in ("R", "L", "C"):
            if self.value is None or not math.isfinite(self.value) or self.value <= 0:
                raise ValueError(f"{self.id}: value は正の有限値が必要（現在 {self.value}）")
            if self.waveform is not None:
                raise ValueError(f"{self.id}: {self.type} に waveform は指定できない")
        else:
            if self.waveform is None:
                raise ValueError(f"{self.id}: 電源には waveform が必要")
            if self.value is not None:
                raise ValueError(f"{self.id}: 電源の値は waveform で指定する")
        if self.ic != 0.0 and self.type not in ("L", "C"):
            raise ValueError(f"{self.id}: ic を指定できるのは L と C だけ")
        return self


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
