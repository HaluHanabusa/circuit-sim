"""ネットリストを時間応答に変換する入口。

基本は台形則。計算の先頭と不連続点の直後だけ、刻みより速い成分を消す減衰ステップで進める。
スイッチ（S）やダイオード（D）を含む回路は、回路状態ごとの行列を使い回しながら進め、
制御周期ごとに核から戻って制御器を呼ぶ。
大きすぎる計算は配列を確保する前に断り、刻みが粗いときなどは結果の notes で知らせる。

注意とエラーは番号と差し込む値で返す（messages.Message、messages.CodedError。文面は辞書が持つ）。
計算は区切って進め（核の外。区切りは核の呼び出しの単位）、区切りごとに、進み具合の知らせ（progress）、
中断の求め（cancel）、回路の変数の発散を確かめる。区切っても、区切らないときと結果はビット単位で同じ。
"""
from __future__ import annotations

import csv
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import kernel
from .control import Controller, Pwm
from .ir import DC, Command, Netlist, Sine, Step
from .messages import (
    CodedError,
    CodedKeyError,
    CodedTypeError,
    Message,
    Names,
    SimulationCancelled,
    Value,
)
from .mna import CircuitError, System, build_system, describe_state, state_conductance
from .signals import SignalInfo, describe_signals

DEFAULT_STEPS_PER_PERIOD = 200   # キャリア1周期の分割数の既定値
DEFAULT_DAMPING_STEPS = 2        # 計算の先頭と不連続点の直後に入れる減衰ステップの数の既定値
MAX_STEPS = 20_000_000           # ステップ数の上限の既定値（スコープが扱う点数の目安と同じ 2000万）
MAX_RESULT_BYTES = 2 * 2**30     # 計算に使う配列（結果と作業用）の大きさの上限 [バイト]。8 GB の PC で、
#                                  画面とブラウザの分を残して1回の計算に使ってよい目安。
#                                  見積もりは _memory_bytes
MIN_POINTS_PER_PERIOD = 10       # 正弦波や回路の振動の1周期に、これより点が少ないと注意を出す
MAX_STEP_PER_TIME_CONSTANT = 2.0  # 刻みが回路の時定数のこの倍数を超えると注意を出す（台形則が振動し始める境）
DAMP_STEP_PER_TIME_CONSTANT = 0.5  # スイッチの無い回路で、刻みが時定数のこの倍数を超えると
#                                    減衰ステップを入れる。
#                                    台形則だけの誤差は RC のステップ応答で h = τ/2 のとき 0.79%、τ で 3.5%、
#                                    1.99τ で 13%（E1a-A.md）。減衰ステップを入れると τ/2〜2τ で 0.5% 以下
DIODE_ON_VOLTAGE = 1e-9          # オフのダイオードをオンにする順方向電圧 [V]（丸め誤差での反転を防ぐ）
DIODE_BAND_REL = 1e-8            # t = 0 の状態が落ち着かないとき、丸め誤差とみなす電圧の幅
#                                  （節点電圧の最大値に対する比。_diode_band）。三相整流（r_on 1 mΩ、
#                                  r_off 1 MΩ）の t=0 で測った丸め誤差は最大 6.5e-10 倍で、その約15倍。
#                                  通常の判定には使わない
SIMULTANEOUS_FLIPS = 4           # ダイオードの状態決定で、違反したものを同時に反転する回数
_GRID_TOL = 1e-6                 # 周期が時間刻みの整数倍かどうかの判定（ステップ数に対する相対値）

# --- 区切り（進み具合、中断、発散の確かめ）。核の外で、核を呼ぶ単位で区切る ---------------------------
CHUNK_SECONDS = 0.02             # 1つの区切りの計算時間の目安 [s]。中断に応じるまでの遅れと、進み具合を
#                                  知らせる間隔の上限は、ほぼこの値になる（受け入れ基準: どちらも 0.2 s 以内）
CHUNK_STEPS_FIRST = 512          # 最初の区切りのステップ数。以後は、かかった時間から CHUNK_SECONDS に合わせる
CHUNK_STEPS_MIN = 64
CHUNK_STEPS_MAX = 1 << 20
PROGRESS_SECONDS = 0.05          # 進み具合を知らせる最短の間隔 [s]（知らせる側の手間を抑える）
SETUP_SHARE = 0.02               # 進み具合のうち、計算の準備（電源の配列など）に割り当てる分
POST_SHARE = 0.01                # 進み具合のうち、後処理（注意の判定、信号の配列）に割り当てる分
SETUP_BLOCK = 1 << 17            # 準備の配列を埋める単位の行数（この単位ごとに中断の求めを見る。
#                                  1000 万ステップの計算で、1つの単位が数 ms〜十数 ms）
DIVERGENCE_FACTOR = 1e12         # 発散とみなす大きさ: 回路の電圧（電流）の目安のこの倍（_divergence_limits）


class StepLimitError(CodedError):
    """計算が大きすぎて、計算の前に断ったときのエラー（番号 STEPS_OVER_LIMIT か MEMORY_OVER_LIMIT）。

    文面は「理由と直し方（h と t_end）」＋「simulate() の max_steps で上限を上げられること」。
    reason と without_api_hint は前半だけ（max_steps を渡せない仕様JSONやコマンドから使うとき）。
    code と params は前半の番号と差し込む値。
    """

    def __init__(self, reason: Message, api_hint: Message):
        super().__init__(Message("WITH_API_HINT", reason=reason, hint=api_hint))
        self._set_message(self.message, reason.code, reason.params)
        self.reason = reason
        self.without_api_hint = str(reason)


class DivergenceError(CodedError):
    """計算の途中で、回路の変数が有限でなくなった、または発散とみなす大きさを超えた（番号 RUN_DIVERGED、
    RUN_DIVERGED_NOT_FINITE）。params に時刻 t、変数の名前 signal、値 value。途中の結果は返さない。"""


@dataclass
class Result:
    t: np.ndarray                 # (n_steps + 1,)
    x: np.ndarray                 # (n_steps + 1, 未知数の数)
    unknowns: list[str]
    netlist: Netlist
    h: float
    notes: list[Message] = field(default_factory=list)   # 番号つきの注意（Message は str の一種）
    # --- スイッチを含む計算で入るもの ---
    signals: dict[str, np.ndarray] = field(default_factory=dict)   # "gate(名前)" "cmd(名前)" "on(素子ID)"
    devices: list[str] = field(default_factory=list)               # S と D の素子ID（状態の符号のビット順）
    sample_state: np.ndarray | None = None    # 各時刻の値と整合する回路状態の符号（S と D の電流に使う）
    stats: dict[str, int] = field(default_factory=dict)            # 用意した回路状態の数など
    # stats の欄: states（用意した回路状態の数）、state_changes（回路状態が変わったステップと、時刻で決まる
    #   電源の不連続点の数）、command_changes（指令で決まる電源の値が変わった制御周期の先頭の数。
    #   state_changes とは別に数える。どちらも、その直後を減衰ステップにする不連続点として扱う）、
    #   retries（捨てた試行）、rescues_at_start（t = 0 でダイオードの状態が確定せず、違反が丸め誤差の
    #   幅の中の状態を採った（救済した）なら 1。_initial_state を参照）
    signal_info: dict[str, SignalInfo] = field(default_factory=dict)
    # 名前ごとの信号の情報（単位、種類、正の向き、表示するか内部か）。names のすべてを、漏れなく持つ。
    # simulate() は回路から分かる情報（表示と内部の区別はしない）を入れ、テンプレートの Expanded.run() が
    # 表示する信号と内部の信号の区別を重ねる

    @property
    def names(self) -> list[str]:
        """[] で取り出せる波形の名前（回路の変数、ゲート信号、指令、導通状態）。"""
        return self.unknowns + list(self.signals)

    @property
    def display_names(self) -> list[str]:
        """表示する信号の名前（内部の印の無いもの。並びは signal_info の順）。"""
        return [n for n, s in self.signal_info.items() if not s.internal]

    @property
    def internal_names(self) -> list[str]:
        """内部の信号の名前（画面に出さない。display_names と合わせると names のすべてになる）。"""
        return [n for n, s in self.signal_info.items() if s.internal]

    def __getitem__(self, name: str) -> np.ndarray:
        """"v(out)" や "i(L1)"、"gate(q1)"、"cmd(duty)"、"on(D1)" の形で波形を取り出す。

        回路の変数は、その時刻の値。スイッチやダイオードがその時刻で切り替わる場合と、指令で値が決まる電源が
        その時刻で変わる場合は、変わる前の値。時刻で決まる電源がステップする場合は、変わった後の値。
        gate / cmd / on は、その時刻から次の時刻までの区間で使われた値（1 = オン）。
        最後の点は、最後の区間の値を繰り返す。
        """
        if name in self.signals:
            return self.signals[name]
        try:
            return self.x[:, self.unknowns.index(name)]
        except ValueError:
            raise CodedKeyError("RESULT_NO_SIGNAL", name=name, names=Names(self.names)) from None

    def v(self, node: str, ref: str | None = None) -> np.ndarray:
        """節点電圧。ref を指定すると2節点間の電圧。"""
        def one(n: str) -> np.ndarray:
            return np.zeros_like(self.t) if n == self.netlist.ground else self[f"v({n})"]
        return one(node) - (one(ref) if ref is not None else 0.0)

    def i(self, comp_id: str) -> np.ndarray:
        """素子を n+ から n- へ流れる電流。電圧源・インダクタ・抵抗・スイッチ・ダイオードに対応。"""
        c = self.netlist.component(comp_id)
        if c.type in ("V", "L"):
            return self[f"i({comp_id})"]
        if c.type == "R":
            return self.v(c.nodes[0], c.nodes[1]) / c.value
        if c.type in ("S", "D") and self.sample_state is not None:
            on = (self.sample_state >> self.devices.index(comp_id)) & 1
            return self.v(c.nodes[0], c.nodes[1]) * np.where(on == 1, 1.0 / c.r_on, 1.0 / c.r_off)
        raise CodedKeyError("RESULT_NO_CURRENT", id=comp_id, type=c.type)

    def to_csv(
        self, path: str | Path, names: list[str] | None = None, *, units: bool = False,
        notes: bool = False,
    ) -> None:
        """波形を CSV に書く。UTF-8、1行目が見出し、1列目が時刻、値は倍精度を失わない桁数（repr）。

        units : 見出しに単位を角かっこで付ける（"t [s],v(out) [V],cmd(duty) [1]"。画面と同じ形。D-042）
        notes : 注意を、データの後ろに1つ1行で足す（"# [番号] 注意: 文"。表計算ソフトや numpy.loadtxt では
                # で始まる行を飛ばせる）
        行の終わりは、どの行も csv モジュールの既定（CR LF。E0 から同じ）。
        """
        names = names or self.unknowns
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if units:
                w.writerow(["t [s]"] + [f"{n} [{self.unit_of(n) or '1'}]" for n in names])
            else:
                w.writerow(["t"] + names)
            cols = [self[n] for n in names]
            for k in range(len(self.t)):
                w.writerow([repr(float(self.t[k]))] + [repr(float(c[k])) for c in cols])
            if notes:
                for note in self.notes:
                    f.write(render_note_line(note, "CSV_NOTE") + w.dialect.lineterminator)

    def unit_of(self, name: str) -> str:
        """信号の単位の記号（無次元は ""）。"""
        info = self.signal_info.get(name)
        if info is None:
            self[name]                                         # 無い名前はここで番号つきのエラー
            info = describe_signals(self.netlist, [name])[name]
        return info.unit


