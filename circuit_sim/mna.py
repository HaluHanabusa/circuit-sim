"""ネットリストから回路方程式  E x' + G x = B u  を組み立てる（修正節点解析）。

未知数 x : 節点電圧（基準節点を除く）、続いて電圧源とインダクタの枝電流。
入力   u : 電圧源・電流源の値（素子の並び順）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .ir import Netlist


class CircuitError(ValueError):
    """回路として解けないときのエラー。"""


@dataclass
class System:
    E: np.ndarray            # (n, n) 微分項の係数
    G: np.ndarray            # (n, n) 代数項の係数
    B: np.ndarray            # (n, m) 電源の係数
    ex0: np.ndarray          # (n,)   初期条件から直接組んだ E @ x(0)
    unknowns: list[str]      # "v(節点)" / "i(素子ID)"
    sources: list[str]       # B の列に対応する電源の素子ID
    netlist: Netlist


def build_system(netlist: Netlist) -> System:
    nodes = netlist.nodes()
    node_index = {name: k for k, name in enumerate(nodes)}
    branches = [c.id for c in netlist.components if c.type in ("V", "L")]
    branch_index = {cid: len(nodes) + k for k, cid in enumerate(branches)}
    sources = [c.id for c in netlist.components if c.type in ("V", "I")]
    source_index = {cid: k for k, cid in enumerate(sources)}

    n = len(nodes) + len(branches)
    E = np.zeros((n, n))
    G = np.zeros((n, n))
    B = np.zeros((n, len(sources)))
    ex0 = np.zeros(n)

    def idx(node: str) -> int | None:
        return None if node == netlist.ground else node_index[node]

    def stamp_two_terminal(M: np.ndarray, a: int | None, b: int | None, g: float) -> None:
        if a is not None:
            M[a, a] += g
        if b is not None:
            M[b, b] += g
        if a is not None and b is not None:
            M[a, b] -= g
            M[b, a] -= g

    for c in netlist.components:
        a, b = idx(c.nodes[0]), idx(c.nodes[1])

        if c.type == "R":
            stamp_two_terminal(G, a, b, 1.0 / c.value)

        elif c.type == "C":
            stamp_two_terminal(E, a, b, c.value)
            # E @ x(0) への寄与: 節点aの行に +C*Vc0、節点bの行に -C*Vc0
            if a is not None:
                ex0[a] += c.value * c.ic
            if b is not None:
                ex0[b] -= c.value * c.ic

        elif c.type == "I":
            # 電流は n+ から素子を通って n- へ。n+ から流れ出し、n- に流れ込む。
            j = source_index[c.id]
            if a is not None:
                B[a, j] -= 1.0
            if b is not None:
                B[b, j] += 1.0

        else:  # "V" と "L" は枝電流を未知数に持つ
            k = branch_index[c.id]
            # 節点の電流則: 枝電流は n+ から流れ出し、n- に流れ込む
            if a is not None:
                G[a, k] += 1.0
                G[k, a] += 1.0
            if b is not None:
                G[b, k] -= 1.0
                G[k, b] -= 1.0
            if c.type == "V":
                # v(n+) - v(n-) = V(t)
                B[k, source_index[c.id]] = 1.0
            else:
                # v(n+) - v(n-) - L di/dt = 0
                E[k, k] -= c.value
                ex0[k] = -c.value * c.ic

    unknowns = [f"v({name})" for name in nodes] + [f"i({cid})" for cid in branches]
    return System(E=E, G=G, B=B, ex0=ex0, unknowns=unknowns, sources=sources, netlist=netlist)
