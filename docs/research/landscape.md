# 既存の道具との比較（回路シミュレータ 仮称 circuit-sim）

調べた日: 2026-11-16。各社の公式の製品ページ、文書、リリースノート、PyPI と npm の登録情報を優先した。古い情報には年を書いた。確かめられなかった項目は「未確認」と書いた。価格は、公開されていて確かめられたものだけを書いた（地域で変わる表示は、そう書いた）。出典の記号（[P1] など）は 11章の一覧に対応する。

この文書の前提にした circuit-sim の姿は、リポジトリの `docs/design/requirements.md`（1〜3章、7章）、`docs/plan/roadmap.md`、`docs/design/architecture.md`、`docs/decisions.md`（D-068、D-069）である。D-068（2026-11-16）で、テンプレート方式から自由な回路の記述へ、理想スイッチから寄生容量つきの半導体へと方向が変わった。設計はまだ検討中なので、本文では「いま（main と計画）」と「D-068 の方向」を分けて書いた。

---

## 1. 要約

- circuit-sim は、パワーエレクトロニクスとモーター制御の設計者向けの、ローカルで動く開発中の道具（v0.1 の手前）。いまは理想スイッチの区分線形のエンジンと、降圧コンバータの閉ループ、解析式による初期ゲインまでがある。
- 差別化として掲げる2つ（設計書から仕様を作る / 設計目標からゲインを求めて検証し未達を返す）は、**部品ごとには既にある**。設計目標からゲインを出す機能は MATLAB（PID Tuner、Response Optimizer、systune）と PSIM＋SmartCtrl にある。文書からモデルを組む AI は Dyad 3.0（JuliaHub）と Simulink Agentic Toolkit（MathWorks）が 2026年に出した。データシートから素子のモデルを作る機能は QSPICE の Model Generator などにある。
- 見つからなかったのは、**この2つを、ローカルで、自由なライセンスで、出典と未達を隠さずに一続きに行う道具**である。差別化は「機能」ではなく「つなぎ方と規律」と「入手のしやすさ」に絞られる。
- 劣る点もはっきりしている。開発中で実績が無い。回路を自由に描けない（D-068 で方向は変わったが未着手）。素子と電動機のモデルが少ない。周波数解析と掃引の専用の機能が無い。
- D-069 に当てはめると（○ / △ / × の3段階。7章）、成熟したオープンソースの SPICE エンジンで ○ か △ のものは1つも無い（ngspice も LGPL と GPL の部分を含む）。NumPy と SciPy は、Windows のホイールが △（GCC のランタイム例外、BSL-1.0、Qhull など）、Linux と macOS のホイールが ×（libquadmath が LGPL-2.1+）。D-068 の全部を自前で作ると、全体の 7割前後が自作になる見込み。回路エンジンを絞り、電動機を motulator の取り込み（MIT）で式の水準で解く形なら、自作は 6割前後まで下がる（10章）。

---

## 2. 比較の表

セルは要点だけ。根拠は 3章と 11章の出典。

| 相手 | 1 ライセンスと価格 | 2 主な用途 | 3 計算の方式 | 4 電動機・磁気 | 5 制御とゲインの調整 | 6 周波数解析・掃引 | 7 設計書・AI | 8 動く場所・OS・口 |
|---|---|---|---|---|---|---|---|---|
| **circuit-sim（いま）** | Apache-2.0（暫定）、無料。非公開 | パワエレ・モーター制御の設計（想定） | 理想スイッチの区分線形＋固定刻みの台形則。状態平均化モデルも | 無し（DC モーターは v0.4 予定、誘導機・PMSM は構想）。変圧器・結合インダクタ無し | 離散 PID。解析式の初期ゲイン（位相余裕の目標、降圧のみ）。最適化は v0.2 予定 | 専用の機能は無し（初期ゲインの設計の中で開ループの余裕を計算） | 予定（v0.3）。AI は抽出と説明だけ、計算は決定的 | ローカル（PC 上のサーバー＋ブラウザ）。Python。CLI と Python の口 |
| **circuit-sim（D-068 の方向）** | 同上（D-069 で依存も緩いライセンスに限る） | 上に加えて研究・教育 | 未定（自由な回路、寄生容量つきの半導体） | 大学の水準の素子と電動機（誘導機の励磁など） | 同上 | 未定 | データシートから素子の設定を自動で | 同上 |
| **PLECS**（Plexim） | 商用。永久＋保守1年。Blockset 個人 €7,000、年貸しは永久の 50% [P1]。学術 $2,240〜（表示は地域依存）[P2]。学生は無料（大学の契約が条件、1年）[P3] | パワエレの系の計算。Simulink 上（Blockset）か単体（Standalone）。RT Box で HIL | 理想スイッチの区分線形の状態空間。スイッチの組ごとに行列を作り再利用。可変・固定刻み [P5]。5.0 で SPICE の取り込み（PLECS Spice、別売）[P4] | 誘導機（飽和つき: 主磁束の飽和と d/q の相互飽和）、PMSM（FEM の表引きも）、同期機、SynRM、SRM、BLDC、DC [P6]。磁気の領域、飽和する変圧器 [P7] | 離散 PID（アンチワインドアップ）[P8]。ゲインの自動調整は公式文書で見つからず | 定常解析、AC 掃引、インパルス応答、マルチトーン、線形化した AC 解析、周波数応答解析（5.0）[P9][P10]。掃引はスクリプトで、並列も可 [P11] | 公式の機能は見つからず [P4]。フォーラムで LLM によるモデル生成の話題（2026-03）[P13] | ローカル。Windows / macOS / Linux [P12]。Octave、MATLAB、XML-RPC / JSON-RPC（Python の例あり）[P11] |
| **PSIM**（Siemens。旧 Altair） | 商用。価格は公開なし。学生版あり（中身は公式に未確認）[S8] | パワエレと電動機ドライブ。損失、EMI、C コード生成、設計スイート [S1] | 公式の現行文書で方式の記述は未確認。平均ブロック（2026.1）[S2] | PMSM・SynRM の表引き（Flux 由来）、ステッピング、高周波効果つき誘導機・PMSM [S3]。非線形 L・C（2026）[S2]。飽和の明記は未確認 | アナログ・デジタル制御、C ブロック [S1]。**SmartCtrl**（別製品）: 定義済みトポロジ、PSIM の周波数応答の取り込み、解の地図で設計、PSIM に戻して計算 [S4] | スイッチングモデルのまま AC 掃引 [S5]。モンテカルロ、感度、故障解析 [S1]。HyperStudy で位相余裕の最適化の例 [S9] | 公式は見つからず。第三者の MCP サーバー（MIT）[S10] | ローカル。GUI は Windows のみ（コミュニティの回答）[S7]。Python API（2023.1〜）[S6] |
| **MATLAB / Simulink**＋Simscape Electrical、Motor Control Blockset ほか（MathWorks） | 商用。本体と追加製品。価格の多くは地域別・見積もり [M1] | 制御とモデルベース開発全般。パワエレ・電動機ドライブ | 精度を選べる: 平均、PWM の等価、区分線形の I-V の素子 [M2] | 誘導機・PMSM・同期機を3段階の詳細度で。FEM の PMSM は飽和を表せる [M3][M4] | PID Tuner（帯域・位相余裕）[M6]、Response Optimizer（立ち上がり・整定・オーバーシュート）[M7]、systune（目標ごとの値、1 未満で達成）[M8]、FOC Autotuner（d/q 電流・速度 PI）[M5] | スイッチングモデルの周波数応答推定（sinestream）[M6]、線形化 | Requirements Toolbox（Word / Excel の要求を取り込む）[M10]。MATLAB Copilot（R2025b）[M11]、Simulink Copilot（R2026a）[M12]、Simulink Agentic Toolkit（外部の AI エージェントがモデルを作り、計算を回す）[M14] | ローカル（Windows / macOS / Linux。R2026b）[M17]。Python から MATLAB Engine [M16] |
| **LTspice**（Analog Devices） | 無料 [L1]。利用条件の文書は未確認 | アナログ回路、スイッチング電源 | SPICE 型の非線形 | 電動機モデルは未確認 | 制御の専用ライブラリは未確認 | FRA 部品でスイッチング電源のボード線図（17.1〜）[L2] | 無し | Windows 10/11（x64、ARM64）、macOS [L1]。公式の Python の口は無し（第三者の spicelib は GPL-3）[L4] |
| **QSPICE**（Qorvo） | 無料。商用利用可 [Q1] | 混合信号の回路 | SPICE 型。C++ / Verilog の部品 [Q1] | 未確認 | 未確認 | 未確認 | Model Generator: データシートの図から点を取り、MOSFET・ダイオード・JFET のモデルを作る [Q2][Q3] | Windows 10/11 64bit のみ [Q1] |
| **SIMetrix/SIMPLIS** | 商用（Classic / Pro / Elite、価格は公開なし）[X3]。無料の Elements（規模に上限）[X4] | スイッチング電源 | SIMPLIS は区分線形の時間領域。SIMetrix は SPICE 型 [X2] | 未確認 | 未確認 | POP（周期定常）、スイッチング回路の AC 解析 [X1][X2] | 無し | Elements は Windows のみ [X4]。Python（9.4、2026-05。有償版）[X5] |
| **GeckoCIRCUITS** | GPL-3（公式リポジトリ）[G1] | 教育、パワエレ | MNA＋固定刻み（後退オイラー、台形、Gear）[G2] | 分岐版のサイトに BLDC、PMSM の FOC、誘導機の例 [G2] | 未確認 | 未確認 | 無し | Java。Windows / macOS / Linux [G2]。MATLAB（RMI）、分岐版に REST の口 |
| **OpenModelica**＋Modelica 標準ライブラリ（MSL） | 処理系は AGPLv3 か OSMC-PL [O1]。MSL は BSD-3 [O3] | 多領域の物理モデル | 方程式ベース（DAE）。MSL に理想スイッチ | 誘導機・同期機・DC 機・変圧器。案内では定数パラメータで飽和なし [O4] | MSL の制御ブロック。自動調整は未確認 | linearize（状態空間）[O5] | 無し | ローカル。OMPython（ライセンスは未確認） |
| **ngspice**／PySpice | ngspice は BSD-3 主体だが LGPL-2 と GPL-2+ の部分を含む [N1][N3]。PySpice は GPL-3、最終版 2021 [N5] | 汎用 SPICE | SPICE 型の非線形。XSPICE、Verilog-A（OSDI） | 電動機は未確認。電力用 MOSFET の VDMOS [N4] | XSPICE のコードモデル（詳細は未確認） | SPICE 標準の解析 | 無し | Windows / Linux / macOS [N2]。共有ライブラリの C の口 [N1] |
| **Typhoon HIL** | 実時間 HIL は商用。仮想 HIL は無料（全機能、非実時間）[T2] | HIL 試験 | 未確認 | 未確認 | 未確認 | 未確認 | 無し | Python API は LGPL-3.0-only [T3] |
| **SIMBA**（Aesim.tech） | 商用（価格は公開なし）。無料の DEMO（機能制限）[B3] | パワエレと電動機ドライブ | 未確認 | Motor Library [B2] | 未確認 | AC 掃引、パラメータ掃引（Desktop と Python）[B2] | 無し | Desktop、Python ライブラリ（Windows / macOS / Linux）、Online [B1][B2] |
| **Dyad 3.0**（JuliaHub） | source-available。教育・個人の非商用は無料、商用は有償 [D2] | 多領域の物理モデル | 未確認 | 未確認（パワエレの記載なし）[D1] | 制御器の調整を自動化（記載あり）[D1] | 未確認 | 要求文書や PDF の仕様からエージェントがモデルを組む [D1] | VS Code の拡張 [D2]。ローカルかクラウドかは未確認 |
| **motulator** / **gym-electric-motor** | どちらも MIT [Y2][Y1] | 研究（駆動制御、強化学習） | Python の常微分方程式（連続時間） | motulator: 誘導機・SynRM・PMSM（磁気飽和あり）[Y2]。GEM: PMSM、SynRM、EESM、かご形・二重給電誘導機、DC [Y1] | motulator: 離散時間の制御（V/Hz、ベクトル制御、オブザーバ）[Y2]。GEM: PI の枠組み、強化学習 [Y1] | 未確認 | 無し | Python |

