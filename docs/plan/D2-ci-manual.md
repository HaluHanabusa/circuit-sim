---
id: D2
owner: tooling-engineer
needs_ux: false
gates: implement, check
---

# D2: GitHub上の自動テストを、手で起動したときだけ走るようにする

POからの連絡: 今月の GitHub Actions の無料枠が残っていない。push のたびに走る設定をやめ、手で起動したときだけ走るようにする（D-031）。
設定1か所の変更なので、ゲートは implement と check。Windowsでの確認は、POのPCで行う形に切り替える（W2）。

## 受け入れ基準

| # | 基準 | 合格の条件 |
|---|---|---|
| 1 | 起動条件 | `.github/workflows/ci.yml` の起動条件が `workflow_dispatch` だけになっている。手で起動したときに、ジョブがスキップされずに走る条件になっている |
| 2 | 後退が無いこと | `python scripts/check.py` が通る |
