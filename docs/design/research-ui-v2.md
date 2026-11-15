# 画面の見た目の調査（A0r）

最終更新: 2026-11-16 ／ 責任者: ux-designer ／ 作業単位: A0r（範囲1 調査、範囲2 方向の案）
関連: `docs/plan/A0r-ui-redesign-research.md`、`app-design.md`、`design-system.md`、`screens-v0.1.md`

PO の指摘（2026-11-16、Windows の実機で A0 を見て）: 全体のデザインがよくない、字が小さく見づらい、エディタの役割もあるので見やすくするべき。
この文書は、原因を測り、今の製品とデザインシステムが何をなぜしているかを集め、このアプリへの当てはめ方と方向の案をまとめる。
他社の固有のデザインは写さない。持ち帰るのは考え方と数値の傾向。引用は短くし、ほとんどを自分の言葉でまとめた。

## 0. 要約

1. **PO の画像は、ブラウザの表示倍率 67% で描かれていた可能性が高い**（1章）。13px の文字は約 8.7 画素で出ていた。ただし 100% でも A0 の水準は足りない。
2. **基準の文字は 14px が業務向けの共通の線、読むための画面は 16px。** Windows（Fluent）、IBM Carbon の productive、GitHub Primer、Ant Design はどれも本文 14px。Ant Design は 12px から 14px に上げた。デジタル庁と SmartHR は 16px を基準にし、デジタル庁は 14px 未満を原則として認めない。いちばん小さい文字は欧文と数字で 12px、日本語で 14px が線になる（2〜4章）。
3. **部品の高さは 32px が標準、24〜28px が詰めた形、40px が大きい形。** 間隔は 4px 刻み。密度は画面ごとに散らさず、切り替え（標準 / 詰める）として持つ製品が多い（JetBrains の Compact Mode、Material の密度、Carbon の行の高さ5段）。
4. **日本語は字体で見え方が大きく変わる。** 游ゴシックは字面が小さく、Windows の Chrome では細く出やすい。BIZ UDPゴシック（Windows 10 1809 以降の日本語版に標準で入る。OFL でも配られている）は字面が大きく、同じ 14px でもひと回り大きく見える。同梱するなら、JIS 第1・第2水準と欧文に絞った WOFF2 で BIZ UDPゴシック 1.55 MB／太さ、Noto Sans JP 2.09 MB（全太さ）（5章、実測）。
5. **計測器と回路シミュレータは、波形の面を最大にし、1段に1つの縦軸、読み取りは決まった場所の表か札に出す。** これは v0.1 の仕様の方針と同じ。足りなかったのは文字と強弱と、横幅の使い方（3章）。
6. このアプリへの当てはめ方と3つの方向の案は 6章・7章。

## 1. A0 の画面で起きていたこと（PO の画像を測った）

### 1.1 表示倍率

PO の画像（1906 × 905）を画素で測った。

| 部分 | 実装の大きさ（CSS） | 画像の画素 | 比 |
|---|---|---|---|
| ヘッダー | 48px | 32 画素（0〜31、境目の線が 32） | 0.67 |
| お知らせの帯 | 32px | 21 画素（境目の線が 883、帯が 884〜904） | 0.66 |
| 「仕様」の字（13px の見出し） | 13px | 字の黒い部分の高さ 9 画素 | — |

