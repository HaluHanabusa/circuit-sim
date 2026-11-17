# 半導体メーカーの無料の設計ツールと circuit-sim v0.1 の比較

調べた日: 2026-11-17 ／ 調べた人: 調査担当（general-purpose） ／ きっかけ: 差別化のスレッドの持ち込みメモ（Project `claude/開発スレッドへの持ち込みメモ（2026-11-17）.md` の事実2）
各社の公式のページ、利用者の手引き、アプリケーションノートを優先した。「見つからない」（読んだ資料に書かれていない）と「無い」（無いと書かれている）は分けた。v0.1 は「計画どおりにできたときの姿」（E2a は未着手）。

## 1. 要約

- 調べた範囲で、**「設計目標を入れる → デジタルの PID のゲインを出す → スイッチングモデルの時間領域の計算で確かめる → 設計目標ごとに達成 / 未達と未達の量を返す」を1つの無料の道具で閉じるものは見つからなかった**。
- ただし **前半（目標からゲインを出す）は、無料の道具で既にできる**: WEBENCH の Compensation Designer（位相余裕とゲイン交差周波数。TI の IC だけ、アナログ）、LTpowerCAD（目標の帯域から推奨の補償。ADI の IC だけ、アナログ）、ST-WDS（交差周波数・位相余裕・純粋な遅れから極と零点を自動で置き係数。STM32 向け、デジタル）、Microchip DCDT（遅れと PWM の分解能を入れて位相余裕を見る。極と零点は人が置く）、TI C2000 の Compensation Designer、Renesas CompZL（2009）。
- **後半（スイッチングの時間領域で確かめる）も無料の道具があるが、前半とつながっていない**: WEBENCH（オンライン、TI の IC だけ、モデルの種類は未記載）、LTpowerCAD → LTspice、SIMPLIS 系（Mindi、eDSim、iSim:PE）。デジタルの係数の道具で設計した制御器を、スイッチングの計算に入れて確かめる機能は見つからなかった。
- **設計目標ごとの「達成 / 未達、未達の量」は、どの道具にも見つからなかった**。近いもの: C2000 の「Stable / UnStable Loop」、LTpowerCAD の過渡の上限の欄、eDesignSuite のオンラインの計算がオーバーシュート・アンダーシュート・整定時間を値で示すこと、PowerSmart DCLD の係数の丸めの誤差の警告。整定時間・定常偏差・回復時間を目標として入れる欄は、どの道具にも無かった。
- **PLECS**: 公式のデモ「Synchronous Buck Converter」は、降圧の定数と、指定したゲイン交差周波数・位相余裕から PID のゲインを計算している（デモの中の計算で製品の機能ではない。連続時間の PID。確かめは波形だけ）。`landscape.md` の「公式に自動調整は見当たらない」には注記が要る。
- **利用者の目線**: 「メーカーの IC を使うアナログの降圧」なら、無料のメーカーの道具でほぼ足りる（判定は人が図と値で見る）。v0.1 に残る差は「**IC に縛られない一般の降圧のデジタル制御を、スイッチングモデルで確かめ、目標ごとの判定（判定できないを含む）と未達の量を1か所で返す**」こと、ローカル・3つの OS・自由なライセンス。
- **メーカーの道具の方が進んでいる所**: 量子化と係数の表し方（v0.1 では凍結した E1h）、実測の周波数応答の取り込み、係数とコードの生成、部品選定と効率、電流モード、多くのトポロジー。

## 2. 比較の表