def render_note_line(note: Message, code: str = "CLI_NOTE", lang: str = "ja") -> str:
    """注意1つを、番号つきの1行にする（コマンドラインと CSV の書き方）。"""
    text = note.text(lang) if isinstance(note, Message) else str(note)
    return Message(code, text=text, code=getattr(note, "code", "")).text(lang)


def _sample_sources(system: System, n_steps: int, h: float, notes: list[Message], tick=None):
    """電源を時間格子上の配列にする。戻り値: u_start, u_end, event, u_last

    u_last は、最後の格子点でちょうど電源がステップするときの、ステップした後の値（無ければ None）。
    指令で決まる電源（Command）の列は 0 のまま返す（制御周期ごとに _simulate_switched が埋める）。
    配列は SETUP_BLOCK 行ずつ埋め、そのたびに tick() を呼ぶ（中断の求めを見る）。どの要素も、まとめて
    埋めたときと同じ式で計算するので、値はビット単位で同じ。
    """
    m = len(system.sources)
    u_start = np.zeros((n_steps, m))
    u_end = np.zeros((n_steps, m))
    event = np.zeros(n_steps, dtype=bool)
    last_step: dict[int, float] = {}       # 最後の格子点でステップする電源の、ステップした後の値

    for j, cid in enumerate(system.sources):
        w = system.netlist.component(cid).waveform
        if isinstance(w, DC):
            for b0, b1 in _blocks(n_steps, tick):
                u_start[b0:b1, j] = w.value
                u_end[b0:b1, j] = w.value
        elif isinstance(w, Sine):
            for b0, b1 in _blocks(n_steps, tick):
                t = h * np.arange(b0, b1 + 1)
                y = w.offset + w.amplitude * np.sin(
                    2.0 * math.pi * w.frequency * t + math.radians(w.phase_deg)
                )
                u_start[b0:b1, j] = y[:-1]
                u_end[b0:b1, j] = y[1:]
            points = 1.0 / (w.frequency * h)
            if points < MIN_POINTS_PER_PERIOD:
                notes.append(Message(
                    "SINE_UNDERSAMPLED", id=cid, period=Value(1.0 / w.frequency, "s"), points=Value(points),
                    h_suggest=Value(1.0 / (20 * w.frequency), "s"), h=Value(h, "s"),
                ))
        elif isinstance(w, Step):
            ks = int(round(w.t_step / h))
            if abs(ks * h - w.t_step) > 1e-9 * h:
                notes.append(Message("STEP_TIME_ROUNDED", id=cid, t=Value(w.t_step, "s"),
                                     t_grid=Value(ks * h, "s")))
            for b0, b1 in _blocks(n_steps, tick):
                k = np.arange(b0, b1)
                u_start[b0:b1, j] = np.where(k >= ks, w.final, w.initial)       # t[k] の右側の値
                u_end[b0:b1, j] = np.where(k + 1 > ks, w.final, w.initial)      # t[k+1] の左側の値
            if 0 < ks < n_steps and w.final != w.initial:
                event[ks] = True
            if ks == n_steps and w.final != w.initial:
                last_step[j] = w.final
        elif isinstance(w, Command):
            pass
        else:  # pragma: no cover
            raise CircuitError("UNSUPPORTED_WAVEFORM", id=cid, kind=type(w).__name__)
    u_last = None
    if last_step:
        u_last = u_end[-1].copy()          # ほかの電源は、最後の区間の終わりの値のまま
        for j, value in last_step.items():
            u_last[j] = value
    return u_start, u_end, event, u_last


def _count_steps(t_end: float, h: float, max_steps: int | None, notes: list[Message]) -> int:
    """ステップ数。配列を確保する前に、大きすぎる計算を理由つきで断る。"""
    limit = MAX_STEPS if max_steps is None else max_steps
    ratio = t_end / h
    if not math.isfinite(ratio) or ratio > limit + 0.5:
        raise StepLimitError(
            Message("STEPS_OVER_LIMIT", steps=Value(ratio), t_end=Value(t_end, "s"), h=Value(h, "s"),
                    limit=limit, t_max=Value(limit * h, "s"), h_min=Value(t_end / limit, "s")),
            Message("STEPS_API_HINT"),
        )
    n_steps = int(round(ratio))
    if n_steps < 1:
        raise CodedError("T_END_SHORTER_THAN_H")
    if abs(ratio - n_steps) > 1e-6:
        given, used = _distinct(t_end, n_steps * h)
        notes.append(Message(
            "T_END_ROUNDED", t_end=Value(t_end, "s", given), h=Value(h, "s"),
            t_used=Value(n_steps * h, "s", used), n=n_steps,
        ))
    return n_steps


def _distinct(a: float, b: float) -> tuple[str, str]:
    """2つの値を、違いが見える桁数で書く（6桁で同じに見えるときは桁を増やす）。

    a（利用者が指定した値）は、10桁までで書けるならその桁数まで書く（0.4999995 を 0.499999 と書かない）。
    """
    for digits in range(6, 18):
        x, y = f"{a:.{digits}g}", f"{b:.{digits}g}"
        if x != y and (float(x) == a or digits >= 10):
            return x, y
    return repr(a), repr(b)


FIXED_BYTES = 2**20      # ステップ数によらない分（回路状態ごとの行列など。小さな回路の実測は 0.1 MB 未満）


def _memory_bytes(
    n_steps: int, n_unknowns: int, n_sources: int, *,
    switched: bool = False, n_gates: int = 0, n_commands: int = 0, n_devices: int = 0,
) -> tuple[int, int]:
    """計算に使う配列の大きさの見積もり [バイト]。戻り値: （全体、そのうち結果の回路の変数 x）。

    1ステップあたり（8 バイト = float64 / int64、1 バイト = bool）:
      共通       : x 8·未知数、電源の値 8·電源数 × 2（区間の両端）、不連続の印 1、時刻 8、
                   電源の波形や時刻を作る一時配列 8。ほかに、ステップ数によらない分 FIXED_BYTES
      スイッチあり: 電源の値の平均 8·電源数、回路状態の記録 8 × 3（区間ごと、スイッチのビット、各時刻）、
                   ゲート 1 + 8（bool と、結果に返す値）× ゲート数、指令 8 × 2 × 指令数、
                   導通の信号 8 × 素子数、その計算の一時配列 8 × 2
    実測（tracemalloc の最大値、40万ステップ）: 基準の降圧回路で 138 バイト（見積もり 146）、
    直列RLC（スイッチなし）で 73 バイト（見積もり 73）。結果の x に対して 3.5 倍と 1.8 倍。
    """
    per_step = 8 * n_unknowns + 16 * n_sources + 1 + 8 + 8
    if switched:
        per_step += 8 * n_sources + 24 + 9 * n_gates + 16 * n_commands + 8 * n_devices + 16
    return (n_steps + 1) * per_step + FIXED_BYTES, (n_steps + 1) * 8 * n_unknowns


def _check_result_size(
    n_steps: int, n_unknowns: int, max_steps: int | None, n_sources: int = 0, **switching
) -> None:
    """結果と作業用の配列が大きくなりすぎないか（ステップ数の上限を自分で指定したときは見ない）。"""
    total, result = _memory_bytes(n_steps, n_unknowns, n_sources, **switching)
    if max_steps is None and total > MAX_RESULT_BYTES:
        raise StepLimitError(
            Message("MEMORY_OVER_LIMIT", total=Value(total / 2**30, "GB"), steps=n_steps, unknowns=n_unknowns,
                    result=Value(result / 2**30, "GB"), limit=Value(MAX_RESULT_BYTES / 2**30, "GB")),
            Message("MEMORY_API_HINT"),
        )


def _has_unresolved_rate(rates: np.ndarray, h: float) -> bool:
    """台形則だけでは誤差が大きくなる速い成分があるか（|λ|·h > DAMP_STEP_PER_TIME_CONSTANT）。

    |λ|·h > 2 では、その成分が符号を変えながら残る。0.5〜2 では振動はしないが、計算の先頭や
    電源のステップの直後の誤差が 1% を超える。
    """
    rates = rates[np.isfinite(rates)]
    return bool(rates.size) and float(np.abs(rates).max()) * h > DAMP_STEP_PER_TIME_CONSTANT


