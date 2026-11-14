"""ネットリストを時間応答に変換する入口。"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import kernel
from .ir import DC, Netlist, Sine, Step
from .mna import CircuitError, System, build_system


@dataclass
class Result:
    t: np.ndarray                 # (n_steps + 1,)
    x: np.ndarray                 # (n_steps + 1, 未知数の数)
    unknowns: list[str]
    netlist: Netlist
    h: float
    notes: list[str] = field(default_factory=list)

    def __getitem__(self, name: str) -> np.ndarray:
        """"v(out)" や "i(L1)" の形で波形を取り出す。"""
        try:
            return self.x[:, self.unknowns.index(name)]
        except ValueError:
            raise KeyError(f"{name} は無い。使える名前: {self.unknowns}") from None

    def v(self, node: str, ref: str | None = None) -> np.ndarray:
        """節点電圧。ref を指定すると2節点間の電圧。"""
        def one(n: str) -> np.ndarray:
            return np.zeros_like(self.t) if n == self.netlist.ground else self[f"v({n})"]
        return one(node) - (one(ref) if ref is not None else 0.0)

    def i(self, comp_id: str) -> np.ndarray:
        """素子を n+ から n- へ流れる電流。電圧源・インダクタ・抵抗に対応。"""
        c = self.netlist.component(comp_id)
        if c.type in ("V", "L"):
            return self[f"i({comp_id})"]
        if c.type == "R":
            return self.v(c.nodes[0], c.nodes[1]) / c.value
        raise KeyError(f"{comp_id}（{c.type}）の電流はまだ取り出せない")

    def to_csv(self, path: str | Path, names: list[str] | None = None) -> None:
        names = names or self.unknowns
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t"] + names)
            cols = [self[n] for n in names]
            for k in range(len(self.t)):
                w.writerow([repr(float(self.t[k]))] + [repr(float(c[k])) for c in cols])


def _sample_sources(system: System, n_steps: int, h: float, notes: list[str]):
    """電源を時間格子上の配列にする。戻り値: u_start, u_end, event"""
    m = len(system.sources)
    t = h * np.arange(n_steps + 1)
    u_start = np.zeros((n_steps, m))
    u_end = np.zeros((n_steps, m))
    event = np.zeros(n_steps, dtype=bool)

    for j, cid in enumerate(system.sources):
        w = system.netlist.component(cid).waveform
        if isinstance(w, DC):
            u_start[:, j] = w.value
            u_end[:, j] = w.value
        elif isinstance(w, Sine):
            y = w.offset + w.amplitude * np.sin(
                2.0 * math.pi * w.frequency * t + math.radians(w.phase_deg)
            )
            u_start[:, j] = y[:-1]
            u_end[:, j] = y[1:]
        elif isinstance(w, Step):
            ks = int(round(w.t_step / h))
            if abs(ks * h - w.t_step) > 1e-9 * h:
                notes.append(f"{cid}: ステップ時刻 {w.t_step} を格子点 {ks * h} に丸めた")
            k = np.arange(n_steps)
            u_start[:, j] = np.where(k >= ks, w.final, w.initial)       # t[k] の右側の値
            u_end[:, j] = np.where(k + 1 > ks, w.final, w.initial)      # t[k+1] の左側の値
            if 0 < ks < n_steps and w.final != w.initial:
                event[ks] = True
        else:  # pragma: no cover
            raise CircuitError(f"{cid}: 未対応の波形 {type(w).__name__}")
    return u_start, u_end, event


def simulate(netlist: Netlist, t_end: float, h: float) -> Result:
    if not (h > 0 and t_end > 0):
        raise ValueError("t_end と h は正の値が必要")
    n_steps = int(round(t_end / h))
    if n_steps < 1:
        raise ValueError("t_end が h より短い")

    system = build_system(netlist)
    notes: list[str] = []
    u_start, u_end, event = _sample_sources(system, n_steps, h, notes)

    ops = kernel.prepare(system.E, system.G, system.B, h)
    x0 = ops.Rex @ system.ex0 + ops.Ru @ u_start[0]

    out = np.empty((n_steps + 1, len(system.unknowns)))
    kernel.run(ops.P, ops.Q, ops.Rx, ops.Ru, x0, u_start, u_end, event, out)

    return Result(
        t=h * np.arange(n_steps + 1),
        x=out,
        unknowns=system.unknowns,
        netlist=netlist,
        h=h,
        notes=notes,
    )
