"""
バッチ分割実行スクリプト
使い方: python batch_run.py --file リスト.xlsx --profile sanko_steel --url-col 企業ホームページURL --batch 300
"""
import argparse, asyncio, sys, time, gc
from pathlib import Path

sys.path.insert(0, '/opt/ses_scraper/SES_scra_anaraiz')

import pandas as pd
import psutil

def read_file(path: str, url_col: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == '.csv':
        for enc in ['utf-8', 'cp932', 'shift_jis']:
            try:
                df = pd.read_csv(p, dtype=str, encoding=enc).fillna('')
                break
            except Exception:
                continue
    else:
        df = pd.read_excel(p, dtype=str).fillna('')

    # 列名候補を探す
    def find_col(candidates):
        for col in df.columns:
            if any(k in col for k in candidates):
                return col
        return None

    actual_url_col = url_col if url_col in df.columns else find_col(['ホームページ', 'URL', 'url', 'website'])
    name_col       = find_col(['企業名', '会社名', 'name'])
    addr_col       = find_col(['住所', '所在地'])
    phone_col      = find_col(['電話', 'TEL', '電話番号'])

    companies = []
    seen = set()
    for _, row in df.iterrows():
        url = str(row.get(actual_url_col, '') or '').strip().rstrip('/')
        if not url or not url.startswith('http') or url in seen:
            continue
        seen.add(url)
        companies.append({
            'name':  str(row.get(name_col, url) or url).strip(),
            'url':   url,
            '住所':  str(row.get(addr_col,  '') or '').strip() if addr_col  else '',
            '電話':  str(row.get(phone_col, '') or '').strip() if phone_col else '',
            'keyword': '',
            'source':  p.name,
        })

    print(f'読み込み完了: {len(companies)} 社（URL有効）')
    return companies, df, actual_url_col


async def run_batch(companies, profile, output_prefix, batch_num):
    from ses_pipeline import run_ses_pipeline
    from profile_loader import load_profile

    prof = load_profile(profile) if profile else None
    results = await run_ses_pipeline(
        companies     = companies,
        output_prefix = f'{output_prefix}_batch{batch_num:03d}',
        concurrency   = 4,
        use_ollama    = False,
        profile       = prof,
    )
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file',    required=True)
    parser.add_argument('--profile', default='ses')
    parser.add_argument('--url-col', default='企業ホームページURL')
    parser.add_argument('--batch',   type=int, default=300)
    parser.add_argument('--skip',    type=int, default=0, help='スキップするバッチ数（途中再開用）')
    args = parser.parse_args()

    companies, df_src, actual_url_col = read_file(args.file, args.url_col)
    total = len(companies)
    batch_size = args.batch
    n_batches  = (total + batch_size - 1) // batch_size
    prefix     = Path(args.file).stem

    print(f'総社数: {total} / バッチサイズ: {batch_size} / バッチ数: {n_batches}')

    all_results = []

    for i in range(args.skip, n_batches):
        batch = companies[i * batch_size: (i + 1) * batch_size]
        mem = psutil.virtual_memory()
        print(f'\n{"="*50}')
        print(f'バッチ {i+1}/{n_batches} 開始 ({len(batch)}社) | メモリ: {mem.percent:.1f}%')
        print(f'{"="*50}')

        # メモリ危険域なら少し待つ
        if mem.percent > 80:
            print(f'⚠ メモリ{mem.percent:.1f}% — 30秒待機')
            time.sleep(30)
            gc.collect()

        try:
            results = asyncio.run(run_batch(batch, args.profile, prefix, i + 1))
            all_results.extend(results)
        except Exception as e:
            print(f'❌ バッチ{i+1} エラー: {e}')

        # バッチ間でGC
        gc.collect()
        time.sleep(5)

    print(f'\n✅ 全バッチ完了: {len(all_results)}社処理')
    excellent = sum(1 for r in all_results if r.judgment == '◎')
    good      = sum(1 for r in all_results if r.judgment == '○')
    print(f'◎ {excellent}社 / ○ {good}社')


if __name__ == '__main__':
    main()
