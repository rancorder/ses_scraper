"""
ses_pipeline.py - SES事業スクリーニング専用パイプライン
=======================================================
フロー:
  入力（Excel/IPROS/SalesNow）
    ↓ [1] クロール
    ↓ [2] テキスト抽出
    ↓ [3] キーワード一次フィルタ
    ↓ [4] Ollama AI判定（任意）
    ↓ [5] プロファイル採点・Excel出力

案件プロファイルが rank_thresholds / report_fields を持つ場合は、
S/A/B/C 等の案件固有ランクと採点外のEvidence列も出力する。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# ════════════════════════════════════════════════════════
#  データモデル
# ════════════════════════════════════════════════════════

@dataclass
class SesResult:
    """1社分のSESスクリーニング結果"""
    company_name:    str
    company_url:     str

    住所:            str = ""
    電話:            str = ""
    検索キーワード:  str = ""
    ソース:          str = ""

    kw_client_score:  int = 0
    kw_partner_score: int = 0
    kw_hits_client:   list[str] = field(default_factory=list)
    kw_hits_partner:  list[str] = field(default_factory=list)

    ai_client_score:  int = 0
    ai_partner_score: int = 0
    ai_reason:        str = ""
    ai_sales_talk:    str = ""
    ai_error:         Optional[str] = None

    final_client_score:  int = 0
    final_partner_score: int = 0
    judgment:            str = ""

    profile_score: object = None   # type: ignore

    pages_crawled: int = 0
    error:         Optional[str] = None

    def calc_final_scores(self, profile=None) -> None:
        """最終点を計算する。

        プロファイル採点がある場合はその点を正とする。AI補正率は
        profile.ai_score_bonus_weight で案件ごとに制御し、0ならAIは参考情報のみ。
        """
        if self.profile_score is not None:
            ps = self.profile_score
            self.final_client_score = ps.total_score
            self.final_partner_score = (
                min(100, int(self.ai_partner_score * 0.7))
                if self.ai_partner_score else 0
            )

            ai_weight = (
                float(getattr(profile, "ai_score_bonus_weight", 0.15))
                if profile else 0.15
            )
            if self.ai_client_score and ai_weight > 0:
                ai_bonus = int(self.ai_client_score * ai_weight)
                self.final_client_score = min(100, ps.total_score + ai_bonus)

            if profile and hasattr(profile, "judge"):
                self.judgment = profile.judge(self.final_client_score)
            else:
                self.judgment = ps.judgment
            return

        kw_c = min(self.kw_client_score * 5, 30)
        kw_p = min(self.kw_partner_score * 5, 30)
        self.final_client_score = min(100, kw_c + int(self.ai_client_score * 0.7))
        self.final_partner_score = min(100, kw_p + int(self.ai_partner_score * 0.7))
        best = max(self.final_client_score, self.final_partner_score)

        if profile and hasattr(profile, "judge"):
            self.judgment = profile.judge(best)
        else:
            th_exc = getattr(profile, "threshold_excellent", 70) if profile else 70
            th_gd  = getattr(profile, "threshold_good", 50) if profile else 50
            if best >= th_exc:
                self.judgment = "◎"
            elif best >= th_gd:
                self.judgment = "○"
            elif best >= 30:
                self.judgment = "△"
            else:
                self.judgment = "－"


# ════════════════════════════════════════════════════════
#  Step 1: Excel / CSV からの企業リスト読み込み
# ════════════════════════════════════════════════════════

def load_companies(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        rows = []
        for df in sheets.values():
            rows.extend(df.fillna("").to_dict(orient="records"))
    elif path.suffix.lower() == ".csv":
        rows = pd.read_csv(path, dtype=str).fillna("").to_dict(orient="records")
    else:
        raise ValueError(f"未対応ファイル形式: {path.suffix}")

    results = []
    for row in rows:
        name = _find_col(row, ["会社名", "企業名", "company_name", "name", "社名"])
        url  = _find_col(row, ["公式サイト", "URL", "url", "ホームページ", "website", "サイト"])
        if not name and not url:
            continue
        if url and not url.startswith("http"):
            url = "https://" + url
        results.append({"name": name or url, "url": url or ""})

    seen, unique = set(), []
    for company in results:
        key = company["url"] or company["name"]
        if key not in seen:
            seen.add(key)
            unique.append(company)

    log.info(f"  読み込み: {len(unique)} 社 ({path.name})")
    return unique


def _find_col(row: dict, candidates: list[str]) -> str:
    for key in candidates:
        val = str(row.get(key, "")).strip()
        if val:
            return val
    return ""


# ════════════════════════════════════════════════════════
#  Step 2: クロール
# ════════════════════════════════════════════════════════

async def _crawl(
    companies: list[dict],
    concurrency: int,
    crawl_paths: list[str] | None = None,
) -> dict[str, list]:
    import time
    from company_analyzer.crawler.crawler import crawl_all

    total = len(companies)
    safe_concurrency = min(concurrency, 4)
    log.info(f"  対象: {total}社 / 同時接続: {safe_concurrency}（VPS安定化）")

    start = time.monotonic()

    async def _hb():
        interval = 0
        while True:
            await asyncio.sleep(30)
            interval += 30
            log.info(f"  ⏳ クロール継続中... ({interval // 60}分{interval % 60}秒経過)")

    hb = asyncio.create_task(_hb())
    try:
        timeout_sec = min(int(total / safe_concurrency * 30 * 2), 10800)
        log.info(f"  タイムアウト設定: {timeout_sec // 60}分")
        results = await asyncio.wait_for(
            crawl_all(companies, safe_concurrency, paths=crawl_paths),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        log.warning(f"  ⚠ クロールタイムアウト ({timeout_sec // 60}分) → 取得済み分で継続")
        results = {}
    finally:
        hb.cancel()

    ok = sum(1 for value in results.values() if value)
    elapsed = int(time.monotonic() - start)
    log.info(f"  クロール完了: {ok}/{total}社取得 ({elapsed}秒)")
    return results


# ════════════════════════════════════════════════════════
#  Step 3: テキスト抽出
# ════════════════════════════════════════════════════════

def _extract_text(pages: list) -> str:
    parts = []
    for page in pages:
        for attr in ["title", "body_text", "meta_desc", "footer_text"]:
            value = getattr(page, attr, "")
            if value:
                parts.append(str(value))
        for attr in ["h1", "h2", "h3"]:
            value = getattr(page, attr, [])
            if isinstance(value, list):
                parts.extend(str(v) for v in value if v)
    return "\n".join(parts)


# ════════════════════════════════════════════════════════
#  Step 4: キーワード一次フィルタ
# ════════════════════════════════════════════════════════

def _keyword_scan(
    text: str,
    client_keywords: dict | None = None,
    partner_keywords: dict | None = None,
) -> tuple[list[str], list[str]]:
    if client_keywords is None or partner_keywords is None:
        from ses_config import CLIENT_KEYWORDS, PARTNER_KEYWORDS
        client_keywords  = client_keywords or CLIENT_KEYWORDS
        partner_keywords = partner_keywords or PARTNER_KEYWORDS

    text_lower = text.lower()
    client_hits: list[str] = []
    partner_hits: list[str] = []

    for words in client_keywords.values():
        for word in words:
            if word.lower() in text_lower:
                client_hits.append(word)

    for words in partner_keywords.values():
        for word in words:
            if word.lower() in text_lower:
                partner_hits.append(word)

    return client_hits, partner_hits


# ════════════════════════════════════════════════════════
#  Step 5: Excel出力
# ════════════════════════════════════════════════════════

_FILL_EXCELLENT = PatternFill("solid", fgColor="C6EFCE")
_FILL_GOOD      = PatternFill("solid", fgColor="FFEB9C")
_FILL_FAIR      = PatternFill("solid", fgColor="DDEBF7")
_FILL_LOW       = PatternFill("solid", fgColor="FFCCCC")
_FILL_HEADER    = PatternFill("solid", fgColor="2F5496")
_FONT_HEADER    = Font(color="FFFFFF", bold=True)


def _judgment_fill(judgment: str):
    return {
        "◎": _FILL_EXCELLENT,
        "○": _FILL_GOOD,
        "△": _FILL_FAIR,
        "S": _FILL_EXCELLENT,
        "A": _FILL_GOOD,
        "B": _FILL_FAIR,
        "C": _FILL_LOW,
    }.get(judgment)


def _profile_defs(profile):
    axes_defs = profile.scoring_axes if profile and profile.scoring_axes else []
    report_defs = getattr(profile, "report_fields", []) if profile else []
    return axes_defs, report_defs


def _format_score_axis(axis_result) -> str:
    if axis_result is None:
        return "-"
    if axis_result.earned:
        value = f"✓ {axis_result.score}点"
        if axis_result.hit_keywords:
            value += "\n(" + ", ".join(axis_result.hit_keywords[:2]) + ")"
        elif axis_result.detail:
            value += "\n(" + axis_result.detail + ")"
        return value
    return "✗ 0点" + (f"\n({axis_result.detail})" if axis_result.detail else "")


def _format_report_field(field_result) -> str:
    if field_result is None:
        return "未確認"
    if field_result.earned:
        evidence = ", ".join(field_result.hit_keywords[:3])
        if not evidence:
            evidence = field_result.detail
        return f"✓ {evidence}" if evidence else "✓ Evidenceあり"
    if field_result.detail:
        return field_result.detail
    return "未確認"


def _build_result_row(
    result: SesResult,
    axes_defs: list,
    report_defs: list | None = None,
    profile=None,
) -> list:
    report_defs = report_defs or []

    if axes_defs and result.profile_score:
        axis_results = {axis.id: axis for axis in result.profile_score.axes}
        axis_cols = [
            _format_score_axis(axis_results.get(axis_def.get("id", "")))
            for axis_def in axes_defs
        ]
    else:
        axis_cols = [
            result.final_client_score,
            result.final_partner_score,
            ", ".join(result.kw_hits_client[:6]),
            ", ".join(result.kw_hits_partner[:6]),
        ]

    report_cols: list[str] = []
    if report_defs:
        report_results = {}
        if result.profile_score:
            report_results = {
                item.id: item
                for item in getattr(result.profile_score, "report_fields", [])
            }
        report_cols = [
            _format_report_field(report_results.get(field_def.get("id", "")))
            for field_def in report_defs
        ]

    ai_cols = [result.ai_client_score or "", result.ai_reason or "", result.ai_sales_talk or ""]
    tail_cols = [result.pages_crawled, result.error or ""]
    return axis_cols + report_cols + ai_cols + tail_cols


def _score_headers(profile, axes_defs: list, report_defs: list):
    axis_headers = [f"{axis['name']}({axis['points']}点)" for axis in axes_defs]
    score_headers = axis_headers if axis_headers else [
        "発注側スコア", "供給側スコア", "発注側KWヒット", "供給側KWヒット"
    ]
    report_headers = [field_def["name"] for field_def in report_defs]
    ai_score_name = (
        "AI参考スコア"
        if profile and float(getattr(profile, "ai_score_bonus_weight", 0.15)) == 0
        else "AI補正スコア"
    )
    return score_headers, report_headers, ai_score_name


def save_ses_excel(results: list[SesResult], output_path: Path, profile=None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    profile_name = profile.name if profile else "SES"
    ws.title = f"{profile_name[:20]}_結果"

    axes_defs, report_defs = _profile_defs(profile)
    score_headers, report_headers, ai_score_name = _score_headers(
        profile, axes_defs, report_defs
    )

    judgment_header = "ランク" if profile and getattr(profile, "rank_thresholds", {}) else "判定"
    base_headers = [
        judgment_header, "総合スコア", "会社名", "サイトURL",
        "電話", "住所", "ソース", "検索KW",
    ]
    ai_headers = [ai_score_name, "AI判定理由", "営業トーク案"]
    tail_headers = ["クロールページ数", "エラー"]
    headers = base_headers + score_headers + report_headers + ai_headers + tail_headers

    ws.append(headers)
    for cell in ws[1]:
        cell.fill = _FILL_HEADER
        cell.font = _FONT_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center")

    sorted_results = sorted(results, key=lambda x: x.final_client_score, reverse=True)
    for result in sorted_results:
        base_cols = [
            result.judgment, result.final_client_score,
            result.company_name, result.company_url,
            result.電話, result.住所, result.ソース, result.検索キーワード,
        ]
        row_data = base_cols + _build_result_row(
            result, axes_defs, report_defs, profile
        )
        ws.append(row_data)

        fill = _judgment_fill(result.judgment)
        if fill:
            for col in range(1, len(headers) + 1):
                ws.cell(row=ws.max_row, column=col).fill = fill

    fixed_widths = [8, 12, 28, 38, 16, 30, 12, 18]
    for idx, width in enumerate(fixed_widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    score_start = 9
    for idx in range(score_start, score_start + len(score_headers)):
        ws.column_dimensions[get_column_letter(idx)].width = 22

    report_start = score_start + len(score_headers)
    for idx in range(report_start, report_start + len(report_headers)):
        ws.column_dimensions[get_column_letter(idx)].width = 24

    ai_start = report_start + len(report_headers)
    for idx, width in enumerate([12, 50, 60], ai_start):
        ws.column_dimensions[get_column_letter(idx)].width = width

    link_font = Font(color="0563C1", underline="single")
    url_col_idx = headers.index("サイトURL") + 1
    for row_idx in range(2, ws.max_row + 1):
        cell = ws.cell(row=row_idx, column=url_col_idx)
        url_val = str(cell.value or "").strip()
        if url_val.startswith("http"):
            cell.hyperlink = url_val
            cell.font = link_font

    for row in ws.iter_rows(min_row=2, min_col=score_start, max_col=len(headers)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    _add_summary_and_axes_sheets(wb, results, profile)
    wb.save(output_path)
    log.info(f"  保存完了: {output_path}")


def save_ses_excel_append(
    results: list[SesResult],
    source_path: Path,
    output_path: Path,
    profile=None,
    url_col: str = "URL",
) -> None:
    """元ファイルの右側に評価列を追記して保存する。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = Path(source_path)

    if src.suffix.lower() == ".csv":
        df_src = None
        for enc in ["utf-8", "cp932", "shift_jis"]:
            try:
                df_src = pd.read_csv(src, dtype=str, encoding=enc).fillna("")
                break
            except Exception:
                continue
        if df_src is None:
            df_src = pd.read_csv(src, dtype=str, encoding="utf-8", encoding_errors="replace").fillna("")
    else:
        df_src = pd.read_excel(src, dtype=str).fillna("")

    result_map: dict[str, SesResult] = {}
    for result in results:
        result_map[result.company_url.rstrip("/")] = result

    axes_defs, report_defs = _profile_defs(profile)
    score_headers, report_headers, ai_score_name = _score_headers(
        profile, axes_defs, report_defs
    )

    rank_header = "【ランク】" if profile and getattr(profile, "rank_thresholds", {}) else "【判定】"
    append_headers = (
        [rank_header, "【総合スコア】"]
        + [f"【{header}】" for header in score_headers]
        + [f"【{header}】" for header in report_headers]
        + [f"【{ai_score_name}】", "【AI判定理由】", "【営業トーク案】", "【クロールページ数】", "【エラー】"]
    )

    append_rows = []
    for _, row in df_src.iterrows():
        url_val = str(row.get(url_col, "") or "").strip().rstrip("/")
        if not url_val:
            for col_candidate in ["公式サイト", "URL", "url", "ホームページ", "website"]:
                url_val = str(row.get(col_candidate, "") or "").strip().rstrip("/")
                if url_val:
                    break

        result = result_map.get(url_val)
        if result:
            score_cols = _build_result_row(
                result, axes_defs, report_defs, profile
            )
            append_rows.append([result.judgment, result.final_client_score] + score_cols)
        else:
            append_rows.append([""] * len(append_headers))

    df_append = pd.DataFrame(append_rows, columns=append_headers)
    df_out = pd.concat([df_src.reset_index(drop=True), df_append], axis=1)

    wb = Workbook()
    ws = wb.active
    profile_name = profile.name if profile else "SES"
    ws.title = f"{profile_name[:20]}_結果"

    all_headers = list(df_out.columns)
    ws.append(all_headers)

    orig_col_count = len(df_src.columns)
    append_col_start = orig_col_count + 1
    orig_header_fill = PatternFill("solid", fgColor="D9D9D9")
    orig_header_font = Font(bold=True, color="333333")

    for idx, cell in enumerate(ws[1], 1):
        cell.alignment = Alignment(horizontal="center", vertical="center")
        if idx <= orig_col_count:
            cell.fill = orig_header_fill
            cell.font = orig_header_font
        else:
            cell.fill = _FILL_HEADER
            cell.font = _FONT_HEADER
    ws.row_dimensions[1].height = 22

    for _, row in df_out.iterrows():
        ws.append(list(row))
        judgment = str(row.get(rank_header, "") or "")
        fill = _judgment_fill(judgment)
        if fill:
            for col in range(append_col_start, len(all_headers) + 1):
                ws.cell(row=ws.max_row, column=col).fill = fill

    for idx in range(1, orig_col_count + 1):
        ws.column_dimensions[get_column_letter(idx)].width = 18

    append_widths = (
        [8, 12]
        + [22] * len(score_headers)
        + [24] * len(report_headers)
        + [12, 50, 60, 12, 15]
    )
    for idx, width in enumerate(append_widths, append_col_start):
        ws.column_dimensions[get_column_letter(idx)].width = width

    link_font = Font(color="0563C1", underline="single")
    url_col_names = ["URL", "公式サイト", "url", "ホームページ", "website", "サイト"]
    url_col_idx = None
    for col_name in url_col_names:
        if col_name in all_headers:
            url_col_idx = all_headers.index(col_name) + 1
            break
    if url_col_idx:
        for row_idx in range(2, ws.max_row + 1):
            cell = ws.cell(row=row_idx, column=url_col_idx)
            url_value = str(cell.value or "").strip()
            if url_value.startswith("http"):
                cell.hyperlink = url_value
                cell.font = link_font

    for row in ws.iter_rows(min_row=2, min_col=append_col_start, max_col=len(all_headers)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws.freeze_panes = "A2"
    _add_summary_and_axes_sheets(wb, results, profile)
    wb.save(output_path)
    log.info(
        f"  追記保存完了: {output_path}  "
        f"(元{orig_col_count}列 + 追記{len(append_headers)}列)"
    )


def _add_summary_and_axes_sheets(wb, results: list[SesResult], profile=None) -> None:
    """サマリー、採点軸集計、調査項目Evidence率を追加する。"""
    axes_defs, report_defs = _profile_defs(profile)
    profile_name = profile.name if profile else "SES"
    total = len(results)

    ws2 = wb.create_sheet("サマリー")
    summary_rows = [
        ["項目", "値"],
        ["案件プロファイル", profile_name],
        ["解析企業数", total],
    ]

    rank_thresholds = getattr(profile, "rank_thresholds", {}) if profile else {}
    if rank_thresholds:
        ordered = sorted(rank_thresholds.items(), key=lambda item: item[1], reverse=True)
        for label, minimum in ordered:
            count = sum(1 for result in results if result.judgment == str(label))
            summary_rows.append([f"{label}ランク（{minimum}点以上）", count])

        candidate_ranks = (
            profile.effective_candidate_ranks
            if hasattr(profile, "effective_candidate_ranks")
            else [str(label) for label, _ in ordered[:2]]
        )
        candidate_count = sum(
            1 for result in results if result.judgment in candidate_ranks
        )
        summary_rows.extend([
            ["優先対象ランク", " / ".join(candidate_ranks)],
            ["優先対象企業数", candidate_count],
            ["優先対象率", f"{candidate_count / total * 100:.1f}%" if total else "0%"],
        ])
    else:
        excellent = sum(1 for result in results if result.judgment == "◎")
        good = sum(1 for result in results if result.judgment == "○")
        fair = sum(1 for result in results if result.judgment == "△")
        th_exc = getattr(profile, "threshold_excellent", 70) if profile else 70
        th_gd = getattr(profile, "threshold_good", 50) if profile else 50
        summary_rows.extend([
            [f"◎ 優先候補（{th_exc}点以上）", excellent],
            [f"○ 候補（{th_gd}点以上）", good],
            ["△ 要確認", fair],
            ["候補合計", excellent + good],
            ["候補率", f"{(excellent + good) / total * 100:.1f}%" if total else "0%"],
        ])

    summary_rows.extend([
        ["AI判定成功数", sum(1 for result in results if result.ai_client_score)],
        ["クロール失敗数", sum(1 for result in results if result.error)],
        ["平均スコア", f"{sum(result.final_client_score for result in results) / total:.1f}" if total else "0"],
    ])

    for row in summary_rows:
        ws2.append(row)
    ws2.column_dimensions["A"].width = 32
    ws2.column_dimensions["B"].width = 22
    for cell in ws2[1]:
        cell.fill = _FILL_HEADER
        cell.font = _FONT_HEADER

    if axes_defs:
        ws3 = wb.create_sheet("評価軸別集計")
        ws3.append(["評価軸", "配点", "ヒット社数", "ヒット率", "主なEvidence（上位3）"])
        for cell in ws3[1]:
            cell.fill = _FILL_HEADER
            cell.font = _FONT_HEADER

        for axis_def in axes_defs:
            axis_id = axis_def.get("id", "")
            hits = []
            kw_counter: dict[str, int] = {}
            for result in results:
                if result.profile_score:
                    axis_result = next(
                        (item for item in result.profile_score.axes if item.id == axis_id),
                        None,
                    )
                    if axis_result and axis_result.earned:
                        hits.append(result.company_name)
                        for keyword in axis_result.hit_keywords:
                            kw_counter[keyword] = kw_counter.get(keyword, 0) + 1
            top_kws = sorted(kw_counter, key=lambda key: -kw_counter[key])[:3]
            hit_rate = f"{len(hits) / total * 100:.1f}%" if total else "0%"
            ws3.append([
                axis_def.get("name", ""),
                f"{axis_def.get('points', 0)}点",
                len(hits), hit_rate, "、".join(top_kws),
            ])

        for idx, width in enumerate([30, 10, 12, 10, 45], 1):
            ws3.column_dimensions[get_column_letter(idx)].width = width

    if report_defs:
        ws4 = wb.create_sheet("調査項目集計")
        ws4.append(["調査項目", "Evidence確認社数", "確認率", "備考"])
        for cell in ws4[1]:
            cell.fill = _FILL_HEADER
            cell.font = _FONT_HEADER

        for field_def in report_defs:
            field_id = field_def.get("id", "")
            evidence_count = 0
            for result in results:
                if not result.profile_score:
                    continue
                field_result = next(
                    (
                        item for item in getattr(result.profile_score, "report_fields", [])
                        if item.id == field_id
                    ),
                    None,
                )
                if field_result and field_result.earned:
                    evidence_count += 1
            rate = f"{evidence_count / total * 100:.1f}%" if total else "0%"
            note = field_def.get("unverified_text", "") if field_def.get("detection") == "manual" else ""
            ws4.append([field_def.get("name", ""), evidence_count, rate, note])

        for idx, width in enumerate([30, 18, 12, 42], 1):
            ws4.column_dimensions[get_column_letter(idx)].width = width


# ════════════════════════════════════════════════════════
#  メインパイプライン
# ════════════════════════════════════════════════════════

async def run_ses_pipeline(
    companies: list[dict],
    output_prefix: str = "ses_analysis",
    concurrency: int = 4,
    use_ollama: bool = True,
    profile=None,
    source_path: str | Path | None = None,
    url_col: str = "URL",
) -> list[SesResult]:
    from ses_config import OUTPUT_DIR
    from company_analyzer.parser.site_parser import parse_page
    from ollama_scorer import create_scorer

    profile_name = profile.name if profile else "SES（デフォルト）"
    client_kws = profile.client_keywords if profile else None
    partner_kws = profile.partner_keywords if profile else None
    if profile:
        output_prefix = f"{profile.slug}_{output_prefix.lstrip('ses_')}"

    total = len(companies)
    log.info("=" * 60)
    log.info(f"スクリーニング開始: {total}社  案件: [{profile_name}]")
    log.info("=" * 60)

    scorer = None
    if use_ollama:
        scorer = create_scorer()
        if profile and profile.ai_instruction:
            scorer.custom_instruction = profile.ai_instruction
        if scorer.is_available():
            models = scorer.get_available_models()
            log.info(f"  Ollama 接続OK | モデル: {scorer.model}")
            log.info(f"  利用可能モデル: {', '.join(models[:5])}")
        else:
            log.warning("  ⚠ Ollamaに接続できません → ルール判定のみで実行します")
            scorer = None

    log.info(f"\n[1/4] クロール開始 ({total}社 / 同時{concurrency}接続)")
    crawl_paths = getattr(profile, "crawl_paths", None) if profile else None
    crawl_results = await _crawl(companies, concurrency, crawl_paths=crawl_paths)

    log.info("\n[2-3/4] テキスト抽出 & キーワードスキャン中...")
    results: list[SesResult] = []

    for idx, company in enumerate(companies, 1):
        url = company.get("url", "")
        name = company.get("name", url)
        pages = crawl_results.get(url, [])

        result = SesResult(
            company_name=name,
            company_url=url,
            住所=company.get("住所", "") or "",
            電話=company.get("電話", "") or "",
            検索キーワード=company.get("keyword", "") or "",
            ソース=company.get("source", "") or "",
        )
        result.pages_crawled = len(pages)

        if not pages:
            result.error = "no_pages"
            results.append(result)
        else:
            parsed = [parse_page(page.url, page.html) for page in pages if page.html]
            site_text = _extract_text(parsed)
            client_hits, partner_hits = _keyword_scan(
                site_text, client_kws, partner_kws
            )
            result.kw_hits_client = client_hits
            result.kw_hits_partner = partner_hits
            result.kw_client_score = len(client_hits)
            result.kw_partner_score = len(partner_hits)
            result._site_text = site_text  # type: ignore[attr-defined]

            if profile and getattr(profile, "scoring_axes", []):
                try:
                    from profile_scorer import ProfileScorer
                    result.profile_score = ProfileScorer(profile).score(name, url, parsed)
                except Exception as exc:
                    log.debug(f"  ProfileScorer エラー ({name}): {exc}")

            results.append(result)

        if idx % 20 == 0 or idx == total:
            hit_c = sum(1 for item in results if item.kw_client_score > 0)
            hit_p = sum(1 for item in results if item.kw_partner_score > 0)
            no_pg = sum(1 for item in results if item.error == "no_pages")
            log.info(
                f"  KWスキャン {idx}/{total}社 | "
                f"発注ヒット:{hit_c} 供給ヒット:{hit_p} クロール失敗:{no_pg}"
            )

    log.info(f"  完了: {len(results)}社")

    if scorer:
        ollama_parallel = 3
        ollama_sem = asyncio.Semaphore(ollama_parallel)
        targets = [
            result for result in results
            if not result.error and getattr(result, "_site_text", "").strip()
        ]
        log.info(
            f"\n[4/4] Ollama AI判定中... ({scorer.model}) "
            f"[{len(targets)}社 / 並列{ollama_parallel}]"
        )
        completed = 0

        async def _score_one(result: SesResult) -> None:
            nonlocal completed
            async with ollama_sem:
                loop = asyncio.get_event_loop()
                ollama_result = await loop.run_in_executor(
                    None,
                    scorer.score,
                    result.company_name,
                    getattr(result, "_site_text", ""),
                )
                result.ai_client_score = ollama_result.client_score
                result.ai_partner_score = ollama_result.partner_score
                result.ai_reason = ollama_result.reason
                result.ai_sales_talk = ollama_result.sales_talk
                result.ai_error = ollama_result.error
                completed += 1
                log.info(
                    f"  [{completed:>3}/{len(targets)}] "
                    f"参考:{result.ai_client_score:>3} 協業:{result.ai_partner_score:>3} "
                    f"({ollama_result.elapsed_sec:.1f}秒) {result.company_name[:25]}"
                )

        await asyncio.gather(*[_score_one(result) for result in targets])
        log.info(f"  AI判定完了: {completed} 社")
    else:
        log.info("\n[4/4] AI判定スキップ（ルール判定のみ）")

    log.info("\n最終スコア計算 & 保存中...")
    for result in results:
        result.calc_final_scores(profile=profile)

    candidate_ranks = (
        profile.effective_candidate_ranks
        if profile and hasattr(profile, "effective_candidate_ranks")
        else ["◎", "○"]
    )
    candidates = [result for result in results if result.judgment in candidate_ranks]
    counts = ", ".join(
        f"{rank}:{sum(1 for result in results if result.judgment == rank)}社"
        for rank in candidate_ranks
    )
    log.info(
        f"  営業優先候補: {counts} "
        f"（計{len(candidates)}社 / {total}社中）"
    )

    output_path = OUTPUT_DIR / f"{output_prefix}.xlsx"
    if source_path and Path(source_path).exists():
        log.info(f"  元ファイルに追記形式で保存: {source_path}")
        save_ses_excel_append(
            results=results,
            source_path=Path(source_path),
            output_path=output_path,
            profile=profile,
            url_col=url_col,
        )
    else:
        save_ses_excel(results, output_path, profile=profile)

    log.info("\n" + "=" * 60)
    log.info("  🏆  TOP 10 営業候補")
    log.info("=" * 60)
    top = sorted(results, key=lambda item: item.final_client_score, reverse=True)
    for idx, result in enumerate(top[:10], 1):
        log.info(
            f"  {idx:2}. [{result.judgment}] {result.company_name} | "
            f"スコア:{result.final_client_score}"
        )
        if result.ai_reason:
            log.info(f"      → {result.ai_reason[:60]}")

    return results
