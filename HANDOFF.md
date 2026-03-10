# HANDOFF - 漢字書き順エディタ

## 現在の状態（セッション24末）

### セッション24で実装したこと
- **Cloudflare Workers + KV によるAPI構築**
  - `worker/src/index.ts`: 4エンドポイント（list/load/save/kvg）
  - KVネームスペース作成済み（ID: `eb74b298326846c79ad4e05a5c2fb21e`）
  - デプロイ済み: `kanji-stroke-api.yagukyou.workers.dev`
- **editor.htmlのAPI_BASE自動切替**
  - localhost → ローカルサーバー、本番 → Worker
- **Cloudflare Pagesへのデプロイ**
  - `kanji-stroke-a8h.pages.dev/editor` でスマホからアクセス可能
- **`.claude/settings.local.json` のMCPルールエラー修正**
  - `mcp__ide__executeCode(*)` → `mcp__ide__*`

### アクセスURL
- エディタ: https://kanji-stroke-a8h.pages.dev/editor
- API: https://kanji-stroke-api.yagukyou.workers.dev

### 既存のエディタ機能（セッション23までに実装済み）
- ライトテーマ、5ツール（描画/選択/消しゴム/Undo/スナップ）
- 直線/曲線トグル、はね・はらいボタン
- KVG+Mine対のステップパネル
- 選択ツール（2段階）、キーボードショートカット
- フォント中心線表示（KleeOne-SemiBold）

### 未実装
- **タイル一覧**（漢字を一覧で見る画面）
- **テーパー描画**（はね・はらいの太さ変化の視覚表示）
- **既存stroke_dataのKVへの移行**（ローカルにのみ存在）

## ブランチ
`fix/skeleton-graph-improvements`（未コミットの変更多数 - stroke_data, viz_output等）

## 次のアクション
1. エディタUIの改善・フィードバック対応
2. スマホでの操作感テスト
3. 漢字の手入力作業開始
4. タイル一覧の実装

## 注意事項
- `py` コマンドを使う（`python` ではない）
- `PYTHONUTF8=1` を設定してから実行
- Googleドライブ上で`npm install`不可 → `/tmp/`にコピーして作業
- Workerデプロイ: `cd /tmp/kanji-worker && npx wrangler deploy`
