"""時間ステップの核。

決まり: run() の中では配列と数値だけを扱う（辞書やオブジェクトを持ち込まない）。
こうしておけば、後から Numba や C / C++ / Rust に載せ替えられる。
回路の解釈（ネットリスト → 行列）は核の外（mna.py）でやる。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.linalg import LinAlgWarning, lu_factor, lu_solve, null_space

from .mna import CircuitError


@dataclass
class Operators:
    """1つの回路状態（スイッチ状態）に対して一度だけ計算して使い回す行列。"""

    P: np.ndarray    # 台形則: x[k+1] = P x[k] + Q * (u[k] + u[k+1]) / 2
    Q: np.ndarray
    Rx: np.ndarray   # 不連続点での整合: x(t+) = Rx x(t-) + Ru u(t+)
    Ru: np.ndarray
    Rex: np.ndarray  # 初期化: x(0) = Rex (E x(0)) + Ru u(0)


def _row_scale(A: np.ndarray) -> np.ndarray:
    m = np.abs(A).max(axis=1) if A.size else np.zeros(A.shape[0])
    return np.where(m > 0, 1.0 / np.where(m > 0, m, 1.0), 1.0)


def prepare(E: np.ndarray, G: np.ndarray, B: np.ndarray, h: float) -> Operators:
    n = E.shape[0]

    # --- 整合の取れた初期値・不連続点の処理 -----------------------------------
    # 状態（コンデンサ電圧・インダクタ電流）は E x で決まる。残りの変数は、
    # 方程式のうち微分を含まない部分  W^T G x = W^T B u  （W は E の左零空間）で決まる。
    scale = np.abs(E).max()
    W = np.eye(n) if scale == 0 else null_space((E / scale).T)
    A_alg = W.T @ G
    B_alg = W.T @ B
    dE, dA = _row_scale(E), _row_scale(A_alg)
    S = np.vstack([dE[:, None] * E, dA[:, None] * A_alg])
    if np.linalg.matrix_rank(S) < n:
        raise CircuitError(
            "回路が一意に解けない。基準節点から浮いた部分、電圧源だけのループ、"
            "電圧源とコンデンサの直結、電流源とインダクタの直列などが無いか確認する"
        )
    S_pinv = np.linalg.pinv(S)
    Rex = S_pinv[:, :n] * dE[None, :]
    Ru = (S_pinv[:, n:] * dA[None, :]) @ B_alg
    Rx = Rex @ E

    # --- 台形則 ---------------------------------------------------------------
    M = E / h + G / 2.0
    with warnings.catch_warnings():
        warnings.simplefilter("error", LinAlgWarning)
        try:
            lu = lu_factor(M)
            P = lu_solve(lu, E / h - G / 2.0)
            Q = lu_solve(lu, B)
        except (LinAlgWarning, np.linalg.LinAlgError, ValueError) as exc:
            raise CircuitError(f"時間刻み h={h} で行列が解けない: {exc}") from exc
    if not (np.isfinite(P).all() and np.isfinite(Q).all()):
        raise CircuitError(f"時間刻み h={h} で行列が解けない")

    return Operators(P=P, Q=Q, Rx=Rx, Ru=Ru, Rex=Rex)


def run(
    P: np.ndarray,
    Q: np.ndarray,
    Rx: np.ndarray,
    Ru: np.ndarray,
    x0: np.ndarray,
    u_start: np.ndarray,
    u_end: np.ndarray,
    event: np.ndarray,
    out: np.ndarray,
) -> None:
    """固定刻みの台形則で n_steps 進め、結果を out に書く。

    u_start[k] : 時刻 t[k]   での電源値（不連続点では切り替わった後の値）
    u_end[k]   : 時刻 t[k+1] での電源値（不連続点では切り替わる前の値）
    event[k]   : t[k] で電源が不連続に変わるなら True。状態を保ったまま他の変数を合わせ直す
    out        : (n_steps + 1, n)。out[k] は t[k] の値（不連続点では切り替わった後の値）
    """
    n_steps = u_start.shape[0]
    x = x0.copy()
    out[0] = x
    for k in range(n_steps):
        if event[k]:
            x = Rx @ x + Ru @ u_start[k]
            out[k] = x
        x = P @ x + Q @ (0.5 * (u_start[k] + u_end[k]))
        out[k + 1] = x