def _resolution_notes(
    rates: np.ndarray, h: float, notes: list[Message], damped: bool = True, *,
    n_steps: int | None = None, limit: int = MAX_STEPS, who=None,
) -> None:
    """時間刻みが回路の速い成分を解像していなければ、何に対して粗いかを注意に書く。

    damped : 計算の先頭と不連続点の直後に減衰ステップを入れているか（damping_steps > 0）。
    n_steps, limit : 計算のステップ数と上限。勧める刻みでのステップ数が上限を超えるなら、刻みを
             細かくする代わりに、その成分を作る素子を挙げて、値を見直すよう書く。
    who    : 固有値 λ → その成分を作る素子の ID の一覧（_mode_elements）。
    """
    rates = rates[np.isfinite(rates)]
    decaying = rates[np.abs(rates.imag) <= np.abs(rates.real)]
    ringing = rates[np.abs(rates.imag) > np.abs(rates.real)]
    if decaying.size:
        k = int(np.argmax(np.abs(decaying)))
        fastest = float(np.abs(decaying[k]))
        if fastest * h > MAX_STEP_PER_TIME_CONSTANT:
            tau = 1.0 / fastest
            common = {"h": Value(h, "s"), "tau": Value(tau, "s"), "ratio": Value(h / tau),
                      "h_suggest": Value(tau / 10, "s")}
            needed = None if n_steps is None else n_steps * h / (tau / 10)
            if not damped:
                notes.append(Message("STEP_COARSE_UNDAMPED", **common))
            elif needed is not None and needed > limit:
                ids = who(decaying[k]) if who is not None else []
                code = "STEP_COARSE_OVER_LIMIT" if ids else "STEP_COARSE_OVER_LIMIT_NO_IDS"
                notes.append(Message(code, **common, ids=Names(ids), needed=Value(needed), limit=limit))
            else:
                notes.append(Message("STEP_COARSE", **common))
    if ringing.size:
        omega = float(np.abs(ringing.imag).max())
        points = 2.0 * math.pi / (omega * h)
        if points < MIN_POINTS_PER_PERIOD:
            period = 2.0 * math.pi / omega
            notes.append(Message(
                "STEP_COARSE_RINGING", h=Value(h, "s"), period=Value(period, "s"), points=Value(points),
                h_suggest=Value(period / 20, "s"),
            ))


MODE_SHARE = 0.1          # 成分のエネルギー（L、C）や損失（抵抗）の最大に対して、
#                           この比以上の素子を「作る素子」とする


def _mode_elements(system: System, G: np.ndarray, Rex: np.ndarray, lam: complex, code: int = 0) -> list[str]:
    """固有値 lam の成分を作る素子の ID（蓄える素子 L・C と、損失の大きい抵抗・スイッチ・ダイオード）。

    状態量 y = E x について y' = −G Rex y なので、−G Rex の固有ベクトル w から、その成分の
    回路の変数 x = Rex w が分かる。インダクタは L·|i|²、コンデンサは C·|v|²、抵抗は |v|² / R
    （スイッチとダイオードは回路状態 code のオン・オフの抵抗）で比べ、それぞれ最大の MODE_SHARE 倍
    以上のものを挙げる。
    """
    values, vectors = np.linalg.eig(-G @ Rex)
    x = Rex @ vectors[:, int(np.argmin(np.abs(values - lam)))]
    netlist = system.netlist
    idx = {name: i for i, name in enumerate(system.unknowns)}

    def across(c) -> float:
        a = 0.0 if c.nodes[0] == netlist.ground else x[idx[f"v({c.nodes[0]})"]]
        b = 0.0 if c.nodes[1] == netlist.ground else x[idx[f"v({c.nodes[1]})"]]
        return float(abs(a - b))

    stored, lost = {}, {}
    for c in netlist.components:
        if c.type == "L":
            stored[c.id] = c.value * abs(x[idx[f"i({c.id})"]]) ** 2
        elif c.type == "C":
            stored[c.id] = c.value * across(c) ** 2
        elif c.type == "R":
            lost[c.id] = across(c) ** 2 / c.value
        elif c.type in ("S", "D"):
            on = (code >> system.devices.index(c.id)) & 1
            lost[c.id] = across(c) ** 2 / (c.r_on if on else c.r_off)
    out = []
    for group in (stored, lost):
        top = max(group.values(), default=0.0)
        out += [cid for cid, v in group.items() if top > 0 and v >= MODE_SHARE * top]
    return out


OFF_PROBE_SCALE = 0.1     # オフの素子のコンダクタンスをこの倍（オフ抵抗を10倍）にして、動く成分を見分ける
SAME_RATE_TOL = 0.01      # オフ抵抗を変えても、この相対差より動かない成分を回路そのものの成分とみなす


