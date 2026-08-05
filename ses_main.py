"""
ses_main.py - SES事業スクリーニング エントリポイント
=====================================================

使い方:
  # Excelリストから（会社名+URL列があればOK）
  python ses_main.py --input companies.xlsx

  # IPROSスクレイパー出力から
  python ses_main.py --input ipros_companies.xlsx

  # URLを直接指定してテスト
  python ses_main.py --url https://example.co.jp

  # Ollamaモデルを指定
  python ses_main.py --input companies.xlsx --model gemma3:4b

  # キーワード判定のみ（Ollamaなし）
  python ses_main.py --input companies.xlsx --no-ai

  # 同時接続数・出力名を変更
  python ses_main.py --input companies.xlsx --concurrency 5 --output ses_result_0312
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# パスを通す
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _check_ollama(model: str) -> None:
    """起動前にOllamaの状態を確認・案内"""
    from ollama_scorer import OllamaScorer
    scorer = OllamaScorer(model=model)

    if not scorer.is_available():
        log.warning("=" * 55)
        log.warning("  ⚠  Ollamaが起動していません")
        log.warning("  以下のコマンドで起動してください:")
        log.warning("    ollama serve")
        log.warning("")
        log.warning("  モデルが未インストールの場合:")
        log.warning(f"    ollama pull {model}")
        log.warning("=" * 55)
        log.warning("  → キーワード判定のみで続行します")
        return

    models = scorer.get_available_models()
    if model not in models and not any(model in m for m in models):
        log.warning(f"  ⚠  モデル '{model}' が見つかりません")
        log.warning(f"  インストール済み: {', '.join(models[:5])}")
        log.warning(f"  インストール: ollama pull {model}")
    else:
        log.info(f"  ✅ Ollama OK | モデル: {model}")


def main():
    parser = argparse.ArgumentParser(
        description="SES事業スクリーニングツール（Ollama対応）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input",       help="入力ファイル (.xlsx / .csv)")
    parser.add_argument("--url",         nargs="+", help="URLを直接指定")
    parser.add_argument("--output",      default="ses_analysis", help="出力ファイル名プレフィックス")
    parser.add_argument("--concurrency", type=int, default=10, help="同時クロール数 (default: 10)")
    parser.add_argument("--model",       default=None, help="Ollamaモデル名 (default: ses_config.pyの設定)")
    parser.add_argument("--no-ai",       action="store_true", help="キーワード判定のみ（Ollama不使用）")
    parser.add_argument("--debug",       action="store_true", help="デバッグログ")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # モデル上書き
    if args.model:
        from ses_config import OLLAMA_CFG
        OLLAMA_CFG.model = args.model

    # 企業リスト組み立て
    companies: list[dict] = []

    if args.input:
        from ses_pipeline import load_companies
        path = Path(args.input)
        if not path.exists():
            log.error(f"ファイルが見つかりません: {path}")
            sys.exit(1)
        companies = load_companies(path)

    if args.url:
        for url in args.url:
            if not url.startswith("http"):
                url = "https://" + url
            companies.append({"name": url, "url": url})

    if not companies:
        log.error("入力がありません。--input または --url を指定してください。")
        parser.print_help()
        sys.exit(1)

    log.info(f"対象企業数: {len(companies)} 社")

    # Ollama確認
    if not args.no_ai:
        from ses_config import OLLAMA_CFG
        _check_ollama(OLLAMA_CFG.model)

    # パイプライン実行
    from ses_pipeline import run_ses_pipeline
    results = asyncio.run(run_ses_pipeline(
        companies    = companies,
        output_prefix = args.output,
        concurrency  = args.concurrency,
        use_ollama   = not args.no_ai,
    ))

    # 最終サマリー
    from ses_config import OUTPUT_DIR
    output_path = OUTPUT_DIR / f"{args.output}.xlsx"
    excellent = sum(1 for r in results if r.judgment == "◎")
    good      = sum(1 for r in results if r.judgment == "○")
    print("\n" + "=" * 55)
    print(f"  ✅ 完了: {len(results)} 社解析")
    print(f"  ◎ 優先営業候補: {excellent} 社")
    print(f"  ○ 営業候補:     {good} 社")
    print(f"  📁 {output_path}")
    print("=" * 55)


if __name__ == "__main__":
    main()
