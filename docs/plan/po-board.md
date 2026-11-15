# PO向けの掲示板

最終更新: 2026-11-15

## 現在地

v0.1「見える」に向けて、エンジン側のスイッチング回路（E1a）、画面の設計（A0s）、Windows対応（W1）が終わったところ。残りは、制御ブロックと降圧コンバータのテンプレート（E1b）、アプリの実装（A0、A1）。

## 今回できたこと

- **W1 完了（Windowsで動くこと）**: 本体、確認スクリプト、ハーネスがWindowsで動く。GitHub上の自動テストにWindows（Python 3.11 / 3.13）を加え、832件が通過。日本語Windowsの端末（cp932）でも表示で落ちない。
  - レビューと独立検証は、初回はどちらも差し戻し（ヘッドレス実行の打ち切りがLinuxで効かなくなる後退、Windowsで作業フォルダの偽の `claude` が選ばれる経路など）。直してから、再レビューと再検証を通した。
- **POのPC用の確認スクリプト** `scripts/doctor.py`: OS、Python、git、claude の有無、確認の結果を調べる。`--headless` を付けると、本物の `claude -p` を1回だけ実行して、ハーネスが前提にしている出力の形を記録する。
- **訂正（前回分）**: ヘッドレス実行は、Claudeのプランでサインインしていればサブスクの利用枠から引かれる（D-024）。

## POにお願いしたいこと

PowerShellで次を実行する（`C:\dev\circuit-sim` は空のフォルダ）。結果は `.cycle\doctor-result.json` に書き出され、オーケストレーターがフォルダ越しに読む。

```powershell
cd C:\dev\circuit-sim
git clone https://github.com/HaluHanabusa/circuit-sim .
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python scripts\doctor.py
python scripts\doctor.py --headless
```

- 最後の1行だけが、本物の `claude -p` を1回実行する（サブスクの利用枠を少し使う）。実行前に、何をするかが表示される。
- Python は 3.11 以上が必要。

## POのPCでの確認結果（2026-11-15）

- Windows 11（日本語、既定の文字コード cp932）、Python 3.13.2、git 2.49.0、claude 2.1.227（claude.EXE）。
- `python scripts\doctor.py`: 問題なし。確認は 831 件通過、スキップ4件（Windowsに無い仕組みを試すものと、Windowsでは別の試験が担うもの）。日本語Windowsの実機での確認が取れた。
- `python scripts\doctor.py --headless`: 問題あり。`claude -p` が 0.2 秒で終了コード 1、`is_error` が真、`structured_output` なし、費用 0 で終わった。Claude Code の中で、問い合わせの前に失敗している形（クラウドの作業環境で認証できなかったときと同じ形）。原因の文面は記録しない作りなので、まだ分からない。
- ハーネスの判定は error（合格にしない）で、正しく働いた。実物で分かった点: 失敗のときも `subtype` は `success` で、失敗は `is_error` と終了コードで分かる。
- 成功したときの出力の形（`structured_output` の有無）は、まだ確かめられていない。

## 次にやること

- H2: ハーネスの残課題（土台の付け替えで main の成果が消える経路、Windowsで作業フォルダのファイルが先に動く経路、ほか）
- E1c: E1a の残課題（三相整流器が止まる、スイッチの経路で刻みの注意が出ない、ほか）
- E1b: 制御ブロック、レグ部品と平均モデル、降圧コンバータのテンプレート
- A0 / A1: 画面仕様に沿ったアプリの実装

## 既定で進めた判断

`docs/decisions.md` の D-025〜D-029。主なもの:

- 動作環境はWindowsも対象（D-026、POの方針）
- W1 の基準5のうち「1か所ずつ壊す確認で生き残りが無い」を読み替えた（D-028）。機械的に作った全通りは過大だったため、対象を「W1 で足した分岐と、ゲートの決まりに関わる分岐」に絞り、残りは H2 に送った
- H2 が終わるまで、Windowsではヘッドレス実行をゲートの記録に使わない（D-029）

## POの判断が必要なこと

- 製品名（急がない。公開までに）
- 公開の時期（急がない）

## 止まっていること・リスク

- 本物のCLIでのヘッドレス実行は未確認。上の「POにお願いしたいこと」の結果で確かめる。
- 日本語Windowsの実機と macOS では未確認（自動テストのWindowsは英語環境で、cp932 は設定で模擬している）。
- GitHub上の自動テストは、非公開リポジトリの実行時間を使う（1回およそ6分。Windowsは2倍に数えられる）。無料枠を超えそうなら、Windowsの実行を main への統合時だけに絞る。
- セッション内のエージェントは、POからの発言が無い間は進まない。
