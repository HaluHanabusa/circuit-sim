"""降圧コンバータ（同期整流、電圧モードのPID制御）のテンプレート buck_voltage_mode。

回路（スイッチングモデル）:
    V1（Vin）─ in ─ S1（上）─ sw ─ L1 ─ out ─┬─ R1（R_load）─ 0
                      S2（下）: sw ─ 0       ├─ [R_esr ─ cap ─] C1 ─ 0   （C_esr があるとき）
                                             └─ 負荷ステップ: R2 ─ ld ─ S3 ─ 0（R_load_step があるとき）
    S1 と S2 は相補のゲート（デッドタイム0）。S1 のデューティが制御器の指令 duty。
平均モデル: S1 と S2 を、電圧源 Vleg = duty × Vin とオン抵抗 Rleg の直列（0 ─ Vleg ─ sw_avg ─ Rleg ─ sw）に
置き換える（V1 は使わない。D-038。legs.py）。

制御: 制御周期ごとに v(out) を測り、偏差 Vref − v(out) にPIDを掛けてデューティ指令にする
（control/blocks.py の Pid。出力の上下限は d_min〜d_max、アンチワインドアップつき）。
指令は次の制御周期の先頭から使われ、
PWMはキャリアの周期の先頭で取り込む（演算遅れ 1 制御周期＋保持 0.5 サンプル）。
制御周期はキャリアの周期の整数倍にする（f_sw / f_ctrl が整数）。
"""
from __future__ import annotations

import math

from ..control import Pid, PidController, Pwm, Schedule, ScheduleController
from ..ir import DC, DEFAULT_R_ON, Component, Netlist, Spec
from ..messages import CodedError, Message, Names, Value
from .items import Display, FixedValue, Item, SignalInfo, read_items
from .legs import HalfBridgeLeg
from .t0 import Expanded

SWITCHING_STEPS_PER_PERIOD = 200   # スイッチングモデルの、キャリア1周期の分割数（エンジンの既定と同じ）
AVERAGE_STEPS_PER_PERIOD = 10      # 平均モデルの、キャリア1周期の分割数。レグの電圧は周期ごとに一定なので、
#                                    LC の共振（基準の回路でキャリアの43周期）を十分に解像し、
#                                    ステップ数はスイッチングモデルの 1/20
DEFAULT_LC_PERIODS = 20            # 既定の計算時間: LC の共振の周期のこの倍（基準の回路で 8.6 ms）。
#                                    起動やステップの過渡が収まる目安。ステップがあれば、
#   最後のステップの時刻に足す
FILTER_PER_PERIOD = 0.25           # Tf を省略したときの微分のフィルタの時定数（制御周期に対する比）
LOAD_GATE = "q_load"               # 負荷ステップのスイッチのゲート信号と指令の名前
LOAD_COMMAND = "load"
DUTY_COMMAND = "duty"              # PID が出すデューティの指令の名前
LEG_R_ON = DEFAULT_R_ON            # レグのスイッチ S1・S2 のオン抵抗（平均モデルの Rleg も同じ値）[Ω]。仕様の
#                                    項目ではなく、テンプレートが決める値。表示定義（DISPLAY）で画面に渡す
POSITIVE_ITEMS = ("Vin", "L", "C", "R_load", "R_load_step", "f_sw", "f_ctrl")     # 0 より大きい
NON_NEGATIVE_ITEMS = ("C_esr", "Kp", "Ki", "Kd", "Tf", "t_load_step", "t_ref_step")  # 0 以上

