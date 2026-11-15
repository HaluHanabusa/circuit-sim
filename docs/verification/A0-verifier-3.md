# A0（アプリの骨組み）の独立検証（3回目: 最後の修正の差分）

- 担当: verifier（実装の ui-engineer とは別。実装の作業は見ていない）
- 対象: ブランチ `A0`、先頭 `70a1e1d`、ツリー `5fdbc3cb05a2eb65f35fc7565f8e0a93a8c5bf35`。差分は `git diff f976c11 70a1e1d`
  （3回目のレビューの軽3件と、2回目の検証の軽微1件の修正）
- 前回の記録: `docs/verification/A0-verifier-2.md`（合格、軽微1件）
- 日付: 2026-11-16
- 環境: Linux、Python 3.13、Chrome for Testing 153（headless）、Python の playwright。起動の確かめは
  `git archive 70a1e1d` の写しを、まっさらな仮想環境に `pip install` して行った（fastapi 0.163.1、starlette 1.10.0、
  uvicorn 0.54.0）。試験と起動は `PATH=/usr/local/bin:/usr/bin:/bin`（本物の claude の無い PATH）。設定の場所は
  すべて一時フォルダ。使い捨ての道具は `/tmp/a0-verify-4/`（作業ツリーの外。消える）。繰り返せるものは受け入れ
  試験にした。

## 判定: 合格

4件のうち3件（「使い終わった URL」の案内、場所の ACL の判定、読み上げの言語）は直った。RST の件は、POSIX では
直った（RST を使わなくなり、遅い相手にも最後まで届く）が、Windows の分岐は、OS がすでに受け取った分を RST で
捨てるので、遅い相手には届かない（問題1、軽微。止める瞬間に送っている応答だけに限られる）。ほかに SDDL の
解析の取りこぼしが1件（問題2、軽微。場所の判定は誤らない）。前回の基準 1〜6・8 と安全の確かめに後退は無い。
重大な問題は無い。

## 確かめたことごとの結果

時間のしきい値は前回と同じく仕様の値（起動 5 秒など）。自分で決めた許容差は無い（今回の確かめは、届いたバイト
列がファイルと一致するか、端末に案内が出たか、判定が真か偽か、で数値の許容差が要らない）。

| 確かめたこと | 実測 | 測り方 | 合否 |
|---|---|---|---|
| 1a 止めるとき、遅い相手に最後まで届く（POSIX、製品のまま） | 12/12 で最後まで届き、切れない（画面の JS 307,959 B と CSS 16,185 B × 受け取りの量 4096 と既定 × 読む間隔 0・2・20 ms。読み終えるのに最長 3.21 s）。終了コード 0 | アプリ本体を別のプロセスで起動し、要求を送って 1 s 読まずに SIGINT、その後に決めた間隔で読む。届いた本文をファイルと比べる | 合格 |
| 1b 同じ（Windows の RST の分岐） | Linux で分岐を強制すると 5/12 で切れる（JS は 10,240・67,584・128,000・128,000 B、CSS は 4,096 B で RST）。実装の試験 `test_slow_reader_gets_the_whole_response_when_stopping` も、分岐を強制すると 8/8 で落ちる（強制しなければ 3/3 で通る。Linux では分岐を通らない） | `reset_idle_connections` の windows を真にして同じことをした。実機の Windows は未確認 | 不合格（軽微、問題1） |
| 2 「使い終わった URL」の案内 | 生の要求: ヘッダーなし、Accept: text/html、no-cors の画像とスクリプト、cors、same-origin、websocket、`navigatex` の 8 通りで出ない。navigate（大文字も）で出る。実際の Chrome で、別のポートのページから画像・スクリプト・スタイル・fetch（no-cors、cors）・prefetch・音声と動画として読んでも出ない。そのページから location で開くと出る | 使い終わった番号の URL を、クッキーなしで送る。端末の出力で見る | 合格 |
| 3 SDDL の解析（Linux で確かめられる部分） | 自分で書いた 25 通りのうち 22 通りで正しい。継承だけ（OICIIO、OIIO、CIIO、IO、IOOICI、OICIIOID、CREATOR OWNER）、条件つき（XA、入れ子のかっこと中かっこ、ZA）、OA、監査の種類 AU、DACL なし、NULL DACL はどれも本人だけと誤らない。場所の判定（完全一致）は 25 通りすべて正しい。誤るのは、条件の文字列の中に `)` がある 2 通り（ファイルを本人だけと判定する）と、項目のあいだに余計な文字がある 1 通り（Windows は出さない形） | 期待値は SDDL の書式（MS-DTYP 2.5.1）から自分で決めた。ファイルの判定は `_Win32.private` と同じ合わせ方で、`sddl_trustees` を直接呼ぶ | 合格（軽微1件、問題2） |
| 4 読み上げの言語 | 日本語のファイル名（`回路_試作.json`）の開けなかったお知らせで、日本語 → English → 日本語と切り替えるたびに、読み上げの文がお知らせの行に見える文と同じ。English では日本語はファイル名だけ。次の開けなかったお知らせに置き換わり、言語にも付いてくる。好みを保存できなかったお知らせ（uid 4242 で場所を 0500 にする）も English に切り替わる | 実際の Chrome。`data-testid=announce` と `notice-row` の文を比べる | 合格 |
| 5 前回の基準と安全（要点） | 起動: 3.10〜3.97 s（5 回、check.py と並んで混んだ状態）、ネットワークの無い名前空間で 3.30〜3.82 s（3 回）、画面からの外部への要求 0、ブラウザの引数は `open-….html` だけ。止める → 帯 0.57〜2.28 s（4 回）、同じポートで起動し直す → 帯が消えるまで 0.79〜0.82 s、枠は残る。別のポートのページからの書き込み 80 通りで状態の変化 0（対照の同じ Origin の PUT は 200）。生の要求の Origin・Content-Type・Host・パスは前回と同じ結果（すべて 403・415・400・404、対照だけ 200）。画面の CSS のビルドは前回と同じもの（`index-BRkRmiLu.css`） | 前回の道具を新しい写しに向けて流し直した | 合格 |
| 6 check.py | 追加した試験を置く前: 1795 passed、18 skipped、10 xfailed、終了コード 0（13 分 9 秒）。置いた後は下の「check.py の結果」 | 作業ツリーで、claude の無い PATH で | 合格 |

