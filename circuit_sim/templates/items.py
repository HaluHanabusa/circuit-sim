"""テンプレートの項目（仕様の中の名前、次元、必須か、図の素子）と、表示する信号の定義。値の読み取り。

項目の一覧は、画面（仕様パネルと回路図）とテンプレートの展開の両方が使う。文言（項目の名前、説明）は
画面の側が言語ごとに持つ（ここには置かない）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..ir import Quantity, Spec
from ..messages import CodedError, Message, Names, Value
from ..signals import SignalInfo, SignalKind
from ..units import DIMENSION_LABEL, UnitError, parse_unit, to_si

Section = Literal["plant", "control"]


class MissingValueError(CodedError):
    """仕様に値が入っていないパラメータがある（番号 MISSING_VALUES）。

    names : 足りない名前（必須の項目と、組で入れる項目の足りない側）
    pairs : 組で入れる項目の足りない側 → 入っている相手（画面のお知らせ N-MISSING-PAIR に使う）
    """

    def __init__(self, names: list[str], pairs: dict[str, str] | None = None):
        self.names = list(names)
        self.pairs = dict(pairs or {})
        super().__init__("MISSING_VALUES", names=Names(self.names), pairs=dict(self.pairs))


def units_error(problems: list[tuple[str, str, str, bool, UnitError]]) -> UnitError:
    """読めない単位、次元の違う単位の項目をまとめたエラー（番号 UNITS_*）。

    problems: （項目、書かれた単位、項目の SI 単位、読めないか、to_si の UnitError）の並び。
    """
    kinds = {unreadable for _, _, _, unreadable, _ in problems}
    code = {frozenset({True}): "UNITS_UNREADABLE", frozenset({False}): "UNITS_WRONG_DIMENSION"}.get(
        frozenset(kinds), "UNITS_UNREADABLE_OR_WRONG")
    items = [
        Message("UNIT_PROBLEM", key=key, problem=exc.message, written=written, unit=unit,
                unreadable=unreadable)
        for key, written, unit, unreadable, exc in problems
    ]
    return UnitError(code, problems=items)


@dataclass(frozen=True)
class Item:
    """テンプレートの項目。

    key       : 仕様の中の名前（plant.Vin なら "Vin"）
    section   : "plant" か "control"
    dimension : 次元（units.py の DIMENSION_LABEL の鍵）
    required  : 必須なら True。省略できるなら False
    together  : この項目があるときに一緒に要る項目（ステップの大きさと時刻など）
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
class FixedValue:
    """仕様の項目でなく、テンプレートが決める値（画面の回路図とツールチップに出す。画面仕様 14章の未決18）。

    key      : 値の名前（"r_on"）
    value    : 値（SI）
    unit     : 単位の記号（"ohm" など。Item.si_unit と同じ書き方）
    elements : その値を持つ図の素子（"S1"、"S2"、"Rleg"）
    """

    key: str
    value: float
    unit: str
    elements: tuple[str, ...] = ()


@dataclass(frozen=True)
class Display:
    """テンプレートの表示定義のうち、エンジンの側が決めるもの（画面仕様 付録A.1、12章の D2）。

    文言（表示名、項目と信号の説明）と図の配置は画面の側が持つ。
    items     : 項目（仕様パネルの行。降圧コンバータだけ。ほかのテンプレートは空）
    fidelities: 選べるモデルの種類（スイッチの無いテンプレートでは空）。default_fidelity はその既定
    signals   : モデルの種類ごとの表示する信号（付録A の信号の表の順）。モデルを選べないテンプレートは鍵 None
    values    : 仕様の値によらない、テンプレートが決める値（降圧コンバータのオン抵抗 r_on）
    """

    template: str
    items: tuple[Item, ...] = ()
    fidelities: tuple[str, ...] = ()
    default_fidelity: str | None = None
    signals: dict[str | None, tuple[SignalInfo, ...]] = field(default_factory=dict)
    values: tuple[FixedValue, ...] = ()

    def value(self, key: str) -> FixedValue:
        for v in self.values:
            if v.key == key:
                return v
        raise KeyError(key)


__all__ = ["Display", "FixedValue", "Item", "MissingValueError", "SignalInfo", "SignalKind", "read_items"]


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


def read_items(spec: Spec, items: tuple[Item, ...], notes: list[Message]) -> dict[str, float]:
    """項目の値を SI単位で取り出す。省略された省略できる項目は、結果に入れない。

    足りない値（必須の項目、または一緒に要る項目）は、まとめて MissingValueError にする。
    単位が読めない、または次元が違う項目は、まとめて UnitError にする。
    版 0.1 の control の数値（単位 ""、出典「記載なし」）は、項目の SI単位の値とみなし、注意に書く。
    """
    present = {it.key for it in items if _quantity(spec, it) is not None}
    missing = [it.key for it in items if it.required and it.key not in present]
    pairs: dict[str, str] = {}
    for it in items:
        if it.key in present:
            for k in it.together:
                if k not in present and k not in missing:
                    missing.append(k)
                    pairs[k] = it.key
    if missing:
        raise MissingValueError(missing, pairs)

    legacy = set(spec.legacy_control())
    values: dict[str, float] = {}
    problems, legacy_used = [], []
    for it in items:
        if it.key not in present:
            continue
        q = _quantity(spec, it)
        if it.section == "control" and it.key in legacy:
            values[it.key] = float(q.value)
            legacy_used.append((it.key, it.si_unit))
            continue
        try:
            values[it.key] = to_si(float(q.value), q.unit, it.dimension)
        except UnitError as exc:
            problems.append((it.key, q.unit, it.si_unit, not _readable(q.unit), exc))
            continue
        if values[it.key] != q.value:
            # 無次元の量は単位の記号を付けない（「0.9 1」と書かない）
            written = Value(q.value, q.unit.strip())
            if it.dimension == "ratio":
                notes.append(Message("UNIT_CONVERTED_RATIO", key=it.key, value=written,
                                     si=Value(values[it.key])))
            else:
                notes.append(Message("UNIT_CONVERTED", key=it.key, value=written,
                                     si=Value(values[it.key], it.si_unit)))
    if problems:
        raise units_error(problems)
    if legacy_used:
        code = "SPEC_LEGACY_CONTROL_INFERRED" if spec.version_inferred else "SPEC_LEGACY_CONTROL"
        notes.append(Message(code, items=Names(f"{k} [{u}]" for k, u in legacy_used),
                             keys=Names(k for k, _ in legacy_used)))
    known = {(it.section, it.key) for it in items}
    unused = [f"plant.{k}" for k in spec.plant if ("plant", k) not in known]
    unused += [f"control.{k}" for k in (spec.control or {}) if ("control", k) not in known]
    if unused:
        notes.append(Message("UNUSED_ITEMS", items=Names(unused)))
    return values