---

## 3. 相手ごとの解説

### 3.1 PLECS（Plexim）

- **ライセンスと価格**: 商用。永久ライセンスに1年の保守が付く。Blockset と Coder は、個人 €7,000、ドングル €9,800、同時使用 €14,000。年貸しは永久の 50%。Standalone の価格はページに無い [P1]。学術向けは Standalone、Blockset、Coder、Spice をまとめた形で、個人1本 $2,240（取得時の表示。通貨は地域で変わる）[P2]。学生向けは無料で1年、大学が Plexim と契約していることが条件 [P3]。
- **用途**: パワエレの系の計算。Simulink の上で動く Blockset と、単体の Standalone がある。RT Box で実時間の計算もできる [P2][P4]。
- **計算の方式**: 理想スイッチの区分線形。スイッチの組ごとに状態空間の行列を作るか、キャッシュから取り出す。可変刻みでは事象の位置を探し、固定刻みでは事象を補間する [P5]。5.0（2026-02-06）で、SPICE のネットリストを取り込む PLECS Spice（Standalone の別売）が入った [P4]。
- **電動機・磁気**: 飽和つきの誘導機は、主磁束の飽和と、d 軸と q 軸の相互飽和を表す [P6]。ほかに PMSM（開放巻線を含む）、同期機（円筒・突極）、SynRM、SRM、BLDC、DC 機がある [P6]。磁気の回路を組む領域と、飽和する変圧器がある [P7][P14]。
- **制御**: 離散 PID はアンチワインドアップ（逆算など）と出力の制限を持つ。公式文書では自動調整の機能は見つからなかった [P8]。
- **周波数解析・掃引**: 定常解析、AC 掃引、インパルス応答、マルチトーン、線形化した AC 解析、5.0 の周波数応答解析（正弦波を加えた時間領域の計算を周波数ごとに並列に回す）[P9][P10]。掃引はスクリプトで回し、並列に計算させられる [P11]。
- **AI**: リリースノートに AI の項目は無い [P4]。フォーラムでは、汎用の LLM にモデルを書き換えさせた投稿（2026-03-12。投稿者が社員かは未確認）と、LLM が読めるネットリストを書き出す要望が出ている [P13]。
- **動く場所**: Windows / macOS / Linux [P12]。スクリプトは Octave、MATLAB、XML-RPC / JSON-RPC（Python 3 の例が文書にある）[P11]。

### 3.2 PSIM（Siemens。旧 Altair）と SmartCtrl

- **ライセンスと価格**: 商用。価格は公開されていない。学生版があり、コミュニティの回答ではコード生成と連成の機能が無いとされる（公式の記述は未確認）[S8]。
- **用途**: パワエレと電動機ドライブ。損失の計算、EMI、C コード生成、モンテカルロ・感度・故障解析、電源（LLC を含む）・電動機ドライブ・EMI・HEV の設計スイート [S1]。
- **計算の方式**: 現行の公式文書で方式の説明は確かめられなかった（未確認）。2026.1 で平均ブロックとインパルス版が入り、FMU の取り込みができるようになった [S2]。
- **電動機**: 2025.0 で Flux/FluxMotor の表引きによる SynRM、ステッピングモーターが入り、表引きの PMSM、高周波効果つきの誘導機と PMSM の資料がある [S3]。2026 で電圧・電流に依存する非線形の L と C [S2]。磁気飽和の扱いの明記は未確認。
- **SmartCtrl**（Power Smart Control 社の製品。Siemens の提携製品として提供）: パワーコンバータの制御段を設計して最適化する。降圧、昇圧、フライバック、フォワード、位相シフトフルブリッジ、DAB の定義済みトポロジがあり、s と z の領域で補償器を組める。PSIM や周波数分析器から周波数応答を取り込み、解の地図（solutions map）で設計し、結果を PSIM に戻して計算する [S4]。2017年の紹介記事では、解の地図が交差周波数と位相余裕の選択を助けるとされる [S11]。時間領域の目標（オーバーシュート、整定時間）を直接の目標にする機能は、確かめた資料には無かった（未確認）。
- **周波数解析**: スイッチングモデルのまま AC 掃引ができ、平均モデルが要らない（Altair のコミュニティの記事、2023年ごろ）[S5]。HyperStudy と組み合わせて位相余裕を最大化する例がある [S9]。
- **AI**: 公式の機能は見つからなかった。第三者の MCP サーバー（MIT）があり、AI エージェントから既存の PSIM 回路を開いて変え、計算を回す。README では新しい回路は作らず、ゲインの調整も無い [S10]。
- **動く場所**: GUI は Windows のみ（コミュニティの回答）[S7]。Python API は 2023.1 から [S6]。

### 3.3 MATLAB / Simulink、Simscape Electrical、Motor Control Blockset（MathWorks）

- **ライセンスと価格**: 商用。本体と追加製品の組み合わせ。価格ページは地域と区分で変わり、多くは見積もり [M1]。大学には全学ライセンス（Campus-Wide License）の形がある（5章）。
- **計算の方式**: Simscape Electrical のコンバータは精度を選べる。平均の模型、PWM の等価の模型、区分線形の I-V を持つ素子の模型などがある [M2]。
- **電動機**: 誘導機、PMSM、同期機を、エネルギー収支・等価回路・FEM の鎖交磁束の3段階で持つ [M3]。FEM の PMSM は電流と回転角に対して非線形の鎖交磁束を持ち、飽和を表せる [M4]。
- **ゲインの調整（この道具の差別化と重なる所）**:
  - スイッチングのある昇圧コンバータを線形化できないので、時間領域の計算で周波数応答を推定し、PID Tuner で帯域と位相余裕を目標にゲインを決め、負荷と入力の急変の計算で確かめる例が公式にある（Simscape Electrical と Simulink Control Design）[M6]。
  - Response Optimizer は、立ち上がり時間、整定時間、オーバーシュートの要求を満たすようにパラメータを最適化する（Simulink Design Optimization）[M7]。
  - systune は目標ごとの値を返し、1 未満なら達成、1 を超えれば未達。どこで、どれだけ外れたかは図で見る [M8]。ただし、確かめた一覧に整定時間の目標は無い [M9]。
  - Motor Control Blockset の FOC Autotuner は、d 軸・q 軸の電流と速度の PI を、帯域と位相余裕を目標に、実機か計算で順に調整する [M5]。
- **設計書・AI**: Requirements Toolbox は Word と Excel の要求を取り込み、モデルや試験とつなぐ [M10]。MATLAB Copilot（R2025b、2025-10-07 発表）はコードの生成と説明 [M11]。Simulink Copilot（R2026a、2026-04-27 発表）はモデルの説明、不具合の原因の提案などで、モデルを自分で作るとは書かれていない [M12][M13]。Simulink Agentic Toolkit は MCP で外部の AI エージェント（Claude Code、GitHub Copilot など）をつなぎ、エージェントがモデルを作り・変え、計算を回す [M14]。リポジトリは 2026-04-15 作成、ライセンスは独自の文書 [M18]。MATLAB MCP Server は MathWorks のソフトウェア使用許諾に従い、複数の利用者で共有しないこととされる [M15]。
- **動く場所**: ローカル。R2026b の動作環境の文書がある（Mac と Linux は別ページ）[M17]。Python から Simulink を呼ぶ公式の例がある [M16]。

### 3.4 LTspice（Analog Devices）

- 無料の SPICE シミュレータ。最新は Windows 10/11 x64 が 26.1.1、macOS と Windows 11 ARM64 が 26.0.2 [L1]。製品ページは商用利用の条件に触れていない。フォーラムでは「無料で、ライセンスを買う必要は無い」という回答がある（回答者が社員かは未確認）[L3]。
- 17.1 以降、FRA（周波数応答解析器）の部品をスイッチング電源に置き、交差周波数と位相余裕を読める [L2]。
- 公式の Python の口は無い。第三者の spicelib / PyLTSpice は GPL-3 [L4]。電動機モデル、制御のライブラリ、ゲインの自動調整は未確認。

### 3.5 QSPICE（Qorvo）

- 商用利用も無料。Windows 10/11 の 64bit のみ。C++ と Verilog のコンパイラを含む [Q1]。
- 2025年に、標準のデータシートの値から JFET、MOSFET、ダイオードのモデルを作る道具が加わった [Q2]。フォーラムによると、データシートの図の上で点を選んで当てはめる方式で、2025-02 時点では正式な公開の前とされた [Q3]。PDF を自動で読むとは書かれていない。

### 3.6 SIMetrix/SIMPLIS

- SIMPLIS はスイッチング電源のための区分線形の時間領域のシミュレータで、周期定常（POP）と AC 解析を持つ [X1][X2]。
- 版は Classic、Pro、Elite（価格は公開なし）と、無料の Elements [X3]。Elements は個人・教育・業務で使えるが、Windows のみで、SIMPLIS は状態変数 15、スイッチ 6 などの上限があり、スクリプトと Python は使えない [X4]。
- 9.4（2026-05-05 発表）で Python から操作できるようになった [X5]。PyPI の simetrix パッケージは同社の使用許諾に従い、検証の自動化の追加機能（DVM）と組む前提 [X6]。
- 電動機、ゲインの自動調整は未確認。

### 3.7 GeckoCIRCUITS