## 見つけた問題

### 1. 軽微: Windows で止めるとき、読むのが遅い相手への応答の残りを RST で捨てる

- 内容: RST の印（`SO_LINGER` 0 秒）は、応答を渡し終えて transport の送りの待ち（`get_write_buffer_size()`）が空の
  接続に付く。RST で閉じると、OS の送りの待ちにあるデータも捨てられる（Windows の closesocket の hard close も同じ）。
  相手が読むのが遅いと、応答の残りは transport ではなく OS の送りの待ちにあるので、印が付いて捨てられる。
  関数の説明の「OS が送ったが相手の受け取りの確かめがまだのデータ（止める直前に書き終えた応答の終わり）は、
  ごくまれに捨てうる」より大きい。Linux で強制すると、約 300 KB のうち 10〜130 KB しか届かない。Windows で OS が
  受け取る量（どれだけ失うか）は確かめていない。
- 影響: 止める瞬間に送っている応答だけ。もう止まるところなので、画面の続きは使えない。ただし画面の JS を読み込んで
  いる最中に止めると、画面は「つながっていない」の帯も出せない形で切れる（起動し直して、手で開き直す）。
  POSIX は RST を使わないので起きない。
- 再現（Linux）: `tests/acceptance/test_a0_fix3_stop_stale_sddl.py::test_slow_reader_gets_the_whole_asset_with_the_windows_reset_branch`
  （xfail。Linux では strict、Windows では strict にしない）。手で: 受け取りの量を 4096 にしたソケットで
  `/assets/index-….js` を求め、1 秒読まずに待ってから止め、さらに 1 秒待ってから読む。
- 想定される原因: transport の送りの待ちだけを見ていて、OS の送りの待ちを見ていない。案: OS の送りの待ちも見る
  （Windows は `SIO_TCP_INFO`、Linux は `SIOCOUTQ`）、または送り終わったものに限るために、相手の受け取りを短く
  待ってから RST にする。
- PO の Windows で: 実装の試験 `test_slow_reader_gets_the_whole_response_when_stopping` は Windows では分岐を通る。
  Windows の OS が残りを受け取っていれば、この試験が落ちる（local_ci で落ちたら、この問題）。

### 2. 軽微: 条件つきの項目の文字列の中の `)` で、SDDL の後ろの項目を読み落とす

- 内容: `sddl_dacl` はかっこの深さで項目の終わりを見つけるので、条件の文字列（MS-DTYP 2.5.1.1 の
  `char-string = DQUOTE *(CHAR) DQUOTE`。`(` も `)` も入る）の中の `)` を項目の終わりと読む。その後ろが `(` で
  始まらないと読むのを止め、それまでの項目だけを返す（「読めない形なら None」にならない）。後ろに Everyone の
  許可があっても、`sddl_trustees` に出ず、ファイルを本人だけと判定する。
- 影響: ファイル（合言葉など）の判定だけ。場所の判定は完全一致なので誤らない（付け直す）。そのような項目を
  ファイルに付けられるのは DACL を書ける人（本人か管理者）だけ。
- 再現: `sddl_trustees('D:P(A;;FA;;;<本人>)(XA;;FA;;;SY;(@User.D == "R)D"))(A;;FA;;;WD)')` が `{<本人>, 'SY'}`
  （WD が無い）。試験は `test_sddl_with_parenthesis_inside_a_condition_string`（xfail、strict）。
