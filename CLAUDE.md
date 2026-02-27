# 漢字書き順ジェネレーター

## プロジェクト概要
AIを使って漢字の書き順SVGデータを自前生成し、ブラウザ上でアニメーション表示する実験プロジェクト。
既存のオープンソースデータ（KanjiVGなど）に依存せず、自分だけの書き順データを構築することが目標。

- デプロイ先: https://kanji-stroke-a8h.pages.dev
- Cloudflare Pages で静的ホスティング
- 親プロジェクト: 宿題レスキュー（homework-rescue）の将来機能として書き順学習を検討中。単体アプリとしての別リリースも視野。

## デプロイ方法
```bash
npx wrangler pages deploy . --project-name=kanji-stroke --branch=main --commit-dirty=true
```

## 現在の状態（2026-02-27）

### 動いているもの
- **アニメーションエンジン**: CSS transition + stroke-dashoffset 方式。モバイルでも問題なく動作。
- **Noto Serif JP フォントオーバーレイ**: Google Fonts から読み込んだ Noto Serif JP (weight:900) を薄いグレーで背景表示。その上にSVGストロークを重ねて精度を目視比較できる。
- **UI**: 書き順スタート / リセット / 参照ON・OFF / 色分けトグル

### 未解決の課題
- **SVGパス座標の精度が低い**: AIが「記憶」から生成した座標は、フォントの字形と大きくずれる。「山」のような単純な漢字でも崩壊する。
- 精度の高い座標データの生成方法が確立されていない。

## 実験の経緯

### 試行1: SVG SMIL animate（homework-rescue/test-stroke.html）
- `<animate begin="btn.click">` でストロークアニメーション
- **結果**: PCでは動いたがモバイルブラウザで動作せず。SVG SMILのサポートが不安定。
- **判定**: 不採用

### 試行2: JS + CSS transition（homework-rescue/frontend/test-stroke.html）
- `stroke-dasharray` / `stroke-dashoffset` をJSで設定し、CSS transition でアニメーション
- `setTimeout` で各画の開始タイミングを制御
- **結果**: PC・モバイル両方で安定動作
- **判定**: 採用。現在のアニメーションエンジンの基盤。

### 試行3: Noto Serif JP オーバーレイ（本プロジェクト index.html）
- フォントで描画した文字を薄く背景に表示し、その上にAI生成のSVGストロークを重ねる
- 視覚的にズレを確認しやすい
- **結果**: 比較ツールとしては優秀。ただしAIが生成する座標が不正確で字形が崩壊。
- **判定**: 比較ツールとして継続活用。座標生成の精度向上が必要。

## 技術的な知見

### アニメーション方式
```
方式A: SVG <animate> (SMIL)     → モバイル非対応。不採用。
方式B: JS + CSS transition       → 全環境で安定動作。採用。
方式C: requestAnimationFrame     → 未検証。Bで十分なので不要。
```

### CSS transition アニメーションの仕組み
```javascript
// 各パスの長さを取得して非表示にする
const len = path.getTotalLength();
path.style.strokeDasharray = len;
path.style.strokeDashoffset = len;   // 完全に隠す

// アニメーション開始: transitionを設定してoffsetを0にする
path.style.transition = `stroke-dashoffset ${duration}ms ease`;
path.style.strokeDashoffset = "0";   // 描画される
```

### 座標系
- 現在の index.html は viewBox="0 0 300 300" の座標系
- KanjiVG は viewBox="0 0 109 109" を使用
- 座標系は統一した方が後々楽

## 既存のオープンソースリソース調査

### KanjiVG
- URL: https://github.com/KanjiVG/kanjivg
- ライセンス: CC BY-SA 3.0（商用利用可、改変可、継承必須）
- 内容: 約6500字のSVG書き順データ。各画がpath要素で分離済み。
- 懸念: ライセンスの継承義務、作者の方針変更リスク

### animCJK
- URL: https://github.com/parsimonhi/animCJK
- ライセンス: LGPL + Arphic Public License
- 内容: CJK文字のアニメーション表示ライブラリ。SVGデータ内蔵。
- 備考: 実装の参考になるが、データはArphicフォント由来

### フォント（参照用に安心して使えるもの）

| フォント | ライセンス | 安全性 | 特徴 |
|----------|-----------|--------|------|
| Noto Serif CJK JP | SIL OFL 1.1 | 極めて高い（Google/Adobe） | とめ・はね・はらい再現。楷書的。 |
| IPAex明朝 | IPA Font License（OSI認定） | 極めて高い（国の機関IPAが公開） | 教科書体に近い。日本語特化。 |
| 源ノ明朝 (Source Han Serif) | SIL OFL 1.1 | 極めて高い（Adobe） | Noto Serif CJK の Adobe 版（同一データ） |

- **SIL OFL 1.1**: 商用利用・改変・再配布すべてOK。フォント名を変更して再配布する場合のみ制限あり。
- **IPA Font License**: 商用利用OK。フォント自体の改変配布時に一部条件あり（派生フォント明示等）。
- いずれも長年普及しており有料化リスクは極めて低い。

### 筆順指導の手びき
- 文部省（現・文部科学省）が1958年に刊行
- 小学校で教える漢字の標準的な書き順を定めた公的資料
- 著作権は消滅（パブリックドメイン）
- 書き順の「正解」の根拠として利用可能

## 今後の方針（検討中）

### アプローチ候補

1. **フォントグリフ抽出**
   - IPAex明朝やNoto Serifのフォントファイル(.otf/.ttf)からグリフのアウトラインを抽出
   - fontTools (Python) などでパスデータを取得
   - 問題: フォントのパスは「画の順番」情報を持たない。アウトラインであってストロークではない。

2. **AI Vision + 参照画像**
   - フォントで描画した漢字の画像をAIに渡し、「この字の各画のSVGパスを生成して」と指示
   - 参照画像があれば、記憶だけよりは正確になる可能性
   - まだ未検証

3. **手動微調整 + AIアシスト**
   - AIが大まかな座標を生成 → フォントオーバーレイで目視 → 手動で座標修正
   - 確実だが手間がかかる
   - 小1配当漢字(80字)だけなら現実的

4. **KanjiVG をフォールバックとして使う**
   - 自前生成がうまくいかない漢字はKanjiVGデータを使用
   - CC BY-SA 3.0 の表記義務あり
   - 最悪のケースの保険として

### 優先順
- まず「AI Vision + 参照画像」(アプローチ2)を試す
- それでも精度不足なら「手動微調整」(アプローチ3)で小1漢字80字を攻略
- 時間対効果が悪ければKanjiVGフォールバック(アプローチ4)

## ファイル構成
```
Kanji_Stroke/
├── index.html          # メインのHTML（CSS・JSインライン、単一ファイル）
├── CLAUDE.md           # このファイル（プロジェクト説明）
└── .git/               # gitリポジトリ
```

## 宿題レスキューとの関係
- 宿題レスキューのROADMAP.mdの「アイデアメモ」に書き順アニメーションの項目がある
- 本プロジェクトで精度が確保できたら、宿題レスキューの漢字ヒントに組み込む予定
- animCJKの実装を参考に自前実装する方針