- ETH Zürich で作られた Java のパワエレ用シミュレータ。公式リポジトリは GPL-3 [G1]。教育での利用を述べた論文がある（IPEC 2014）[G3]。
- 分岐版のサイトの FAQ には、MNA と固定刻みの積分、MATLAB との連携（RMI）、Python からの使い方が書かれ、GPL-3 と商用の二重ライセンスと書かれている [G2]。分岐版の位置づけ（元の作者との関係）は確かめていない。

### 3.8 OpenModelica と Modelica 標準ライブラリ

- OpenModelica は AGPLv3、または OSMC-PL で配られる。OSMC-PL の GPL の選択肢は誰でも使え、独自のコードと組む選択肢は会員向けで年会費がある [O1]。最新は 1.26 系（1.26.0 は 2025-12-18）[O2]。
- Modelica 標準ライブラリは BSD-3、最新は 4.1.0（2025-05-23）[O3]。電気機械の案内では、誘導機・同期機・DC 機・変圧器のモデルは定数パラメータで飽和を含まないとされる [O4]。線形化の関数がある [O5]。

### 3.9 ngspice と、Python から使う道具

- ngspice は BSD-3 が既定だが、Debian のパッケージ（47）の著作権表示では、パラメータの処理（numparam）と KLU が LGPL-2、表の部品モデル（table2D/3D など）が GPL-2+ [N3]。FAQ は「修正 BSD」と書き、XSPICE をパブリックドメインとする [N1]。最近の版は 45（2025-09-01）、45.2（2025-09-06）[N2]、マニュアルは 46（2026-03-31）[N4]。電力用 MOSFET の VDMOS モデルを持つ [N4]。
- PySpice は GPL-3 で、最後の版は 1.5（2021-05-15）[N5]。分岐版の InSpice、LTspice 用の spicelib / PyLTSpice も GPL-3（PyPI、2026-11-16 確認）。
- 他の SPICE: Xyce は GPL [N6]、Qucs-S は GPL-2.0 [N7]、ブラウザで動く CircuitJS1 は GPL-2+ [N8]。

### 3.10 実時間シミュレータと Python の道具

- **Typhoon HIL**: 回路図エディタ、HIL SCADA、試験の環境を持つ [T1]。実機の無い仮想 HIL のライセンスは無料で全機能、非実時間 [T2]。Python API は LGPL-3.0-only（1.32.2、2026-11-10）[T3]。
- **SIMBA**: パワエレと電動機ドライブの商用のシミュレータ。Desktop、Python ライブラリ、ブラウザ版があり、電動機ライブラリ、AC 掃引、パラメータ掃引（ブラウザ版を除く）を持つ [B2]。Python ライブラリは Windows / macOS / Linux 向けで、使うにはライセンスの鍵が要る（2026.11.12）[B1]。無料の DEMO は機能制限つき [B3]。
- **motulator**（Aalto 大学の研究室の公開物、MIT）: 誘導機・SynRM・PMSM（磁気飽和を含む）、離散時間の制御、系統連系の変換器 [Y2]。最新 0.25.0（2026-11-15。PyPI）。依存に tqdm（MPL-2.0 AND MIT）と matplotlib を含む [Y3]。
- **gym-electric-motor**（Paderborn 大学、MIT）: 強化学習の環境として、PMSM、SynRM、EESM、誘導機、DC 機と変換器を持つ。3.0.3（2025-12-19）[Y1]。
- **ParaEMT**（NREL、BSD-3）: 電力系統の電磁過渡の計算（2022）[Y4]。対象が系統なので参考に留まる。

### 3.11 設計書・データシートと AI に関わる、その他の動き

- **Dyad 3.0**（JuliaHub、2026-05-27 一般提供）: 要求文書、前の世代の設計、試験データ、言葉の依頼から、エージェントがモデルを組み、計算し、制御器の調整を自動化するとされる。PDF の仕様から機体のモデルを組んで確かめた例を挙げる。分野にパワエレは挙がっていない [D1]。Dyad Studio は source-available で、教育と個人の非商用は無料、商用は有償 [D2]。
- **研究**: SPICEAssistant（Bosch ほか、2025）は、データシートの典型回路を出発点に、LLM が LTspice の計算結果を見ながらスイッチング電源の回路を直す。256 問で解けた割合は 53.0% [R1]。D2S-FLOW（2025）は LLM でデータシートから SPICE モデルのパラメータを取り出す [R2]。PE-GPT（2024）は DAB の変調設計を対話で勧める [R3]。
- **transistordatabase**（Paderborn 大学、GPL-3）: データシートの曲線を人が WebPlotDigitizer で読み取って登録し、PLECS、GeckoCIRCUITS、Simulink などの損失モデルとして書き出す [Y9]。

---

## 4. このツールにしかないもの、足りないもの

### 4.1 このツールにしかないもの（調べた範囲で、ほかに見つからなかった組み合わせ）

機能を1つずつ見ると、どれも既にある（9章）。ほかに見つからなかったのは、次の組み合わせと規律である。

1. 設計書の取り込み → 仕様 → ゲイン → スイッチングモデルでの確認 → 目標ごとの達成・未達、を1つの道具で、人が別の道具でつながずに行う（**計画。v0.2〜v0.3**）。
2. すべての値に単位と出典（設計書の位置、手入力、既定値）を持たせ、不足を推測で埋めず、未達を達成と表示しない、と決まりにしていること（`CLAUDE.md` の決まり4。いまの CLI と仕様 JSON で実装済みの部分がある）。
3. AI を抽出と説明に限り、計算の経路を決定的にすること。AI が無くても仕様 JSON を手で書けば残りは動く。
4. 上の全部を、ローカルで、商用利用できる緩いライセンス（Apache-2.0 予定）で配ること。

### 4.2 このツールに足りないもの（正直に）

- **開発中で、実績が無い**。v0.1 の手前。利用者に確かめた記録も無い（`requirements.md` 2章）。
- **回路を自由に描けない**（いま）。回路は RC ローパス、直列 RLC、降圧コンバータのテンプレートだけ。D-068 で自由な記述へ方向を変えたが、設計は検討中。
- **モデルの種類が少ない**。理想スイッチだけで、デッドタイム 0、損失・熱・寄生成分なし（`requirements.md` X-7）。電動機、変圧器、結合インダクタは無い。PLECS や Simulink が持つ誘導機の飽和、FEM の PMSM などは無い。
- **解析の機能が少ない**。AC 掃引、周波数応答の推定、定常解析、パラメータ掃引の専用の機能は無い。
- **ゲインの最適化は未着手**（E2）。いまあるのは、降圧コンバータの電圧モード PID の初期ゲインを解析式で決める関数だけ。
- **設計書の取り込みは未着手**（I1、I2、v0.3）。
- **実時間の計算、コード生成、HIL は無い**（範囲の外）。
- **画面は作っている途中**（A0、A1）。

---

## 5. アカデミック（研究・教育）での使われ方

分かった範囲だけを書く。採用の数や割合は、確かめられた資料が無いので書かない（未確認）。

| 道具 | 学術向けの扱い | 教科書・教育での採用の例 |
|---|---|---|
| PLECS | 学生は無料（大学の契約が条件、1年、私用の PC で学習目的のみ）[P3]。大学向けの価格表がある [P2] | 教科書 *Simulation of Power Electronics Converters Using PLECS*（Asadi、Eguchi、Academic Press、2019）[A1]。RWTH Aachen の学内のソフトウェアの一覧に載る [A3] |
| PSIM | 学生版がある（中身は公式に未確認）[S8]。Siemens への移行に伴う学生ライセンスの質問がコミュニティにある（2026） | 教科書 *Power Electronics Circuit Analysis with PSIM*（Asadi、Eguchi、De Gruyter Textbook、2021）[A2] |
| MATLAB / Simulink | 大学向けの全学ライセンスの形がある（数は未確認） | 未確認（この調査では一次の資料を確かめていない） |
| LTspice、QSPICE、SIMetrix/SIMPLIS Elements | 無料 [L1][Q1][X4]。Elements は教育での利用を明記 [X4] | 未確認 |
| GeckoCIRCUITS | GPL-3 で無料 [G1] | ETH のオンライン教育での利用の論文（2014）[G3] |
| GSEIM（IIT Bombay） | 無料（ライセンスは未確認） | パワエレ教育向けに作られた。費用のために計算の宿題を出せない大学がある、と動機を述べる（2022）[R4] |
| OpenModelica | 無料（AGPLv3 / OSMC-PL）[O1] | 未確認 |
| Typhoon HIL | 仮想 HIL のライセンスは無料 [T2] | 未確認 |
| motulator、gym-electric-motor | MIT | 研究室の研究と講義の教材として公開（motulator は Aalto の実験台で確かめた制御、と記載）[Y2]。GEM は強化学習の研究 [Y1] |

研究の論文で PLECS と Simulink がよく使われるという印象は広くあるが、数で確かめた資料は見つけていない（未確認）。

---

## 6. 公開用のページに載せる短い比較の表（案）

事実だけを書く。2026-11-16 に、各社の公式のページと文書で確かめた。circuit-sim の行は計画を含むので、そう明記する。

| 道具 | 配布の形 | 回路の持ち方 | 設計目標からゲインを求める機能 | 設計書・データシートからの取り込み | 動く場所 |
|---|---|---|---|---|---|
| circuit-sim（開発中） | Apache-2.0 で無料（予定） | いまはテンプレート（自由な記述は検討中） | 計画（v0.2）。いまは降圧コンバータの初期ゲインの解析式のみ | 計画（v0.3）。AI は抽出だけで、計算は決定的 | ローカル（Windows / macOS / Linux、Python） |
| PLECS | 商用（学生は大学の契約で無料） | 回路図。理想スイッチの区分線形 | 公式文書に自動調整の機能は見当たらない | 公式の機能は見当たらない | ローカル（Windows / macOS / Linux） |
| PSIM＋SmartCtrl | 商用 | 回路図 | SmartCtrl で周波数応答から補償器を設計し、PSIM で確かめる | 公式の機能は見当たらない | ローカル（Windows） |
| MATLAB / Simulink（追加製品） | 商用 | ブロック図と物理モデル | PID Tuner、Response Optimizer、systune など | Word / Excel の要求の取り込み（Requirements Toolbox）。AI エージェントの接続（2026） | ローカル（Windows / macOS / Linux） |
| LTspice | 無料 | 回路図。SPICE | 無し（FRA でボード線図を読む） | 無し | ローカル（Windows / macOS） |
| QSPICE | 無料（商用利用可） | 回路図。SPICE | 未確認 | データシートの図から素子のモデルを作る道具 | ローカル（Windows） |

注: 「見当たらない」は、2026-11-16 時点で公式の文書とリリースノートで見つからなかったことを表す。機能が無いことの保証ではない。

---

## 7. 土台にできる既存のオープンソースの一覧（A）

### 7.1 判定の決まり（3段階）