- 想定される原因: 文字列の中（`"` から `"` まで）を、かっこの数え方から外していない。項目の並びの後ろが
  `S:` でも終わりでもないときに None にしていない。

### 観察（問題としない）

- 「使い終わった URL」の案内は、別のサイトのページの iframe・object・embed（どれも Sec-Fetch-Mode: navigate）からは
  まだ出る。同じ PC のほかの利用者の道具も、ヘッダーを足せば出せる。出るのは本人の端末で、Enter で出る新しい URL
  も本人の端末だけなので、漏れにはならない（前回と同じ見立て）。絞るなら Sec-Fetch-Dest: document も見る。
- 実装担当が、2回目の検証の受け入れ試験 `test_a0_browser_recheck.py` から xfail の印（9 行）だけを外した。2回目の
  記録の「直ったら XPASS で落ちるので、印を外す」のとおりで、確かめる中身は変わっていない。
- English の形の誤りのお知らせが「(1 place(s))」になる（今回の差分の外。UX の確かめに任せる）。
- 生の要求で Origin を2つ送る（別のサイト、自分の順）と書ける。ブラウザは Origin を2つ送らず、ブラウザでない道具は
  合言葉を持たないので、漏れにはならない。今回の差分は Origin の確かめに触れていない。

## テストの実効性についての所見

- 追加した試験は、製品を1か所ずつ壊した写し6通りですべて落ちることを確かめた: 案内を Sec-Fetch-Mode で絞らない
  （生の要求とブラウザの両方が落ちる）、POSIX でも RST の印を付ける、継承だけ（IO）を数えない、条件つき（XA）を
  拒否と同じに読み飛ばす、場所の判定を前の形（`D:P` と相手の集合）に戻す、画面を前の版（f976c11 のビルド）に戻す。
  xfail の3件は、直れば XPASS で落ちる。
- 実装の試験: RST の件の `test_slow_reader_gets_the_whole_response_when_stopping` は Linux では分岐を通らないので、
  Linux では「POSIX は RST を使わない」ことしか見ていない（POSIX でも印を付ける形に壊すと落ちるので、その意味では
  効いている）。`FakeTransport` の単体の試験は、OS の送りの待ちを表せないので、問題1を見つけられない。
  案内の件と SDDL の件の実装の試験は、壊した形で落ちる（案内の件は生の要求だけで、実際のブラウザの種類は見ていない）。
- 追加した試験は3回続けて流して、毎回同じ結果（26 passed、3 xfailed）。

## 追加した試験（新しいファイルだけ。既存の試験・記録・製品のコードは変えていない）

- `tests/acceptance/test_a0_fix3_stop_stale_sddl.py`（ブラウザ不要。POの Windows の PC でも動く。遅い相手の
  製品のままの確かめは POSIX だけ）: 遅い相手に最後まで届く（POSIX、3 通りの読む速さ）、Windows の RST の分岐を
  強制して同じこと（xfail、問題1）、案内が navigate のときだけ出る（7 通り＋対照）、SDDL の解析 20 通り、`)` の
  2 通り（xfail、問題2）。
- `tests/acceptance/test_a0_fix3_browser.py`（playwright とブラウザが無ければ理由を残してスキップ）: 別のポートの
  ページの部分の読み込み（画像・スクリプト・スタイル・fetch・prefetch・音声と動画）で案内が出ず、location で開くと
  出る。読み上げの文が、日本語 → English → 日本語のたびに見えるお知らせの文と同じで、次のお知らせに置き換わる。

## check.py の結果

追加した2つのファイルを置いた作業ツリーで、`PATH=/usr/local/bin:/usr/bin:/bin python3 scripts/check.py`:
ruff は通り、pytest は 1821 passed、18 skipped（Windows・権限の分岐。前回より1件多いのは、新しい Windows だけの
ACL の試験）、13 xfailed（10 件は前からあるもの、3 件は問題1・2）、終了コード 0（11 分 2 秒。ほかの担当の作業と
並んで走った）。置く前は 1795 passed、18 skipped、10 xfailed、終了コード 0。前回 xfail だった読み上げの試験は通る。
画面のビルド結果とソースの一致、部品の試験（vitest）も中で流れた（node は /usr/local/bin）。

## PO の Windows の PC で確かめてほしいこと

- `python scripts\local_ci.py --ref origin/A0` が合格する。とくに `test_slow_reader_gets_the_whole_response_when_stopping`
  （落ちたら問題1）と、新しい `test_inherit_only_entry_on_the_folder_is_removed_on_windows`（場所に継承だけの
  Everyone を足しても、次の起動で付け直す）。追加した `test_a0_fix3_stop_stale_sddl.py` も流れる（RST の分岐の
  試験は Windows では strict にしていないので、落ちても通っても全体は落ちない。結果の XFAIL か XPASS を見てほしい）。
- 前回の一覧（`A0-verifier-2.md` の最後）はそのまま有効。