| 道具 | 価格と条件 | ゲイン・補償器の設計 | 確かめ方 | 目標ごとの達成・未達 | デジタル制御 | IC の縛り | OS |
|---|---|---|---|---|---|---|---|
| circuit-sim v0.1（計画） | Apache-2.0（暫定）、無料、ローカル。開発中 | 位相余裕の目標から離散 PID の初期ゲインを解析式で（E1b、完了）。設計目標 11 種類（E2a、未着手） | 安定余裕は線形化。時間領域の目標はスイッチングモデルで測り、状態平均化モデルの予測を並べる | 達成 / 未達 / 判定できない、未達の量（E2a） | 離散 PID、演算遅れ 1 周期、PWM の保持。量子化は v0.1 の後 | 無し（同期整流の降圧、電圧モード） | Win / mac / Linux |
| ADI LTpowerCAD（II） | 無料、オフライン | 目標の帯域で推奨の補償（RTH・CTH・CTHP）。電圧モードはタイプ III を人が置く | 小信号のボード線図、出力インピーダンス、過渡の一次の近似。時間領域は LTspice へ書き出し | 過渡の上限の欄あり。判定は見つからず | 見つからず（アナログ） | ADI の対応 IC だけ | Windows |
| ADI Power Studio Designer（2025-10） | 無料、要ログイン | ループゲインと位相余裕を見る | ループと負荷の過渡。SIMPLIS・LTspice へ書き出し | 見つからず | 見つからず | ADI の部品が中心 | ブラウザ |
| TI Power Stage Designer（Loop Calculator） | 無料（輸出の承認） | 規則で零点・極を置く提案。位相余裕の目標からは解かない | 小信号のボード線図。Load Step Calculator は必要な出力容量を逆算 | 無し | 無し | 無し | Java |
| TI WEBENCH Power Designer | 無料、オンライン | Compensation Designer: 位相余裕とゲイン交差周波数の目標 | オンラインの時間領域（起動、定常、入力・負荷の過渡）とボード線図。モデルの種類は未記載 | 判定の表示は見つからず | 無し | TI の IC だけ | ブラウザ |
| TI C2000 Compensation Designer | 価格の記載なし | PID、2P2Z、3P3Z など。極・零点は人が動かす | 小信号（モデルか実測 SFRA） | 「Stable / UnStable Loop」 | 有り（係数） | C2000 | Windows |
| Microchip DCDT | 無料（MPLAB X） | デジタル PID、2P2Z、3P3Z。極・零点は人が置く | 小信号（ボード、根軌跡、ナイキスト、ステップ）。測定値の取り込み | 見つからず | 有り: サンプルの比、PWM の分解能、演算遅れ、ゲート駆動の遅れ。固定小数点の係数 | 係数は dsPIC 向け | 記載なし |
| Microchip PowerSmart DCLD | GitHub（条件は未確認） | z 領域 1P1Z〜6P6Z | 小信号 | 係数の丸めの誤差の警告 | 有り: 数の表し方 4 つ、コード生成 | dsPIC33 | Windows |
| ST-WDS（Biricha） | STM32 の利用者に無料（入手性は未確認） | 交差周波数・位相余裕・純粋な遅れから極・零点を自動で、2p2z / 3p3z | 小信号。最後は実機 | 見つからず | 有り: 遅れ、PWM のカウント、ADC のビット数 | STM32 | 記載なし |
| ST eDesignSuite（オンライン） | 無料 | 見つからず | 起動、負荷ステップ、定常、入力の過渡 | オーバーシュート、アンダーシュート、整定時間の値。判定は見つからず | 見つからず | ST の設計 | ブラウザ |
| Renesas CompZL（2009） | 記載なし | 位相余裕・ゲイン余裕・交差周波数の目標から PID の係数 | 小信号（簡略化） | 予測の余裕。判定は見つからず | 有り | ZL 系 | 記載なし |
| PLECS のデモ（参考、商用） | PLECS のライセンス | 交差周波数・位相余裕の指定から PID のゲイン（デモの中の計算） | スイッチングの波形だけ | 無し | 連続時間の PID | 無し | Win / mac / Linux |

参考（ゲインの設計は持たないが、無料でスイッチングを計算できるもの）: Microchip MPLAB Mindi（SIMetrix/SIMPLIS）、ST eDSim（上限あり）、Renesas iSim:PE、onsemi Elite Power Simulator（PLECS、要ログイン、損失と熱が主）。

## 3. v0.1 との重なりと残る差

- 重なる所: 目標からゲインを出す（ただし時間領域の目標からは、どれも解かない）、遅れを含めた余裕（DCDT、ST-WDS。小信号）、時間領域の計算（WEBENCH、LTspice、SIMPLIS 系。デジタルの制御器をそのまま入れる道具は見つからない）、判定に近い表示。
- 一般の降圧をデジタルの PID で設計するなら、道具を3つ前後つなげば近いことはできる（DCDT か ST-WDS で係数 → LTspice か Mindi でスイッチングの回路とデジタル制御器の模型を自分で組む → 波形から自分で測って目標と比べる）。2つ目と3つ目は手作業で、制御器の模型の作り間違いに気づく手立てが無い。
- 残る差（無料のメーカーの道具に見つからなかったもの）: (1) デジタルの PID を同じ道具の中でスイッチングモデルの時間領域で確かめる、(2) 時間領域の設計目標を入力として持ち、目標ごとに判定して未達の量を返す、(3) 「判定できない」を分ける、(4) 平均モデルとスイッチングモデルの差を並べて示す、(5) IC とマイコンに縛られず、ローカルで、ログインなしで、3つの OS、(6) 自由なライセンス。
- 見立て（決定ではない）: 差は「目標からゲインを出す」ではなく「一般の降圧のデジタル制御を、スイッチングモデルで確かめ、目標ごとの判定と未達の量で返す」と言う方が事実に合う。メーカーの IC でアナログの電源を作る設計者には使う理由が少なく、効くのはマイコンでデジタル制御の電源を作る人と研究・教育。量子化（E1h）の凍結は、デジタルの係数の道具と比べると見劣りする。