- **○**: 本体、同梱物、実行時の依存のすべてが MIT、BSD-2/3、Apache-2.0、ISC、PSF、zlib、パブリックドメイン相当だけ。
- **△**: コピーレフトではなく、商用利用を妨げる条件も見当たらないが、同梱物や依存に上の一覧に無いライセンスが混ざる（例: BSL-1.0、HPND、独自の緩い文面、GCC のランタイム例外、LLVM の例外、0BSD）。何が混ざるかをセルに書いた。
- **×**: GPL / AGPL / LGPL / MPL / EPL などのコピーレフト（弱いもの、字体の GPL を含む）、非商用限定、利用条件が不明、確かめられなかったもの。

確かめ方: 公式の LICENSE、PyPI と npm の登録情報（`https://pypi.org/pypi/<名前>/json`、`https://registry.npmjs.org/<名前>`）、ホイールと npm の配布物の中のライセンスの文書（実際にダウンロードして読んだ）。すべて 2026-11-16。純 Python のパッケージは、登録情報と依存の宣言で判定した（配布物の中までは開いていない）。

### 7.2 先に PO の判断が要る点

**(1) NumPy と SciPy は、配布物の OS によって判定が変わる。Linux と macOS のホイールは × に当たる。**

| 配布物 | 同梱物（ホイールの `LICENSE.txt` と同梱の文書で確認） | 判定 |
|---|---|---|
| NumPy 2.3.4、Windows（win_amd64） | OpenBLAS（BSD-3）、LAPACK（BSD-3-Clause-Open-MPI）、GCC の実行時ライブラリ（GPL-3.0-or-later WITH GCC-exception-3.1）[Y10] | △（GCC のランタイム例外、BSD-3-Clause-Open-MPI） |
| NumPy 2.3.4、Linux（manylinux_2_28 x86_64）と macOS（14.0 arm64） | 上に加えて libquadmath（LGPL-2.1-or-later）[Y10] | × |
| SciPy 1.18.1、Windows | OpenBLAS、LAPACK、GCC の実行時ライブラリ。ソースの同梱物に Qhull（独自の文面。ホイールに `COPYING_QHULL.txt`）、Boost と ryu（BSL-1.0）、xsf（BSD-3 AND MIT AND BSD-3-Clause-LBNL AND Apache-2.0 WITH LLVM-exception）[Y11][Y18] | △（Qhull の独自の文面、BSL-1.0、GCC のランタイム例外、LLVM の例外） |
| SciPy 1.18.1、Linux と macOS | 上に加えて libquadmath（LGPL-2.1-or-later）[Y11] | × |

- いまの main は numpy と scipy に依存している（`pyproject.toml`）。先の検討（`/home/claude/academic/academic-extension.md` 11.3）は SciPy の同梱物を「コピーレフトは無い」とした。これはソースの同梱物の一覧（`LICENSES_bundled.txt`）による判断で、**Linux と macOS のホイールが追加で同梱する libquadmath（LGPL-2.1+）は含んでいない**。3段階の決まりでは、Linux と macOS のホイールは × になる。
- 決めることの例: (a) △ の範囲（GCC のランタイム例外、BSL-1.0、Qhull など）を許すか。(b) 配布物が動的に読み込む LGPL の実行時ライブラリ（libquadmath）を許すか。許さないなら、NumPy と SciPy を libquadmath なしで組み直すことになり、現実的でない。
- 以下の表で NumPy や SciPy に依存するものは、本体の判定に「＋NumPy / SciPy の判定に従う」と添えた。

**(2) 字体と小さな例外の扱い**: 字体の OFL（matplotlib）、字体の GPL-2＋例外（pdf.js の Liberation）、0BSD（tslib）、Apache-2.0 WITH LLVM-exception（llvmlite、mousetrap）。

### 7.3 回路の計算エンジン

| 候補 | ライセンス（確かめた所） | 最近の版 | 言語 | 判定 | 理由 |
|---|---|---|---|---|---|
| ngspice | 既定は BSD-3。numparam と KLU が LGPL-2、table2D/3D などが GPL-2+ [N3]。XSPICE はパブリックドメイン [N1] | 45.2（2025-09-06）[N2]。マニュアル 46（2026-03-31） | C | × | LGPL と GPL の部分を含む。numparam（.param の処理）を外して組めるかは未確認 |
| Xyce | GPL [N6] | 7.10 | C++ | × | GPL |
| Qucs-S | GPL-2.0 [N7] | 25.x | C++ | × | GPL |
| CircuitJS1（Falstad） | GPL-2+ [N8] | — | Java（GWT） | × | GPL |
| GeckoCIRCUITS | GPL-3 [G1] | — | Java | × | GPL |
| OpenModelica | AGPLv3 / OSMC-PL [O1] | 1.26 系 | C ほか | × | AGPL |
| Modelica 標準ライブラリ | BSD-3 [O3] | 4.1.0（2025-05-23） | Modelica | × | 同梱の C の資源（表の読み込みなど）のライセンスを確かめていない。しかも実行には Modelica の処理系が要る。式の参考にはなる |
| ModelingToolkit.jl ＋ 標準ライブラリ | MIT [Y15] | 11.47.0 | Julia | × | 本体は MIT だが、Julia の配布物が SuiteSparse の GPL-2+ と LGPL-2.1+、GMP / MPFR の LGPL を含む [Y14] |
| PySpice / InSpice | GPL-3 [N5] | 1.5（2021）/ 1.7.0.7（2026-09） | Python | × | GPL |
| spicelib / PyLTSpice | GPL-3 [L4] | 1.6.4（2026-10-04） | Python | × | GPL |
| Lcapy | LGPLv2+ | 1.26（2025-09-28） | Python | × | LGPL |
| SLiCAP | MIT | 6.4.7（2026-11-10） | Python | × | 依存の svglib が LGPL-3.0-or-later。ほかに matplotlib |
| ahkab | GPL | 0.18（2015） | Python | × | GPL、保守止まり |
| spicey（tscircuit） | npm に license 欄なし | 0.0.14 | JS | × | 不明 |

**数値計算の土台**

| 候補 | ライセンス | 最近の版 | 判定 | 理由 |
|---|---|---|---|---|
| NumPy | 7.2 の表 | 2.3.4 を確認 | Windows △ / Linux・macOS × | 7.2 |
| SciPy（signal、optimize、sparse、integrate） | 7.2 の表 | 1.18.1 を確認 | Windows △ / Linux・macOS × | 7.2 |
| SUNDIALS（IDA、CVODE） | BSD-3 [Y7] | 7.9.0（2026-09） | ○（ソースから組む場合） | 組み方しだいで同梱物が増える（ホイールにすると 7.2 と同じ問題） |
| scikit-sundae | BSD-3。Linux のホイールは libgfortran と libquadmath（LGPL-2.1+）、Windows のホイールは Microsoft の OpenMP 実行時ライブラリ（vcomp140.dll）を同梱 [Y17] | 1.2.0（2026-10-16） | × | libquadmath（Linux）。vcomp140.dll の再配布の条件は確かめていない（Windows） |
| SymPy（＋mpmath） | BSD / BSD-3 | 1.14.0 | ○ | 純 Python、依存も BSD |
| cma（CMA-ES） | BSD-3、依存は numpy だけ | 4.6.0（2026-11-15） | 本体 ○＋NumPy の判定に従う | — |
| Numba（＋llvmlite） | Numba は BSD。llvmlite は BSD-2-Clause AND Apache-2.0 WITH LLVM-exception | 0.68.1 / 0.50.0 | △＋NumPy の判定に従う | LLVM の例外。llvmlite のホイールの中の同梱物は開いていない |
| CasADi | LGPLv3+ | 3.8.2 | × | LGPL |
| Optuna | MIT | 5.1.0 | × | 依存の tqdm が MPL-2.0 AND MIT |
| pymoo | Apache-2.0 | 0.6.2 | × | 依存に matplotlib（7.7） |
| JAX / diffrax | Apache-2.0 | 0.12.0.1 / 0.7.2 | × | jaxlib の同梱物を確かめていない |

### 7.4 パワエレ・電動機ドライブのシミュレーション

| 候補 | ライセンス | 最近の版 | 言語 | 判定 | 理由 |
|---|---|---|---|---|---|
| motulator（依存として入れる） | MIT [Y2] | 0.25.0（2026-11-15） | Python | × | 実行時の依存に tqdm（MPL-2.0 AND MIT）と matplotlib [Y3] |
| motulator（コードを表示つきで取り込む） | MIT | 同上 | Python | ○（条件つき）＋SciPy の判定に従う | 取り込む部分が matplotlib と tqdm を import しないことが条件（未確認。先の検討の判定と同じ）。連続時間は SciPy の solve_ivp で解く（先の検討 [S3]）ので、SciPy の扱いに従う |
| gym-electric-motor（依存として入れる） | MIT [Y1] | 3.0.3（2025-12-19） | Python | × | 実行時の依存に matplotlib、pathspec（MPL-2.0）、pytest、gymnasium |
| gym-electric-motor（コードを取り込む） | MIT | 同上 | Python | ○（条件つき）＋NumPy / SciPy の判定に従う | 取り込む部分が上の依存を import しないことが条件（未確認） |
| ParaEMT | BSD-3 [Y4] | 2022-03 | Python | × | 依存を確かめていない。対象も電力系統の電磁過渡 |
| transistordatabase | GPL-3 [Y9] | 0.5.1（2024-06-22） | Python | × | GPL |
| GeckoCIRCUITS、OpenModelica | 7.3 のとおり | — | — | × | — |

### 7.5 制御系の解析

| 候補 | ライセンス | 最近の版 | 判定 | 理由 |
|---|---|---|---|---|
| python-control | BSD-3 [Y5] | 0.10.2（2025-07-05） | × | matplotlib が必須の依存（7.7）。任意の slycot は GPL-2.0 を含む [Y6] |
| harold | MIT | 1.0.3（2022-12） | × | 依存に matplotlib。保守止まり |
| ControlSystems.jl | MIT [Y16] | — | × | Julia の配布物（7.3） |
| SciPy.signal | 7.2 のとおり | — | Windows △ / Linux・macOS × | 伝達関数、離散化、周波数応答は SciPy の中にある |

### 7.6 回路図エディタと図の部品（ブラウザ）