ITEMS: tuple[Item, ...] = (
    Item("Vin", "plant", "voltage", element="V1"),
    Item("L", "plant", "inductance", element="L1"),
    Item("C", "plant", "capacitance", element="C1"),
    Item("C_esr", "plant", "resistance", required=False, element="R_esr"),
    Item("R_load", "plant", "resistance", element="R1"),
    Item("R_load_step", "plant", "resistance", required=False, together=("t_load_step",), element="R2"),
    Item("t_load_step", "plant", "time", required=False, together=("R_load_step",), element="S3"),
    Item("f_sw", "control", "frequency", element="PWM"),
    Item("f_ctrl", "control", "frequency", element="PID"),
    Item("Vref", "control", "voltage", element="PID"),
    Item("Vref_step", "control", "voltage", required=False, together=("t_ref_step",), element="PID"),
    Item("t_ref_step", "control", "time", required=False, together=("Vref_step",), element="PID"),
    Item("Kp", "control", "per_voltage", element="PID"),
    Item("Ki", "control", "per_voltage_time", element="PID"),
    Item("Kd", "control", "time_per_voltage", element="PID"),
    Item("Tf", "control", "time", required=False, element="PID"),
    Item("d_min", "control", "ratio", element="PID"),
    Item("d_max", "control", "ratio", element="PID"),
)

# 表示する信号（画面仕様 付録A.4 の信号の表と同じ順。説明の文言は画面の辞書が持つ）
_COMMON_SIGNALS = (
    SignalInfo("v(out)", "V", True, "node_voltage", node="out"),
    SignalInfo("i(L1)", "A", True, "element_current", element="L1", positive=("sw", "out")),
    SignalInfo("v(sw)", "V", False, "node_voltage", node="sw"),
    SignalInfo("cmd(duty)", "", False, "command", command="duty"),
)
SWITCHING_SIGNALS = _COMMON_SIGNALS + (
    SignalInfo("gate(q1)", "", False, "gate", gate="q1", element="S1"),
    SignalInfo("gate(q2)", "", False, "gate", gate="q2", element="S2"),
    SignalInfo("v(in)", "V", False, "node_voltage", node="in"),
    SignalInfo("i(V1)", "A", False, "element_current", element="V1", positive=("in", "0")),
)
AVERAGE_SIGNALS = _COMMON_SIGNALS + (
    SignalInfo("i(Vleg)", "A", False, "element_current", element="Vleg", positive=("sw_avg", "0")),
)

# 内部の信号（画面に出さない。付録A.4 の内部の信号の表）。結果に出るかは、モデルと省略できる項目で変わる
SWITCHING_INTERNAL = (
    SignalInfo("on(S1)", "", False, "conduction", element="S1", internal=True),
    SignalInfo("on(S2)", "", False, "conduction", element="S2", internal=True),
)
AVERAGE_INTERNAL = (
    SignalInfo("v(sw_avg)", "V", False, "node_voltage", node="sw_avg", internal=True),
)
LOAD_STEP_INTERNAL = (
    SignalInfo("v(ld)", "V", False, "node_voltage", node="ld", internal=True),
    SignalInfo("gate(q_load)", "", False, "gate", gate="q_load", element="S3", internal=True),
    SignalInfo("cmd(load)", "", False, "command", command="load", internal=True),
    SignalInfo("on(S3)", "", False, "conduction", element="S3", internal=True),
)
ESR_INTERNAL = (
    SignalInfo("v(cap)", "V", False, "node_voltage", node="cap", internal=True),
)

# 表示定義のうちエンジンの側が決めるもの（画面仕様 12章の D2、14章の未決18）
DISPLAY = Display(
    template="buck_voltage_mode",
    items=ITEMS,
    fidelities=("switching", "average"),
    default_fidelity="switching",
    signals={"switching": SWITCHING_SIGNALS, "average": AVERAGE_SIGNALS},
    values=(FixedValue("r_on", LEG_R_ON, "ohm", ("S1", "S2", "Rleg")),),
)


def _check_values(p: dict[str, float]) -> None:
    positive = [k for k in POSITIVE_ITEMS if k in p and p[k] <= 0]
    non_negative = [k for k in NON_NEGATIVE_ITEMS if k in p and p[k] < 0]
    if positive or non_negative:
        parts = []
        if positive:
            parts.append(Message("RANGE_POSITIVE", names=Names(positive)))
        if non_negative:
            parts.append(Message("RANGE_NON_NEGATIVE", names=Names(non_negative)))
        raise CodedError("VALUES_OUT_OF_RANGE", parts=parts, positive=Names(positive),
                         non_negative=Names(non_negative))
    if not (0.0 <= p["d_min"] < p["d_max"] <= 1.0):
        raise CodedError("DUTY_LIMITS_ORDER", d_min=Value(p["d_min"]), d_max=Value(p["d_max"]))
    ratio = p["f_sw"] / p["f_ctrl"]
    if abs(ratio - round(ratio)) > 1e-9 * ratio or round(ratio) < 1:
        raise CodedError("FSW_NOT_MULTIPLE", f_sw=Value(p["f_sw"], "Hz"), f_ctrl=Value(p["f_ctrl"], "Hz"),
                         ratio=Value(ratio))
    if "R_load_step" in p and p["R_load_step"] == p["R_load"]:
        raise CodedError("LOAD_STEP_SAME")


