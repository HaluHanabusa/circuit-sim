"""注意とエラーの番号（コード）と差し込む値、文面の辞書（日本語と英語）。

エンジンとテンプレートは、注意とエラーを文面ではなく「番号と差し込む値」で返す（決まり8。画面の文言は
画面の辞書が持つ。`docs/design/screens-v0.1.md` 12章）。ここにあるのは、その番号ごとの日本語と英語の文面
（コマンドラインの表示と、画面の「詳細」に使う）と、画面のお知らせ（付録B の N-…）との対応。

  Message      : 注意1つ（str の一種。文字列としての中身は日本語の文面）。code、params、text(lang)、to_dict()
  CodedError   : 番号つきのエラー（ValueError の一種）。code、params、message、text(lang)、to_dict()
  Value        : 単位つきの数値（差し込む値）。文面の中では数値だけを書き、単位は文面が書く
  Names        : 名前の並び（差し込む値）。区切りは文面の書式で決める（{ids}、{ids:、}、{ids:repr}）
  SwitchState  : スイッチング状態（どの素子がオンか）。文面では言語ごとに「S1=オン」「S1=on」

文面の書式は str.format。差し込む値ごとに次の書き方ができる。
  {h:.6g}         Value の数値を書式つきで（単位は文面に書く: "{h:.6g} s"）。{ic.unit} で単位の記号
  {ids}           Names を ", " で区切る。{ids:、} は "、" で、{ids:repr} は Python の並びの形
  {hints:。}      Message の並びを "。" で区切る。{hints:+。} は、それぞれの前に "。
  " を置く（空なら何も書かない）
  {more}          Message 1つ、または None（何も書かない）
文面を変えるときは、日本語と英語の差し込む値の名前をそろえる（試験が確かめる）。用語は
`docs/design/glossary.md`（日本語は D-063、英語は glossary の英語の列と 11章）に従う。
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

LANGS = ("ja", "en")


# ------------------------------------------------------------------ 差し込む値の型


@dataclass(frozen=True)
class Value:
    """単位つきの数値。unit は SI の記号（V、A、s、Hz、ohm、F、H、GB など。無次元は ""）。

    text: 値の書き方を呼び出し側が決めたとき（違いが見える桁数など）。書式の指定が無い {x} で使う。
    """

    value: float
    unit: str = ""
    text: str | None = None

    def __format__(self, spec: str) -> str:
        if not spec and self.text is not None:
            return self.text
        return format(self.value, spec)

    def to_json(self) -> dict:
        out: dict[str, Any] = {"value": _json_number(self.value), "unit": self.unit}
        if self.text is not None:
            out["text"] = self.text
        return out


def _json_number(x: float) -> float | int | str:
    if isinstance(x, int) and not isinstance(x, bool):
        return x
    x = float(x)
    return x if math.isfinite(x) else repr(x)      # JSON は inf と nan を書けないので文字列にする


class Names(tuple):
    """名前の並び。{ids} は ", " で、{ids:、} は "、" で区切る。{ids:repr} は ['a', 'b'] の形。"""

    def __new__(cls, items: Iterable[Any] = ()):
        return super().__new__(cls, [str(x) for x in items])

    def __format__(self, spec: str) -> str:
        if spec == "repr":
            return repr(list(self))
        return (spec or ", ").join(self)


@dataclass(frozen=True)
class SwitchState:
    """スイッチング状態（回路状態）。devices は状態の符号のビットの順の素子ID。"""

    devices: tuple[str, ...]
    code: int

    def text(self, lang: str) -> str:
        if not self.devices:
            return MESSAGES["WORD_NO_SWITCHES"][_lang_index(lang)]
        on, off = MESSAGES["WORD_ON"][_lang_index(lang)], MESSAGES["WORD_OFF"][_lang_index(lang)]
        return ", ".join(f"{d}={on if (self.code >> j) & 1 else off}" for j, d in enumerate(self.devices))

    def to_json(self) -> dict:
        return {d: bool((self.code >> j) & 1) for j, d in enumerate(self.devices)}

    def __str__(self) -> str:
        return self.text("ja")


class _Joined:
    """Message の並び（言語に合わせて文面にしたもの）。{x:sep} で区切る。{x:+sep} はそれぞれの前に sep。"""

    def __init__(self, texts: list[str]):
        self.texts = texts

    def __format__(self, spec: str) -> str:
        if spec.startswith("+"):
            return "".join(spec[1:] + t for t in self.texts)
        return spec.join(self.texts)


class _Empty:
    def __format__(self, spec: str) -> str:
        return ""


# ------------------------------------------------------------------ 注意（Message）


def _lang_index(lang: str) -> int:
    try:
        return LANGS.index(lang)
    except ValueError:
        raise ValueError(f"lang は {LANGS} のどれか（{lang!r}）") from None


def _prepare(value: Any, lang: str) -> Any:
    if isinstance(value, Message):
        return value.text(lang)
    if isinstance(value, Coded):
        return value.message.text(lang)
    if isinstance(value, SwitchState):
        return value.text(lang)
    if value is None:
        return _Empty()
    if isinstance(value, list | tuple) and not isinstance(value, Names):
        return _Joined([_prepare(v, lang) if isinstance(v, Message | Coded | SwitchState) else str(v)
                        for v in value])
    return value


def render(code: str, params: Mapping[str, Any], lang: str = "ja") -> str:
    """番号と差し込む値から、その言語の文面を作る。"""
    try:
        template = MESSAGES[code][_lang_index(lang)]
    except KeyError:
        raise KeyError(f"辞書に無い番号 {code!r}") from None
    return template.format_map({k: _prepare(v, lang) for k, v in params.items()})


def _to_json(value: Any) -> Any:
    if isinstance(value, Message):
        return value.to_dict()
    if isinstance(value, Coded):
        return value.to_dict()
    if isinstance(value, Value | SwitchState):
        return value.to_json()
    if isinstance(value, Names):
        return list(value)
    if isinstance(value, list | tuple):
        return [_to_json(v) for v in value]
    if isinstance(value, float):
        return _json_number(value)
    return value


class Message(str):
    """番号つきの注意（またはエラーの文の部品）。文字列としての中身は日本語の文面。

    code   : 番号（MESSAGES の鍵）
    params : 差し込む値（Value、Names、SwitchState、Message、その並び、文字列、整数、None）
    """

    code: str
    params: dict[str, Any]

    def __new__(cls, code: str, /, **params: Any) -> Message:
        obj = super().__new__(cls, render(code, params, "ja"))
        obj.code = code
        obj.params = params
        return obj

    def text(self, lang: str = "ja") -> str:
        """その言語の文面。"""
        return str.__str__(self) if lang == "ja" else render(self.code, self.params, lang)

    def to_dict(self) -> dict:
        """画面とサーバーに渡す形（JSON にできる）: {"code": 番号, "params": {名前: 値}}。"""
        return {"code": self.code, "params": {k: _to_json(v) for k, v in self.params.items()}}

    def __reduce__(self):
        return (_message, (self.code, self.params))


def _message(code: str, params: dict) -> Message:
    return Message(code, **params)


# ------------------------------------------------------------------ 番号つきのエラー


class Coded:
    """番号つきのエラーの共通部分。message は文全体（str(エラー) と同じ）、code と params は番号と差し込む値。

    文全体が、番号の文に手がかりを足したもの（解けない回路など）のときは、code と params は元の番号のもの
    （手がかりは params に入る）。
    """

    code: str
    params: dict[str, Any]
    message: Message

    def _set_message(self, message: Message, code: str | None = None, params: dict | None = None) -> None:
        self.message = message
        self.code = message.code if code is None else code
        self.params = dict(message.params if params is None else params)

    def text(self, lang: str = "ja") -> str:
        return self.message.text(lang)

    def to_dict(self) -> dict:
        out = self.message.to_dict()
        out["code"] = self.code
        out["params"] = {k: _to_json(v) for k, v in self.params.items()}
        return out

    def __reduce__(self):
        # 別のプロセスに渡せるように（multiprocessing など）。属性もそのまま戻す
        return (_rebuild_error, (type(self), self.message, self.code, self.params), dict(self.__dict__))


def _rebuild_error(cls, message: Message, code: str, params: dict):
    err = cls.__new__(cls)
    Exception.__init__(err, str(message))
    err._set_message(message, code, params)
    return err


class CodedError(Coded, ValueError):
    """番号つきのエラー（値の誤り、範囲の外、組み合わせの誤りなど）。

    CodedError("番号", 差し込む値...)、または CodedError(Message(...)) で作る。
    """

    def __init__(self, code: str | Message, /, **params: Any):
        message = code if isinstance(code, Message) else Message(code, **params)
        super().__init__(str(message))
        self._set_message(message)


class CodedTypeError(Coded, TypeError):
    """番号つきの型の誤り（制御器の形が口に合わないなど）。"""

    def __init__(self, code: str | Message, /, **params: Any):
        message = code if isinstance(code, Message) else Message(code, **params)
        super().__init__(str(message))
        self._set_message(message)


class CodedKeyError(Coded, KeyError):
    """番号つきの、名前で引けないときのエラー（結果に無い信号の名前など）。"""

    def __init__(self, code: str | Message, /, **params: Any):
        message = code if isinstance(code, Message) else Message(code, **params)
        super().__init__(str(message))
        self._set_message(message)


class SimulationCancelled(Coded, Exception):
    """利用者が計算を中断した（simulate の cancel が真を返した）。途中の結果は返さない。

    ValueError の一種ではない（入力の誤りではないので、エラーの扱いと分ける）。
    """

    def __init__(self, code: str | Message, /, **params: Any):
        message = code if isinstance(code, Message) else Message(code, **params)
        super().__init__(str(message))
        self._set_message(message)


def message_of(exc: BaseException) -> Message | None:
    """エラーの番号つきの文（番号の無いエラーなら None）。pydantic が包んだエラーの中も見る。"""
    if isinstance(exc, Coded):
        return exc.message
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:     # noqa: BLE001  （pydantic 以外の errors() は見ない）
            return None
        for item in items:
            inner = (item.get("ctx") or {}).get("error")
            if isinstance(inner, Coded):
                return inner.message
    return None


# ------------------------------------------------------------------ 辞書

# 番号 → （日本語、英語）。番号の並びはモジュールごと。
MESSAGES: dict[str, tuple[str, str]] = {
    # --- 共通の語 -------------------------------------------------------------------------
    "WORD_ON": ("オン", "on"),
    "WORD_OFF": ("オフ", "off"),
    "WORD_NO_SWITCHES": ("（スイッチなし）", "(no switches)"),
    "WITH_API_HINT": ("{reason}。{hint}", "{reason}. {hint}"),
    # --- 計算の入口と結果（simulate.py） ----------------------------------------------------
    "RESULT_NO_SIGNAL": (
        "{name} は無い。使える名前: {names:repr}",
        "No signal named {name}. Available names: {names:repr}",
    ),
    "RESULT_NO_CURRENT": (
        "{id}（{type}）の電流はまだ取り出せない",
        "The current of {id} ({type}) can't be extracted yet",
    ),
    "SINE_UNDERSAMPLED": (
        "{id}: 正弦波の1周期（{period:.6g} s）に時間刻みが {points:.3g} 点しか無い。"
        "エイリアシング（折り返し）で、別の周波数や振幅に見える。h を {h_suggest:.3g} s 以下（1周期 20 "
        "点以上）にする",
        "{id}: one period of the sine wave ({period:.6g} s) has only {points:.3g} time steps. Aliasing "
        "makes it look like a different frequency or amplitude. Use a time step h of {h_suggest:.3g} s or "
        "less (20 points or more per period)",
    ),
    "STEP_TIME_ROUNDED": (
        "{id}: ステップ時刻 {t} を格子点 {t_grid} に丸めた",
        "{id}: the step time {t} was rounded to the time-step grid point {t_grid}",
    ),
    "UNSUPPORTED_WAVEFORM": ("{id}: 未対応の波形 {kind}", "{id}: unsupported waveform {kind}"),
    "STEPS_OVER_LIMIT": (
        "ステップ数が約 {steps:.3g}（t_end / h = {t_end:.6g} s / {h:.6g} s）になり、上限の {limit} "
        "を超える。時間刻み h を大きくするか、終了時刻 t_end を短くする",
        "The step count would be about {steps:.3g} (t_end / h = {t_end:.6g} s / {h:.6g} s), over the limit "
        "of {limit}. Use a larger time step h or an earlier stop time t_end",
    ),
    "STEPS_API_HINT": (
        "この大きさが本当に必要なら simulate(..., max_steps=...) で上限を上げる（記憶量は、1ステップあたり "
        "8 バイト × 未知数の数の数倍）",
        "If you really need this size, raise the limit with simulate(..., max_steps=...) (memory is a few "
        "times 8 bytes × the number of unknowns per step)",
    ),
    "T_END_SHORTER_THAN_H": ("t_end が h より短い", "t_end is shorter than h"),
    "T_END_ROUNDED": (
        "終了時刻 t_end={t_end} s は時間刻み h={h:.6g} s の整数倍でないので、{t_used} s（{n} ステップ）"
        "で終える",
        "The stop time t_end={t_end} s is not an integer multiple of the time step h={h:.6g} s, so the run "
        "ends at {t_used} s ({n} steps)",
    ),
    "MEMORY_OVER_LIMIT": (
        "結果の配列と計算の作業用の配列が約 {total:.1f} GB（{steps} ステップ × 未知数 {unknowns} 個。"
        "結果の配列はそのうち {result:.1f} GB）になり、上限の {limit:.0f} GB を超える。h を大きくするか "
        "t_end を短くする",
        "The result and work arrays would take about {total:.1f} GB ({steps} steps × {unknowns} unknowns; "
        "the result array is {result:.1f} GB of it), over the limit of {limit:.0f} GB. Use a larger h or "
        "an earlier t_end",
    ),
    "MEMORY_API_HINT": (
        "この大きさが本当に必要なら simulate(..., max_steps=...) でステップ数の上限を指定する",
        "If you really need this size, set the step limit with simulate(..., max_steps=...)",
    ),
    "STEP_COARSE": (
        "時間刻み h={h:.6g} s は、回路の最も速い時定数 {tau:.3g} s の {ratio:.3g} 倍で、"
        "この成分の変化は波形に現れない（計算の先頭と、電源のステップやスイッチの切り替えの直後は、"
        "数値振動が出ないように減衰させている）。この成分を見るには h を {h_suggest:.3g} s 以下にする",
        "The time step h={h:.6g} s is {ratio:.3g} times the circuit's fastest time constant {tau:.3g} s, "
        "so changes of this component don't appear in the waveforms (at the start and right after source "
        "steps and switching, it is damped so that no numerical oscillation appears). To see this "
        "component, use h of {h_suggest:.3g} s or less",
    ),
    "STEP_COARSE_OVER_LIMIT": (
        "時間刻み h={h:.6g} s は、回路の最も速い時定数 {tau:.3g} s の {ratio:.3g} 倍で、"
        "この成分の変化は波形に現れない（計算の先頭と、電源のステップやスイッチの切り替えの直後は、"
        "数値振動が出ないように減衰させている）。この成分（{ids} が作る成分）を見るには h を "
        "{h_suggest:.3g} s 以下にする必要があるが、この終了時刻では約 {needed:.2g} ステップになり、上限の "
        "{limit} を超える。この成分を見る必要が無ければ、対処は要らない。見るには {ids} "
        "の値を見直して時定数を大きくする（インダクタと抵抗なら L / R、コンデンサと抵抗なら R·C）",
        "The time step h={h:.6g} s is {ratio:.3g} times the circuit's fastest time constant {tau:.3g} s, "
        "so changes of this component don't appear in the waveforms (at the start and right after source "
        "steps and switching, it is damped so that no numerical oscillation appears). Seeing this "
        "component (made by {ids}) needs h of {h_suggest:.3g} s or less, but with this stop time that is "
        "about {needed:.2g} steps, over the limit of {limit}. If you don't need this component, nothing "
        "needs to be done. To see it, review the values of {ids} to make the time constant longer (L / R "
        "for an inductor and a resistor, R·C for a capacitor and a resistor)",
    ),
    "STEP_COARSE_OVER_LIMIT_NO_IDS": (
        "時間刻み h={h:.6g} s は、回路の最も速い時定数 {tau:.3g} s の {ratio:.3g} 倍で、"
        "この成分の変化は波形に現れない（計算の先頭と、電源のステップやスイッチの切り替えの直後は、"
        "数値振動が出ないように減衰させている）。この成分を見るには h を {h_suggest:.3g} s "
        "以下にする必要があるが、この終了時刻では約 {needed:.2g} ステップになり、上限の {limit} を超える。"
        "この成分を見る必要が無ければ、対処は要らない。見るには時定数を大きくする（インダクタと抵抗なら L "
        "/ R、コンデンサと抵抗なら R·C）",
        "The time step h={h:.6g} s is {ratio:.3g} times the circuit's fastest time constant {tau:.3g} s, "
        "so changes of this component don't appear in the waveforms (at the start and right after source "
        "steps and switching, it is damped so that no numerical oscillation appears). Seeing this "
        "component needs h of {h_suggest:.3g} s or less, but with this stop time that is about "
        "{needed:.2g} steps, over the limit of {limit}. If you don't need this component, nothing needs to "
        "be done. To see it, make the time constant longer (L / R for an inductor and a resistor, R·C for "
        "a capacitor and a resistor)",
    ),
    "STEP_COARSE_UNDAMPED": (
        "時間刻み h={h:.6g} s は、回路の最も速い時定数 {tau:.3g} s の {ratio:.3g} 倍で、"
        "この成分の変化は波形に現れない。減衰ステップを切っている（damping_steps=0）ので、"
        "計算の先頭と電源のステップの直後に、この成分が符号を変えながら残り、波形が振れる。h を "
        "{h_suggest:.3g} s 以下にするか、damping_steps を 1 以上にする",
        "The time step h={h:.6g} s is {ratio:.3g} times the circuit's fastest time constant {tau:.3g} s, "
        "so changes of this component don't appear in the waveforms. Numerical damping is off "
        "(damping_steps=0), so at the start and right after source steps this component stays with "
        "alternating sign and the waveforms oscillate. Use h of {h_suggest:.3g} s or less, or set "
        "damping_steps to 1 or more",
    ),
    "STEP_COARSE_RINGING": (
        "時間刻み h={h:.6g} s では、回路の振動（周期 {period:.3g} s）の1周期が {points:.3g} 点しか無い。"
        "振動の周波数と減衰が正しく出ない。h を {h_suggest:.3g} s 以下（1周期 20 点以上）にする",
        "With the time step h={h:.6g} s, one period of the circuit's oscillation ({period:.3g} s) has only "
        "{points:.3g} points, so its frequency and damping come out wrong. Use h of {h_suggest:.3g} s or "
        "less (20 points or more per period)",
    ),
    "INDUCTOR_CURRENT_INTERRUPTED": (
        "時刻 t={t:.6g} s で、インダクタ {id} の電流 {current:.3g} A "
        "の還流経路が無くなり（残るのはオフのスイッチやダイオード（{off}）だけ）、"
        "電流が1刻みで消えた{more}。その直後の点のオフの素子の電圧（最大 {peak:.3g} V）"
        "は刻みの大きさで決まる見かけの値で、実際の回路ではサージ電圧になる。"
        "還流ダイオードやスナバを入れる（還流ダイオードがあっても、逆向きに流れていた電流は遮断される。"
        "降圧回路で出力の初期値が入力より高いときなど）",
        "At t={t:.6g} s, the current {current:.3g} A of inductor {id} lost its freewheeling path (only the "
        "off switches or diodes ({off}) remain), so the current vanished within one time step{more}. The "
        "off-device voltage at the next point (up to {peak:.3g} V) is an artifact set by the time step; in "
        "a real circuit it is a surge voltage. Add a freewheeling diode or a snubber (even with a "
        "freewheeling diode, a current flowing the other way is interrupted, e.g. in a buck converter "
        "whose output starts above the input)",
    ),
    "FRAG_MORE_TIMES": ("（ほかに {n} 回）", " ({n} more times)"),
    "INITIAL_VALUE_ADJUSTED": (
        "{id}: 初期値 ic={ic:.6g} {ic.unit} は回路のつなぎ方と矛盾するので、{actual:.6g} {actual.unit} "
        "から始めた（コンデンサの並列や電圧源との直結、インダクタの直列などで、"
        "初期値どうしが食い違っている）",
        "{id}: the initial value ic={ic:.6g} {ic.unit} contradicts how the circuit is connected, so the "
        "run started from {actual:.6g} {actual.unit} (initial values disagree, e.g. capacitors in parallel "
        "or directly across a voltage source, or inductors in series)",
    ),
    "HINT_UNDETERMINED": ("決まらない変数: {names}", "Undetermined variables: {names}"),
    "HINT_INDUCTORS_IN_SERIES": (
        "節点 '{node}' には {ids} しかつながっていない（インダクタどうしの直列）。"
        "直列のインダクタは1つにまとめる（値は和）",
        "Only {ids} connect to node '{node}' (inductors in series). Merge inductors in series into one "
        "(the sum of the values)",
    ),
    "HINT_SOURCE_INDUCTOR_SERIES": (
        "節点 '{node}' には {ids} しかつながっていない（電流源とインダクタの直列）。"
        "直列のインダクタは1つにまとめる（値は和）",
        "Only {ids} connect to node '{node}' (a current source in series with an inductor). Merge "
        "inductors in series into one (the sum of the values)",
    ),
    "HINT_INDUCTOR_CUTSET": (
        "節点 {nodes} の部分は、{links} だけで回路の残りとつながっていて、流れ込む電流の和が 0 "
        "に縛られるので、この部分の電位が決まらない。この部分のどこか1点と基準節点の間に、"
        "回路のほかの抵抗より十分に大きな抵抗（例 1 MΩ）を入れる（小さいと、"
        "この抵抗に流れる電流で回路の動きが変わる）。この抵抗と {inductors} で、時定数（インダクタンス / "
        "抵抗）がとても短い成分ができるが、計算では減衰させるので波形には現れない（そのことは注意に出る）",
        "The part with nodes {nodes} connects to the rest of the circuit only through {links}; the "
        "currents flowing into it must sum to 0, so its potential is undetermined. Put a resistor much "
        "larger than the other resistors in the circuit (e.g. 1 MΩ) between one point of this part and the "
        "reference node (a small one changes how the circuit behaves). This resistor and {inductors} form "
        "a component with a very short time constant (inductance / resistance); it is damped in the "
        "simulation and doesn't appear in the waveforms (a warning says so)",
    ),
    "CIRCUIT_EXPLAINED": ("{cause}{hints:+。}", "{cause}{hints:+. }"),
    "CIRCUIT_EXPLAINED_IN_STATE": (
        "スイッチング状態（回路状態。{state}）で: {cause}{hints:+。}{r_off}",
        "In the switching state ({state}): {cause}{hints:+. }{r_off}",
    ),
    "FRAG_R_OFF_FLOATING": (
        "。オフ抵抗 r_off が極端に大きいと、オフのときに節点が浮いたのと同じになって解けない",
        ". An extremely large off-resistance r_off makes a node float while the device is off, and the "
        "circuit can't be solved",
    ),
    "DIODES_NOT_SETTLED_AT_START": (
        "時刻 t=0 でダイオードの状態が {flips} 回の反転で確定しない。最後に試した状態: {state}。{hints:。}",
        "At t=0 the diode states didn't settle within {flips} flips. Last state tried: {state}. {hints:. }",
    ),
    "DIODES_NOT_SETTLED": (
        "時刻 t={t:.6g} s（ステップ {k}）でダイオードの状態が {flips} 回の反転で確定しない。"
        "最後に試した状態: {state}。{hints:。}",
        "At t={t:.6g} s (step {k}) the diode states didn't settle within {flips} flips. Last state tried: "
        "{state}. {hints:. }",
    ),
    "HINT_DIODES_FLIPPING": (
        "オンとオフを繰り返したダイオード: {diodes:, }",
        "Diodes that kept switching on and off: {diodes:, }",
    ),
    "HINT_DIODE_VOLTAGE": ("{id}（最後の電圧 {v:.3g} V）", "{id} (last voltage {v:.3g} V)"),
    "HINT_DIODES_ROUNDING": (
        "オンにしてもオフにしても、ダイオードの電圧が判定と食い違い、"
        "その食い違いが丸め誤差とみなせる幅{band}より大きい。r_off / r_on の比（いまは最大 {ratio:.3g}）"
        "が大きいほど丸め誤差が大きくなる。r_off を小さく（例 1e5 Ω）、または r_on を大きくして試す。"
        "ダイオードどうしが向きをそろえずに並列・直列につながった部分が無いかも確認する",
        "Whether on or off, the diode voltage contradicts the decision by more than what counts as "
        "rounding error{band}. The larger the ratio r_off / r_on (now up to {ratio:.3g}), the larger the "
        "rounding error. Try a smaller r_off (e.g. 1e5 Ω) or a larger r_on. Also check for diodes "
        "connected in parallel or in series without matching directions",
    ),
    "FRAG_BAND": ("（いまは ±{band:.3g} V）", " (now ±{band:.3g} V)"),
    "ARG_T_END_H": ("t_end と h は正の有限値が必要", "t_end and h must be positive and finite"),
    "ARG_DAMPING_STEPS": ("damping_steps は 0 以上にする", "damping_steps must be 0 or more"),
    "ARG_COMMAND_NEEDS_CONTROLLER": (
        "指令で値が決まる電源 {ids:repr} がある。指令を出す制御器（controllers）を渡す",
        "There are sources set by a command {ids:repr}. Pass the controllers that issue the command",
    ),
    "ARG_H_REQUIRED": (
        "スイッチの無い回路では時間刻み h が必要",
        "A circuit without switches needs the time step h",
    ),
    "ARG_STEPS_PER_PERIOD_WITHOUT_GATES": (
        "steps_per_period を使えるのは、ゲート信号（gates）を渡すときだけ",
        "steps_per_period can be used only when gate signals (gates) are passed",
    ),
    "ARG_H_REQUIRED_WITHOUT_GATES": (
        "ゲート信号（gates）が無いときは、時間刻み h を指定する",
        "Without gate signals (gates), specify the time step h",
    ),
    "ARG_STEPS_PER_PERIOD_MIN": ("steps_per_period は 2 以上にする", "steps_per_period must be 2 or more"),
    "ARG_H_AND_STEPS_PER_PERIOD": (
        "h と steps_per_period は、どちらか一方だけを指定する",
        "Specify only one of h and steps_per_period",
    ),
    "ARG_PROGRESS": (
        "progress は、進み具合（0〜1）を受け取る関数にする（現在 {value}）",
        "progress must be a function that takes the progress (0 to 1) (now {value})",
    ),
    "ARG_CANCEL": (
        "cancel は、中断するなら真を返す引数なしの関数にする（現在 {value}）",
        "cancel must be a function without arguments that returns true to stop (now {value})",
    ),
    "GATE_PERIOD_NOT_MULTIPLE": (
        "ゲート信号 '{name}' の周期（{period:.6g} s）が時間刻み h（{h:.6g} s）の整数倍になっていない。h "
        "を周期の整数分の1にする",
        "The period of gate signal '{name}' ({period:.6g} s) is not an integer multiple of the time step h "
        "({h:.6g} s). Make h an integer fraction of the period",
    ),
    "CONTROL_PERIOD_NOT_MULTIPLE": (
        "制御周期（{period:.6g} s）が時間刻み h（{h:.6g} s）の整数倍になっていない。h "
        "を周期の整数分の1にする",
        "The control period ({period:.6g} s) is not an integer multiple of the time step h ({h:.6g} s). "
        "Make h an integer fraction of the period",
    ),
    "GATE_PERIOD_ONE_STEP": (
        "ゲート信号 '{name}': 1周期が1ステップしか無い。h を小さくする",
        "Gate signal '{name}': one period is only one time step. Use a smaller h",
    ),
    "GATE_NAMES_DUPLICATE": (
        "ゲート信号の名前が重複している: {names:repr}",
        "Duplicate gate signal names: {names:repr}",
    ),
    "GATE_NOT_GIVEN": (
        "{id}: ゲート信号 '{gate}' が渡されていない。渡された名前: {names:repr}",
        "{id}: gate signal '{gate}' was not passed. Names passed: {names:repr}",
    ),
    "CONTROLLER_SHAPE": (
        "制御器 {type} に period / inputs / outputs / reset() / step() のどれかが無い",
        "Controller {type} lacks one of period / inputs / outputs / reset() / step()",
    ),
    "CONTROLLER_INPUT_UNKNOWN": (
        "制御器が測る信号 {names:repr} が回路に無い。使える名前: {available:repr}",
        "The signals {names:repr} that the controller measures aren't in the circuit. Available names: "
        "{available:repr}",
    ),
    "COMMAND_NAME_DUPLICATE": ("指令の名前 '{name}' が重複している", "Duplicate command name '{name}'"),
    "GATE_DUTY_COMMAND_UNKNOWN": (
        "ゲート信号 '{name}': デューティ指令 '{command}' を出す制御器が無い。制御器が出す指令: "
        "{commands:repr}",
        "Gate signal '{name}': no controller issues the duty command '{command}'. Commands issued by "
        "controllers: {commands:repr}",
    ),
    "SOURCE_COMMAND_UNKNOWN": (
        "{id}: 電源の値を決める指令 '{command}' を出す制御器が無い。制御器が出す指令: {commands:repr}",
        "{id}: no controller issues the command '{command}' that sets this source. Commands issued by "
        "controllers: {commands:repr}",
    ),
    "CONTROLLER_OUTPUT_COUNT": (
        "制御器が返した指令の数が outputs（{outputs:repr}）と合わない: {values}",
        "The number of commands returned by the controller doesn't match outputs ({outputs:repr}): {values}",
    ),
    "CONTROLLER_COMMAND_NOT_FINITE": (
        "時刻 t={t:.6g} s で制御器の指令 {outputs:repr} が有限値でない: {values}",
        "At t={t:.6g} s the controller commands {outputs:repr} are not finite: {values}",
    ),
    "RUN_DIVERGED_NOT_FINITE": (
        "時刻 t={t:.6g} s で {signal} の値が有限でなくなったので、計算を止めた（発散）。"
        "時間刻みを小さくして、もう一度計算する。制御のある回路では、"
        "ゲインも確かめる（閉ループが不安定だと、値が指数的に大きくなる）",
        "At t={t:.6g} s, {signal} stopped being finite, so the run was stopped (diverged). Use a smaller "
        "time step and run again. For circuits with control, check the gains too (an unstable closed loop "
        "makes the values grow exponentially)",
    ),
    "RUN_DIVERGED": (
        "時刻 t={t:.6g} s で {signal} の値が {value:.3g} {value.unit} になり、"
        "発散とみなす大きさ（{limit:.3g} {limit.unit}。この回路の{kind}の目安 {scale:.3g} {scale.unit} の "
        "{factor:.0e} 倍）を超えたので、計算を止めた（発散）。時間刻みを小さくして、もう一度計算する。"
        "制御のある回路では、ゲインも確かめる（閉ループが不安定だと、値が指数的に大きくなる）",
        "At t={t:.6g} s, {signal} reached {value:.3g} {value.unit}, beyond the size treated as divergence "
        "({limit:.3g} {limit.unit}, {factor:.0e} times this circuit's {kind} scale {scale:.3g} "
        "{scale.unit}), so the run was stopped (diverged). Use a smaller time step and run again. For "
        "circuits with control, check the gains too (an unstable closed loop makes the values grow "
        "exponentially)",
    ),
    "WORD_VOLTAGE": ("電圧", "voltage"),
    "WORD_CURRENT": ("電流", "current"),
    "RUN_CANCELLED": (
        "計算を中断した（時刻 t={t:.6g} s、ステップ {k} / {n}）。途中の結果は返さない",
        "The run was stopped (t={t:.6g} s, step {k} / {n}). No partial result is returned",
    ),
    # --- 核と回路方程式（kernel.py、mna.py） -------------------------------------------------
    "CIRCUIT_UNSOLVABLE": (
        "回路が一意に解けない。基準節点から浮いた部分、電圧源だけのループ、電圧源とコンデンサの直結、"
        "電流源とインダクタの直列、インダクタどうしの直列などが無いか確認する",
        "The circuit has no unique solution. Check for parts floating from the reference node, loops of "
        "voltage sources only, a capacitor directly across a voltage source, a current source in series "
        "with an inductor, inductors in series, and the like",
    ),
    "MATRIX_SINGULAR": (
        "時間刻み h={h} で行列が解けない",
        "The matrix can't be solved with the time step h={h}",
    ),
    "MATRIX_SINGULAR_DETAIL": (
        "時間刻み h={h} で行列が解けない: {detail}",
        "The matrix can't be solved with the time step h={h}: {detail}",
    ),
    "TOO_MANY_DEVICES": (
        "スイッチとダイオードが {n} 個ある。扱えるのは合わせて {max} 個まで",
        "There are {n} switches and diodes. Up to {max} in total can be handled",
    ),
    # --- 仕様とネットリストの形（ir.py） ---------------------------------------------------
    "SPEC_LEGACY_CONTROL_NOT_NUMBER": (
        "仕様の版 0.1 の control.{name} は数値でないので読めない（{value}）。版 0.2 の形（value、unit、"
        "source）で書く",
        "control.{name} in spec version 0.1 isn't a number and can't be read ({value}). Write it in the "
        "version 0.2 form (value, unit, source)",
    ),
    "COMMAND_LIMITS_ORDER": (
        "指令 '{command}': lower（{lower}）は upper（{upper}）以下にする",
        "Command '{command}': lower ({lower}) must not exceed upper ({upper})",
    ),
    "COMPONENT_SAME_NODES": (
        "{id}: 両端が同じ節点 '{node}' につながっている",
        "{id}: both ends connect to the same node '{node}'",
    ),
    "COMPONENT_VALUE_NOT_POSITIVE": (
        "{id}: value は正の有限値が必要（現在 {value}）",
        "{id}: value must be positive and finite (now {value})",
    ),
    "COMPONENT_WAVEFORM_NOT_ALLOWED": (
        "{id}: {type} に waveform は指定できない",
        "{id}: {type} can't have a waveform",
    ),
    "SOURCE_WAVEFORM_REQUIRED": ("{id}: 電源には waveform が必要", "{id}: a source needs a waveform"),
    "SOURCE_VALUE_NOT_ALLOWED": (
        "{id}: 電源の値は waveform で指定する",
        "{id}: give the source value as a waveform",
    ),
    "R_ON_OFF_NOT_ALLOWED": (
        "{id}: r_on / r_off を指定できるのは S と D だけ",
        "{id}: only S and D can have r_on / r_off",
    ),
    "GATE_NOT_ALLOWED": ("{id}: gate を指定できるのは S だけ", "{id}: only S can have a gate"),
    "IC_NOT_ALLOWED": ("{id}: ic を指定できるのは L と C だけ", "{id}: only L and C can have ic"),
    "SWITCH_VALUE_NOT_ALLOWED": (
        "{id}: {type} に value / waveform は指定できない（r_on / r_off を使う）",
        "{id}: {type} can't have value / waveform (use r_on / r_off)",
    ),
    "SWITCH_R_NOT_POSITIVE": (
        "{id}: {name} は正の有限値が必要（現在 {value}）",
        "{id}: {name} must be positive and finite (now {value})",
    ),
    "SWITCH_R_ORDER": (
        "{id}: r_on（{r_on}）は r_off（{r_off}）より小さくする",
        "{id}: r_on ({r_on}) must be smaller than r_off ({r_off})",
    ),
    "SWITCH_GATE_REQUIRED": (
        "{id}: スイッチにはゲート信号の名前 gate が必要",
        "{id}: a switch needs the name of its gate signal (gate)",
    ),
    "NETLIST_DUPLICATE_IDS": ("素子IDが重複している: {ids:repr}", "Duplicate component IDs: {ids:repr}"),
    "NETLIST_NO_GROUND": (
        "基準節点 '{ground}' につながる素子が無い",
        "No component connects to the reference node '{ground}'",
    ),
    "NETLIST_DANGLING": (
        "素子が1つしかつながっていない節点がある: {nodes:repr}",
        "Some nodes connect to only one component: {nodes:repr}",
    ),
    # --- 単位（units.py） --------------------------------------------------------------------
    "UNIT_UNREADABLE": ("単位 '{unit}' は読めない", "The unit '{unit}' can't be read"),
    "UNIT_UNREADABLE_EXPECTED": (
        "単位 '{unit}' は読めない（{expected}を期待。{how}）",
        "The unit '{unit}' can't be read (expected {expected}; {how})",
    ),
    "UNIT_WRONG_DIMENSION": (
        "単位 '{unit}' は{found}の単位。ここには{expected}の単位が要る（{how}）",
        "The unit '{unit}' is a unit of {found}. A unit of {expected} is needed here ({how})",
    ),
    "HOW_EXACT": ("{units: か } と書く。接頭語は付けない", "write {units: or }, without a prefix"),
    "HOW_PREFIXED": (
        "{si} か、mH や kohm のような接頭語つきで書く。m はミリ、M はメガ",
        "write {si}, or with a prefix such as mH or kohm; m is milli and M is mega",
    ),
    "DIM_voltage": ("電圧", "voltage"),
    "DIM_current": ("電流", "current"),
    "DIM_resistance": ("抵抗", "resistance"),
    "DIM_inductance": ("インダクタンス", "inductance"),
    "DIM_capacitance": ("キャパシタンス", "capacitance"),
    "DIM_frequency": ("周波数", "frequency"),
    "DIM_time": ("時間", "time"),
    "DIM_power": ("電力", "power"),
    "DIM_per_voltage": ("比例ゲイン（1/電圧）", "proportional gain (1/voltage)"),
    "DIM_per_voltage_time": ("積分ゲイン（1/(電圧·時間)）", "integral gain (1/(voltage·time))"),
    "DIM_time_per_voltage": ("微分ゲイン（時間/電圧）", "derivative gain (time/voltage)"),
    "DIM_ratio": ("無次元の量", "dimensionless quantity"),
    # --- テンプレート（templates/） ----------------------------------------------------------
    "TEMPLATE_UNKNOWN": (
        "未対応のテンプレート '{template}'。使えるもの: {names:repr}",
        "Unknown template '{template}'. Available: {names:repr}",
    ),
    "MISSING_VALUES": (
        "値が入っていないパラメータ: {names:repr}",
        "Parameters without a value: {names:repr}",
    ),
    "UNIT_CONVERTED": (
        "{key}: {value:g} {value.unit} を {si:g} {si.unit} に換算した",
        "{key}: converted {value:g} {value.unit} to {si:g} {si.unit}",
    ),
    "UNIT_CONVERTED_RATIO": (
        "{key}: {value:g} {value.unit} を {si:g} に換算した",
        "{key}: converted {value:g} {value.unit} to {si:g}",
    ),
    "UNITS_UNREADABLE": (
        "単位を読めない項目がある。{problems:。}",
        "Some items have units that can't be read. {problems:. }",
    ),
    "UNITS_WRONG_DIMENSION": (
        "単位の量の種類が違う項目がある。{problems:。}",
        "Some items have units of the wrong kind of quantity. {problems:. }",
    ),
    "UNITS_UNREADABLE_OR_WRONG": (
        "単位を読めない、または量の種類が違う項目がある。{problems:。}",
        "Some items have units that can't be read or are of the wrong kind of quantity. {problems:. }",
    ),
    "UNIT_PROBLEM": ("{key}: {problem}", "{key}: {problem}"),
    "SPEC_LEGACY_CONTROL": (
        "仕様の版 0.1 の形なので、control の数値を SI単位の値とみなした（出典は「記載なし」）: {items:、}。"
        "単位と出典を確かめて、版 0.2 の形（value、unit、source）で書き直す",
        "The spec is in the version 0.1 form, so the control numbers were taken as values in SI units "
        "(source: not given): {items:, }. Check the units and sources, and rewrite them in the version 0.2 "
        "form (value, unit, source)",
    ),
    "SPEC_LEGACY_CONTROL_INFERRED": (
        "仕様に版（schema_version）が無く、control の値が単位の無い数値なので、版 0.1 として読んだ。"
        "control の数値を SI単位の値とみなした（出典は「記載なし」）: {items:、}。単位と出典を確かめて、版 "
        "0.2 の形（value、unit、source）で書き直す",
        "The spec has no version (schema_version) and its control values are numbers without units, so it "
        "was read as version 0.1. The control numbers were taken as values in SI units (source: not "
        "given): {items:, }. Check the units and sources, and rewrite them in the version 0.2 form (value, "
        "unit, source)",
    ),
    "UNUSED_ITEMS": (
        "このテンプレートが使わない項目を無視した: {items:、}",
        "Ignored items this template doesn't use: {items:, }",
    ),
    "VALUES_NOT_POSITIVE": (
        "正の値が必要なパラメータ: {names:repr}",
        "Parameters that must be positive: {names:repr}",
    ),
    "DEFAULT_T_END_SHORTENED": (
        "減衰が遅い（収まるまで約 {t_full:.3g} s）ので、既定の終了時刻を {t_end:.3g} s（{n} ステップ）"
        "に短くした。全体を見るには simulation.t_end を指定する",
        "The decay is slow (about {t_full:.3g} s to settle), so the default stop time was shortened to "
        "{t_end:.3g} s ({n} steps). To see all of it, specify simulation.t_end",
    ),
    "DEFAULT_H_COARSENED": (
        "指定の t_end={t_end:.6g} s は回路の振動の周期 {period:.3g} s の {ratio:.3g} 倍あり、"
        "既定の刻み（1周期 200 点）では {n} ステップを超えるので、刻みを {h:.3g} s（1周期 {points:.3g} 点、"
        "{n} ステップ）に粗くした",
        "The given t_end={t_end:.6g} s is {ratio:.3g} times the circuit's oscillation period {period:.3g} "
        "s, and the default time step (200 points per period) would exceed {n} steps, so the time step was "
        "made coarser: {h:.3g} s ({points:.3g} points per period, {n} steps)",
    ),
    "DEFAULT_H_STIFF": (
        "回路の速い成分（{fast:.3g} s）と遅い成分（{slow:.3g} s）が大きく離れているので、"
        "既定の刻みを遅い側に合わせて {h:.3g} s（{n} ステップ）にした。速い成分の変化を見るには "
        "simulation.h と simulation.t_end を指定する",
        "The circuit's fast component ({fast:.3g} s) and slow component ({slow:.3g} s) are far apart, so "
        "the default time step was set for the slow side: {h:.3g} s ({n} steps). To see changes of the "
        "fast component, specify simulation.h and simulation.t_end",
    ),
    "PHASE_DRIFT": (
        "回路の振動（周期 {period:.3g} s）を1周期 {points:.3g} 点で計算するので、"
        "台形則の位相のずれが積み上がり、計算の精度が落ちる（振動の波形が入力の約 {error:.1%} "
        "ずれる見込み）。ずれを入力の {target:.1%} 程度にするには、simulation.h を {h_fine:.3g} s "
        "以下（1周期 {points_fine:.3g} 点以上）にするか、simulation.t_end を短くする",
        "The circuit's oscillation (period {period:.3g} s) is computed with {points:.3g} points per "
        "period, so the phase error of the trapezoidal rule accumulates and accuracy drops (the "
        "oscillation is expected to be off by about {error:.1%} of the input). To bring this to about "
        "{target:.1%} of the input, use simulation.h of {h_fine:.3g} s or less ({points_fine:.3g} points "
        "or more per period), or an earlier simulation.t_end",
    ),
    "FIDELITY_IGNORED": (
        "このテンプレートにはスイッチが無く、モデルを選べないので、simulation.fidelity の指定（{fidelity}）"
        "は使わなかった",
        "This template has no switches and its model can't be chosen, so simulation.fidelity ({fidelity}) "
        "was not used",
    ),
    "VALUES_OUT_OF_RANGE": (
        "値が範囲の外の項目がある。{parts:。}",
        "Some values are out of range. {parts:. }",
    ),
    "RANGE_POSITIVE": ("{names:、} は 0 より大きくする", "{names:, } must be greater than 0"),
    "RANGE_NON_NEGATIVE": ("{names:、} は 0 以上にする", "{names:, } must be 0 or more"),
    "DUTY_LIMITS_ORDER": (
        "デューティの上下限は 0 ≦ d_min < d_max ≦ 1 にする（d_min {d_min:g}、d_max {d_max:g}）",
        "The duty cycle limits must satisfy 0 ≤ d_min < d_max ≤ 1 (d_min {d_min:g}, d_max {d_max:g})",
    ),
    "FSW_NOT_MULTIPLE": (
        "スイッチング周波数 f_sw（{f_sw:g} Hz）は、制御周波数 f_ctrl（{f_ctrl:g} Hz）"
        "の整数倍にする（制御周期をスイッチング周期の整数倍にする）",
        "The switching frequency f_sw ({f_sw:g} Hz) must be an integer multiple of the control frequency "
        "f_ctrl ({f_ctrl:g} Hz) (the control period must be an integer multiple of the switching period)",
    ),
    "LOAD_STEP_SAME": (
        "R_load_step が R_load と同じ値。負荷ステップを入れないなら、R_load_step と t_load_step "
        "を空欄にする",
        "R_load_step equals R_load. To skip the load step, leave R_load_step and t_load_step blank",
    ),
    "LOAD_STEP_TOO_SMALL": (
        "{key}（{r:g} Ω）が小さすぎて、負荷ステップを作れない。負荷ステップは、"
        "並列の抵抗をスイッチ（オン抵抗 {r_on:g} Ω）で入れたり切ったりして作るので、{key} "
        "はオン抵抗より十分大きくする",
        "{key} ({r:g} Ω) is too small to make the load step. The load step switches a parallel resistor in "
        "or out with a switch (on-resistance {r_on:g} Ω), so {key} must be much larger than the "
        "on-resistance",
    ),
    "LOAD_STEP_SNAPPED": (
        "負荷ステップの時刻 {t:.6g} s を、次のスイッチング周期の先頭 {t_grid:.6g} s に合わせた（切り上げ）",
        "The load step time {t:.6g} s was moved to the start of the next switching period, {t_grid:.6g} s "
        "(rounded up)",
    ),
    "AVERAGE_MODEL_USED": (
        "平均モデルで計算した。スイッチ S1・S2 を、電圧「デューティ × "
        "Vin」の電源とオン抵抗に置き換えている。入力の電源にインピーダンスが無いことを前提にしているので、"
        "入力側にインピーダンスがある回路や、入力の電流を見たいときは、スイッチングモデルで計算する",
        "Computed with the averaged model (state-space averaging). Switches S1 and S2 are replaced by a "
        "source of duty × Vin and the on-resistance. The input is assumed to be an ideal voltage source "
        "(zero source impedance), so for circuits with input-side impedance, or to see the input current, "
        "use the switching model",
    ),
    "ESR_OMITTED": (
        "C_esr（出力コンデンサの等価直列抵抗）の値が無いので、ESR は計算に入れていない（0 Ω "
        "として計算した）",
        "C_esr (equivalent series resistance of the output capacitor) has no value, so the ESR is not "
        "simulated (computed as 0 Ω)",
    ),
    "TF_DEFAULTED": (
        "Tf（微分のフィルタの時定数）の値が無いので、制御周期の {ratio:g} 倍（{tf:.3g} s）とした",
        "Tf (derivative filter time constant) has no value, so {ratio:g} times the control period "
        "({tf:.3g} s) was used",
    ),
    "REFERENCE_OUT_OF_RANGE": (
        "出力電圧の指令値 {value:g} V は、デューティの上下限で出せる範囲（{low:g}〜{high:g} V。損失を除く）"
        "の外なので、出力はこの値に届かない",
        "The output voltage reference {value:g} V is outside what the duty cycle limits allow ({low:g} to "
        "{high:g} V, ignoring losses), so the output can't reach it",
    ),
    "STEP_AFTER_END": (
        "{key}（{t:g} s）が終了時刻（{t_end:g} s）以降なので、ステップは波形に現れない",
        "{key} ({t:g} s) is at or after the stop time ({t_end:g} s), so the step won't appear",
    ),
    "FIDELITY_UNKNOWN": (
        "未対応のモデルの種類 '{fidelity}'（switching か average）",
        "Unknown model fidelity '{fidelity}' (switching or average)",
    ),
    "SIGNAL_INFO_INCONSISTENT": (
        "信号 {name}: 種類 {kind} に要る欄が無いか、名前と合わない",
        "Signal {name}: fields needed for kind {kind} are missing or don't match the name",
    ),
    # --- 制御ブロックと PWM（control/） ------------------------------------------------------
    "PWM_FREQUENCY_NOT_FINITE": ("{name}: frequency は有限値が必要", "{name}: frequency must be finite"),
    "PWM_COMPLEMENT_NAME": (
        "{name}: complement には、name と違う空でない名前を付ける",
        "{name}: complement must be a non-empty name different from name",
    ),
    "PWM_DEAD_TIME": (
        "{name}: デッドタイム（dead_time={dead_time}）はまだ扱えない。0 にする",
        "{name}: dead time (dead_time={dead_time}) isn't supported yet. Use 0",
    ),
    "PWM_DUTY_NAME_EMPTY": (
        "{name}: duty に指令の名前を使うなら空にしない",
        "{name}: a command name used as duty must not be empty",
    ),
    "PWM_DUTY_RANGE": (
        "{name}: duty の固定値は 0〜1 にする（現在 {duty}）",
        "{name}: a fixed duty must be 0 to 1 (now {duty})",
    ),
    "CARRIER_UNKNOWN": ("未対応のキャリア '{carrier}'", "Unknown carrier '{carrier}'"),
    "LIMITER_ORDER": (
        "リミッタの下限（{lower}）は上限（{upper}）以下にする",
        "The limiter's lower limit ({lower}) must not exceed the upper limit ({upper})",
    ),
    "PID_GAIN_NOT_FINITE": (
        "PIDの {name} は有限値が必要（現在 {value}）",
        "PID {name} must be finite (now {value})",
    ),
    "PID_PERIOD": (
        "PIDの周期 period は正の有限値が必要（現在 {value}）",
        "The PID period must be positive and finite (now {value})",
    ),
    "PID_FILTER": (
        "微分のフィルタの時定数 t_filter は 0 以上の有限値にする（現在 {value}）",
        "The derivative filter time constant t_filter must be finite and 0 or more (now {value})",
    ),
    "PID_ANTI_WINDUP": (
        "anti_windup は 'clamp' か 'none'（現在 {value}）",
        "anti_windup must be 'clamp' or 'none' (now {value})",
    ),
    "PID_DERIVATIVE_ON": (
        "derivative_on は 'error' か 'measurement'（現在 {value}）",
        "derivative_on must be 'error' or 'measurement' (now {value})",
    ),
    "PID_MEASUREMENT_REQUIRED": (
        "微分を測定値に掛けるPIDには、update() に測定値を渡す",
        "A PID with derivative on measurement needs the measurement in update()",
    ),
    "SCHEDULE_NOT_FINITE": (
        "区分一定の値の時刻と値は有限値にする（時刻は 0 以上）: {schedule}",
        "Times and values of a piecewise-constant value must be finite (times 0 or more): {schedule}",
    ),
    "SCHEDULE_ORDER": (
        "区分一定の値の切り替えの時刻は昇順に並べる: {times}",
        "Change times of a piecewise-constant value must be in ascending order: {times}",
    ),
    # --- コマンドライン（__main__.py） -------------------------------------------------------
    "CLI_READ_FAILED": ("仕様ファイルを読めません: {detail}", "Can't read the spec file: {detail}"),
    "CLI_SCHEMA_INVALID": (
        "仕様JSONの形が正しくありません。{problems}",
        "The spec JSON has the wrong form. {problems}",
    ),
    "CLI_WHOLE": ("全体", "whole"),
    "CLI_CANNOT_RUN": ("計算できません。{reason}", "Can't run the simulation. {reason}"),
    "CLI_STEP_LIMIT_HINT": (
        "（仕様JSONの simulation.h と simulation.t_end で指定する）",
        " (set them with simulation.h and simulation.t_end in the spec JSON)",
    ),
    "CLI_TEMPLATE": ("テンプレート: {template}", "Template: {template}"),
    "CLI_SUMMARY": (
        "終了時刻: {t_end:.6g} s / 刻み: {h:.6g} s / ステップ数: {steps}",
        "Stop time: {t_end:.6g} s / time step: {h:.6g} s / steps: {steps}",
    ),
    "CLI_SIGNAL": (
        "  {name}: 最終値 {last:.6g} / 最大 {max:.6g} / 最小 {min:.6g}",
        "  {name}: final {last:.6g} / max {max:.6g} / min {min:.6g}",
    ),
    "CLI_NOTE": ("  [{code}] 注意: {text}", "  [{code}] Warning: {text}"),
    "CLI_CSV_WRITTEN": ("波形を書き出しました: {path}", "Waveforms written: {path}"),
    "CSV_NOTE": ("# [{code}] 注意: {text}", "# [{code}] Warning: {text}"),
}


# 番号 → 画面のお知らせ（`docs/design/screens-v0.1.md` 付録B）。画面は、この表の N-… の文面（画面の辞書）で
# 見せ、差し込む値はエンジンの params から取る。() は、画面ではお知らせに出さない番号（行の見え方で示す、
# 文の部品、プログラムの使い方の誤り、コマンドラインの文言）。表に無い番号は、画面の決まり（12章）どおり
# N-RUN-NOTE-OTHER（注意）か N-RUN-INTERNAL（エラー）で、文面を「詳細」に入れる。
# 対応表の説明は `docs/design/basic-design.md` 10.4（試験がこの表と照らす）。
SCREEN_NOTICES: dict[str, tuple[str, ...]] = {
    "SINE_UNDERSAMPLED": ("N-RUN-NOTE-STEP",),
    "STEP_TIME_ROUNDED": ("N-RUN-NOTE-ROUND",),
    "STEPS_OVER_LIMIT": ("N-STEPS-LIMIT", "N-RUN-STEPS"),
    "MEMORY_OVER_LIMIT": ("N-RUN-MEMORY",),
    "STEP_COARSE": ("N-STEP-COARSE", "N-RUN-NOTE-STEP"),
    "STEP_COARSE_OVER_LIMIT": ("N-STEP-COARSE", "N-RUN-NOTE-STEP"),
    "STEP_COARSE_OVER_LIMIT_NO_IDS": ("N-STEP-COARSE", "N-RUN-NOTE-STEP"),
    "STEP_COARSE_UNDAMPED": ("N-STEP-COARSE", "N-RUN-NOTE-STEP"),
    "STEP_COARSE_RINGING": ("N-STEP-COARSE", "N-RUN-NOTE-STEP"),
    "CIRCUIT_UNSOLVABLE": ("N-RUN-CIRCUIT",),
    "MATRIX_SINGULAR": ("N-RUN-CIRCUIT",),
    "MATRIX_SINGULAR_DETAIL": ("N-RUN-CIRCUIT",),
    "TOO_MANY_DEVICES": ("N-RUN-CIRCUIT",),
    "DIODES_NOT_SETTLED_AT_START": ("N-RUN-SWITCH",),
    "DIODES_NOT_SETTLED": ("N-RUN-SWITCH",),
    "RUN_DIVERGED": ("N-RUN-DIVERGED",),
    "RUN_DIVERGED_NOT_FINITE": ("N-RUN-DIVERGED",),
    "CONTROLLER_COMMAND_NOT_FINITE": ("N-RUN-DIVERGED",),
    "TEMPLATE_UNKNOWN": ("N-OPEN-TEMPLATE",),
    "MISSING_VALUES": ("N-MISSING", "N-MISSING-PAIR"),
    "UNIT_CONVERTED": ("N-UNIT-CONVERTED",),
    "UNIT_CONVERTED_RATIO": ("N-UNIT-CONVERTED",),
    "UNITS_UNREADABLE": ("N-UNIT-WRONG",),
    "UNITS_WRONG_DIMENSION": ("N-UNIT-WRONG",),
    "UNITS_UNREADABLE_OR_WRONG": ("N-UNIT-WRONG",),
    "SPEC_LEGACY_CONTROL": ("N-OPEN-LEGACY",),
    "SPEC_LEGACY_CONTROL_INFERRED": ("N-OPEN-LEGACY",),
    "UNUSED_ITEMS": ("N-UNUSED",),
    "VALUES_NOT_POSITIVE": ("N-INVALID",),
    "VALUES_OUT_OF_RANGE": ("N-INVALID",),
    "DUTY_LIMITS_ORDER": ("N-INVALID",),
    "FSW_NOT_MULTIPLE": ("N-INVALID",),
    "LOAD_STEP_SAME": ("N-INVALID",),
    "LOAD_STEP_TOO_SMALL": ("N-INVALID",),
    "LOAD_STEP_SNAPPED": ("N-STEP-SNAP",),
    "REFERENCE_OUT_OF_RANGE": ("N-REF-RANGE",),
    "STEP_AFTER_END": ("N-STEP-AFTER-END",),
    "SPEC_LEGACY_CONTROL_NOT_NUMBER": ("N-OPEN-SCHEMA",),
    # 画面ではお知らせに出さない（行の見え方で示す。screens-v0.1.md 付録A.4）
    "ESR_OMITTED": (),
    "TF_DEFAULTED": (),
}
