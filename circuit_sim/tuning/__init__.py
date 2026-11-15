"""ゲイン設計（評価関数、線形化、安定余裕、最適化。E2）。いまは解析式による初期値だけ。"""
from .initial import PidGains, buck_voltage_mode_pid, loop_response

__all__ = ["PidGains", "buck_voltage_mode_pid", "loop_response"]