| 候補 | ライセンス（依存を含む） | 最近の版 | 言語 | 判定 | 理由 |
|---|---|---|---|---|---|
| React Flow（@xyflow/react） | MIT。依存の zustand、classcat、@xyflow/system は MIT、d3 の部品は ISC | 12.12.1（2026-11-02） | TS | ○ | ノードと配線の編集の土台。回路の記号、配線の規則、ネットリストへの変換は自作 |
| maxGraph（@maxgraph/core） | Apache-2.0、依存なし | 0.25.1（2026-10-21） | TS | ○ | 図の編集の土台。版が 0.x |
| Rete.js | MIT（@babel/runtime も MIT） | 2.0.6（2025-06-30） | TS | ○ | ノードの編集の土台 |
| Konva | MIT、依存なし | 10.9.1 | TS | ○ | 描画の低い層 |
| @dagrejs/dagre | MIT（graphlib も MIT） | 3.1.3 | JS | ○ | 自動配置 |
| schemdraw | MIT、必須の依存なし | 0.23（2026-05-29） | Python | ○（matplotlib を使わない形で） | 回路図を描くだけで編集はできない（レポート用の静的な図） |
| @antv/x6 | MIT。依存の mousetrap が Apache-2.0 WITH LLVM-exception | 3.1.8 | TS | △ | LLVM の例外 |
| tscircuit | リポジトリは MIT。npm の一部（spicey、spicets、kicadts）に license 欄なし | 0.0.3197 | TS | × | 不明が混じる。PCB の設計が主 |
| JointJS（@joint/core） | MPL-2.0 | 4.3.4 | JS | × | MPL |
| elkjs / netlistsvg | EPL-2.0 OR GPL-3.0+ / netlistsvg は MIT だが elkjs に依存 | 0.12.1 / 1.0.2（2020） | JS | × | EPL・GPL |
| Sprotty、Eclipse GLSP | EPL-2.0 OR GPL-2.0 WITH Classpath-exception | — | TS | × | EPL |
| CircuitJS1 | GPL-2+ | — | — | × | GPL |

### 7.7 波形の表示

| 候補 | ライセンス | 最近の版 | 判定 | 理由 |
|---|---|---|---|---|
| uPlot | MIT、依存なし | 1.6.32（2025-03-14） | ○ | いま使っている |
| webgl-plot | MIT、依存なし | 1.1.2 | ○ | 大量の点の描画 |
| ECharts | Apache-2.0。zrender は BSD-3、tslib は 0BSD | 6.1.0 | △ | 0BSD（パブリックドメイン相当と見れば ○） |
| plotly.js | MIT | 4.3.0（2026-11-10） | × | 同梱物を確かめていない |
| matplotlib | PSF を元にした独自の文面。同梱に STIX、AMS、Last Resort の字体（OFL 1.1）、FreeType（FTL か GPL-2 の選択）、Turbo の色の表（文面なし）ほか [Y8] | 3.11.2 | × | 字体の OFL、文面の無い同梱物 |
| Bokeh | BSD-3 | 3.10.2 | × | 依存の Pillow（下） |
| Pillow（Bokeh、pdfplumber などの依存） | 本体は MIT-CMU（HPND）。ホイールに FreeType（FTL か GPL-2）、XZ（LGPL-2.1+ の getopt_long を含むと記載）ほか [Y13] | 13.0.0 | × | XZ の LGPL 部分がホイールのバイナリに入るかを確かめていない。入っていなければ △（HPND、FTL など） |
| pyqtgraph | MIT | 0.14.0 | × | Qt の束縛（PyQt / PySide）のライセンスを確かめていない |

### 7.8 文書の読み取り

| 候補 | ライセンス（依存を含む） | 最近の版 | 判定 | 理由 |
|---|---|---|---|---|
| openpyxl（＋et-xmlfile） | MIT | 3.1.5（2024-06-28） | ○ | Excel（.xlsx）。純 Python |
| xlrd | BSD | 2.0.2（2025-06-14） | ○ | 古い Excel（.xls）。純 Python |
| pypdf（＋typing_extensions） | BSD-3、PSF-2.0 | 7.1.0（2026-11-13） | ○ | PDF の文字の取り出し。純 Python |
| mammoth（＋cobble） | BSD-2、cobble は BSD（PyPI の分類） | 1.13.0（2026-09-26） | ○ | Word（.docx）→ HTML / 文字。純 Python |
| pdf.js（pdfjs-dist、配布物のまま） | 本体は Apache-2.0。同梱に Liberation の字体（GPL-2＋字体の例外）、Foxit の字体（BSD 系）、cmaps（Adobe の BSD 系の文面）、iccs（CC0）、wasm（OpenJPEG BSD-2、QCMS MIT、QuickJS MIT、JBIG2 BSD 系）[Y19] | 6.5.438 | × | Liberation の字体が GPL-2 |
| pdf.js（standard_fonts を外して同梱） | 上から Liberation を除く | 同上 | △ | Adobe の cmaps の文面、CC0。字体を外したときの表示の劣化は確かめていない |
| pdfminer.six | MIT。依存の cryptography は Apache-2.0 OR BSD-3 | 20260107 | × | cryptography のホイールの同梱物を確かめていない |
| python-docx | MIT | 1.2.0 | × | 依存の lxml のホイールが iconv（LGPL-2.1）を同梱 [Y12] |
| pdfplumber | MIT | 0.11.10 | × | 依存の Pillow と pypdfium2（「依存のライセンス」を含むと自己申告） |
| PyMuPDF | AGPL-3.0 か商用 | 2.1 | × | AGPL |
| MarkItDown | MIT | 0.1.9 | × | 依存の certifi が MPL-2.0 |
| Docling | MIT | 2.150.1 | × | 依存に torch、transformers、学習済みモデル。同梱物を確かめきれない |
| unstructured | Apache-2.0 | 0.27.37 | × | 依存が多く、確かめきれない |
| Camelot | MIT | 2.0.0 | × | 依存の opencv-python-headless と pypdfium2 の同梱物 |
| tabula-py | MIT | 2.11.0 | × | Java の実行環境が要り、そのライセンスを確かめていない |
| Tesseract（pytesseract） | pytesseract は Apache-2.0 | 0.3.13 | × | 本体と同梱物を確かめていない（スキャン PDF は範囲の外 X-2） |

### 7.9 アプリの土台

| 候補 | ライセンス（依存を含む） | 判定 | 理由 |
|---|---|---|---|
| FastAPI、Starlette、anyio（＋idna、sniffio）、uvicorn（＋click、h11）、typing_extensions、opentelemetry-api | MIT、BSD-3、MIT OR Apache-2.0、PSF-2.0、Apache-2.0（PyPI の登録情報） | ○ | 純 Python |
| Pydantic（＋pydantic-core、annotated-types、typing-inspection） | MIT。pydantic-core は Rust で組まれたバイナリで、ホイールには MIT の文書が1つあるだけ | × → 要確認 | 組み込まれた Rust のクレートの一覧とライセンスがホイールに無く、確かめていない。ソースの Cargo.lock で確かめられる見込み。いまの main の依存 |

---

## 8. 「既存の道具の拡張として作る」選択肢（B）

| 形 | 中身 | 利点 | 欠点 | ライセンス・利用条件 | 利用者の入手のしやすさ |
|---|---|---|---|---|---|
| B1 独立した道具（いまの方針） | 自前のエンジン、画面、ゲインの設計、取り込み | 決まり（出典、未達、決定性）を端から端まで守れる。ライセンスを自分で決められる | 自作が最も多い。自由な回路と寄生容量つきの素子を D-069 のもとで作ると、エンジンがほぼ全部自作（10章） | 自由に決められる。ただし土台の NumPy / SciPy が Linux・macOS で ×（7.2） | 無料で入れられる（予定） |
| B2 SPICE エンジンの前後に付ける Python のライブラリ | 利用者が入れた ngspice を別のプロセスで呼ぶ。ネットリストの生成と結果の読み取りを自作（PySpice は GPL なので使わない） | 素子のモデル（VDMOS、BSIM、Verilog-A）がそのまま使える | ngspice は ×（LGPL と GPL の部分。7.3）。同梱もできない。SPICE 型は区分線形より遅いことが多い（この調査では測っていない） | 別のプロセスで呼ぶ形でも、D-069 の文言では対象外 | 無料。Windows / Linux / macOS |
| B3 無料の商用の道具を外部の計算機として使う（LTspice、QSPICE） | 利用者が入れた道具に、ネットリストを渡して計算させ、結果を読む | 無料で、設計者に馴染みがある。素子モデルが多い。QSPICE にはデータシートからモデルを作る道具がある | プロプライエタリで、同梱も再配布もできない。LTspice の利用条件の文書は未確認。QSPICE は Windows のみ。公式の Python の口が無い（コマンドラインとファイル） | 各社の利用条件に従う | 無料（入れるのは利用者） |
| B4 PLECS の拡張 | XML-RPC / JSON-RPC で、Python から計算と解析を呼ぶ [P11] | 区分線形の成熟したエンジン、電動機・磁気、AC 掃引と周波数応答解析が使える | 利用者に PLECS が要る。1つの接続で同時に走る命令は1つ [P11] | Plexim の使用許諾（詳細は未確認） | 商用は €7,000〜（Blockset 個人）。学生は大学の契約で無料 [P1][P3] |
| B5 Simulink の拡張 | MATLAB Engine for Python [M16]、Simulink Agentic Toolkit [M14]、MATLAB の追加機能として配る | 周波数応答推定、PID Tuner、Response Optimizer、systune が既にある | 要る製品が多い（MATLAB、Simulink、Simscape Electrical、Simulink Control Design、Simulink Design Optimization）。差別化の多くが MathWorks の製品と重なる | MathWorks の使用許諾。MCP サーバーは複数の利用者で共有しない [M15] | 価格の多くは見積もり [M1]。大学では全学ライセンスがある所も |
| B6 PSIM の拡張 | psimapipy で計算と回路図を操作 [S6] | 電動機ドライブの例が多い。SmartCtrl と組める | Windows のみ（GUI）[S7]。SmartCtrl と重なる | Siemens の使用許諾 | 商用（価格は公開なし） |
| B7 SIMBA の Python ライブラリの上 | Python から回路を作って計算 [B1] | Python が主の口。Windows / macOS / Linux。電動機ライブラリ、AC 掃引 | 商用ライセンスの鍵が要る。DEMO は機能制限 [B3] | 同社の使用許諾 | 商用（価格は公開なし） |
| B8 Typhoon HIL の上 | 無料の仮想 HIL と Python API | 実時間 HIL への道がある | Python API が LGPL-3.0-only で ×（7.1）[T3] | — | 仮想 HIL は無料 [T2] |
| B9 OpenModelica / Julia（ModelingToolkit）の上 | 方程式ベースのモデルに、ゲインの設計と取り込みを足す | 多領域。MSL（BSD-3）と MTK（MIT）は緩い | 処理系（AGPL）か配布物（Julia の GPL / LGPL）が ×（7.3）。言語が変わる | — | 無料 |
| B10 式の水準の電動機ドライブのライブラリを土台にする（motulator、gym-electric-motor） | MIT のコードを、著作権の表示つきで取り込む（依存としては入れない）。電動機・変換器・制御を式で書いて常微分方程式で解く形をそのまま使い、その上に「設計目標 → ゲイン → 確認 → 未達」と設計書の取り込みを載せる。先の検討（`academic-extension.md` 12.2）の「道2」 | motulator は誘導機・SynRM・PMSM（磁気飽和、深溝のかご）、電圧形変換器、ダイオード整流、LCL、系統、離散時間の制御、観測器、グリッドフォーミングまで持つ [Y2]。研究室の実験台で確かめた制御が元。言語が同じ Python。研究・教育の教材の形に近い | 依存として入れられない（motulator は tqdm の MPL-2.0 と matplotlib、GEM は matplotlib と pathspec の MPL-2.0）ので、取り込んだコードは自分で保守し、上流の更新を手で追う。回路のネットリストを持たないので、半導体の寄生容量と電動機を同じ回路で見られない（変換器は理想スイッチか平均の式）。設計目標からゲインを決めて確かめる機能と、設計書から値を取る機能は持たない（先の検討の 12.3）。GEM は強化学習の環境の形で、取り込む利点は小さい（同 11.1） | 取り込む部分は ○（matplotlib・tqdm を import しないことが条件で未確認）。解法に SciPy を使うので、SciPy の判定（Linux・macOS で ×）に従う | 無料。Python 3.12 以上（motulator）|

