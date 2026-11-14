"""制御器を回路の計算に挟むための型（PWMのゲート信号と、制御器の口）。

制御ブロックの中身（PID、リミッタなど）はここには無い。E1b 以降で足す。
"""
from .interface import Controller
from .pwm import Pwm, gate_pattern, on_steps

__all__ = ["Controller", "Pwm", "gate_pattern", "on_steps"]