## 4. landscape.md への反映（案）

- 2 章と 9.2 の表に、LTpowerCAD、Power Stage Designer、WEBENCH、DCDT / DCLD、ST-WDS、C2000 Compensation Designer、CompZL、PLECS のデモの行を足す。9.2 の「見つからなかったもの」に「無料のメーカーの道具（ADI、TI、Microchip、ST、Renesas、Infineon、onsemi）にも見つからなかった（2026-11-17）」と足す。
- PLECS の注記: 製品の機能としてのゲインの自動調整は、公式文書で見つからない。ただし公式のデモ「Synchronous Buck Converter」は、降圧の定数と、指定したゲイン交差周波数・位相余裕から、連続時間の PID のゲインを計算している（Corradini ほかの教科書による）。確かめは時間領域の波形で、目標ごとの判定は無い。
- まだ探していないもの: MPS（MPSmart、DC/DC Designer）、Würth（REDEXPERT）、Power Integrations（PI Expert）、ROHM、Toshiba、Vishay、Nexperia、ADI のデジタル制御 IC の GUI、Infineon Designer。

## 5. 確かめられなかったこと

LTpowerCAD の最新の要件と電圧モードの推奨の補償、Power Studio Designer の補償の設計の方法、WEBENCH のモデルの種類とログインの要否、C2000 の演算遅れの扱い、DCDT の OS と係数の形式、DCLD のライセンス、ST-WDS の入手性（2021 年のフォーラムに入手できないという投稿）、PLECS のデモの式そのもの、各社の使用許諾の細部。

## 6. 出典（取得日 2026-11-17）

- ADI: https://www.analog.com/en/lp/ltpowercad.html 、AN-158 https://www.analog.com/en/app-notes/an-158f.html 、AN-149 https://www.analog.com/en/resources/app-notes/an-149.html 、https://www.analog.com/en/resources/technical-articles/power-supply-loop-stability-compensation-part-3.html 、https://www.analog.com/en/newsroom/press-releases/2025/10-14-2025-adi-launches-adi-power-studio-new-web-based-tools.html 、https://www.analog.com/en/resources/evaluation-hardware-and-software/embedded-development-software/power-studio-designer.html
- TI: https://www.ti.com/tool/POWERSTAGE-DESIGNER 、SLVUBB4B https://www.ti.com/lit/pdf/slvubb4 、SSZTCG2 https://www.ti.com/lit/pdf/SSZTCG2 、SLYP708 https://www.ti.com/lit/slyp708 、https://webench.ti.com/help/PowerDesigner/Electrical_Simulation/Electrical_Simulation.htm 、https://www.ti.com/tool/ja-jp/COMPENSATION_DESIGNER 、SPRUHZ5A https://www.ti.com/lit/pdf/spruhz5 、SPRUIK4A https://www.ti.com/lit/pdf/spruik4
- Microchip: https://www.microchip.com/en-us/development-tool/dcdt 、https://gallery.microchip.com/packages/Digital-Compensator-Design-Tool/ 、https://developerhelp.microchip.com/xwiki/bin/view/applications/power/switching-regulators-/digital-compensator-design-tool/ 、https://microchip-pic-avr-tools.github.io/powersmart-dcld 、DS20005617A（Mindi）
- ST: https://www.st.com/content/st_com/en/ecosystems/stm32-digital-power.html 、AN5788 https://www.st.com.cn/resource/en/application_note/an5788-stm32-digital-power-pid-and-iir-filters-for-smps-control-design-and-comparison-on-bg414edpow1-discovery-kit-stmicroelectronics.pdf 、https://www.st.com/content/st_com/en/campaigns/predict-converter-performance-with-edesignsuite-online-simulations.html 、UM3017
- Renesas: AN2027 https://www.renesas.com/us/en/document/apn/an2027-compzl-user-guide 、https://www.renesas.com/us/en/software-tool/isimpe-offline-simulation-tool
- Infineon: XDPP1100 のアプリケーションノート（2021-09-20）
- onsemi: https://www.onsemi.com/design/tools-software/elite-power-simulator 、TND6423
- Plexim: https://docs.plexim.com/plecs-demos/power-supplies/synchronous-buck-converter/ 、https://docs.plexim.com/plecs-demos/power-supplies/type2-and-type3-compensator-analysis-for-power-supplies/
