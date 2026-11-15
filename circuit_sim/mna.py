"""ネットリストから回路方程式  E x' + G x = B u  を組み立てる（修正節点解析）。

未知数 x : 節点電圧（基準節点を除く）、続いて電圧源とインダクタの枝電流。
入力   u : 電圧源・電流源の値（素子の並び順）。

スイッチ（S）とダイオード（D）は、オンとオフで値が変わる抵抗として扱う。
回路状態（オン・オフの組）は整数の符号で表す。ビット j が 1 なら devices[j] がオン。
G にはスイッチとダイオードを含めず、状態ごとの G は state_conductance() で作る。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .ir import Netlist

MAX_DEVICES = 62   # 状態の符号を 64 ビット整数に収める


class CircuitError(ValueError):
    """回路として解けないときのエラー。"""


def _no_index() -> np.ndarray:
    return np.zeros(0, dtype=np.int64)


def _no_value() -> np.ndarray:
    return np.zeros(0)


@dataclass
class System:
    E: np.ndarray            # (n, n) 微分項の係数
    G: np.ndarray            # (n, n) 代数項の係数（S と D を除く）
    B: np.ndarray            # (n, m) 電源の係数
    ex0: np.ndarray          # (n,)   初期条件から直接組んだ E @ x(0)
    unknowns: list[str]      # "v(節点)" / "i(素子ID)"
    sources: list[str]       # B の列に対応する電源の素子ID
    netlist: Netlist
    # --- スイッチとダイオード（状態の符号のビット順。S が先、D が後） ---
    devices: list[str] = field(default_factory=list)
    n_switches: int = 0
    dev_pos: np.ndarray = field(default_factory=_no_index)   # n+ の未知数の番号（基準節点は -1）
    dev_neg: np.ndarray = field(default_factory=_no_index)   # n- の未知数の番号（基準節点は -1）
    dev_g_on: np.ndarray = field(default_factory=_no_value)  # オンのコンダクタンス [S]
    dev_g_off: np.ndarray = field(default_factory=_no_value)  # オフのコンダクタンス [S]


def _stamp_two_terminal(M: np.ndarray, a: int, b: int, g: float) -> None:
    """節点 a と b の間のコンダクタンス g を M に足す。番号 -1 は基準節点。"""
    if a >= 0:
        M[a, a] += g
    if b >= 0:
        M[b, b] += g
    if a >= 0 and b >= 0:
        M[a, b] -= g
        M[b, a] -= g


def state_conductance(system: System, code: int, off_scale: float = 1.0) -> np.ndarray:
    """回路状態 code のときの G（スイッチとダイオードを抵抗として足したもの）。

    off_scale: オフの素子のコンダクタンスに掛ける数（オフ抵抗が作る成分を見分けるときに使う）。
    """
    G = system.G.copy()
    for j in range(len(system.devices)):
        g = system.dev_g_on[j] if (code >> j) & 1 else system.dev_g_off[j] * off_scale
        _stamp_two_terminal(G, int(system.dev_pos[j]), int(system.dev_neg[j]), float(g))
    return G


def describe_state(system: System, code: int) -> str:
    """回路状態を人が読める形にする（エラーメッセージ用）。"""
    if not system.devices:
        return "（スイッチなし）"
    return ", ".join(
        f"{cid}={'オン' if (code >> j) & 1 else 'オフ'}" for j, cid in enumerate(system.devices)
    )


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

    def idx(node: str) -> int:
        return -1 if node == netlist.ground else node_index[node]

    for c in netlist.components:
        a, b = idx(c.nodes[0]), idx(c.nodes[1])

        if c.type == "R":
            _stamp_two_terminal(G, a, b, 1.0 / c.value)

        elif c.type == "C":
            _stamp_two_terminal(E, a, b, c.value)
            # E @ x(0) への寄与: 節点aの行に +C*Vc0、節点bの行に -C*Vc0
            if a >= 0:
                ex0[a] += c.value * c.ic
            if b >= 0:
                ex0[b] -= c.value * c.ic

        elif c.type == "I":
            # 電流は n+ から素子を通って n- へ。n+ から流れ出し、n- に流れ込む。
            j = source_index[c.id]
            if a >= 0:
                B[a, j] -= 1.0
            if b >= 0:
                B[b, j] += 1.0

        elif c.type in ("V", "L"):  # 枝電流を未知数に持つ
            k = branch_index[c.id]
            # 節点の電流則: 枝電流は n+ から流れ出し、n- に流れ込む
            if a >= 0:
                G[a, k] += 1.0
                G[k, a] += 1.0
            if b >= 0:
                G[b, k] -= 1.0
                G[k, b] -= 1.0
            if c.type == "V":
                # v(n+) - v(n-) = V(t)
                B[k, source_index[c.id]] = 1.0
            else:
                # v(n+) - v(n-) - L di/dt = 0
                E[k, k] -= c.value
                ex0[k] = -c.value * c.ic

        # "S" と "D" は状態で値が変わるので、ここでは足さない（state_conductance）

    switching = [c for c in netlist.components if c.type == "S"]
    n_switches = len(switching)
    switching += [c for c in netlist.components if c.type == "D"]
    if len(switching) > MAX_DEVICES:
        raise CircuitError(
            f"スイッチとダイオードが {len(switching)} 個ある。扱えるのは合わせて {MAX_DEVICES} 個まで"
        )

    unknowns = [f"v({name})" for name in nodes] + [f"i({cid})" for cid in branches]
    return System(
        E=E, G=G, B=B, ex0=ex0, unknowns=unknowns, sources=sources, netlist=netlist,
        devices=[c.id for c in switching],
        n_switches=n_switches,
        dev_pos=np.array([idx(c.nodes[0]) for c in switching], dtype=np.int64),
        dev_neg=np.array([idx(c.nodes[1]) for c in switching], dtype=np.int64),
        dev_g_on=np.array([1.0 / c.r_on for c in switching], dtype=float),
        dev_g_off=np.array([1.0 / c.r_off for c in switching], dtype=float),
    )
