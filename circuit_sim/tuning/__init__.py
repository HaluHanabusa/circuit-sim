"""ゲイン設計（評価関数、線形化、安定余裕、最適化。E2）。いまは解析式による初期値だけ。"""
from .initial import (
    GainDesignError,
    PidGains,
    buck_voltage_mode_pid,
    closed_loop_pole_radius,
    loop_polynomials,
    loop_response,
)

__all__ = [
    "GainDesignError",
    "PidGains",
    "buck_voltage_mode_pid",
    "closed_loop_pole_radius",
    "loop_polynomials",
    "loop_response",
]