- 境目の線が1画素の単色（`#dfe3e8` そのまま）なので、縮小した画像ではない。
- 同じ高さの要素を Chrome で表示倍率 66.7% にして撮ると、ヘッダーが 0〜31、境目が 32、帯が 21 画素になり、画像と一致した（`/tmp/design-v2-check/out/zoom_0.667.png`）。
- **PO の Chrome は、このページを 67% で表示していた可能性が高い。** CSS の 13px は約 8.7 画素、1906 画素の窓は CSS では約 2858px 幅に当たる。「1920 幅の大部分が空いている」の一部もこれで説明できる。
- Chrome は表示倍率を「URL のホストの部分」ごとに覚える（[Chromium HostZoomMap](https://chromium.googlesource.com/chromium/src/+/main/content/public/browser/host_zoom_map.h)）。アプリは 127.0.0.1 で毎回違う番号の口を開くが、ホストの部分は同じなので、以前に別のローカルの道具を縮小していれば、その倍率を引き継ぐ（番号を含まないことは URL の構成からの推測で、Windows の実機で確かめる）。
- **PO に確かめてほしいこと:** アプリの画面で Ctrl+0 を押して 100% に戻るか。Windows の表示スケール（100% / 125% / 150%）と、画面の解像度。

### 1.2 100% で見ても足りないもの

| # | 原因 | 根拠 |
|---|---|---|
| 1 | 文字が小さい: 本文 13px、補足 12px、目盛り 11px | Windows の本文は 14px（[Windows の文字](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography)）。デジタル庁は日本語の本文と UI を 16px 以上、14px 未満は原則不可（[DADS](https://design.digital.go.jp/dads/foundations/typography/)） |
| 2 | 日本語の字体: 指定の先頭が Segoe UI で、日本語は後ろの字体に落ちる。画像の字形から、日本語は Yu Gothic UI の Regular で出ている | 游ゴシックは字面が小さめ（[SmartHR](https://smarthr.design/basics/typography/)）。Windows の Chrome で細く出やすい（[PoC: Perfect Yu Gothic](https://neos21.github.io/poc-perfect-yu-gothic/)） |
| 3 | 強弱が無い: 見出し（13px・600）と本文（13px）が同じ大きさ。太さだけで1段 | Atlassian は見出しを太くして階層をはっきりさせた（[Atlassian](https://www.atlassian.com/blog/design/implementing-typography-at-scale-the-journey-behind-the-screens)） |
| 4 | 領域の区切りが弱い: 区切り線 1.29:1、面はどれも白、領域の名前「回路図」は 12px の灰色で隅 | （自分の測定）どこからどこまでが何の領域かが、文字を読まないと分からない |
| 5 | 空の状態が、広い面の中央に 13px の2行 | （自分の測定）画面の 9 割が白い面 |
| 6 | 等幅の字体が細い（Windows では Cascadia Mono か Consolas。13px） | 画像の `rlc_series.json` が細く薄い |
| 7 | A0 の範囲では仕様パネルの表と回路図がまだ無く（A1 の範囲）、空に見える | `docs/plan/A0-app-shell.md` |
| 8 | UX の確認のやり方: Linux の字体（Noto Sans CJK）で撮って見ていた。Windows の字体、実物の大きさ、表示倍率、今の製品との比較をしていなかった | A0r の受け入れ基準4で見方に足す |

## 2. 作業の道具とエディタ

| 製品 | 文字と大きさ | 何を、なぜ |
|---|---|---|
| VS Code | 画面の文字 13px（行の高さ 1.4em）。エディタ 14px（Windows・Linux、macOS は 12px）、行の高さは字の 1.35 倍（macOS 1.5 倍）。日本語の Windows では `"Segoe WPC", "Segoe UI", "Yu Gothic UI", "Meiryo UI"` | A0 とほぼ同じ指定。小さいと感じる人は全体の拡大（zoomLevel）で調整する前提（[style.css](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/browser/media/style.css)、[fontInfo.ts](https://github.com/microsoft/vscode/blob/main/src/vs/editor/common/config/fontInfo.ts)） |
| JetBrains の IDE（新しい UI） | 画面の字体を Inter に。Compact Mode で、道具の列と見出しの高さ、余白、アイコンを小さくできる | 目的は「見た目の複雑さを減らし、よく使う機能に届きやすく」。5000 人以上の試用で詰めた。2024.2 で既定になった（[New UI](https://www.jetbrains.com/help/webstorm/2026.2/new-ui.html)、[IntelliJ IDEA ブログ](https://blog.jetbrains.com/idea/2024/10/bridging-the-gap-between-the-classic-and-new-uis/)） |
| Zed | 画面 16px、エディタ 15px、行の高さ「comfortable」= 1.618 倍 | 新しいエディタほど既定の文字が大きい（[Zed の見た目の設定](https://zed.dev/docs/visual-customization)） |
| Figma（UI3） | — | 浮いた面を固定の、幅を変えられる面に戻した（浮いた面は作業の面を狭め、遅くした。「Speed is a feature」）。ポインタを乗せたときだけ出る UI は不安定で捨てた。ラベルを省いた案は使いやすさを損ねて捨てた（[UI3 の設計](https://www.figma.com/blog/our-approach-to-designing-ui3/)、[UI3 の舞台裏](https://www.figma.com/blog/behind-our-redesign-ui3/)）。利用者からは「字が細くなって読みにくい」という声も出た（[フォーラム](https://forum.figma.com/share-your-feedback-26/figma-font-weight-too-thin-since-ui3-40087)） |
| Linear | 見出しに Inter Display、本文は Inter | 色の作り方を HSL から LCH に替え、テーマを「基調色・強調色・コントラスト」の3つの入力から作る。98 個の変数を3つにした。明るいテーマで文字とアイコンを濃く、暗いテーマで明るくした。見た目の雑音を減らし、ナビゲーションの階層と密度を上げた（[Linear](https://linear.app/blog/how-we-redesigned-the-linear-ui)） |
| Atlassian | 独自の字体（Inter を元にした Atlassian Sans、JetBrains Mono を元にした Atlassian Mono） | OS の字体では見え方が OS ごとに違い、1つの体験にできないため字体を持った。いちばん小さい文字を 11px から 12px に上げた。全部大文字をやめた（[Atlassian](https://www.atlassian.com/blog/design/implementing-typography-at-scale-the-journey-behind-the-screens)、[書体と大きさ](https://atlassian.design/foundations/typography/product-typefaces-and-scale)） |

**傾向。**
- 画面の文字は 13〜16px。新しく作り直した製品ほど大きい側にある（Zed 16px、Atlassian の最小 11→12px、Ant Design の基準 12→14px）。
- 「密度」は利用者が選べる設定にする（JetBrains の Compact Mode）。画面ごとにばらばらに詰めない。
- 面は固定して幅を変えられる形に戻る流れ（Figma）。ポインタを乗せないと見えない情報を作らない。
- 色は少数の入力から作り、コントラストを1つの値で上げ下げできる形（Linear）。

## 3. エンジニアリングの道具（波形とパラメータの表）

| 製品 | 波形の見せ方 | パラメータ・読み取りの見せ方 |
|---|---|---|
| Tektronix 5 Series MSO | 15.6 型のフル HD のタッチ画面の大部分を波形に。「段を分けた表示（stacked）」で、波形ごとに自分の段を持ち、重ならず、それぞれが縦の分解能を全部使える。重ねる表示も残す | 測定と検索の結果は右下の「結果の列」に札（badge）で、波形の設定は左下に札で。横軸・トリガ・取り込みの設定は下端の列（[Tektronix](https://www.tek.com.cn/blog/inside-5-series-mso-re-inventing-oscilloscope-user-interface)） |
| R&S RTO6 | 「波形の表示領域を最大に」。窓をタブに重ね、信号を波形の領域へドラッグして配置（SmartGrid） | 測定の図には全部ラベルを付ける。道具は大きなアイコンと説明つき（[R&S のファクトシート](https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_brochures_and_datasheets/fact_sheet/3609_7790_32/Fact_sheet-RTO6_series_streamlined_user_interface_v100.pdf)） |
| PLECS（スコープ） | 1つの窓に複数のプロット。縦軸の自動と拡大はプロットごと。拡大の履歴を前後に戻れる。軸をドラッグで移動、ダブルクリックで範囲を数値で入れる | カーソルは計算の刻みに吸い付く（Shift で任意の位置、そのとき値は補間で斜体）。Data の窓にカーソルの値、見出しの右クリックで Delta・Min・Max・RMS などの列を足す。「今の波形を保持」して前の結果と比べる（[PLECS](https://docs.plexim.com/plecs/5.0/using-plecs/using-scope/)） |
| Simulink のデータインスペクター | 複数のサブプロット。値の範囲が近い信号を同じサブプロットに | カーソルは1本か2本。2本のときは、それぞれの時刻と間の時間の3つを出す。サンプルが無い時刻の値は補間して印を付ける。信号の表は実行ごとにまとめ、チェックボックスで描く信号を選ぶ（[MathWorks](https://au.mathworks.com/help/simulink/ug/visual-inspection-of-signal-data.html)） |
| KiCad 8 | シミュレータの画面を作り直し、多くの信号のプロットと、カーソルと解析の関数での測定を扱う | 回路図の編集に、選んだ部品の値をすぐ直せる「プロパティの面」を足した（[KiCad 8.0](https://www.kicad.org/blog/2024/02/Version-8.0.0-Released/)） |
| Flux（Web の基板設計） | — | 部品の一覧と、たためる Inspector を右に並べ、見ながら直せるようにした。「タブの切り替えとマウスの移動を減らす」（[Flux](https://unified.flux.ai/p/blog/less-clicking-more-building-the-new-ai-first-ui)） |
| LTspice 24 | 波形の表示に機能を追加 | 道具の列のアイコンを描き直し、コントラストを上げた（[EEVblog の利用者の声](https://www.eevblog.com/forum/eda/ltspice-24-significant-update)） |

**傾向。**
- 波形は最も広い面を取る。1段に1つの縦軸（Tek の stacked、PLECS のプロットごとの軸）。v0.1 の仕様（段、縦軸は段ごと）と同じ。
- 数値は波形の上に浮かべず、決まった場所（表、札、結果の列）で読む。差（Delta）は列として出す。補間した値には印を付ける。v0.1 の仕様（読み取りの表、`≈`）と同じ。
- パラメータは、選んだものの値をその場で直せる、たためる面に置く（KiCad、Flux）。
- 前の結果と比べる操作は、どの道具も持つ（PLECS の保持、Simulink の実行ごとの表）。

## 4. デザインシステムの数値

| システム | 本文 | 小さい文字 | 行の高さ | 部品の高さ | 間隔 | 密度 |
|---|---|---|---|---|---|---|
| Windows（Fluent、Windows 11） | Body 14/20 epx | Caption 12/16。最小の目安は「14px の Semibold、12px の Regular」。これより小さいと読めない言語がある | 20/14 ≈ 1.43 | — | 8 epx（ボタンの間）、12 epx（領域の間） | — |
| Fluent 2（Web） | Body 1 14/20 | Caption 1 12/16、Caption 2 10/14 | 1.43 | 入力欄 24 / 32 / 40（小・標準・大） | 4px 刻み（2〜56） | 「詰めすぎると方向を見失う」 |
| IBM Carbon | productive の本文 14/20、詰めた本文 14/18 | label・helper 12/16 | 1.29〜1.43 | 表の行 24 / 32 / 40 / 48 / 64 | — | productive（密な製品画面、14px 基準）と expressive（Web ページ、16px 基準）の2組 |
| GitHub Primer | 14px が UI の本文 | 12px が最小 | 1.5（本文）、1.25（1行の部品） | 24 / 28 / 32 / 40 / 48 | 4px 格子 | — |
| Ant Design | 14px、行の高さ 22（基準を 12px から 14px に上げた。50 cm の距離で読む前提） | — | 1.57 | — | — | 大きさは 3〜5 種類に絞る。数字は `tabular-nums` |
| Material（密度） | — | — | — | 密度 0 / −1 / −2 / −3、1段で高さ 4px ずつ | — | データの多い表や長い入力欄向け。警告やメッセージは詰めると気づきにくい。詰めたら外側の余白を広げる |
| Atlassian | 基準の rem は 16px、本文の行の高さは約 1.5 倍 | 12px（11px から上げた） | 1.5 | — | 4px の倍数 | — |
| デジタル庁（DADS） | 16px 以上（本文と UI） | 14px（例外の用途だけ。14px 未満は原則不可） | 1.5 以上。本文 1.7〜1.75、詰めた形 1.2〜1.3、1行の UI 1.0 | — | — | 字間: 1行の UI は 2%、詰めた形と等幅は 0 |
| SmartHR | 16px（M）。小さい側は 13.71px（S）、12px（XS）を必要なときだけ | — | 段落 1.5、見出し 1.25、ラベル 1.0 | — | — | 游ゴシック。推奨の太さは M（Medium）と B |

出典: [Windows の文字](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography)、[Windows の間隔](https://learn.microsoft.com/en-us/windows/apps/design/style/spacing)、[Fluent 2 の文字](https://fluent2.microsoft.design/typography)、[Fluent 2 の配置](https://fluent2.microsoft.design/layout)、[Fluent UI の入力欄の高さ](https://unpkg.com/@fluentui/react-input/lib/components/Input/useInputStyles.styles.js)、[Carbon の文字の組](https://carbondesignsystem.com/elements/typography/type-sets/)、[Carbon の表](https://carbondesignsystem.com/components/data-table/style)、[Primer の大きさ](https://unpkg.com/@primer/primitives/dist/css/functional/size/size.css)、[Primer の文字](https://unpkg.com/@primer/primitives/dist/css/base/typography/typography.css)、[Ant Design の文字](https://ant.design/docs/spec/font)、[Material の密度](https://medium.com/google-design/using-material-density-on-the-web-59d85f1918f0)、[Atlassian の書体と大きさ](https://atlassian.design/foundations/typography/product-typefaces-and-scale)、[DADS](https://design.digital.go.jp/dads/foundations/typography/)、[SmartHR の文字の大きさ](https://smarthr.design/products/design-tokens/typography/)、[SmartHR の書体](https://smarthr.design/basics/typography/)

**傾向（数値）。**

| 項目 | 業務向け・密な画面 | 読むための画面 | A0（v0.1） |
|---|---|---|---|
| 本文 | 14px | 16px | 13px |
| 小さい文字 | 12px（欧文・数字）。日本語は 14px（DADS） | 14px | 12px（日本語を含む）、11px（数字） |
| 行の高さ（本文） | 1.4〜1.5 | 1.5〜1.75 | 1.54（20/13） |
| 部品の高さ | 32px（詰めて 24〜28px） | 40px | 28px |
| 表の行 | 32px（詰めて 24〜28px） | 40〜48px | 28px |
| 間隔 | 4px 刻み | 4px 刻み | 4px 刻み |
| 密度 | 利用者が切り替える | — | 1つに固定 |

## 5. 日本語の UI の文字組（Windows）

### 5.1 字体の候補

| 字体 | Windows での入手 | 太さ | 性格 | 注意 |
|---|---|---|---|---|
| Yu Gothic UI | Windows 10（1507）以降に標準。Windows 11 の日本語の UI の字体 | Light, Semilight, Regular, Semibold, Bold | OS の画面と同じ見え方。字面が小さめ | A0 で PO が見た字体。Regular は細く見えやすい |
| 游ゴシック（Yu Gothic） | Windows 8.1 以降。Medium は Windows 10（1507）で追加 | Light, Regular, Medium, Bold | 字面が小さく、ゆとりがあり、1字ずつの見分けがよい（SmartHR の採用理由） | `font-family: "Yu Gothic"` だけだと細い Regular が当たる。`@font-face` の `local("Yu Gothic Medium")` で Medium を当てる回避が広く使われる |
| メイリオ（Meiryo） | Windows Vista 以降 | Regular, Bold | 画面で読むために作られた。字形が大きく開いていて、小さい大きさでも読める | 行の高さの既定が大きい（`line-height` を指定すれば問題にならない）。中間の太さが無い |
| BIZ UDPゴシック | Windows 10 October 2018 Update（1809）以降に標準。2022 年から OFL でも配布 | Regular, Bold の2つだけ | UD（ユニバーサルデザイン）の字体。見分けやすさと読みやすさを重視。字面が大きい | 中間の太さが無い。プロポーショナル版（P）は欧文・記号も自然な幅。等幅版（BIZ UDゴシック）は Ω や中点が全角になる（実測） |
| Noto Sans JP | 標準では入らない。OFL | 100〜900（可変） | どの OS でも同じ見え方。DADS の基本書体 | 同梱が要る。OFL に予約字体名 'Source' がある（名前に使わなければよい） |

出典: [Yu Gothic の一族](https://learn.microsoft.com/ko-kr/typography/font-list/yu-gothic)、[Windows の文字（日本語は Yu Gothic UI）](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography)、[Meiryo](https://learn.microsoft.com/en-us/typography/font-list/meiryo)、[窓の杜: BIZ UD の標準搭載](https://forest.watch.impress.co.jp/docs/news/1149745.html)、[Google Fonts: BIZ UD の公開](https://fonts.googleblog.com/2022/04/morisawa-biz-universal-design-ud.html)、[SmartHR の書体](https://smarthr.design/basics/typography/)、[PoC: Perfect Yu Gothic](https://neos21.github.io/poc-perfect-yu-gothic/)、[DADS](https://design.digital.go.jp/dads/foundations/typography/)

**自分で確かめたこと**（Chrome、Google Fonts から読んだ字体。`/tmp/design-v2-check/out/fonttest.png`）:
- 同じ 14px でも、BIZ UDPゴシックは Noto Sans JP（と Noto Sans CJK JP）よりひと回り大きく見える。BIZ UDPゴシックの 14px ≈ Noto の 15px。
- BIZ UDPゴシックは `47 µH`、`6 Ω`、`1/(V·s)` が自然な幅で出る。BIZ UDゴシック（等幅版）は `Ω` と `·` が全角の幅になり、数値の欄には使えない。

### 5.2 欧文と和文の並べ方

- **欧文の字体を先に置く理由は今も有効。** 和文の字体を先にすると、µ や Ω が全角の字形になる字体がある（BIZ UDゴシックで実測）。ただし「先頭が欧文、和文は OS 任せ」だと、和文が何で出るかを制御できない（A0 の問題）。和文の字体を名前で明示する。
- **`@font-face` の `local()` と `unicode-range` で、1つの名前の中に欧文と和文を組み合わせられる。** さらに `size-adjust`（Chrome 92 以降）で、和文だけを数 % 大きくして欧文と字面をそろえられる（[web.dev](https://web.dev/articles/css-size-adjust?hl=en)）。游ゴシックの細さには、`local("Yu Gothic Medium")` を通常の太さに当てる回避がある（5.1）。
- **約物の詰め:** `text-spacing-trim`（Chrome 123）で、続く約物（「、」「）」など）の余分な空きを詰められる（[Chrome 123](https://developer.chrome.com/blog/chrome-123-beta?hl=en)）。**和欧間の空き:** `text-autospace` で和文と欧文・数字の間に少し空きを入れられる（Chrome 139 で出荷の予定が示され、既定で効くか選んで効かせるかは議論が残る。[blink-dev](https://groups.google.com/a/chromium.org/g/blink-dev/c/gwRvkPJ5pws)、[Chrome の解説](https://developer.chrome.com/blog/css-i18n-features)）。このアプリの文言は「不足 2 件」「Ctrl+Enter」のように和欧が混ざるので、効かせると幅が変わる。改訂では明示的に指定する。
- **大きさ:** 日本語は 14px 以上（DADS）。12〜13px は数字と欧文だけ（目盛り、時刻）。
- **行の高さ:** 1行の UI（ボタン、表の行）は部品の高さで決まるので、字の行の高さは 1.4 前後。複数行の文（お知らせ、説明）は 1.5 以上（DADS）。
- **字間:** 本文 0。日本語の 1行の UI で 0〜2%（DADS は 2%）。等幅は 0。
- **太さ:** 強弱は 400 と 600〜700 の2段。BIZ UDPゴシックは 400 と 700 しかない。Noto Sans JP は可変で 500・600 を使える。斜体は使わない（`design-system.md` 4.1 のまま）。

### 5.3 OS の字体か、同梱か

| | OS の字体 | 同梱 |
|---|---|---|
| 見え方 | OS ごとに違う（Windows は Yu Gothic UI / BIZ UDP、macOS はヒラギノ、Linux は Noto CJK）。Atlassian はこれを理由に同梱へ移った | どの OS でも同じ |
| 大きさ | 0 | 下の表 |
| ライセンス | 不要 | OFL 1.1。決まり7により決定記録が要る。字体だけを売らない、ライセンス文を同梱する、改変版（字の絞り込みを含む）に予約字体名を使わない |
| 端末の外に出ないこと（決まり9） | 問題なし | Web の字体の読み込み（Google Fonts）は使えない。Python のパッケージに WOFF2 を入れ、ローカルのサーバーから出す |
| 字の欠け | 無い（OS の字体は第3・第4水準まで持つ） | 絞り込むと、利用者のデータ（出どころの文字列、ファイル名）の珍しい漢字が別の字体に落ちる。後ろに OS の字体を並べて受ける |

**同梱したときの大きさ（実測）。** Google Fonts の配布物を fontTools で WOFF2 にし、JIS X 0208（6,879 字）と欧文・ギリシャ文字・記号に絞ったもの。

| 字体 | TTF | WOFF2（全部） | WOFF2（JIS X 0208 ＋ 欧文） |
|---|---|---|---|
| BIZ UDPゴシック Regular | 4.67 MB | 2.20 MB | 1.55 MB |
| BIZ UDPゴシック Bold | 4.64 MB | 2.24 MB | 1.56 MB |
| Noto Sans JP（可変、100〜900） | 9.59 MB | 4.35 MB | 2.09 MB |
| JetBrains Mono（可変） | 0.19 MB | 0.07 MB | — |

- 計算: `/tmp/design-v2-check/scripts/` の `measure_fonts.py`（字体の取得元は google/fonts の `ofl/` の各フォルダ）。
- ライセンス: BIZ UDPゴシック、JetBrains Mono は OFL 1.1、予約字体名なし。Noto Sans JP は OFL 1.1、予約字体名 'Source'。

## 6. このアプリへの当てはめ方

### 6.1 「見やすさ」と「情報の量」の両立

PO の言う見やすさと、設計者が仕様 21 項目と波形と読み取りを同時に見たいことは、次の分け方で両立させる。

1. **文字の大きさは下げない、行の数で調整する。** 日本語は 14px 以上、数字と欧文だけ 12〜13px。詰めるときは、行の高さと余白を減らす（文字を小さくしない）。Material と Carbon も、密度は高さで変え、文字の大きさは変えない。
2. **強弱は「大きさ1段 ＋ 太さ1段 ＋ 面の色」で作る。** 見出しは本文より大きく太く、領域の見出しには帯の面を付ける。記号（`L`、`f_sw`）は太い等幅、項目名は灰色。何が主で何が従かが、文字を読む前に分かる。
3. **横幅は空白でなく情報に使う。** 1920 幅では、波形の下に置いていた読み取りの表を右の列に出し、波形に高さを回す（v0.2 の「目標値と結果」もこの列に入る）。1280 幅では v0.1 と同じく波形の下に畳む。
4. **密度は利用者が選ぶ。** 「標準 / 詰める」の2段（JetBrains の Compact Mode と同じ考え）。既定は標準。
5. **和文の字体を名前で指定する。** OS 任せにしない。候補は Yu Gothic UI（OS の見え方）、BIZ UDPゴシック（字面が大きく、Windows の日本語版には入っている）、Noto Sans JP（同梱）。
6. **実物で見る。** UX の確認は、1920×1080・表示倍率 100%・Windows の字体で、今の製品（VS Code、PLECS など）と並べて見る（受け入れ基準4）。

### 6.2 決まりの案（範囲3で `design-system.md` に入れる候補）

| 項目 | 案 |
|---|---|
| 本文 | 14px / 20px（案A は 16 / 24） |
| 日本語の最小 | 14px |
| 数字・欧文の最小 | 12px（軸の目盛り、時刻の札） |
| 見出し | 領域 15〜16px・600、群 14px・600 ＋ 帯 |
| 部品の高さ | 32px（詰める: 28px） |
| 表の行 | 30px（詰める: 26px） |
| 間隔 | 4px 刻み（4, 8, 12, 16, 24, 32） |
| 等幅 | 数値・記号・信号名だけ。`tabular-nums` |
| 表示倍率 | ブラウザが 100% 未満のとき、お知らせで知らせる案（検出の方法は ui-engineer と決める） |

## 7. 方向の案

3つの案を、別々の軸で作った。見本は `/home/claude/design-v2/project/`（デザインのキャンバス）、確かめた画像は `/tmp/design-v2-check/`。
3案とも、降圧コンバータのサンプル（付録A.4、A.5 の値）を開いて実行した状態。波形は E1b のコード（`/home/claude/wt/E1b`）で計算した本物の結果（終了時刻 8.62 ms、時間刻み 50 ns、172,400 ステップ、所要時間 1.2 s）を、列ごとの最小・最大で描いた。カーソルは A = 115.700 µs（`i(L1)` の最大）、B = 2.00000 ms。

| 案 | 軸 | 文字 | 字体 | 構成 | 見本 |
|---|---|---|---|---|---|
| A「ゆとり」 | 読みやすさ（文字と余白） | 本文 16px、部品 36px、行 36px | BIZ UDPゴシック（Windows は端末、ほかは同梱）＋ JetBrains Mono | v0.1 と同じ2列 | `A-buck.dc.html`、`A-start.dc.html` |
| B「一覧」 | 情報の構造と強弱（密度） | 本文 14px、部品 32px、行 30px、密度の切り替え | OS の字体（Segoe UI Variable ＋ Yu Gothic UI、Cascadia Mono / Consolas） | 3列（仕様 / 回路図とスコープ / 読み取り）。1600 未満は2列 | `B-buck.dc.html`、`B-start.dc.html`、`B-buck-dark.dc.html` |
| C「計測器」 | 波形と読み取りが主役（暗いテーマを基準） | 本文 14px、読み取りの数字 20px | Noto Sans JP ＋ JetBrains Mono（同梱） | 2列。読み取りは波形の下の大きな数字の帯。起動直後は回路図つきのサンプル | `C-buck-dark.dc.html`、`C-start-dark.dc.html` |

**測った違い**（Chrome、`/tmp/design-v2-check/scripts/measure.py`。Linux で撮ったので、案B の字体は Windows と違う）

| | A | B | C |
|---|---|---|---|
| 1920×1080: 波形の1段の高さ | 219px | 331px | 301px |
| 1920×1080: 仕様の行（22 行）のうち見える数 | 19 | 22 | 22 |
| 1280×720: 波形の1段の高さ（v0.1 の基準 150px） | 59px | 150px | 109px |
| 1920×1080: 日本語の最小 | 14px | 14px | 14px |
| 同梱する字体 | 約 3.2 MB（BIZ UDP の2つの太さ）＋ 0.07 MB | 0 | 約 2.2 MB |

各案の理由と失うものは、オーケストレーターへの報告に書いた。範囲3で、PO が選んだ案をもとに `design-system.md` と `screens-v0.1.md` を改訂する。

## 8. 変更の記録

- 2026-11-16（ux-designer、A0r）: 初版。