def _switched_rates(system: System, table: _StateTable, codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """計算で使った回路状態ごとの固有値のうち、回路そのものの成分（オフ抵抗が作る見かけの成分を除く）。

    オフの素子はオフ抵抗 r_off（既定 1 MΩ）の抵抗なので、インダクタの電流がオフの素子しか通れない状態では、
    時定数 L / r_off（数 ps）の成分ができる。これは理想スイッチのモデルの都合で、
    切り替えの直後の後退オイラーと
    減衰ステップが消す。この成分の大きさは r_off に比例するので、r_off を10倍にして同じ大きさ |λ| の
    固有値が（相対 SAME_RATE_TOL 以内で）残るものだけを返す。スイッチを抵抗に置き換えた回路と同じ注意になる。
    戻り値: （固有値、それぞれの回路状態の符号）。
    """
    found, where = [], []
    for code in codes:
        code = int(code)
        G = state_conductance(system, code)
        rates = kernel.natural_rates(G, table.rex[table.index[code]])
        rates = rates[np.isfinite(rates) & (rates != 0)]
        if not rates.size:
            continue
        G_probe = state_conductance(system, code, off_scale=OFF_PROBE_SCALE)
        try:
            Rex_probe, _ = kernel.consistent_maps(system.E, G_probe, system.B)
        except CircuitError:
            found.append(rates)
            where.append(np.full(rates.size, code))
            continue
        # 大きさ |λ| で比べる。オフ抵抗だけで減衰する振動（オフ・オフの区間の L とスイッチ節点の容量など）は、
        # 減衰（実部）は r_off で動くが |λ| = 1/√(LC) は動かない。
        # 見かけの成分 L / r_off は実数で、|λ| ごと動く
        moved = np.abs(kernel.natural_rates(G_probe, Rex_probe))
        same = np.array([np.min(np.abs(moved - abs(lam))) <= SAME_RATE_TOL * abs(lam) for lam in rates])
        found.append(rates[same])
        where.append(np.full(int(same.sum()), code))
    if not found:
        return np.zeros(0, dtype=complex), np.zeros(0, dtype=np.int64)
    return np.concatenate(found), np.concatenate(where)


INTERRUPT_FACTOR = 2.0    # インダクタ電流の遮断とみなす、L·|i| / h と回路の電圧の目安（電源とコンデンサ）の比


def _forced_through_off(system: System, code: int) -> list[int]:
    """回路状態 code で、電流の行き場がオフのスイッチ・ダイオードしか無いインダクタ（素子の並びの番号）。

    インダクタの両端が、そのインダクタ自身とオフの素子と電流源を除いた素子でつながっていなければ、
    電流はオフの素子（r_off）を通るしかない。
    """
    netlist = system.netlist
    on = {cid for j, cid in enumerate(system.devices) if (code >> j) & 1}
    forced = []
    for k, ind in enumerate(netlist.components):
        if ind.type != "L":
            continue
        paths = [
            c.nodes for c in netlist.components
            if c is not ind and c.type != "I" and not (c.type in ("S", "D") and c.id not in on)
        ]
        root = _node_groups(paths)
        if root(ind.nodes[0]) != root(ind.nodes[1]):
            forced.append(k)
    return forced


def _node_groups(edges: list[tuple[str, str]]):
    """節点の組（枝）から、つながった節点を同じ代表に写す関数を作る。"""
    parent: dict[str, str] = {}

    def root(n: str) -> str:
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in edges:
        parent[root(a)] = root(b)
    return root


def _interruption_notes(
    system: System, out: np.ndarray, before: np.ndarray, after: np.ndarray, u: np.ndarray, h: float,
    notes: list[Message], tick=None,
) -> None:
    """インダクタ電流が、還流経路の無いまま切り替えで遮断された点があれば注意に書く。

    before[k], after[k]: ステップ k の直前と、ステップ k の回路状態。out[k] は切り替わる前の値。
    遮断されると、電流はオフ抵抗を通って1刻みで消え、オフの素子の両端に L·|Δi| / h の大きさの
    （刻みで決まる、意味の無い）電圧が1点記録される。ダイオードが電流 0 付近で切れる通常の切り替えでは、
    L·|i| / h は回路の電圧の程度に収まる。そこで L·|i| / h が電源とコンデンサの電圧の最大の
    INTERRUPT_FACTOR 倍を超え、かつ還流経路がオフの素子しか無いときだけ知らせる。
    長い配列の計算は SETUP_BLOCK 行ずつ行い、そのたびに tick() を呼ぶ（中断の求めを見る）。
    """
    netlist = system.netlist
    steps = np.concatenate([np.nonzero(before[b0:b1] != after[b0:b1])[0] + b0
                            for b0, b1 in _blocks(len(after), tick)] or [np.zeros(0, dtype=np.int64)])
    if not steps.size:
        return
    idx = {cid: i for i, cid in enumerate(system.unknowns)}

    def volt(node: str, rows=slice(None)) -> np.ndarray | float:
        return 0.0 if node == netlist.ground else out[rows, idx[f"v({node})"]]

    v_ref = 0.0
    for b0, b1 in _blocks(out.shape[0], tick):
        rows = slice(b0, b1)
        for j, cid in enumerate(system.sources):
            if netlist.component(cid).type == "V" and u[rows].size:
                v_ref = max(v_ref, float(np.abs(u[rows, j]).max()))
        for c in netlist.components:
            if c.type == "C":
                v_ref = max(v_ref, float(np.max(np.abs(volt(c.nodes[0], rows) - volt(c.nodes[1], rows)))))
    events = []
    for code in np.unique(after[steps]):
        forced = _forced_through_off(system, int(code))
        if not forced:
            continue
        at = steps[after[steps] == code]
        for k in forced:
            ind = netlist.components[k]
            current = out[at, idx[f"i({ind.id})"]]
            hit = ind.value * np.abs(current) / h > INTERRUPT_FACTOR * v_ref
            events += [(int(step), ind, float(i)) for step, i in zip(at[hit], current[hit], strict=True)]
    if not events:
        return
    events.sort(key=lambda e: e[0])
    step, ind, current = events[0]
    off = [cid for j, cid in enumerate(system.devices) if not (int(after[step]) >> j) & 1]
    k_next = min(step + 1, out.shape[0] - 1)
    peak = max(
        abs(float(np.asarray(volt(c.nodes[0]) - volt(c.nodes[1]))[k_next]))
        for c in netlist.components if c.id in off
    )
    more = Message("FRAG_MORE_TIMES", n=len(events) - 1) if len(events) > 1 else None
    notes.append(Message(
        "INDUCTOR_CURRENT_INTERRUPTED", t=Value(step * h, "s"), id=ind.id, current=Value(current, "A"),
        off=Names(off), more=more, peak=Value(peak, "V"),
    ))


def _initial_value_notes(system: System, x0: np.ndarray, notes: list[Message]) -> None:
    """与えた初期値（ic）と、実際に始めた値が食い違っていれば注意に書く。"""
    netlist = system.netlist
    floor = 1e-9 * max(float(np.abs(x0).max(initial=0.0)), max(abs(c.ic) for c in netlist.components))

    def at(node: str) -> float:
        return 0.0 if node == netlist.ground else float(x0[system.unknowns.index(f"v({node})")])

    for c in netlist.components:
        if c.type == "C":
            actual, unit = at(c.nodes[0]) - at(c.nodes[1]), "V"
        elif c.type == "L":
            actual, unit = float(x0[system.unknowns.index(f"i({c.id})")]), "A"
        else:
            continue
        if abs(actual - c.ic) > 1e-6 * max(abs(actual), abs(c.ic)) + floor:
            notes.append(Message(
                "INITIAL_VALUE_ADJUSTED", id=c.id, ic=Value(c.ic, unit), actual=Value(actual, unit),
            ))


def _inductor_cutset_hints(netlist: Netlist) -> list[Message]:
    """インダクタ（と電流源）だけで回路の残りとつながる部分（2節点以上）を探して、直し方を添える。

    抵抗・コンデンサ・電圧源・スイッチ・ダイオードでつながった節点を1つの部分とみなす。
    基準節点を含まない部分が、インダクタと電流源だけで外とつながっていると、そこへ流れ込む
    インダクタ電流の和が 0 に縛られ、部分全体の電位が式から決まらない（三相の整流器や負荷の
    中性点・直流側を接地しない場合など）。1節点だけの場合は、呼び出し側の文面が扱う。
    """
    nodes = [netlist.ground] + netlist.nodes()
    parent = {n: n for n in nodes}

    def root(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for c in netlist.components:
        if c.type not in ("L", "I"):
            parent[root(c.nodes[0])] = root(c.nodes[1])
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(root(n), []).append(n)
    hints = []
    for members in groups.values():
        if netlist.ground in members or len(members) < 2:
            continue
        links = [c for c in netlist.components if (c.nodes[0] in members) != (c.nodes[1] in members)]
        inductors = [c.id for c in links if c.type == "L"]
        if not inductors:
            continue
        hints.append(Message(
            "HINT_INDUCTOR_CUTSET", nodes=Names(members), links=Names(c.id for c in links),
            inductors=Names(inductors),
        ))
    return hints


def _explain_unsolvable(system: System, exc: CircuitError, state=None) -> CircuitError:
    """解けない回路のエラーに、手がかり（決まらない変数、インダクタだけの節点）を添える。

    番号と差し込む値は元のエラーのもの（CIRCUIT_UNSOLVABLE など）に、手がかり hints と、スイッチを含む
    回路では、そのときのスイッチング状態 state を足したもの。
    """
    netlist = system.netlist
    hints: list[Message] = []
    names = [system.unknowns[i] for i in getattr(exc, "undetermined", [])]
    if names:
        hints.append(Message("HINT_UNDETERMINED", names=Names(names)))
    for node in netlist.nodes():
        attached = [c for c in netlist.components if node in c.nodes]
        if all(c.type in ("L", "I") for c in attached) and any(c.type == "L" for c in attached):
            ids = Names(c.id for c in attached)
            with_source = any(c.type == "I" for c in attached)
            code = "HINT_SOURCE_INDUCTOR_SERIES" if with_source else "HINT_INDUCTORS_IN_SERIES"
            hints.append(Message(code, node=node, ids=ids))
    cutsets = _inductor_cutset_hints(netlist)
    hints += cutsets
    cause = exc.message
    if state is None:
        message = Message("CIRCUIT_EXPLAINED", cause=cause, hints=hints)
        params = {**exc.params, "hints": hints}
    else:
        r_off = None if cutsets else Message("FRAG_R_OFF_FLOATING")
        message = Message("CIRCUIT_EXPLAINED_IN_STATE", state=state, cause=cause, hints=hints, r_off=r_off)
        params = {**exc.params, "hints": hints, "state": state}
    error = CircuitError(message)
    error._set_message(message, exc.code, params)
    return error


class _Monitor:
    """区切りごとの確かめ（核の外）: 発散、中断の求め、進み具合の知らせ。

    計算の準備（電源の配列など。配列を SETUP_BLOCK 行ずつ埋める）と後処理（注意と信号）のあいだは tick() を、
    核は next_check までの区間を1回の呼び出しで進め、戻ったら after(k) を呼ぶ
    （k は計算の済んだ最後の格子点）。
    区切りのステップ数は、1つの区切りの計算時間が CHUNK_SECONDS になるように合わせ直す
    （区切りの位置は結果に効かないので、時間で決めても結果は同じ）。
    progress と cancel が無くても、発散の確かめのために区切る。
    進み具合は、準備を [0, SETUP_SHARE)、時間ステップを [SETUP_SHARE, 1 − POST_SHARE]（計算したステップ数の
    割合）、後処理を (1 − POST_SHARE, 1) に割り当て、最後に 1 にする。
    """

    def __init__(self, n_steps: int, h: float, progress: Callable[[float], object] | None,
                 cancel: Callable[[], bool] | None):
        self.n_steps, self.h = n_steps, h
        self.progress, self.cancel = progress, cancel
        self.out: np.ndarray | None = None
        self.checked = 0                        # out[: checked + 1] は確かめ済み
        self.last_k = 0                         # 前に区切った点
        self.chunk = CHUNK_STEPS_FIRST
        self.next_check = min(n_steps, self.chunk)
        self.started = self.last_report = time.perf_counter()
        self.last_check = self.started
        self.last_fraction = 0.0
        self.phase, self.ticks = "setup", 0
        self._stop_if_cancelled(0)
        if progress is not None:
            progress(0.0)

    def attach(self, out: np.ndarray, unknowns: list[str], limits: np.ndarray,
               scales: dict[str, tuple[float, str]]) -> None:
        """時間ステップを始める（発散を確かめる配列と大きさを受け取る）。

        目安が 0 で大きさを決められない変数（limits が inf）は、倍精度の最大の有限値までを通す
        （有限かどうかだけを見ることになる。inf は「inf 以下」に当たるので、有限の値に直しておく）。
        """
        self.out, self.unknowns, self.scales = out, unknowns, scales
        self.limits = np.minimum(limits, np.finfo(float).max)
        self.bound = float(self.limits.min())       # いちばん小さい大きさ（速い確かめに使う）
        self.phase = "run"
        self.last_check = time.perf_counter()

    def tick(self) -> None:
        """準備か後処理の1区切りが済んだ（中断の求めを見て、進み具合を少し進める）。"""
        self._stop_if_cancelled(self.last_k)
        self.ticks += 1
        if self.phase == "setup":
            self._report(SETUP_SHARE * self.ticks / (self.ticks + 20))
        else:
            self._report(1.0 - POST_SHARE + POST_SHARE * self.ticks / (self.ticks + 5))

    def finish_run(self) -> None:
        """時間ステップが済んだ（ここから後処理）。"""
        self.phase, self.ticks = "post", 0

    def after(self, k: int) -> None:
        """格子点 k まで計算が済んだ。区切りの点に来ていれば、確かめて次の区切りを決める。"""
        if k < self.next_check:
            return
        self.check_values(k)
        now = time.perf_counter()
        spent, steps = now - self.last_check, max(1, k - self.last_k)
        self.last_check, self.last_k = now, k
        self.chunk = int(min(CHUNK_STEPS_MAX, max(CHUNK_STEPS_MIN, steps * CHUNK_SECONDS / max(spent, 1e-6))))
        self.next_check = min(self.n_steps, k + self.chunk)
        self._stop_if_cancelled(k)
        self._report(SETUP_SHARE + (1.0 - SETUP_SHARE - POST_SHARE) * k / self.n_steps, now)

    def _report(self, fraction: float, now: float | None = None) -> None:
        if self.progress is None:
            return
        now = time.perf_counter() if now is None else now
        if fraction > self.last_fraction and now - self.last_report >= PROGRESS_SECONDS:
            self.last_report, self.last_fraction = now, fraction
            self.progress(fraction)

    def check_values(self, k: int) -> None:
        """out[checked + 1 : k + 1] に、有限でない値や発散とみなす大きさの値が無いかを確かめる。"""
        if k <= self.checked:
            return
        block = self.out[self.checked + 1: k + 1]
        # まず、全部の値の最小と最大を、いちばん小さい大きさと比べる（変数ごとに見るより 2〜15 倍速い。
        # 発散していなければ、ここで済む）。NaN があると最小も最大も NaN で、比較が偽になる
        if not (block.min() >= -self.bound and block.max() <= self.bound):
            big = np.abs(block).max(axis=0)
            if not np.all(big <= self.limits):
                self._raise_diverged(block, self.checked + 1)
        self.checked = k

    def recheck(self, k: int) -> None:
        """格子点 k の値を書き直したので、確かめ直す。"""
        self.checked = min(self.checked, k - 1)
        self.check_values(k)

    def _raise_diverged(self, block: np.ndarray, k0: int) -> None:
        bad = ~(np.abs(block) <= self.limits)
        row = int(np.argmax(bad.any(axis=1)))
        col = int(np.argmax(bad[row]))
        k = k0 + row
        name, value = self.unknowns[col], float(block[row, col])
        unit = "V" if name.startswith("v(") else "A"
        t = Value(k * self.h, "s")
        if not math.isfinite(value):
            raise DivergenceError("RUN_DIVERGED_NOT_FINITE", t=t, signal=name, k=k)
        scale, word = self.scales[unit]
        raise DivergenceError(
            "RUN_DIVERGED", t=t, signal=name, value=Value(value, unit),
            limit=Value(float(self.limits[col]), unit),
            scale=Value(scale, unit), factor=Value(DIVERGENCE_FACTOR), kind=Message(word), k=k,
        )

    def _stop_if_cancelled(self, k: int) -> None:
        if self.cancel is not None and self.cancel():
            raise SimulationCancelled("RUN_CANCELLED", t=Value(k * self.h, "s"), k=k, n=self.n_steps)

    def finish(self) -> None:
        self._stop_if_cancelled(self.n_steps)
        if self.progress is not None:
            self.progress(1.0)


def _blocks(n: int, tick: Callable[[], None] | None, start: int = 0):
    """start〜n の行を SETUP_BLOCK ずつに分けた（始め、終わり）。1つ済むたびに tick() を呼ぶ。"""
    for b0 in range(start, n, SETUP_BLOCK):
        yield b0, min(n, b0 + SETUP_BLOCK)
        if tick is not None:
            tick()


def _waveform_bound(w) -> float:
    """電源の値の大きさの目安（指令で値が決まる電源は、指令が上下限で止まらなければ gain × 1 とみなす）。"""
    if isinstance(w, DC):
        return abs(w.value)
    if isinstance(w, Step):
        return max(abs(w.initial), abs(w.final))
    if isinstance(w, Sine):
        return abs(w.offset) + abs(w.amplitude)
    if isinstance(w, Command):
        bounds = [abs(b) for b in (w.lower, w.upper) if b is not None]
        return abs(w.gain) * (max(bounds) if len(bounds) == 2 else max([1.0] + bounds))
    return 0.0  # pragma: no cover


def _divergence_limits(system: System, t_end: float) -> tuple[np.ndarray, dict[str, tuple[float, str]]]:
    """発散とみなす大きさ（未知数ごと。電圧は V、電流は A）と、その元にした回路の目安。

    目安は、電源と初期値の大きさ（S_V、S_I）から、素子の値で換算して広げたもの:
      電流の目安 I = max(S_I, S_V / Z_min, S_V·t_end / L_min)  （抵抗やオン抵抗を流れる電流、LC の共振電流、
                                                                 インダクタが t_end の間にためる電流）
      電圧の目安 V = max(S_V, I·Z_max, I·t_end / C_min)        （オフ抵抗や大きな抵抗を電流 I が流れたときの
                                                                 電圧（インダクタ電流の遮断を含む）、
                                                                 コンデンサが t_end の間にためる電圧）
      Z_min、Z_max は、抵抗・スイッチとダイオードのオン抵抗とオフ抵抗・√(L/C) の最小と最大
    受動素子と、大きさに限りのある電源だけの回路では、どの変数もこの目安を大きく超えない（共振で目安の
    Q 倍まで振れるが、上限のステップ数 2000 万の中で育つ振れは目安の約 3e7 倍まで）。その 3 万倍以上の余裕を
    取って、目安の DIVERGENCE_FACTOR（1e12）倍を「決めた大きさ」とする。超えるのは、制御器を通した正帰還
    （閉ループが不安定）などで指数的に育つときだけ。目安が 0（電源も初期値も 0）の変数は、
    有限かどうかだけを見る。
    """
    netlist = system.netlist
    s_v = s_i = 0.0
    r_values, inductances, capacitances = [], [], []
    for c in netlist.components:
        if c.type == "V":
            s_v = max(s_v, _waveform_bound(c.waveform))
        elif c.type == "I":
            s_i = max(s_i, _waveform_bound(c.waveform))
        elif c.type == "C":
            s_v = max(s_v, abs(c.ic))
            capacitances.append(c.value)
        elif c.type == "L":
            s_i = max(s_i, abs(c.ic))
            inductances.append(c.value)
        elif c.type == "R":
            r_values.append(c.value)
        else:
            r_values += [c.r_on, c.r_off]
    z_values = list(r_values)
    if inductances and capacitances:
        z_values += [math.sqrt(min(inductances) / max(capacitances)),
                     math.sqrt(max(inductances) / min(capacitances))]
    i = max([s_i] + ([s_v / min(z_values)] if z_values else [])
            + ([s_v * t_end / min(inductances)] if inductances else []))
    v = max([s_v] + ([i * max(z_values)] if z_values else [])
            + ([i * t_end / min(capacitances)] if capacitances else []))
    limit_v = DIVERGENCE_FACTOR * v if v > 0 else math.inf
    limit_i = DIVERGENCE_FACTOR * i if i > 0 else math.inf
    limits = np.array([limit_v if name.startswith("v(") else limit_i for name in system.unknowns])
    return limits, {"V": (v, "WORD_VOLTAGE"), "A": (i, "WORD_CURRENT")}


def _check_callbacks(progress, cancel) -> None:
    if progress is not None and not callable(progress):
        raise CodedTypeError("ARG_PROGRESS", value=repr(progress))
    if cancel is not None and not callable(cancel):
        raise CodedTypeError("ARG_CANCEL", value=repr(cancel))


def simulate(
    netlist: Netlist,
    t_end: float,
    h: float | None = None,
    *,
    gates: Sequence[Pwm] = (),
    controllers: Sequence[Controller] = (),
    steps_per_period: int | None = None,
    damping_steps: int = DEFAULT_DAMPING_STEPS,
    max_steps: int | None = None,
    progress: Callable[[float], object] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Result:
    """ネットリストを固定刻みで計算する。

    基本は台形則。計算の先頭と不連続点（電源のステップ、スイッチやダイオードの切り替え）の
    直後だけ、刻みより速い成分を消す減衰ステップで進める。
    刻みが回路の速い成分や電源の周期を解像していないときは、結果の notes に注意が入る。

      h             : 時間刻み。スイッチの無い回路では必須。
      damping_steps : 計算の先頭と不連続点の直後に入れる減衰ステップの数（既定 2。0 なら入れない）。
                      スイッチの無い回路では、刻みより速い成分があるときだけ入れる。
                      スイッチを含む回路では、状態が変わったステップを後退オイラーで進め、その後に入れる。
      max_steps     : ステップ数の上限（既定 2000万）。超える計算は、配列を確保する前に断る。
      progress      : 進み具合（0〜1）を受け取る関数。計算の初めに 0、区切りごとに（PROGRESS_SECONDS
                      より短い間隔では呼ばない）単調に増える値、最後に 1 で呼ぶ。呼ぶのは計算と同じスレッド。
      cancel        : 引数なしで、中断するなら真を返す関数（threading.Event の is_set など）。
                      区切りごとに呼び、真なら SimulationCancelled（番号 RUN_CANCELLED）で終わる。
                      途中の結果は返さない。

    計算の途中で回路の変数が有限でなくなるか、発散とみなす大きさ（_divergence_limits）を超えたら、
    そこで止めて DivergenceError（番号 RUN_DIVERGED、RUN_DIVERGED_NOT_FINITE）を出す。

    スイッチ（S）やダイオード（D）を含む場合:
      gates            : ゲート信号（PWM）。S の gate から名前で参照する。
      controllers      : 制御器。制御周期ごとに測定して演算し、指令は次の制御周期の先頭から使う。
      h                : 省略すると、最も速いキャリアの周期を steps_per_period で割った値。
      steps_per_period : キャリア1周期の分割数（既定 200）。h と同時には指定しない。
    """
    if not (t_end > 0 and math.isfinite(t_end)) or (h is not None and not (h > 0 and math.isfinite(h))):
        raise CodedError("ARG_T_END_H")
    if damping_steps < 0:
        raise CodedError("ARG_DAMPING_STEPS")
    _check_callbacks(progress, cancel)

    switching = any(c.type in ("S", "D") for c in netlist.components)
    if switching or gates or controllers:
        return _simulate_switched(
            netlist, t_end, h, gates, controllers, steps_per_period, damping_steps, max_steps,
            progress, cancel,
        )

    commanded = [c.id for c in netlist.components if isinstance(c.waveform, Command)]
    if commanded:
        raise CodedError("ARG_COMMAND_NEEDS_CONTROLLER", ids=Names(commanded))
    if h is None:
        raise CodedError("ARG_H_REQUIRED")
    if steps_per_period is not None:
        raise CodedError("ARG_STEPS_PER_PERIOD_WITHOUT_GATES")
    return _simulate_linear(netlist, t_end, h, damping_steps, max_steps, progress, cancel)


def _simulate_linear(
    netlist: Netlist, t_end: float, h: float, damping_steps: int, max_steps: int | None,
    progress=None, cancel=None,
) -> Result:
    """スイッチの無い回路。"""
    notes: list[Message] = []
    n_steps = _count_steps(t_end, h, max_steps, notes)

    system = build_system(netlist)
    _check_result_size(n_steps, len(system.unknowns), max_steps, len(system.sources))
    monitor = _Monitor(n_steps, h, progress, cancel)
    u_start, u_end, event, u_last = _sample_sources(system, n_steps, h, notes, monitor.tick)

    try:
        ops = kernel.prepare(system.E, system.G, system.B, h)
    except CircuitError as exc:
        raise _explain_unsolvable(system, exc) from exc
    x0 = ops.Rex @ system.ex0 + ops.Ru @ u_start[0]
    _initial_value_notes(system, x0, notes)
    rates = kernel.natural_rates(system.G, ops.Rex)
    _resolution_notes(
        rates, h, notes, damped=damping_steps > 0, n_steps=n_steps,
        limit=MAX_STEPS if max_steps is None else max_steps,
        who=lambda lam: _mode_elements(system, system.G, ops.Rex, lam),
    )
    # 減衰ステップは、刻みが最も速い時定数の半分を超えるときだけ入れる。刻みが回路を解像していれば
    # 台形則だけで進め、2次精度をそのまま保つ。
    n_damp = damping_steps if _has_unresolved_rate(rates, h) else 0

    out = np.empty((n_steps + 1, len(system.unknowns)))
    out[0] = x0
    monitor.attach(out, system.unknowns, *_divergence_limits(system, t_end))
    # 区切りごとに核を呼ぶ（区切りの初めの値は、前の区切りの最後の値。減衰ステップの残りも引き継ぐ）
    k, damp = 0, -1
    while k < n_steps:
        k_next = monitor.next_check
        damp = kernel.run(
            ops.P, ops.Q, ops.Rx, ops.Ru, out[k], u_start[k:k_next], u_end[k:k_next], event[k:k_next],
            out[k:k_next + 1], ops.Pd, ops.Qd_start, ops.Qd_end, n_damp, damp,
        )
        k = k_next
        monitor.after(k)
    if u_last is not None:
        # 最後の格子点でちょうど電源がステップする場合も、その点にはステップした後の値を入れる
        out[n_steps] = ops.Rx @ out[n_steps] + ops.Ru @ u_last
        monitor.recheck(n_steps)
    monitor.finish_run()

    result = Result(
        t=_time_grid(n_steps, h, monitor.tick),
        x=out,
        unknowns=system.unknowns,
        netlist=netlist,
        h=h,
        notes=notes,
        signal_info=describe_signals(netlist, system.unknowns),
    )
    monitor.finish()
    return result


# ---------------------------------------------------------------- スイッチング

def _whole_steps(period: float, h: float, gate: str | None = None) -> int:
    """周期が時間刻みの何ステップ分か。整数倍でなければエラー。

    gate はゲート信号の名前（無ければ制御周期）。"""
    ratio = period / h
    n = int(round(ratio))
    if n < 1 or abs(ratio - n) > _GRID_TOL * max(n, 1):
        if gate is None:
            raise CodedError("CONTROL_PERIOD_NOT_MULTIPLE", period=Value(period, "s"), h=Value(h, "s"))
        raise CodedError("GATE_PERIOD_NOT_MULTIPLE", name=gate, period=Value(period, "s"), h=Value(h, "s"))
    return n


class _StateTable:
    """回路状態ごとの行列。出会った状態だけ一度計算して、核に渡せる配列に並べて持つ。"""

    _ARRAYS = ("code", "P", "Q", "Pb", "Qb", "Pd", "Qd_start", "Qd_end", "Rx", "Ru")

    def __init__(self, system: System, h: float):
        self.system = system
        self.h = h
        self.count = 0
        self.index: dict[int, int] = {}      # 状態の符号 → 配列の中の位置
        self.rex: list[np.ndarray] = []      # 初期値の計算にだけ使う（核には渡さない）
        n, m = system.B.shape
        capacity = 4
        self.code = np.full(capacity, -1, dtype=np.int64)
        self.P = np.zeros((capacity, n, n))
        self.Q = np.zeros((capacity, n, m))
        self.Pb = np.zeros((capacity, n, n))
        self.Qb = np.zeros((capacity, n, m))
        self.Pd = np.zeros((capacity, n, n))
        self.Qd_start = np.zeros((capacity, n, m))
        self.Qd_end = np.zeros((capacity, n, m))
        self.Rx = np.zeros((capacity, n, n))
        self.Ru = np.zeros((capacity, n, m))

    def _grow(self) -> None:
        for name in self._ARRAYS:
            old = getattr(self, name)
            new = np.full((2 * old.shape[0],) + old.shape[1:], -1 if name == "code" else 0, dtype=old.dtype)
            new[: old.shape[0]] = old
            setattr(self, name, new)

    def slot(self, code: int) -> int:
        """状態 code の行列の位置。まだ無ければ計算して足す。"""
        if code in self.index:
            return self.index[code]
        try:
            ops = kernel.prepare(
                self.system.E, state_conductance(self.system, code), self.system.B, self.h,
                backward_euler=True,
            )
        except CircuitError as exc:
            raise _explain_unsolvable(self.system, exc, describe_state(self.system, code)) from exc
        if self.count == self.code.shape[0]:
            self._grow()
        s = self.count
        self.code[s] = code
        self.P[s], self.Q[s] = ops.P, ops.Q
        self.Pb[s], self.Qb[s] = ops.Pb, ops.Qb
        self.Pd[s], self.Qd_start[s], self.Qd_end[s] = ops.Pd, ops.Qd_start, ops.Qd_end
        self.Rx[s], self.Ru[s] = ops.Rx, ops.Ru
        self.rex.append(ops.Rex)
        self.index[code] = s
        self.count += 1
        return s


def _time_grid(n_steps: int, h: float, tick) -> np.ndarray:
    """時刻の格子 h·k（k = 0〜n_steps）。区切りで作る（値はまとめて作ったときと同じ）。"""
    t = np.empty(n_steps + 1)
    for b0, b1 in _blocks(n_steps + 1, tick):
        t[b0:b1] = h * np.arange(b0, b1)
    return t


def _used_codes(table: _StateTable, code0: int, codes: np.ndarray, tick) -> np.ndarray:
    """計算で使ったスイッチング状態の符号（昇順。np.unique と同じ）。

    用意した状態が少なければ、状態ごとに「使ったか」を区切りで見る（並べ替えより速い）。"""
    if table.count > 16:
        return np.unique(np.concatenate([[code0], codes]))
    seen = {int(code0)}
    candidates = [int(c) for c in table.code[: table.count]]
    for b0, b1 in _blocks(len(codes), tick):
        block = codes[b0:b1]
        seen.update(c for c in candidates if c not in seen and bool(np.any(block == c)))
    return np.array(sorted(seen), dtype=np.int64)


def _flip_limit(n_diodes: int) -> int:
    """1ステップの中で、ダイオードの状態の反転を何回まで試すか。"""
    return max(20, 4 * n_diodes)


def _diode_voltage(system: System, x: np.ndarray, j: int) -> float:
    a, c = int(system.dev_pos[j]), int(system.dev_neg[j])
    return (x[a] if a >= 0 else 0.0) - (x[c] if c >= 0 else 0.0)


def _initial_state(
    system: System, table: _StateTable, sw_code: int, u0: np.ndarray, max_flips: int
) -> tuple[int, np.ndarray, bool]:
    """t = 0 の回路状態と、整合した初期値 x(0)、救済したか（下の説明）。

    初期条件（コンデンサ電圧、インダクタ電流）を保ったまま、ダイオードの状態を
    「オンなら順方向電流、オフなら逆電圧」になるまで反転して決める（判定は核と同じ）。
    落ち着かないときは、試した状態のうち違反が最も小さいものを、違反が丸め誤差の幅
    （_diode_band）以内なら採る（救済。stats の rescues_at_start に 1 が入る）。インダクタ電流がすべて 0 の
    整流器のように、電流がちょうど 0 のダイオードは、オンでもオフでも電圧が丸め誤差だけになるため。
    状態は最初のステップで核が決め直す。
    """
    n_sw, n_dev = system.n_switches, len(system.devices)
    n_nodes = len(system.netlist.nodes())
    code = sw_code
    flipped = [0] * n_dev
    last: dict[int, float] = {}
    best: tuple[float, int, np.ndarray] | None = None
    for flips in range(max_flips + 1):
        s = table.slot(code)
        x0 = table.rex[s] @ system.ex0 + table.Ru[s] @ u0
        wrong = []
        for j in range(n_sw, n_dev):
            vd = _diode_voltage(system, x0, j)
            if (vd < 0.0) if (code >> j) & 1 else (vd > DIODE_ON_VOLTAGE):
                wrong.append(j)
                last[j] = vd
        if not wrong:
            return code, x0, False
        violation = max(abs(last[j]) for j in wrong)
        if best is None or violation < best[0]:
            best = (violation, code, x0)
        for j in wrong if flips < SIMULTANEOUS_FLIPS else wrong[:1]:
            code ^= 1 << j
            flipped[j] += 1
    violation, best_code, best_x0 = best
    band = _diode_band(best_x0, n_nodes)
    if violation <= band:
        return best_code, best_x0, True
    raise CircuitError(
        "DIODES_NOT_SETTLED_AT_START", t=Value(0.0, "s"), flips=max_flips, state=describe_state(system, code),
        hints=_unsettled_hint(system, flipped, last, band),
    )


def _diode_band(x: np.ndarray, n_nodes: int) -> float:
    """t = 0 のダイオードの状態が落ち着かないときに、丸め誤差とみなす電圧の幅 [V]。

    幅は max(DIODE_ON_VOLTAGE, DIODE_BAND_REL × 節点電圧の最大の絶対値)。ダイオードの電圧は、
    大きさが節点電圧と同程度の2つの値の差なので、丸め誤差も節点電圧に比例する。インダクタ電流が
    すべて 0 の点では、オンでもオフでも電圧が丸め誤差（初期値の整合の計算で 1e-9 倍程度）だけになり、
    その符号で2つの状態を往復する。
    """
    scale = float(np.abs(x[:n_nodes]).max(initial=0.0))
    return max(DIODE_ON_VOLTAGE, DIODE_BAND_REL * scale)


def _unsettled_hint(
    system: System, flipped: list[int], last: dict[int, float], band: float | None = None
) -> list[Message]:
    """ダイオードの状態が確定しないときの、手がかりと直し方。"""
    parts = []
    most = max(flipped, default=0)
    if most > 0:
        detail = [
            Message("HINT_DIODE_VOLTAGE", id=system.devices[j], v=Value(last[j], "V"))
            for j, n in enumerate(flipped) if n == most and j in last
        ]
        parts.append(Message("HINT_DIODES_FLIPPING", diodes=detail))
    ratio = max(
        (float(system.dev_g_on[j] / system.dev_g_off[j])
         for j in range(system.n_switches, len(system.devices))),
        default=0.0,
    )
    band_part = Message("FRAG_BAND", band=Value(band, "V")) if band is not None else None
    parts.append(Message("HINT_DIODES_ROUNDING", band=band_part, ratio=Value(ratio)))
    return parts


def _simulate_switched(
    netlist: Netlist,
    t_end: float,
    h: float | None,
    gates: Sequence[Pwm],
    controllers: Sequence[Controller],
    steps_per_period: int | None,
    damping_steps: int,
    max_steps: int | None,
    progress=None,
    cancel=None,
) -> Result:
    notes: list[Message] = []

    # --- ゲート信号と時間刻み -------------------------------------------------
    # ゲート信号の出力。相補のゲートがあるPWMは2つ出す（反転の印つき）
    gate_names = [name for g in gates for name in g.outputs]
    gate_source = [(i, j == 1) for i, g in enumerate(gates) for j in range(len(g.outputs))]
    dup = sorted({n for n in gate_names if gate_names.count(n) > 1})
    if dup:
        raise CodedError("GATE_NAMES_DUPLICATE", names=Names(dup))
    for c in netlist.components:
        if c.type == "S" and c.gate not in gate_names:
            raise CodedError("GATE_NOT_GIVEN", id=c.id, gate=c.gate, names=Names(gate_names))
    if h is None:
        if not gates:
            raise CodedError("ARG_H_REQUIRED_WITHOUT_GATES")
        n_div = DEFAULT_STEPS_PER_PERIOD if steps_per_period is None else steps_per_period
        if n_div < 2:
            raise CodedError("ARG_STEPS_PER_PERIOD_MIN")
        h = 1.0 / (max(g.frequency for g in gates) * n_div)
    elif steps_per_period is not None:
        raise CodedError("ARG_H_AND_STEPS_PER_PERIOD")
    n_steps = _count_steps(t_end, h, max_steps, notes)
    gate_steps = [_whole_steps(1.0 / g.frequency, h, g.name) for g in gates]
    for g, n_div in zip(gates, gate_steps, strict=True):
        if n_div < 2:
            raise CodedError("GATE_PERIOD_ONE_STEP", name=g.name)

    # --- 回路 -----------------------------------------------------------------
    system = build_system(netlist)
    n = len(system.unknowns)
    n_sw, n_dev = system.n_switches, len(system.devices)
    # 各スイッチを動かすゲート: （PWMの番号、反転するか）
    switch_gate = [
        gate_source[gate_names.index(netlist.component(cid).gate)] for cid in system.devices[:n_sw]
    ]
    _check_result_size(
        n_steps, n, max_steps, len(system.sources), switched=True, n_gates=len(gate_names),
        n_commands=sum(len(c.outputs) for c in controllers if isinstance(c, Controller)), n_devices=n_dev,
    )
    monitor = _Monitor(n_steps, h, progress, cancel)
    u_start, u_end, source_event, u_last = _sample_sources(system, n_steps, h, notes, monitor.tick)
    u_mid = np.empty_like(u_start)
    # 不連続点の印（核に渡す）: event は電源が不連続に変わる点（時刻で決まる電源のステップと、指令で決まる
    # 電源の変わり目）。そのうち source_event（時刻で決まる電源のステップ）の点には変わった後の値を記録し、
    # 指令で決まる電源だけが変わる点には、変わる直前の値を記録する（D-048）
    event = np.empty(n_steps, dtype=bool)
    for b0, b1 in _blocks(n_steps, monitor.tick):
        u_mid[b0:b1] = 0.5 * (u_start[b0:b1] + u_end[b0:b1])
        event[b0:b1] = source_event[b0:b1]
    command_event = np.zeros(n_steps + 1, dtype=bool)    # 指令で決まる電源の値が変わった点

    # --- 制御器と指令 ---------------------------------------------------------
    cmd_names: list[str] = []
    ctrl_steps: list[int] = []
    ctrl_inputs: list[list[int]] = []
    ctrl_outputs: list[list[int]] = []
    for c in controllers:
        if not isinstance(c, Controller):
            raise CodedTypeError("CONTROLLER_SHAPE", type=type(c).__name__)
        ctrl_steps.append(_whole_steps(float(c.period), h))
        missing = [name for name in c.inputs if name not in system.unknowns]
        if missing:
            raise CodedError("CONTROLLER_INPUT_UNKNOWN", names=Names(missing),
                             available=Names(system.unknowns))
        ctrl_inputs.append([system.unknowns.index(name) for name in c.inputs])
        outs = []
        for name in c.outputs:
            if name in cmd_names:
                raise CodedError("COMMAND_NAME_DUPLICATE", name=name)
            outs.append(len(cmd_names))
            cmd_names.append(name)
        ctrl_outputs.append(outs)
    gate_cmd = []     # 各ゲート信号のデューティに使う指令の番号（固定値なら -1）
    for g in gates:
        if isinstance(g.duty, str):
            if g.duty not in cmd_names:
                raise CodedError("GATE_DUTY_COMMAND_UNKNOWN", name=g.name, command=g.duty,
                                 commands=Names(cmd_names))
            gate_cmd.append(cmd_names.index(g.duty))
        else:
            gate_cmd.append(-1)
    # 指令で決まる電源: （電源の列、指令の番号、波形）
    commanded = []
    for j, cid in enumerate(system.sources):
        w = netlist.component(cid).waveform
        if isinstance(w, Command):
            if w.command not in cmd_names:
                raise CodedError("SOURCE_COMMAND_UNKNOWN", id=cid, command=w.command,
                                 commands=Names(cmd_names))
            commanded.append((j, cmd_names.index(w.command), w))

    def commands(c: Controller, values, t: float, n_out: int) -> np.ndarray:
        """制御器が返した指令を配列にして確かめる。"""
        y = np.atleast_1d(np.asarray(values, dtype=float))
        if y.shape != (n_out,):
            raise CodedError("CONTROLLER_OUTPUT_COUNT", outputs=Names(c.outputs), values=repr(values))
        if not np.isfinite(y).all():
            if monitor.out is not None:
                # 回路の変数が先に発散していれば、そちらを知らせる（測った値が有限でないと、指令もそうなる）
                monitor.check_values(int(ctl[kernel.CTL_K]))
            # signal は、有限でない最初の指令（結果の名前の形。画面の N-RUN-DIVERGED の {signal} に使う）
            first_bad = c.outputs[int(np.argmax(~np.isfinite(y)))]
            raise DivergenceError("CONTROLLER_COMMAND_NOT_FINITE", t=Value(t, "s"),
                                  signal=f"cmd({first_bad})", outputs=Names(c.outputs), values=str(y))
        return y

    active = np.zeros(len(cmd_names))      # いま使っている指令
    pending = np.zeros(len(cmd_names))     # 次の制御周期の先頭から使う指令
    for c, outs in zip(controllers, ctrl_outputs, strict=True):
        active[outs] = commands(c, c.reset(), 0.0, len(outs))
    pending[:] = active

    # --- 記録 -----------------------------------------------------------------
    out = np.empty((n_steps + 1, n))
    code_out = np.zeros(n_steps + 1, dtype=np.int64)
    sw_code = np.zeros(n_steps + 1, dtype=np.int64)
    gate_out = np.zeros((n_steps + 1, len(gates)), dtype=bool)
    cmd_out = np.zeros((n_steps + 1, len(cmd_names)))

    latched = np.zeros(len(gates), dtype=np.int64)     # いまのキャリアの周期のオンのステップ数
    carried = np.zeros(len(gates))                      # 次の周期に持ち越す丸めの余り

    def fill(k0: int, k1: int) -> None:
        """区間 [k0, k1) のゲート信号・指令・スイッチのビットを、いまの指令で埋める。

        デューティはキャリアの周期の先頭で取り込む。区間の途中から始まる周期は、取り込み済みの値を使う。
        周期ごとのオンのステップ数は区間でまとめて決め、ステップごとの配列は SETUP_BLOCK 行ずつ埋める
        （制御器の無い回路では区間が計算の全体になるので、そのあいだも中断の求めを見る）。
        """
        tick = monitor.tick if k1 - k0 > SETUP_BLOCK else None
        per_gate = []
        for g, (pwm, n_div) in enumerate(zip(gates, gate_steps, strict=True)):
            duty = pwm.duty if gate_cmd[g] < 0 else float(active[gate_cmd[g]])
            first = -(-k0 // n_div)                     # この区間の中で始まる最初の周期の番号
            n_periods = max(0, -(-k1 // n_div) - first)
            counts, carried[g] = pwm.on_counts(duty, n_div, n_periods, float(carried[g]))
            per_gate.append((pwm, n_div, first, n_periods, counts, int(latched[g])))
            if n_periods:
                latched[g] = counts[-1]
        for b0, b1 in (_blocks(k1, tick, k0) if tick is not None else ((k0, k1),)):
            k = np.arange(b0, b1)
            cmd_out[b0:b1] = active
            for g, (pwm, n_div, first, n_periods, counts, before) in enumerate(per_gate):
                index = k // n_div - first              # 負なら、区間の前に始まった周期
                n_on = np.where(index >= 0, counts[np.maximum(index, 0)] if n_periods else 0, before)
                gate_out[b0:b1, g] = pwm.pattern(k, n_div, n_on)
            code = np.zeros(b1 - b0, dtype=np.int64)
            for j, (g, inverted) in enumerate(switch_gate):
                on = ~gate_out[b0:b1, g] if inverted else gate_out[b0:b1, g]
                code |= on.astype(np.int64) << j
            sw_code[b0:b1] = code
        # 指令で決まる電源は、区間のあいだ一定。前の区間と値が変わったら、その点を電源の不連続点にする
        # （時刻で決まる電源のステップと重ならなければ、その点の記録は変わる直前の値のまま）
        for j, c, w in commanded:
            value = w.value(float(active[c]))
            if k0 > 0 and value != u_end[k0 - 1, j]:
                event[k0] = True
                command_event[k0] = True
            u_start[k0:k1, j] = value
            u_end[k0:k1, j] = value
            u_mid[k0:k1, j] = value

    # --- 初期値 ---------------------------------------------------------------
    def next_stop(k: int) -> int:
        """次に核から戻るステップ番号（制御器を呼ぶ点、または計算の終わり）。"""
        return min([n_steps] + [(k // m + 1) * m for m in ctrl_steps])

    table = _StateTable(system, h)
    max_flips = _flip_limit(n_dev - n_sw)
    k0, k1 = 0, next_stop(0)
    fill(k0, k1)
    code0, x, rescued_at_start = _initial_state(system, table, int(sw_code[0]), u_start[0], max_flips)
    out[0] = x
    _initial_value_notes(system, x, notes)
    ctl = np.zeros(kernel.CTL_SIZE, dtype=np.int64)
    ctl[kernel.CTL_STATE] = code0
    ctl[kernel.CTL_DAMP] = damping_steps      # 計算の先頭も、減衰ステップから始める
    d_pos = np.ascontiguousarray(system.dev_pos[n_sw:])
    d_neg = np.ascontiguousarray(system.dev_neg[n_sw:])
    monitor.attach(out, system.unknowns, *_divergence_limits(system, t_end))

    # --- 制御周期ごとに区切って核を呼ぶ（その中も、確かめの区切りで区切る） ----------------
    # k_done は計算の済んだ最後の格子点（核の ctl[CTL_K] と同じ値。制御周期ごとに ctl から読み直す手間を
    # 省くため、こちらで数える）。next_check は次の確かめの点（monitor.after() の後にだけ変わる）
    k_done, next_check = 0, monitor.next_check
    while True:
        while k_done < k1:
            k_stop = k1 if k1 < next_check else next_check
            status = kernel.run_switched(
                table.P, table.Q, table.Pb, table.Qb, table.Pd, table.Qd_start, table.Qd_end,
                table.Rx, table.Ru, table.code, table.count,
                d_pos, d_neg, n_sw,
                u_start, u_mid, u_end, event, source_event, sw_code,
                x, ctl, k_stop,
                damping_steps, SIMULTANEOUS_FLIPS, max_flips, DIODE_ON_VOLTAGE,
                out, code_out,
            )
            if status == kernel.DONE:
                k_done = k_stop            # 核は k_stop まで進んで戻る（k_done < k_stop なので）
                if k_done >= next_check:
                    monitor.after(k_done)
                    next_check = monitor.next_check
                continue
            code = int(ctl[kernel.CTL_CODE])
            if status == kernel.NEED_STATE:
                table.slot(code)       # 行列を用意して、同じステップからやり直す
                continue
            k = int(ctl[kernel.CTL_K])
            monitor.check_values(k)    # 値が発散していて確定しないなら、発散として知らせる
            raise CircuitError(
                "DIODES_NOT_SETTLED", t=Value(k * h, "s"), k=k, flips=max_flips,
                state=describe_state(system, code), hints=_unsettled_hint(system, [], {}),
            )
        # k0 の測定値で演算する。結果は次の制御周期の先頭から使う
        for c, m, ins, outs in zip(controllers, ctrl_steps, ctrl_inputs, ctrl_outputs, strict=True):
            if k0 % m == 0:
                pending[outs] = commands(c, c.step(k0 * h, out[k0, ins].copy()), k0 * h, len(outs))
        for m, outs in zip(ctrl_steps, ctrl_outputs, strict=True):
            if k1 % m == 0:
                active[outs] = pending[outs]
        if k1 >= n_steps:
            break
        k0, k1 = k1, next_stop(k1)
        fill(k0, k1)
    if u_last is not None:
        # 最後の格子点でちょうど電源がステップする場合も、その点にはステップした後の値を入れる
        s_last = table.slot(int(ctl[kernel.CTL_STATE]))
        out[n_steps] = table.Rx[s_last] @ out[n_steps] + table.Ru[s_last] @ u_last
        monitor.recheck(n_steps)
    monitor.finish_run()
    # 刻みが回路の速い成分を解像しているか（計算で使った回路状態ごと。スイッチの無い回路と同じ注意）
    used = _used_codes(table, code0, code_out[:n_steps], monitor.tick)
    rates, rate_state = _switched_rates(system, table, used)

    def who(lam: complex) -> list[str]:
        code = int(rate_state[int(np.argmin(np.abs(rates - lam)))])
        G_code, Rex_code = state_conductance(system, code), table.rex[table.index[code]]
        return _mode_elements(system, G_code, Rex_code, lam, code)

    _resolution_notes(
        rates, h, notes, damped=damping_steps > 0, n_steps=n_steps,
        limit=MAX_STEPS if max_steps is None else max_steps, who=who,
    )
    monitor.tick()
    before = np.empty(n_steps, dtype=np.int64)
    before[0] = code0
    for b0, b1 in _blocks(n_steps - 1, monitor.tick):
        before[b0 + 1: b1 + 1] = code_out[b0:b1]
    _interruption_notes(system, out, before, code_out[:n_steps], u_start, h, notes, monitor.tick)
    monitor.tick()
    # 最後の点から始まる区間は無いので、区間ごとの記録は最後の区間の値を繰り返す
    gate_out[n_steps] = gate_out[n_steps - 1]
    cmd_out[n_steps] = cmd_out[n_steps - 1]
    code_out[n_steps] = code_out[n_steps - 1]

    # --- 結果 -----------------------------------------------------------------
    signals: dict[str, np.ndarray] = {}
    for name in gate_names:
        signals[f"gate({name})"] = np.empty(n_steps + 1)
    for name in cmd_names:
        signals[f"cmd({name})"] = np.empty(n_steps + 1)
    for cid in system.devices:
        signals[f"on({cid})"] = np.empty(n_steps + 1)
    sample_state = np.empty(n_steps + 1, dtype=np.int64)
    sample_state[0] = code0
    for b0, b1 in _blocks(n_steps + 1, monitor.tick):
        for name, (g, inverted) in zip(gate_names, gate_source, strict=True):
            on = ~gate_out[b0:b1, g] if inverted else gate_out[b0:b1, g]
            signals[f"gate({name})"][b0:b1] = on
        for j, name in enumerate(cmd_names):
            signals[f"cmd({name})"][b0:b1] = cmd_out[b0:b1, j]
        for j, cid in enumerate(system.devices):
            signals[f"on({cid})"][b0:b1] = (code_out[b0:b1] >> j) & 1
        sample_state[max(b0, 1): b1] = code_out[max(b0, 1) - 1: b1 - 1]
    # 核は「回路状態が変わる、または電源が不連続」のステップを数える。指令の変化だけによるものを分けて数える
    only_command = 0
    for b0, b1 in _blocks(n_steps, monitor.tick):
        state_changed = code_out[b0:b1] != sample_state[b0:b1]
        only_command += int(np.count_nonzero(command_event[b0:b1] & ~source_event[b0:b1] & ~state_changed))
    t_grid = _time_grid(n_steps, h, monitor.tick)

    gate_switches: dict[str, list[str]] = {}
    for c in netlist.components:
        if c.type == "S":
            gate_switches.setdefault(c.gate, []).append(c.id)
    result = Result(
        t=t_grid,
        x=out,
        unknowns=system.unknowns,
        netlist=netlist,
        h=h,
        notes=notes,
        signals=signals,
        devices=list(system.devices),
        sample_state=sample_state,
        stats={
            "states": table.count,
            "state_changes": int(ctl[kernel.CTL_CHANGES]) - only_command,
            "command_changes": int(np.count_nonzero(command_event)),
            "retries": int(ctl[kernel.CTL_RETRIES]),
            "rescues_at_start": int(rescued_at_start),
        },
        signal_info=describe_signals(netlist, system.unknowns + list(signals), gate_switches),
    )
    monitor.finish()
    return result
