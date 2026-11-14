"""テンプレートの登録簿。仕様（層A）の template 名から展開関数を引く。"""
from __future__ import annotations

from collections.abc import Callable

from ..ir import Spec
from .t0 import Expanded, MissingValueError, rc_lowpass, rlc_series

TEMPLATES: dict[str, Callable[[Spec], Expanded]] = {
    "rc_lowpass": rc_lowpass,
    "rlc_series": rlc_series,
}


def expand(spec: Spec) -> Expanded:
    try:
        fn = TEMPLATES[spec.template]
    except KeyError:
        raise ValueError(
            f"未対応のテンプレート '{spec.template}'。使えるもの: {sorted(TEMPLATES)}"
        ) from None
    return fn(spec)


__all__ = ["TEMPLATES", "Expanded", "MissingValueError", "expand"]