**組み合わせの形**: 「設計を閉じる層」（取り込み → 仕様 → ゲイン → 確認 → 未達）を独立した Python のライブラリとし、計算の裏側を差し替え式にする形がとれる。自前の区分線形のエンジン（変換器の回路）と、motulator から取り込んだ式の水準の電動機ドライブ（B10）を既定にし、利用者が既に持っている道具（PLECS、Simulink、LTspice など）を、利用者の側で入れた外部の計算機としてつなぐ。いまの設計の「層A（仕様）と層B（ネットリスト）を分ける」形と相性がよい。ただし B2〜B9 のつなぎ先は、それぞれの道具のライセンスと使用許諾の確認が別に要り、D-069 の文言では外部の道具の利用をどこまで許すかが決まっていない（PO の判断が要る）。

---

## 9. 差別化の検証（C）

探した範囲: 2章の各社の公式のページ、文書、リリースノート（PLECS、PSIM と SmartCtrl、MathWorks、LTspice、QSPICE、SIMetrix/SIMPLIS、GeckoCIRCUITS、OpenModelica、ngspice、Typhoon HIL、SIMBA、Dyad）、PyPI と npm、arXiv、各社のフォーラム。**探していないもの**: Synopsys Saber、Cadence、Ansys Twin Builder、Siemens Simcenter Amesim、Wolfram System Modeler、Keysight PEPro、OPAL-RT、RTDS、imperix、PowerSim 以外の SPICE 系の商用品（PSpice など）、各社の有償のコンサルティング。

### 9.1 設計書やデータシートから、回路のモデルや素子の設定を自動で作る

**部分的にある。** 「設計書の全体から回路のモデルを組む」と「データシートから素子のモデルを作る」に分けると次のとおり。

| 道具・研究 | どこまでできるか | 価格・利用条件 |
|---|---|---|
| Dyad 3.0（JuliaHub） | 要求文書、PDF の仕様、言葉の依頼から、エージェントがモデルを組む、と発表 [D1]。パワエレは挙がっていない | 教育・個人の非商用は無料、商用は有償。source-available [D2] |
| Simulink Agentic Toolkit ＋ Requirements Toolbox（MathWorks） | 外部の AI エージェントが Simulink のモデルを作り・変え、計算を回す [M14]。Word / Excel の要求を取り込んでつなぐ [M10]。設計書から定数を出典つきで抜き出す専用の機能は確かめていない | MATLAB と追加製品が要る。AI エージェント（多くはクラウドの LLM）は別に要る |
| QSPICE Model Generator（Qorvo） | データシートの図から点を取り、MOSFET・ダイオード・JFET のモデルを作る。PDF を自動で読むとは書かれていない [Q2][Q3] | 無料、Windows のみ |
| transistordatabase（Paderborn） | 人がデータシートの曲線を読み取って登録し、PLECS などの損失モデルとして書き出す [Y9] | GPL-3 |
| 第三者の PSIM 用 MCP サーバー | AI エージェントが既存の PSIM 回路を開いて変える。新しい回路は作らない（README）[S10] | MIT。PSIM のライセンスが要る |
| 研究: D2S-FLOW、SPICEAssistant | LLM でデータシートから SPICE のパラメータを取り出す（F1 0.92）[R2]。データシートを参照しながら LTspice の回路を直す（解けた割合 53%）[R1] | 論文。道具としての公開とライセンスは未確認 |
| 汎用の LLM（ChatGPT など） | PLECS のモデルを言葉の指示で書き換えた投稿がある [P13] | 各社の条件 |

**見つからなかったもの**: Word / PDF / Excel の設計書から、値ごとに出典（ページ、表、行）を付け、記載の無い値を推測で埋めずに残し、パワエレの回路のモデルの定数を埋める道具。

### 9.2 設計目標から制御のゲインを求め、スイッチングの水準で検証し、未達の量を返す

**商用には、ほぼある。** 一続きの専用の機能ではないが、組み合わせで同じことができる。

| 道具 | どこまでできるか | 価格・利用条件 |
|---|---|---|
| MATLAB / Simulink（Simscape Electrical、Simulink Control Design、Simulink Design Optimization、Control System Toolbox） | スイッチングのモデルから周波数応答を推定し、帯域と位相余裕を目標に PID を決め、スイッチングのモデルの計算で確かめる例が公式にある [M6]。立ち上がり、整定、オーバーシュートの要求でパラメータを最適化 [M7]。systune は目標ごとに達成（1 未満）か未達（1 超）を返し、どこでどれだけ外れたかを図で示す [M8] | 商用。複数の追加製品。人が手順をつなぐ |
| Motor Control Blockset の FOC Autotuner | d/q の電流と速度の PI を、帯域と位相余裕を目標に調整（実機か計算）[M5] | 商用 |
| PSIM ＋ SmartCtrl | PSIM の AC 掃引（スイッチングモデルのまま）[S5] を取り込み、解の地図で補償器を設計し、PSIM に戻して計算 [S4] | 商用。Windows |
| PSIM ＋ HyperStudy | AC 掃引の結果から位相余裕を目的にして最適化する例 [S9] | 商用 |
| Dyad 3.0 | 制御器の調整を自動化、と発表 [D1]。パワエレへの適用は未確認 | 商用（非商用は無料） |
| 緩いライセンスのオープンソース | 見つからなかった。python-control は解析と根軌跡の PID 設計の道具を持つが、時間領域の目標からの最適化とスイッチングの計算での確認は持たない [Y5]（しかも matplotlib が必須の依存で ×）。motulator と gym-electric-motor は制御の部品を持つが、設計目標からゲインを決めて確かめる機能は持たない（先の検討 `academic-extension.md` 12.3） | — |

**見つからなかったもの**: 1つの道具の中で、時間領域の目標（オーバーシュート、整定時間）と周波数領域の目標（位相余裕）を同時に入れ、平均モデルで探し、スイッチングモデルで確かめ、両モデルの差を隠さず、目標ごとに「目標いくつに対して、いくつで、どれだけ足りないか」を、設計者の言葉で報告にまとめる機能。MATLAB は値と図で示すが、手順をつなぐのは利用者である。

### 9.3 上の2つを、ローカルで、自由なライセンスで、一続きに行う

**見つからなかった。** 9.1 と 9.2 の両方を持つのは Dyad 3.0（ライセンスは source-available、商用は有償、パワエレは未確認）と、MATLAB の組み合わせ（商用、AI はクラウドの LLM を外からつなぐ形）だけだった。MIT / BSD / Apache-2.0 のような緩いライセンスで、ローカルに閉じて、この2つを一続きに行うものは、探した範囲には無かった。

### 9.4 判断

- 差別化の2つは、**機能としては差別化にならない**。特に「設計目標 → ゲイン → スイッチングでの確認」は、MATLAB と PSIM＋SmartCtrl が既に持つ。
- 差別化として残るのは、(1) **緩いライセンスでローカルに閉じた一続き**、(2) **出典を持ち、推測で埋めず、未達を隠さない規律**、(3) **MATLAB や PSIM を持たない設計者・学生が無料で使えること**。この3つは調べた範囲で他に無いが、どれも「組み合わせと作り方」の差で、時間がたてば MathWorks の AI エージェントの接続（2026）のような動きで縮む可能性がある。

---

## 10. 見立て（D）

### 10.1 既存のもので済む割合と、自作が要る部分

粗い見積もり。重みは、この道具の全体の作業量に占める割合の見込みで、根拠は `architecture.md` の構成と、7章の判定。

前提: NumPy と SciPy は使える（7.2 の (1) を PO が △ の範囲で許し、Linux と macOS の libquadmath も許す）とした。許さない場合は、数値計算の土台から自作になり、どの形も現実的でない。取り込んだ MIT のコード（motulator）は「既存」に数えた。

| 部分 | 全体に占める重み（見込み） | 既存で済む割合（○ と、許した場合の △） | 使える既存のもの | 自作が要るもの |
|---|---|---|---|---|
| 回路の計算エンジン（自由な回路、MOSFET・IGBT の寄生容量、非線形） | 25〜30% | 10〜15% | SciPy（疎行列、LU）、SUNDIALS（ソースから組む）、SymPy | 素子モデル、非線形の解き方、事象の処理、回路方程式の組み立て、検証のすべて。○ か △ の SPICE エンジンが無い（7.3） |
| 電動機ドライブ（誘導機の励磁と飽和、PMSM、機械系） | 10〜12% | 道1: 約 3 割 / 道2: 6〜7 割 | motulator の式とコードの取り込み、教科書の式 | 道1（回路エンジンの中で駆動する）は結合と検証がほぼ全部自作。道2（式の水準で別に解く）は同期機・DC 機の一部とこの道具の決まりへの合わせ込み（先の検討の 12.1、12.2） |
| ゲインの設計・確認・未達の報告 | 15% | 20〜30% | SciPy（signal、optimize）、cma | 評価関数、目標の部品、線形化、余裕、未達の計算、判定 |
| 設計書の取り込みと AI 抽出 | 15% | 30〜40% | openpyxl、xlrd、pypdf、mammoth（どれも ○） | 位置ラベル、表の構造、正規化、出典、AI への依頼と検証。AI 自体は外部。pdf.js は字体を外せば △ |
| データシートからの素子の設定 | 10% | 0〜10% | 見つからず（QSPICE はプロプライエタリ、transistordatabase は GPL、D2S-FLOW は論文） | ほぼ全部 |
| 回路図エディタ | 10% | 30〜50% | React Flow か maxGraph、dagre、Konva（どれも ○） | 記号、配線の規則、ネットリストとの往復 |
| 波形とアプリの枠 | 10% | 50〜70% | uPlot、FastAPI、uvicorn、React（○）。Pydantic は要確認（7.9） | スコープの機能（カーソル、比較）、画面の文言 |

