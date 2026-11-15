"""テンプレートの項目（仕様の中の名前、次元、必須か、図の素子）と、表示する信号の定義。値の読み取り。

項目の一覧は、画面（仕様パネルと回路図）とテンプレートの展開の両方が使う。文言（項目の名前、説明）は
画面の側が言語ごとに持つ（ここには置かない）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..ir import Quantity, Spec
from ..units import DIMENSION_LABEL, UnitError, parse_unit, to_si

Section = Literal["plant", "control"]


class MissingValueError(ValueError):
    """仕様に値が入っていないパラメータがある。"""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"値が入っていないパラメータ: {names}")


@dataclass(frozen=True)
class Item:
    """テンプレートの項目。

    key       : 仕様の中の名前（plant.Vin なら "Vin"）
    section   : "plant" か "control"
    dimension : 次元（units.py の DIMENSION_LABEL の鍵）
    required  : 必須なら True。省略できるなら False
    together  : この項目があるときに一緒に要る項目（段差の大きさと時刻など）
    element   : 図の素子（無ければ None）
    """

    key: str
    section: Section
    dimension: str
    required: bool = True
    together: tuple[str, ...] = ()
    element: str | None = None

    @property
    def si_unit(self) -> str:
        return DIMENSION_LABEL[self.dimension][1]


@dataclass(frozen=True)
class SignalInfo:
    """表示する信号。name は Result から取り出す名前、unit は単位（無次元は ""）、shown は既定で表示するか、
    positive は電流の正の向き（電流でなければ None）。"""

    name: str
    unit: str
    shown: bool
    positive: str | None = None


def _readable(unit: str) -> bool:
    try:
        parse_unit(unit)
    except UnitError:
        return False
    return True


def _quantity(spec: Spec, item: Item) -> Quantity | None:
    table = spec.plant if item.section == "plant" else (spec.control or {})
    q = table.get(item.key)
    return None if q is None or q.value is None else q


def read_items(spec: Spec, items: tuple[Item, ...], notes: list[str]) -> dict[str, float]:
    """項目の値を SI単位で取り出す。省略された省略できる項目は、結果に入れない。

    足りない値（必須の項目、または一緒に要る項目）は、まとめて MissingValueError にする。
    単位が読めない、または次元が違う項目は、まとめて UnitError にする。
    版 0.1 の control の数値（単位 ""、出どころ「記載なし」）は、項目の SI単位の値とみなし、注意に書く。
    """
    present = {it.key for it in items if _quantity(spec, it) is not None}
    missing = [it.key for it in items if it.required and it.key not in present]
    for it in items:
        if it.key in present:
            missing += [k for k in it.together if k not in present and k not in missing]
    if missing:
        raise MissingValueError(missing)

    legacy = set(spec.legacy_control())
    values: dict[str, float] = {}
    wrong, kinds, legacy_used = [], set(), []
    for it in items:
        if it.key not in present:
            continue
        q = _quantity(spec, it)
        if it.section == "control" and it.key in legacy:
            values[it.key] = float(q.value)
            legacy_used.append(f"{it.key} [{it.si_unit}]")
            continue
        try:
            values[it.key] = to_si(float(q.value), q.unit, it.dimension)
        except UnitError as exc:
            wrong.append(f"{it.key}: {exc}")
            kinds.add("読めない" if not _readable(q.unit) else "量の種類が違う")
            continue
        if values[it.key] != q.value:
            notes.append(
                f"{it.key}: {q.value:g} {q.unit.strip()} を {values[it.key]:g} {it.si_unit} に換算した"
            )
    if wrong:
        heading = {
            frozenset({"読めない"}): "単位を読めない項目がある。",
            frozenset({"量の種類が違う"}): "単位の量の種類が違う項目がある。",
        }.get(frozenset(kinds), "単位を読めない、または量の種類が違う項目がある。")
        raise UnitError(heading + "。".join(wrong))
    if legacy_used:
        notes.append(
            "仕様の版 0.1 の形なので、control の数値を単位の無い値として読み、"
            f"SI単位の値とみなした（出どころは「記載なし」）: {', '.join(legacy_used)}。"
            "単位と出どころを確かめて、版 0.2 の形（value、unit、source）で書き直す"
        )
    known = {(it.section, it.key) for it in items}
    unused = [f"plant.{k}" for k in spec.plant if ("plant", k) not in known]
    unused += [f"control.{k}" for k in (spec.control or {}) if ("control", k) not in known]
    if unused:
        notes.append(f"このテンプレートが使わない項目を無視した: {unused}")
    return values
