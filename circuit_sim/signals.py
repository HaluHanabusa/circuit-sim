"""結果の信号の、構造化した情報（名前、単位、種類、節点・素子・正の向き、表示するか内部か）。

文言（信号の説明）は持たない。説明は画面の辞書（signal.<テンプレート>.<名前>.desc）が持つ（決まり8）。

エンジン（simulate）は、結果のすべての名前について、回路から分かる情報（単位と正の向き）を作る
（describe_signals）。テンプレートは、表示する信号（画面仕様の付録A の信号の表）と内部の信号（画面に出さない。
付録A.4 の内部の信号の表）を自分で決めて返す（Expanded.signals）。Expanded.run() が両方を合わせる。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .ir import Netlist
from .messages import CodedError

SignalKind = Literal["node_voltage", "element_current", "command", "gate", "conduction"]

# 信号の種類 → 単位の記号（無次元は ""。CSV の見出しでは [1]）
KIND_UNIT = {"node_voltage": "V", "element_current": "A", "command": "", "gate": "", "conduction": ""}


@dataclass(frozen=True)
class SignalInfo:
    """1つの信号の情報。

    name    : Result から取り出す名前（"v(out)"、"i(L1)"、"cmd(duty)"、"gate(q1)"、"on(S1)"）
    unit    : 単位の記号（"V"、"A"。無次元は ""）
    shown   : 既定で表示するか（テンプレートが決める。エンジンだけで作った情報では False）
    kind    : "node_voltage"（節点の電圧。基準は基準節点）、"element_current"（素子の電流）、
              "command"（制御器の指令）、"gate"（PWMのゲート信号。1 = オン）、
              "conduction"（スイッチやダイオードの導通。1 = オン）
    node    : node_voltage の節点
    element : element_current と conduction の素子ID。gate では、そのゲートが動かすスイッチの素子ID
              （どのスイッチも動かさないゲートでは None）
    positive: element_current の正の向き（素子の中を、1つ目の節点から2つ目の節点へ。素子の nodes と同じ順）
    command : command の指令の名前
    gate    : gate のゲート信号の名前
    internal: 内部の信号（画面に出さない。平均モデルの v(sw_avg)、負荷ステップを作る素子の信号など）
    """

    name: str
    unit: str
    shown: bool
    kind: SignalKind
    node: str | None = None
    element: str | None = None
    positive: tuple[str, str] | None = None
    command: str | None = None
    gate: str | None = None
    internal: bool = False

    def __post_init__(self) -> None:
        expected = {
            "node_voltage": f"v({self.node})" if self.node else None,
            "element_current": f"i({self.element})" if self.element and self.positive else None,
            "command": f"cmd({self.command})" if self.command else None,
            "gate": f"gate({self.gate})" if self.gate and (self.element or self.internal) else None,
            "conduction": f"on({self.element})" if self.element else None,
        }.get(self.kind)
        if expected is None or expected != self.name:
            raise CodedError("SIGNAL_INFO_INCONSISTENT", name=self.name, kind=self.kind)

    @property
    def csv_unit(self) -> str:
        """CSV の見出しの単位（無次元は "1"。画面仕様 8.4）。"""
        return self.unit or "1"


def describe_signals(
    netlist: Netlist, names: Iterable[str], gate_switches: dict[str, Sequence[str]] | None = None
) -> dict[str, SignalInfo]:
    """結果の名前ごとに、回路から分かる情報を作る（表示するか内部かは決めない: shown と internal は False）。

    gate_switches: ゲート信号の名前 → そのゲートで動くスイッチの素子ID（ネットリストの順）。
    """
    gates = gate_switches or {}
    out: dict[str, SignalInfo] = {}
    for name in names:
        kind, _, arg = name.partition("(")
        arg = arg[:-1]
        if kind == "v":
            info = SignalInfo(name, "V", False, "node_voltage", node=arg)
        elif kind == "i":
            info = SignalInfo(name, "A", False, "element_current", element=arg,
                              positive=tuple(netlist.component(arg).nodes))
        elif kind == "cmd":
            info = SignalInfo(name, "", False, "command", command=arg)
        elif kind == "gate":
            switches = list(gates.get(arg, ()))
            info = SignalInfo(name, "", False, "gate", gate=arg, element=switches[0] if switches else None,
                              internal=not switches)
        elif kind == "on":
            info = SignalInfo(name, "", False, "conduction", element=arg)
        else:  # pragma: no cover  （結果の名前は上の5つの形だけ）
            raise CodedError("SIGNAL_INFO_INCONSISTENT", name=name, kind=kind)
        out[name] = info
    return out


def merge_signal_info(
    engine: dict[str, SignalInfo], declared: Sequence[SignalInfo]
) -> dict[str, SignalInfo]:
    """エンジンの情報に、テンプレートが決めた情報（表示する信号と内部の信号）を重ねる。

    並びは、テンプレートが決めた順（結果に無い名前は飛ばす）、続いて決めていない名前（結果の順）。
    テンプレートが決めていない名前は、内部の信号として扱う（画面仕様 付録A.4: 区別が返らない名前は
    内部の信号。表に無い信号を黙って足さない）。どの名前も漏れなく、どちらかに入る。
    """
    out: dict[str, SignalInfo] = {}
    for s in declared:
        if s.name in engine:
            out[s.name] = s
    for name, s in engine.items():
        if name not in out:
            out[name] = replace(s, shown=False, internal=True)
    return out