def _load(p: dict[str, float], t_sw: float, notes: list[Message]) -> tuple[list[Component], list, list[Pwm]]:
    """負荷（と負荷ステップ）。ステップは、並列の抵抗 R2 をスイッチ S3 で入れる（または切る）。

    R_load > R_load_step（重くなる）: R1 = R_load、
    R2 = R_load·R_new / (R_load − R_new) をステップの時刻に入れる。
    R_load < R_load_step（軽くなる）: R1 = R_new、
    R2 = R_load·R_new / (R_new − R_load) をステップの時刻に切る。
    R2 からは S3 のオン抵抗を引く（オンのとき合成がちょうど目標の値になる）。オフのときは S3 のオフ抵抗
    （1 MΩ）が R2 と直列に残り、負荷が約 R / 1 MΩ（基準の回路で 6 ppm）だけ重く見える。
    ステップの時刻はスイッチング周期の先頭に切り上げる（スイッチは時刻で決まる指令を出す制御器が動かす）。
    R2 が S3 のオン抵抗以下になる（R_load と R_load_step の小さいほうが、ほぼオン抵抗以下）と作れない。
    """
    r_load = p["R_load"]
    if "R_load_step" not in p:
        return [Component(id="R1", type="R", nodes=("out", "0"), value=r_load)], [], []
    r_new, t_step = p["R_load_step"], p["t_load_step"]
    heavier = r_new < r_load
    r_base = r_load if heavier else r_new
    switch = Component(id="S3", type="S", nodes=("ld", "0"), gate=LOAD_GATE)
    r_extra = r_load * r_new / abs(r_load - r_new) - switch.r_on
    if r_extra <= 0:
        small = "R_load_step" if heavier else "R_load"
        raise CodedError("LOAD_STEP_TOO_SMALL", key=small, r=Value(min(r_load, r_new), "ohm"),
                         r_on=Value(switch.r_on, "ohm"))
    comps = [
        Component(id="R1", type="R", nodes=("out", "0"), value=r_base),
        Component(id="R2", type="R", nodes=("out", "ld"), value=r_extra),
        switch,
    ]
    on_before, on_after = (0.0, 1.0) if heavier else (1.0, 0.0)
    snapped = math.ceil(t_step / t_sw - 1e-6) * t_sw
    if abs(snapped - t_step) > 1e-9 * t_sw:
        notes.append(Message("LOAD_STEP_SNAPPED", symbol="t_load_step", t=Value(t_step, "s"),
                             t_grid=Value(snapped, "s"), t_sw=Value(t_sw, "s")))
    ctrl = ScheduleController(Schedule(on_before, ((snapped, on_after),)), LOAD_COMMAND, period=t_sw)
    gate = Pwm(name=LOAD_GATE, frequency=1.0 / t_sw, duty=LOAD_COMMAND)
    return comps, [ctrl], [gate]


