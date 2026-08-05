"""
ses_run.py - IPROSスクレイピング → SESスクリーニング 一気通貫エントリポイント
==============================================================================

【フロー】
  ① IPROSをキーワード検索してスクレイピング（Playwright）
  ② 企業リストを中間Excel保存（途中再開用）
  ③ 各企業の公式サイトをクロール
  ④ キーワード一次フィルタ（発注側/供給側）
  ⑤ Ollama（ローカルAI）でスコア・判定理由・営業トーク案を生成
  ⑥ SES候補企業をハイライト付きExcelで出力

【使い方】
  # SES関連キーワードで0からフル実行
  python ses_run.py --keyword SES システム開発 IT人材

  # キーワードを変えてIT・受託系企業を広く取る
  python ses_run.py --keyword 受託開発 ITコンサル DX推進 システム会社

  # 収集だけ（SESスクリーニングは後で）
  python ses_run.py --keyword SES --no-screen

  # 収集済みExcelから再開（スクレイピングスキップ）
  python ses_run.py --resume scraper_output/collected_20260312_120000.xlsx

  # AIなし（キーワード判定のみ・速い）
  python ses_run.py --keyword SES --no-ai

  # モデル指定 / 同時接続数調整
  python ses_run.py --keyword SES --model gemma3:4b --concurrency 5
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── パス設定 ────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

def _find_scraper_root() -> Path | None:
    if (_HERE / "scraper" / "ipros.py").exists():
        return _HERE
    for candidate in [
        _HERE.parent / "ipros",
        Path.home() / "Desktop" / "ipros",
        Path("C:/Users/masube/Desktop/ipros"),
    ]:
        if (candidate / "scraper" / "ipros.py").exists():
            return candidate
    return None

_SCRAPER_ROOT = _find_scraper_root()
if _SCRAPER_ROOT and str(_SCRAPER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUTPUT_DIR = _HERE / "scraper_output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════════════
#  Step 1: IPROSスクレイピング
# ════════════════════════════════════════════════════════════════════

def run_ipros_scrape(
    keywords: list[str],
    max_pages: int,
    concurrency: int,
    stop_event: threading.Event,
) -> list[dict]:
    """
    IPROSをキーワード検索してスクレイピング。
    既存の scraper/ipros.py をそのまま使用。
    """
    log.info("=" * 60)
    log.info(f"  [Step 1] IPROSスクレイピング開始")
    log.info(f"  キーワード: {keywords}")
    log.info(f"  最大ページ数/キーワード: {max_pages}")
    log.info("=" * 60)

    import asyncio as _asyncio

    async def _run_parallel():
        """複数キーワードを並列スクレイプ（ブラウザコンテキストを共有）"""
        from playwright.async_api import async_playwright
        from scraper.ipros import USER_AGENT, _scrape_keyword_async

        all_companies = []
        seen_urls: set[str] = set()
        async_stop = _asyncio.Event()

        async def _watch():
            while not async_stop.is_set():
                if stop_event.is_set():
                    async_stop.set()
                await _asyncio.sleep(0.3)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

            parallel = min(concurrency, len(keywords), 3)  # 最大3キーワード同時
            sem = _asyncio.Semaphore(parallel)
            log.info(f"  キーワード並列スクレイプ ({len(keywords)}KW / 同時{parallel}並列)")
            watcher = _asyncio.create_task(_watch())

            # 進捗カウンタ
            kw_status: dict[str, str] = {kw: "待機中" for kw in keywords}

            async def _heartbeat():
                """30秒ごとに生存確認ログを出す""", 
                import time
                start = time.monotonic()
                while True:
                    await _asyncio.sleep(30)
                    elapsed = int(time.monotonic() - start)
                    lines = ", ".join(f"[{k}]{v}" for k, v in kw_status.items())
                    log.info(f"  ⏳ スクレイプ継続中 ({elapsed}秒経過) | {lines}")

            heartbeat = _asyncio.create_task(_heartbeat())

            async def _scrape_kw(kw: str):
                kw_status[kw] = "一覧取得中"
                try:
                    result = await _scrape_keyword_async(kw, context, sem, max_pages, async_stop)
                    kw_status[kw] = f"完了({len(result)}社)"
                    return result
                except Exception as e:
                    kw_status[kw] = f"エラー"
                    raise

            results_per_kw = await _asyncio.gather(
                *[_scrape_kw(kw) for kw in keywords], return_exceptions=True
            )
            heartbeat.cancel()
            watcher.cancel()

            for kw, res in zip(keywords, results_per_kw):
                if isinstance(res, Exception):
                    log.warning(f"  [{kw}] エラー: {res}")
                    continue
                added = 0
                for c in (res or []):
                    key = c.詳細URL or c.会社名
                    if key and key not in seen_urls:
                        seen_urls.add(key)
                        all_companies.append(c)
                        added += 1
                log.info(f"  [{kw}] +{added}社（累計: {len(all_companies)}）")

            await context.close()
            await browser.close()
        return all_companies

    try:
        raw = _asyncio.run(_run_parallel())
    except Exception as e:
        log.warning(f"  並列スクレイプ失敗({e}) → 逐次モードで再試行")
        from scraper.ipros import scrape_ipros
        try:
            raw = scrape_ipros(keywords=keywords, max_pages=max_pages,
                               stop_event=stop_event, concurrency=concurrency)
        except Exception as e2:
            log.error(f"  IPROSスクレイピングエラー: {e2}")
            return []

    if not raw:
        log.warning("  収集できた企業が0社でした")
        return []

    # 重複除去
    seen_urls:  set[str] = set()
    seen_names: set[str] = set()
    unique = []
    for c in raw:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        url  = str(d.get("公式サイト", "") or "").strip().rstrip("/")
        name = str(d.get("会社名", "")    or "").strip()
        key = url if url.startswith("http") else name
        if key and key not in seen_urls:
            seen_urls.add(key)
            unique.append(d)

    log.info(f"  収集完了: {len(raw)} 社 → 重複除去後 {len(unique)} 社")
    return unique


def save_collected(companies: list[dict], path: Path) -> None:
    """中間保存（スクレイピングリスト）"""
    df = pd.DataFrame(companies)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="収集済み")
    log.info(f"  中間保存 → {path}")
    log.info(f"  💡 --resume {path.name} で評価のみ再開できます")


def load_collected(path: Path) -> list[dict]:
    """中間保存Excelを読み込む"""
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    rows = []
    for df in sheets.values():
        rows.extend(df.fillna("").to_dict(orient="records"))
    log.info(f"  再開読み込み: {len(rows)} 社 ({path.name})")
    return rows


# ════════════════════════════════════════════════════════════════════
#  Step 2: IPROS形式 → SESパイプライン用に変換
# ════════════════════════════════════════════════════════════════════

def to_ses_input(companies: list[dict]) -> list[dict]:
    """
    IPROSスクレイパー出力（会社名, 公式サイト, 詳細URL...）を
    ses_pipeline の入力形式（name, url）に変換する。
    """
    result = []
    seen = set()

    for c in companies:
        # URL優先順位: 公式サイト > 詳細URL
        url = str(c.get("公式サイト", "") or "").strip()
        if not url or not url.startswith("http"):
            url = str(c.get("詳細URL", "") or "").strip()
        if not url or not url.startswith("http"):
            continue

        url = url.rstrip("/")
        if url in seen:
            continue
        seen.add(url)

        result.append({
            "name":    str(c.get("会社名", url)).strip(),
            "url":     url,
            "keyword": str(c.get("検索キーワード", "") or "").strip(),
            "住所":    str(c.get("住所", "") or "").strip(),
            "電話":    str(c.get("電話", "") or "").strip(),
        })

    log.info(f"  SESパイプライン入力: {len(result)} 社（公式URL保有）")
    return result


# ════════════════════════════════════════════════════════════════════
#  メイン
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IPROS → SESスクリーニング 一気通貫ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── 入力ソース ──
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--keyword", nargs="+",
        help="IPROSで検索するキーワード（例: SES 受託開発 システム開発）",
    )
    group.add_argument(
        "--resume",
        help="収集済みExcelから再開（スクレイピングをスキップ）",
    )

    # ── スクレイピング設定 ──
    parser.add_argument("--max-pages",   type=int, default=5,
                        help="IPROSの最大ページ数/キーワード (default: 5 ≈ 300社)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="IPROS並列接続数 (default: 5)")

    # ── スクリーニング設定 ──
    parser.add_argument("--model",   default=None,
                        help="Ollamaモデル名 (default: ses_config.pyの設定)")
    parser.add_argument("--no-ai",   action="store_true",
                        help="キーワード判定のみ（Ollama不使用）")
    parser.add_argument("--no-screen", action="store_true",
                        help="スクレイピングのみ（SESスクリーニングをスキップ）")
    parser.add_argument("--crawl-concurrency", type=int, default=10,
                        help="企業サイトクロールの同時接続数 (default: 10)")

    # ── 出力設定 ──
    parser.add_argument("--output", default=None,
                        help="出力ファイル名プレフィックス (default: 自動生成)")
    parser.add_argument("--debug",  action="store_true", help="デバッグログ")

    # ── 案件プロファイル ──
    parser.add_argument(
        "--profile", default="ses",
        help="案件プロファイル名 (ses / design / edge_ai) またはYAMLファイルパス。"
             " 省略時はSESデフォルト。"
             " 利用可能なプロファイルを確認: python ses_run.py --list-profiles",
    )
    parser.add_argument(
        "--list-profiles", action="store_true",
        help="利用可能なプロファイル一覧を表示して終了",
    )

    # ── パス設定（別フォルダに配置した場合） ──
    parser.add_argument(
        "--ipros-path", default=None,
        help="ipros/ フォルダのパスを手動指定（例: C:/Users/masube/Desktop/ipros）",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # --list-profiles: プロファイル一覧を表示して終了
    if getattr(args, "list_profiles", False):
        try:
            from profile_loader import show_profiles
            show_profiles()
        except ImportError:
            log.error("profile_loader.py が見つかりません")
        sys.exit(0)

    # プロファイル読み込み
    profile = None
    profile_slug = getattr(args, "profile", "ses") or "ses"
    try:
        from profile_loader import load_profile
        profile = load_profile(profile_slug)
        log.info(f"  案件プロファイル: [{profile.name}]")
        # プロファイルのキーワードが指定されていてもCLIで --keyword が優先
        if not getattr(args, "keyword", None) and not getattr(args, "resume", None):
            args.keyword = profile.keywords_search
            log.info(f"  検索キーワード（プロファイルから）: {profile.keywords_search}")
    except FileNotFoundError as e:
        log.warning(f"  プロファイル読み込み失敗: {e}")
        log.warning("  SESデフォルト設定で続行します")
    except ImportError:
        log.warning("  profile_loader.py が見つかりません → SESデフォルトで続行")

    # --ipros-path が手動指定された場合は優先追加
    if args.ipros_path:
        extra = Path(args.ipros_path)
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
        log.info(f"  iprosパス（手動指定）: {extra}")
    elif _SCRAPER_ROOT:
        log.info(f"  scraper モジュール: {_SCRAPER_ROOT / 'scraper'}")
    else:
        log.error("scraper/ipros.py が見つかりません。")
        log.error("--ipros-path を指定して実行してください。例: --ipros-path C:/Users/masube/Desktop/ipros")
        sys.exit(1)

    if not args.keyword and not args.resume:
        # キーワード未指定 → 対話モードで入力を促す
        print("\n" + "═" * 55)
        print("  IPROS → SESスクリーニング 一気通貫ツール")
        print("═" * 55)
        print("\n  検索キーワードを入力してください（スペース区切りで複数可）")
        print("  例: SES 受託開発 ITコンサル システム開発")
        raw = input("  キーワード: ").strip()
        if not raw:
            log.error("キーワードが入力されませんでした")
            sys.exit(1)
        args.keyword = raw.split()

    # タイムスタンプ
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = args.output or f"ses_{ts}"

    # モデル設定を上書き
    if args.model:
        from ses_config import OLLAMA_CFG
        OLLAMA_CFG.model = args.model

    # Ctrl+C ハンドラ
    stop_event = threading.Event()
    def _handler(sig, frame):
        log.warning("\n⛔ Ctrl+C → 安全停止中...")
        stop_event.set()
    signal.signal(signal.SIGINT, _handler)

    # ──────────────────────────────────────────────────────────────
    # Step 1: スクレイピング or 再開読み込み
    # ──────────────────────────────────────────────────────────────
    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.is_absolute():
            # scraper_output/ からも探す
            if not resume_path.exists():
                resume_path = OUTPUT_DIR / resume_path
        if not resume_path.exists():
            log.error(f"ファイルが見つかりません: {resume_path}")
            sys.exit(1)
        all_companies = load_collected(resume_path)
    else:
        all_companies = run_ipros_scrape(
            keywords=args.keyword,
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            stop_event=stop_event,
        )
        if not all_companies:
            log.error("企業が1社も収集できませんでした")
            sys.exit(1)

        # 中間保存
        collected_path = OUTPUT_DIR / f"collected_{ts}.xlsx"
        save_collected(all_companies, collected_path)

    if stop_event.is_set():
        log.warning("⛔ 停止されました（収集済みExcelは保存済み）")
        sys.exit(0)

    # スクリーニングスキップ？
    if args.no_screen:
        print(f"\n✅ 収集完了: {len(all_companies)} 社")
        print(f"📁 {OUTPUT_DIR / f'collected_{ts}.xlsx'}")
        print(f"\n💡 スクリーニングを実行する場合:")
        print(f"   python ses_run.py --resume scraper_output/collected_{ts}.xlsx")
        return

    # ──────────────────────────────────────────────────────────────
    # Step 2: IPROS形式 → SESパイプライン入力に変換
    # ──────────────────────────────────────────────────────────────
    ses_input = to_ses_input(all_companies)
    if not ses_input:
        log.error("公式URLを持つ企業が0社でした（スクリーニングスキップ）")
        sys.exit(1)

    # ──────────────────────────────────────────────────────────────
    # Step 3: SESスクリーニング実行
    # ──────────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info("  [Step 2-5] SESスクリーニング開始")
    log.info("=" * 60)

    from ses_pipeline import run_ses_pipeline
    results = asyncio.run(run_ses_pipeline(
        companies      = ses_input,
        output_prefix  = output_prefix,
        concurrency    = args.crawl_concurrency,
        use_ollama     = not args.no_ai,
        profile        = profile,
    ))

    # ── 最終サマリー ──────────────────────────────────────────────
    from ses_config import OUTPUT_DIR as SES_OUT
    output_path = SES_OUT / f"{output_prefix}.xlsx"

    excellent = sum(1 for r in results if r.judgment == "◎")
    good      = sum(1 for r in results if r.judgment == "○")
    fair      = sum(1 for r in results if r.judgment == "△")

    print("\n" + "═" * 55)
    print("  ✅ 完了！")
    print("═" * 55)
    print(f"  スクレイピング: {len(all_companies)} 社収集")
    print(f"  スクリーニング: {len(ses_input)} 社解析")
    print(f"  ◎ 優先営業候補: {excellent} 社")
    print(f"  ○ 営業候補:     {good} 社")
    print(f"  △ 要確認:       {fair} 社")
    print(f"\n  📁 {output_path}")
    print("═" * 55)

    if excellent + good > 0:
        print("\n  🏆 TOP候補（◎）:")
        top = [r for r in results if r.judgment == "◎"]
        top.sort(key=lambda x: max(x.final_client_score, x.final_partner_score), reverse=True)
        for r in top[:5]:
            print(f"    ・{r.company_name} | 発注:{r.final_client_score} 供給:{r.final_partner_score}")
            if r.ai_reason:
                print(f"      {r.ai_reason[:70]}")


if __name__ == "__main__":
    main()