場合ごとの合計（加重。粗い）:

| 場合 | 既存 | 自作 | 主な自作 |
|---|---|---|---|
| (i) D-068 の全部を自前で（自由な回路と寄生容量、電動機も同じ回路で駆動。道1） | 25〜30% | 70〜75% | 回路エンジンと電動機の結合、データシートからの設定 |
| (ii) 回路エンジンは区分線形に寄生容量を足す程度に絞り、電動機は motulator を取り込んで式の水準で別に解く（道2、B10） | 35〜45% | 55〜65% | 「設計を閉じる層」（ゲイン、確認、未達、取り込み）。回路エンジンの自作が減る |
| (iii) (ii) に加えて、SPICE の水準は利用者が入れた外部の道具（ngspice など）に任せる（B2。D-069 の解釈しだい） | 45〜55% | 45〜55% | 「設計を閉じる層」と、外部の道具との往復 |
| (iv) 商用の道具（PLECS、Simulink）の拡張にする（B4、B5） | 65〜75% | 25〜35% | 「設計を閉じる層」だけ。ただし利用者に商用のライセンスが要り、「無料で、緩いライセンスで」が消える。9.2 の機能と重なる |

どの場合でも、自作として残る中心は「設計目標 → ゲイン → 確認 → 未達」と「出典つきの取り込み」で、これが差別化の部分にあたる。電動機と制御の式、数値の部品は既存で足り、差別化にならない（先の検討の 12.3 と同じ結論）。

### 10.2 独立した道具として作る理由は残るか、どこに絞るか

以下は調べた事実をもとにした見立てで、決定ではない。

1. **理由は残るが、狭い**。残るのは 9.4 の3点（緩いライセンスでローカルに閉じた一続き、出典と未達の規律、無料で入手できること）。「自由に回路を描けて、寄生容量つきの素子と電動機を大学の水準でひと通り計算できる」ことは、LTspice・QSPICE（無料）、PLECS・Simulink（商用）が最も強い所で、ここで並ぶには自作が最も多く、D-069 のもとでは SPICE エンジンを持ち込めない。
2. **絞るなら、「設計を閉じる層」を中心に置く**: 取り込み（出典つき）→ 仕様 → ゲインの設計 → 確認 → 未達の報告。これは調べた範囲で緩いライセンスのものが無く、いまの設計（層A・層B、決定的な計算、未達の文言）がそのまま活きる。
3. **エンジンは、区分線形を主にして広げすぎない**: 理想スイッチの区分線形（PLECS と同じ系統）に、スイッチの寄生容量や損失を足す段階に留め、SPICE の水準の素子の物理（BSIM、Verilog-A）は目指さない。SPICE の水準が要る利用者には、利用者が入れた外部の道具につなぐ口（8章の組み合わせの形）を用意する案がある。外部の道具をどこまで許すかは、D-069 の解釈として PO の判断が要る。
4. **電動機ドライブは motulator の取り込みを土台にする（道2、B10）のが、自作を最も減らす**: 誘導機の励磁と飽和、PMSM、離散時間の制御、観測器まで、MIT のコードとして揃っている。失うのは「半導体の寄生容量と電動機を同じ回路で見る」こと（D-068 の (2) と電動機の組み合わせ）で、これを要るとするかは PO の判断（先の検討の Q-3）。gym-electric-motor は強化学習の環境の形で、取り込む利点は小さい。
5. **データシートからの素子の設定は、差別化になりうる**: 緩いライセンスの道具が見つからず、研究（D2S-FLOW、SPICEAssistant）が出始めた段階。ただし、何をデータシートから取るか（損失の曲線か、容量の曲線か、SPICE のパラメータか）は、エンジンをどこまで作るか（3）と一緒に決める必要がある。
6. **研究・教育は「設計目標と確認の教材」として狙う**: 回路図エディタの完成度で既存の道具と競うより、「目標を入れるとゲインと未達の量が根拠つきで返る」ことを、無料で配れる点が大学では効く（GSEIM の論文が述べる、費用のために計算の宿題を出せない大学の事情 [R4]）。

### 10.3 PO の判断が要ること（この調査で見つかったもの）

1. △ の範囲（GCC のランタイム例外、BSL-1.0、Qhull の独自の文面、LLVM の例外、0BSD、HPND）を許すか（7.1、7.2）。
2. **NumPy と SciPy の Linux・macOS のホイールが同梱する libquadmath（LGPL-2.1-or-later）を許すか**。3段階の決まりでは ×（コピーレフト）で、いまの main の依存（numpy、scipy）が外れる。先の検討（`academic-extension.md` 11.3）はソースの同梱物だけを見て「コピーレフトは無い」としており、この点を含んでいない（7.2）。
3. 字体のライセンス（matplotlib の OFL、pdf.js の Liberation の GPL-2＋字体の例外）をどう扱うか。pdf.js は字体を外して同梱する手がある。
4. 利用者が自分で入れた外部の道具（ngspice、LTspice、PLECS など）を、別のプロセスとして呼ぶ形を許すか（8章）。
5. D-068 の「自由な回路と寄生容量つきの素子」の範囲を、区分線形に寄生容量を足す程度に留めるか、SPICE の水準まで目指すか。また、電動機を同じ回路で駆動すること（道1）が要るか、式の水準で別に解く形（道2、B10）でよいか（10.2 の 3、4）。
6. Pydantic（pydantic-core に組み込まれた Rust のクレート）のライセンスを、ソースの Cargo.lock で確かめること（7.9。いまの main の依存）。

---

## 11. 出典の一覧

取得日はすべて 2026-11-16。

**Plexim（PLECS）**
- [P1] Commercial Price List — https://www.plexim.com/store/commercial
- [P2] Academic Price List — https://www.plexim.com/store/academic
- [P3] Student License — https://www.plexim.com/node/6
- [P4] PLECS Release Notes（5.0 は 2026-02-06）— https://www.plexim.com/node/1576
- [P5] How PLECS Works（5.0）— https://docs.plexim.com/plecs/5.0/how-plecs-works/
- [P6] Induction Machine with Saturation（5.0）— https://docs.plexim.com/plecs/5.0/components-by-category/im_saturable/
- [P7] Magnetic Modeling（5.0）— https://docs.plexim.com/plecs/5.0/magnetic/
- [P8] Discrete PID Controller（5.0）— https://docs.plexim.com/plecs/5.0/components-by-category/discretepidcontroller/
- [P9] Analysis Tools（5.0）— https://docs.plexim.com/plecs/5.0/analysis/
- [P10] Frequency Response（5.0）— https://docs.plexim.com/plecs/5.0/analysis/frequency-response/
- [P11] Simulation Scripts（5.0）— https://docs.plexim.com/plecs/5.0/scripting/
- [P12] Before You Begin（5.0）— https://docs.plexim.com/plecs/5.0/before-you-begin/
- [P13] Will there be an Official PLECS AI Agent in the Future?（フォーラム、2026-03-12）— https://forum.plexim.com/t/will-there-be-an-official-plecs-ai-agent-in-the-future/2883/13
- [P14] Saturable Transformers（5.0）— https://docs.plexim.com/plecs/5.0/components-by-category/sattransformer/

**Siemens / Altair（PSIM、SmartCtrl）**
- [S1] Simcenter PSIM software — https://www.siemens.com/en-us/products/simcenter/systems-simulation/psim/
- [S2] Simcenter PSIM 2026.1（ブログ、2026-06-26）— https://blogs.sw.siemens.com/simcenter/simcenter-psim-2026-1/
- [S3] PSIM 2025.0 Release Notes — https://2025.help.altair.com/2025/powersim/PSIM_2025.0_Release_Notes.pdf
- [S4] SmartCtrl by Power Smart Control — https://www.siemens.com/en-us/products/siemens-industry-software-smartctrl-by-power-smart-control/
- [S5] AC Sweeps with PSIM（Altair コミュニティ、2023年ごろ）— https://community.altair.com/discussion/33119/ac-sweeps-with-psim-what-makes-them-unique
- [S6] PSIM 2023.1 で Python API がサポートされました（Altair コミュニティ）— https://community.altair.com/discussion/39179
- [S7] PSIM software student trail version download on MacBook（コミュニティの回答）— https://community.altair.com/discussion/65100/psim-software-student-trail-version-download-on-macbook
- [S8] What is the difference between student version and paid version of PSIM（コミュニティの回答）— https://community.altair.com/discussion/63128/what-is-the-difference-between-student-version-and-paid-version-of-psim
- [S9] Type 3 controller optimization part 2（Altair コミュニティ）— https://community.altair.com/discussion/63076/type-3-controller-optimization-part-2-phase-margin-and-crossover-frequency
- [S10] zongseung/psim-mcp（GitHub、MIT）— https://github.com/zongseung/psim-mcp
- [S11] Power supply design software（RadioRadar、2017-02-13）— https://www.radioradar.net/en/news_of_electronic/power_supply_design_software.html

**MathWorks**
- [M1] Pricing and Licensing — https://www.mathworks.com/pricing-licensing.html
- [M2] Choose a Power Electronic Converter Block — https://www.mathworks.com/help/sps/ug/choose-power-electronic-converter-block.html
- [M3] Choose the Right Motor or Actuator Block — https://www.mathworks.com/help/sps/ug/choose-right-motor-or-actuator-block.html
- [M4] FEM-Parameterized PMSM — https://mathworks.com/help/sps/ref/femparameterizedpmsm.html
- [M5] Tune PI Controllers Using Field Oriented Control Autotuner — https://www.mathworks.com/help/mcb/gs/tune-pi-controllers-using-foc-autotuner.html
- [M6] Design Controller for Boost Converter Model Using Frequency Response Data — https://www.mathworks.com/help/slcontrol/ug/design-controller-for-power-electronics-model-using-frequency-response-data.html
- [M7] Design Optimization to Meet Step Response Requirements (GUI) — https://www.mathworks.com/help/sldo/gs/optimize-controller-parameters-to-meet-step-response-requirements-gui.html
- [M8] Interpret Numeric Tuning Results — https://www.mathworks.com/help/control/ug/tuning-algorithm.html
- [M9] Tuning Goals — https://www.mathworks.com/help/control/tuning-goals.html
- [M10] Requirements Toolbox — https://www.mathworks.com/products/requirements-toolbox.html
- [M11] MathWorks launches generative AI-powered MATLAB Copilot（2025-10-07）— https://www.mathworks.com/company/newsroom/mathworks-launches-generative-ai-powered-matlab-copilot-to-boost-productivity-and-accelerate-development-for-engineers-scientists-and-researchers.html
- [M12] Release 2026a（Simulink Copilot、2026-04-27）— https://nl.mathworks.com/company/newsroom/matlab-and-simulink-release-2026a-features-new-generative-ai-powered-simulink-copilot-that-enhances-design-and-development-workflows-for-engineered-systems.html
- [M13] Simulink Copilot — https://www.mathworks.com/products/simulink-copilot.html
- [M14] Simulink Agentic Toolkit — https://au.mathworks.com/products/simulink-agentic-toolkit.html
- [M15] matlab/matlab-mcp-server（GitHub）— https://github.com/matlab/matlab-mcp-server
- [M16] mathworks/Call-simulink-from-Python（GitHub）— https://github.com/mathworks/Call-simulink-from-Python
- [M17] MATLAB System Requirements（R2026b）— https://www.mathworks.com/support/requirements/matlab-system-requirements.html
- [M18] matlab/simulink-agentic-toolkit（ecosyste.ms の登録情報）— https://awesome.ecosyste.ms/projects/github.com%2Fmatlab%2Fsimulink-agentic-toolkit

