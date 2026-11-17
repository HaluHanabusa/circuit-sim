"""テンプレートの登録簿。仕様（層A）の template 名から展開関数を引く。"""
from __future__ import annotations

from collections.abc import Callable

from ..ir import Spec
from ..messages import CodedError, Names
from . import buck
from .buck import buck_voltage_mode
from .items import Display, FixedValue, Item, SignalInfo
from .legs import HalfBridgeLeg
from .t0 import RC_SIGNALS, RLC_SIGNALS, Expanded, MissingValueError, rc_lowpass, rlc_series

TEMPLATES: dict[str, Callable[[Spec], Expanded]] = {
    "rc_lowpass": rc_lowpass,
    "rlc_series": rlc_series,
    "buck_voltage_mode": buck_voltage_mode,
}

# 項目の定義があるテンプレート（画面の仕様パネルと回路図が使う）
TEMPLATE_ITEMS: dict[str, tuple[Item, ...]] = {
    "buck_voltage_mode": buck.ITEMS,
}

# 表示定義のうちエンジンの側が決めるもの（モデルの種類、モデルごとの表示する信号、テンプレートが決める値。
# 画面仕様 12章の D2）。仕様の値によらない
TEMPLATE_DISPLAY: dict[str, Display] = {
    "rc_lowpass": Display("rc_lowpass", signals={None: RC_SIGNALS}),
    "rlc_series": Display("rlc_series", signals={None: RLC_SIGNALS}),
    "buck_voltage_mode": buck.DISPLAY,
}


def expand(spec: Spec) -> Expanded:
    try:
        fn = TEMPLATES[spec.template]
    except KeyError:
        raise CodedError("TEMPLATE_UNKNOWN", template=spec.template, names=Names(sorted(TEMPLATES))) from None
    return fn(spec)


__all__ = [
    "TEMPLATES",
    "TEMPLATE_DISPLAY",
    "TEMPLATE_ITEMS",
    "Display",
    "Expanded",
    "FixedValue",
    "HalfBridgeLeg",
    "Item",
    "MissingValueError",
    "SignalInfo",
    "expand",
]