def buck_voltage_mode(spec: Spec) -> Expanded:
    """仕様（層A）→ 層B ＋ ゲート信号 ＋ 制御器 ＋ 信号の情報。モデルの種類は spec.simulation.fidelity。"""
    notes: list[Message] = []
    p = read_items(spec, ITEMS, notes)
    _check_values(p)
    fidelity = spec.simulation.fidelity or "switching"
    t_sw, t_ctrl = 1.0 / p["f_sw"], 1.0 / p["f_ctrl"]

    # --- 電力の部分 -------------------------------------------------------------
    leg = HalfBridgeLeg(positive="in", negative="0", output="sw", duty=DUTY_COMMAND, frequency=p["f_sw"],
                        v_dc=p["Vin"], r_on=LEG_R_ON)
    leg_parts, gates = leg.expand(fidelity)
    comps = []
    if fidelity == "switching":
        comps.append(Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=p["Vin"])))
    else:
        notes.append(Message("AVERAGE_MODEL_USED"))
    comps += leg_parts
    comps.append(Component(id="L1", type="L", nodes=("sw", "out"), value=p["L"]))
    if p.get("C_esr", 0.0) > 0.0:
        comps.append(Component(id="R_esr", type="R", nodes=("out", "cap"), value=p["C_esr"]))
        comps.append(Component(id="C1", type="C", nodes=("cap", "0"), value=p["C"]))
    else:
        comps.append(Component(id="C1", type="C", nodes=("out", "0"), value=p["C"]))
        if "C_esr" not in p:
            notes.append(Message("ESR_OMITTED"))
    load_parts, load_ctrls, load_gates = _load(p, t_sw, notes)
    comps += load_parts
    netlist = Netlist(components=comps)

    # --- 制御 -----------------------------------------------------------------
    if "Tf" in p:
        t_filter = p["Tf"]
    else:
        t_filter = FILTER_PER_PERIOD * t_ctrl
        if p["Kd"] > 0:
            notes.append(Message("TF_DEFAULTED", ratio=Value(FILTER_PER_PERIOD), tf=Value(t_filter, "s")))
    pid = Pid(p["Kp"], p["Ki"], p["Kd"], period=t_ctrl, t_filter=t_filter, lower=p["d_min"], upper=p["d_max"])
    changes = ((p["t_ref_step"], p["Vref_step"]),) if "Vref_step" in p else ()
    reference = Schedule(p["Vref"], changes)
    controller = PidController(
        pid, measure="v(out)", output=DUTY_COMMAND, reference=reference, initial_output=p["d_min"]
    )
    for key, v in [("Vref", p["Vref"])] + [("Vref_step", v) for _, v in changes]:
        if v > p["d_max"] * p["Vin"] or v < p["d_min"] * p["Vin"]:
            notes.append(Message("REFERENCE_OUT_OF_RANGE", symbol=key, value=Value(v, "V"),
                                 low=Value(p["d_min"] * p["Vin"], "V"),
                                 high=Value(p["d_max"] * p["Vin"], "V")))

    # --- 計算の時間と刻み -------------------------------------------------------
    steps = SWITCHING_STEPS_PER_PERIOD if fidelity == "switching" else AVERAGE_STEPS_PER_PERIOD
    h = spec.simulation.h or t_sw / steps
    if spec.simulation.t_end:
        t_end = spec.simulation.t_end
    else:
        t_lc = 2.0 * math.pi * math.sqrt(p["L"] * p["C"])
        last = max([0.0] + [p[k] for k in ("t_load_step", "t_ref_step") if k in p])
        t_end = math.ceil((last + DEFAULT_LC_PERIODS * t_lc) / t_ctrl) * t_ctrl    # 制御周期の整数倍
    for key in ("t_load_step", "t_ref_step"):
        if key in p and p[key] >= t_end:
            notes.append(Message("STEP_AFTER_END", key=key, symbol=key, t=Value(p[key], "s"),
                                 t_end=Value(t_end, "s")))

    # 表示する信号（付録A.4 の表の順）と、この回路の内部の信号
    signals = list(SWITCHING_SIGNALS + SWITCHING_INTERNAL if fidelity == "switching"
                   else AVERAGE_SIGNALS + AVERAGE_INTERNAL)
    if "R_load_step" in p:
        signals += LOAD_STEP_INTERNAL
    if p.get("C_esr", 0.0) > 0.0:
        signals += ESR_INTERNAL
    return Expanded(
        netlist, t_end, h, probes=["v(out)", "i(L1)"], notes=notes,
        gates=gates + load_gates, controllers=[controller] + load_ctrls,
        signals=signals, fidelity=fidelity,
    )
