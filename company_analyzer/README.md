# 企業サイト解析・スコアリングエンジン

エッジAI・IoT・組み込み開発の協力企業候補を自動スコアリングするシステムです。
IPROSスクレイパーの出力Excelを入力として、企業サイトを解析し営業候補をランキング出力します。

## ディレクトリ構造

```
company_analyzer/
├── main.py                        # エントリポイント
├── config.py                      # 設定・キーワード辞書
├── models.py                      # データモデル（Pydantic）
├── requirements.txt
├── crawler/
│   ├── crawler.py                 # 非同期クローラー（aiohttp）
│   └── robots.py                  # robots.txt 遵守
├── parser/
│   └── site_parser.py             # HTML解析（BeautifulSoup）
├── feature_extractor/
│   └── extractor.py               # 特徴量抽出
├── scoring/
│   └── scoring_engine.py          # スコアリング（差し替え可能設計）
├── storage/
│   └── storage.py                 # CSV/JSON/Excel出力
├── pipeline/
│   └── pipeline.py                # 全モジュール統合
├── output/                        # 出力ファイル（自動生成）
└── tests/
    └── test_pipeline.py           # 単体テスト
```

## インストール

```bash
pip install -r requirements.txt
```

## 使い方

```bash
# IPROSスクレイパーのExcelを入力
python main.py --input ipros_companies.xlsx

# 出力形式をExcelのみに絞る
python main.py --input ipros_companies.xlsx --format excel

# 同時接続数を調整（デフォルト20）
python main.py --input ipros_companies.xlsx --concurrency 10

# URLを直接指定してテスト
python main.py --url https://example.com

# 営業候補スコアの閾値を変更（デフォルト70）
python main.py --input results.xlsx --threshold 60

# デバッグログを有効化
python main.py --input results.xlsx --debug
```

## スコアリング基準

| 項目 | スコア | 判定条件 |
|------|--------|----------|
| 自社製品保有 | +40 | 型番・スペック表記・製品ページの検出 |
| OEM/共同開発対応 | +20 | OEM・ODM・受託開発キーワードの検出 |
| IoTキーワード | +10 | IoT・M2M関連技術用語の検出 |
| 組み込みキーワード | +10 | 組み込み・FPGA・RTOSなどの検出 |
| 画像処理キーワード | +10 | CV・カメラ・画像認識の検出 |
| AI系エンジニア採用 | +10 | 採用ページのAIエンジニア募集 |
| 組み込み系エンジニア採用 | +5 | 組み込みエンジニア募集 |
| サイト更新あり | +5 | ニュース・ブログ・プレスリリース |
| コンタクト手段あり | +5 | メール・問い合わせフォーム |

**70点以上を営業候補（◎）として出力**

## 出力ファイル

`output/` ディレクトリに以下を生成：

- `analysis.xlsx` - スコア順・候補ハイライト付きExcel（サマリーシート付き）
- `analysis.csv`  - CSV形式（他ツール連携用）
- `analysis.json` - JSON形式（将来のAI分類連携用）

## テスト実行

```bash
pytest tests/ -v
```

## 将来の拡張ポイント

- `scoring/scoring_engine.py` の `ScoringStrategy` を差し替えることでLLM分類に移行可能
- `feature_extractor/extractor.py` の `_detect_tech_stack()` でフレームワーク検出を拡張
- `config.py` のキーワード辞書を編集して検索対象を調整可能