**Analog Devices、Qorvo**
- [L1] LTspice — https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html
- [L2] Control Loop Simulation Done the Easy Way（2024-03-01）— https://www.analog.com/en/resources/technical-articles/control-loop-simulation-done-the-easy-way.html
- [L3] LTspice license（EngineerZone のフォーラム）— https://ez.analog.com/design-tools-and-calculators/ltspice/f/q-a/589882/ltspice-license
- [L4] spicelib（PyPI、GPL-3）— https://pypi.org/project/spicelib
- [Q1] QSPICE — https://www.qorvo.com/design-hub/design-tools/interactive/qspice
- [Q2] Qorvo Enhances QSPICE…（2025-03）— https://www.electropages.com/blog/2025/03/qorvo-enhances-qspice-circuit-simulation-software-advanced-modelling-capabilities
- [Q3] Your experience with Qorvo Qspice Model Generator（フォーラム、2025）— https://forum.qorvo.com/t/your-experience-with-qorvo-qspice-model-generator/21656

**SIMetrix/SIMPLIS**
- [X1] What SIMPLIS POP Does（9.1）— https://help.simetrix.co.uk/9.1/simplis/what_is_simplis/topics/simplis_why_simplis_pop_matters.htm
- [X2] What is SIMPLIS?（8.3）— https://help.simetrix.co.uk/8.3/simplis/what_is_simplis/topics/what_is_simplis.htm
- [X3] Product matrix — https://www.simetrix.co.uk/products/product-matrix.html
- [X4] SIMetrix/SIMPLIS Elements — https://www.simetrix.co.uk/downloads/download-elements.html
- [X5] SIMetrix/SIMPLIS 9.4 の発表（2026-05-05）— https://simplistechnologies.com/node/129817
- [X6] simetrix（PyPI）— https://pypi.org/project/simetrix/9.2.1

**GeckoCIRCUITS、OpenModelica、ngspice ほか**
- [G1] geckocircuits/GeckoCIRCUITS（GitHub、GPL-3）— https://github.com/geckocircuits/GeckoCIRCUITS
- [G2] GeckoCIRCUITS FAQ（分岐版）— https://tinix84.github.io/GeckoCIRCUITS/resources/faq
- [G3] Successful online education - GeckoCIRCUITS as open-source simulation platform（IPEC 2014）— https://www.research-collection.ethz.ch/entities/publication/0b320f67-2eab-4ab2-b53b-e816161a6835
- [O1] Open Source Modelica Consortium — https://openmodelica.org/home/consortium
- [O2] OpenModelica 1.26.0（2025-12-18）— https://openmodelica.org/news/openmodelica-1.26.0/
- [O3] modelica/ModelicaStandardLibrary（GitHub）— https://github.com/modelica/ModelicaStandardLibrary
- [O4] Modelica.Electrical.Machines UsersGuide（4.1.0）— https://doc.modelica.org/Modelica%204.1.0/Resources/helpDymola/Modelica_Electrical_Machines_UsersGuide.html
- [O5] OpenModelica.Scripting.linearize — https://build.openmodelica.org/Documentation/OpenModelica.Scripting.linearize.html
- [N1] Ngspice FAQ — https://ngspice.sourceforge.io/faq.html
- [N2] ngspice news — https://ngspice.sourceforge.io/news.html
- [N3] Debian の ngspice 47 の著作権表示 — https://tracker.debian.org/media/packages/n/ngspice/copyright-47ds-1
- [N4] Ngspice User's Manual（46、2026-03-31）— https://ngspice.sourceforge.io/docs/ngspice-html-manual/manual.xhtml
- [N5] PySpice（PyPI）— https://pypi.org/project/PySpice/
- [N6] Xyce — https://xyce.sandia.gov/
- [N7] ra3xdh/qucs_s（GitHub）— https://github.com/ra3xdh/qucs_s
- [N8] sharpie7/circuitjs1（GitHub）— https://github.com/sharpie7/circuitjs1

**実時間シミュレータ、Python の道具、AI**
- [T1] Typhoon HIL software — https://www.typhoon-hil.com/products/software
- [T2] Virtual HIL free licence — https://info.typhoon-hil.com/virtual-hardware-in-the-loop-get-your-free-licence
- [T3] Typhoon-HIL-API（PyPI）— https://pypi.org/project/Typhoon-HIL-API/
- [B1] aesim.simba（PyPI）— https://pypi.org/project/aesim.simba/
- [B2] SIMBA product comparison — https://simba.io/product/comparison
- [B3] SIMBA pricing — https://simba.io/pricing
- [Y1] gym-electric-motor（PyPI）— https://pypi.org/project/gym-electric-motor/
- [Y2] motulator（PyPI）— https://pypi.org/project/motulator/
- [Y3] motulator pyproject.toml — https://raw.githubusercontent.com/Aalto-Electric-Drives/motulator/main/pyproject.toml
- [Y4] ParaEMT（DOE CODE）— https://www.osti.gov/doecode/biblio/99288
- [Y5] control（python-control、PyPI）— https://pypi.org/project/control/
- [Y6] slycot（PyPI）— https://pypi.org/project/slycot/
- [Y7] LLNL/sundials（GitHub）— https://github.com/LLNL/sundials
- [Y8] Matplotlib License — https://matplotlib.org/stable/project/license.html
- [Y9] upb-lea/transistordatabase（GitHub）— https://github.com/upb-lea/transistordatabase
- [Y10] numpy 2.3.4 のホイール（Linux、Windows）の `numpy-2.3.4.dist-info/LICENSE.txt` — https://pypi.org/project/numpy/2.3.4/
- [Y11] scipy 1.18.1 のホイール（Linux）の `LICENSE.txt` — https://pypi.org/project/scipy/1.18.1/
- [Y12] lxml 6.1.3 のホイール（Windows）の `LICENSES.txt` — https://pypi.org/project/lxml/6.1.3/
- [Y13] pillow 13.0.0 のホイール（Windows）の `LICENSE` — https://pypi.org/project/pillow/13.0.0/
- [Y14] Julia THIRDPARTY.md — https://github.com/JuliaLang/julia/blob/master/THIRDPARTY.md
- [Y15] ModelingToolkit.jl LICENSE.md — https://github.com/SciML/ModelingToolkit.jl/blob/master/LICENSE.md
- [Y16] ControlSystems.jl LICENSE.md — https://github.com/JuliaControl/ControlSystems.jl/blob/master/LICENSE.md
- [Y17] scikit-sundae 1.2.0 のホイール（Linux、Windows）— https://pypi.org/project/scikit-sundae/
- [Y18] SciPy のソースの同梱物の一覧 LICENSES_bundled.txt（Qhull、Boost と ryu の BSL-1.0、xsf ほか）— https://github.com/scipy/scipy/blob/main/LICENSES_bundled.txt 。ホイールの `scipy/spatial/qhull_src/COPYING_QHULL.txt` も確認
- [Y19] pdfjs-dist 6.5.438 の配布物の `standard_fonts/LICENSE_LIBERATION`、`LICENSE_FOXIT`、`cmaps/LICENSE`、`iccs/LICENSE`、`wasm/LICENSE_*` — https://www.npmjs.com/package/pdfjs-dist
- [Y20] 先の検討「研究・教育への対応」（motulator、gym-electric-motor、SciPy の判定、道1・道2 の見積もり）— `/home/claude/academic/academic-extension.md` 11章・12章
- [D1] JuliaHub Announces Dyad 3.0 General Availability（2026-05-27）— https://tools.prnewswire.com/en-us/live/20813/release/20260527EN68824
- [D2] JuliaHub Launches Dyad — https://juliahub.com/blog/juliahub-launches-dyad
- [R1] SPICEAssistant（arXiv:2507.10639）— https://arxiv.org/abs/2507.10639
- [R2] D2S-FLOW（arXiv:2502.16540）— https://arxiv.org/abs/2502.16540
- [R3] PE-GPT（arXiv:2403.14059、2024-03-21）— https://arxiv.org/abs/2403.14059v1
- [R4] An open-source simulation package for power electronics education（GSEIM、arXiv:2204.12924）— https://ar5iv.labs.arxiv.org/html/2204.12924

**アカデミック**
- [A1] Simulation of Power Electronics Converters Using PLECS（Academic Press、2019）— https://abbeys.com.au/book/simulation-of-power-electronics-converters-using-plecsa-r.do
- [A2] Power Electronics Circuit Analysis with PSIM（De Gruyter、2021）— https://lehmanns.de/shop/technik/56626814-9783110740639-power-electronics-circuit-analysis-with-psim
- [A3] RWTH Aachen の学内のソフトウェア（PLECS）— https://help.itc.rwth-aachen.de/en/service/u7okrwv9h0s2/article/eacf9cd6535145e1940d715b8dc93fe1

**PyPI と npm の登録情報**（7章の表の版、日付、ライセンス、依存）: `https://pypi.org/pypi/<名前>/json`、`https://registry.npmjs.org/<名前>`（2026-11-16 取得）。

### 確かめられなかったこと（まとめ）

- PSIM の計算の方式、価格、学生版の中身、磁気飽和の扱い（公式の現行文書が取れなかった）。
- SmartCtrl が時間領域の目標を直接扱うか。
- LTspice の利用条件の文書、電動機モデルの有無。
- MATLAB の追加製品の価格（地域別・見積もり）、全学ライセンスの数。
- PLECS の使用許諾の細部（拡張として使う場合の条件）。
- ngspice から LGPL・GPL の部分を外して組めるか。
- 同梱物の細部: pydantic-core に組み込まれた Rust のクレート、Pillow のホイールに XZ の LGPL 部分が入るか、llvmlite と jaxlib のホイールの中身、cryptography のホイールの中身、Modelica 標準ライブラリの C の資源、motulator から取り込む部分が matplotlib・tqdm を import しないか。
- Dyad がローカルで動くか、パワエレに使えるか。
- 研究・教育での各道具の採用の数。
- 探していない道具（9章の冒頭）。
