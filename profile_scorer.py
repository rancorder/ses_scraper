"""
profile_scorer.py - プロファイル駆動スコアリングエンジン
=========================================================
YAMLで定義した評価軸・配点に基づいて企業サイトをスコアリングする。
既存の scoring_engine.py を置き換える案件プロファイル対応版。

各評価軸の detection タイプ:
  keyword_any               : いずれかのキーワードが1つでもヒットしたら加点
  keyword_count             : min_hits 以上のキーワードがヒットしたら加点
  keyword_and_pattern       : キーワード OR 正規表現パターンでヒット
  keyword_groups_all        : keyword_groups の各グループで1件以上ヒットしたら加点
  keyword_any_on_page       : 特定URLパスのページでキーワードがヒット
  recent_keyword_any        : 同一ページでKWと直近N年の日付が確認できたら加点
  recent_keyword_any_on_page: 対象ページ内でKWと直近N年の日付が同時に確認できたら加点
  exhibition_upcoming       : 対象企業本人の展示会出展予定・開催日を確認
  exhibition_recent         : 対象企業本人の直近N年の展示会出展実績を確認
  contact_department        : 連絡手段と部署情報の両方を確認
  url_exists                : url_signalsいずれかのURLが存在したら加点
  contact                   : メールアドレスまたはお問い合わせフォームが存在
  regex                     : 正規表現マッチ
  manual                    : 自動判定せず要確認として出力
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AxisResult:
    """1評価軸／調査項目の判定結果"""
    id:           str
    name:         str
    points:       int
    earned:       bool
    score:        int
    hit_keywords: list[str] = field(default_factory=list)
    detail:       str = ""


@dataclass
class ProfileScore:
    """プロファイルスコアリングの結果"""
    company_name:  str
    company_url:   str
    profile_name:  str
    total_score:   int
    raw_score:     int
    score_cap:     int
    judgment:      str
    axes:          list[AxisResult] = field(default_factory=list)
    report_fields: list[AxisResult] = field(default_factory=list)

    @property
    def is_excellent(self) -> bool:
        return self.judgment in ("◎", "S")

    @property
    def is_candidate(self) -> bool:
        return self.judgment in ("◎", "○", "S", "A")

    def summary(self) -> str:
        lines = [
            f"[{self.profile_name}] {self.company_name}",
            f"  スコア: {self.total_score}点 ({self.judgment})",
        ]
        for ax in self.axes:
            mark = "✓" if ax.earned else "✗"
            lines.append(
                f"  {mark} {ax.name}: {ax.score}点"
                + (f" ({', '.join(ax.hit_keywords[:3])})" if ax.hit_keywords else "")
            )
        return "\n".join(lines)


# ════════════════════════════════════════════════════════
#  テキスト解析ヘルパー
# ════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    return text.lower().replace("\u3000", " ").replace("\n", " ")


def _compact(text: str) -> str:
    """会社名照合用。空白・代表的な区切り記号を除去する。"""
    return re.sub(r"[\s\u3000・･\-‐‑–—_/|｜()（）\[\]【】]", "", text.lower())


def _company_aliases(company_name: str) -> list[str]:
    """対象企業本人かを照合するための保守的な会社名エイリアスを作る。"""
    if not company_name:
        return []
    raw = str(company_name).strip()
    aliases = [raw]
    stripped = re.sub(
        r"(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|一般財団法人|\(株\)|（株）|㈱|inc\.?|co\.?\s*,?\s*ltd\.?|ltd\.?)",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    if stripped:
        aliases.append(stripped)
    compacted = []
    for alias in aliases:
        value = _compact(alias)
        if len(value) >= 3 and value not in compacted:
            compacted.append(value)
    return compacted


def _company_mentioned(company_name: str, text: str) -> bool:
    """ページ本文・タイトルに対象会社名（法人格除外可）が明記されているか。"""
    haystack = _compact(text)
    return any(alias in haystack for alias in _company_aliases(company_name))


def _hit_keywords(text_lower: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw.lower() in text_lower]


def _hit_patterns(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            pass
    return False


def _pages_with_path(pages: list, path_signals: list[str]) -> list:
    """URLパスがシグナルに一致するページを返す"""
    result = []
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in path_signals:
            if sig.lower() in url_lower:
                result.append(p)
                break
    return result


def _all_text_from_pages(pages: list) -> str:
    """全ページのテキストを結合"""
    parts = []
    for p in pages:
        for attr in ["title", "body_text", "meta_desc", "footer_text"]:
            val = getattr(p, attr, "")
            if val:
                parts.append(val if isinstance(val, str) else " ".join(val))
        for attr in ["h1", "h2", "h3"]:
            val = getattr(p, attr, [])
            if isinstance(val, list):
                parts.extend(val)
    return " ".join(parts)


def _has_contact(pages: list) -> bool:
    """メールアドレスまたはフォームの存在確認"""
    all_text = _all_text_from_pages(pages).lower()
    if re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", all_text):
        return True
    contact_words = ["お問い合わせ", "contact", "問い合わせフォーム", "inquiry"]
    return any(w.lower() in all_text for w in contact_words)


def _url_exists(pages: list, url_signals: list[str]) -> bool:
    """いずれかのURLシグナルに一致するページが存在するか"""
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in url_signals:
            if sig.lower() in url_lower:
                return True
    return False


def _extract_full_dates_with_spans(text: str) -> list[tuple[date, int, int]]:
    """本文から日単位で確定できる日付と文字位置を抽出する。"""
    found: dict[tuple[date, int, int], None] = {}
    patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]
    for pat in patterns:
        for match in re.finditer(pat, text):
            try:
                d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            found[(d, match.start(), match.end())] = None
    return sorted(found, key=lambda item: (item[0], item[1]))


def _extract_full_dates(text: str) -> list[date]:
    return sorted({d for d, _, _ in _extract_full_dates_with_spans(text)})


def _contains_recent_date(text: str, recent_years: int) -> tuple[bool, str]:
    """本文中の日付が概ね直近N年かを判定する。"""
    today = date.today()
    cutoff = today - timedelta(days=max(1, recent_years) * 365)

    full_dates = _extract_full_dates(text)
    recent_full = [d for d in full_dates if cutoff <= d <= today + timedelta(days=31)]
    if recent_full:
        return True, max(recent_full).isoformat()

    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    recent_year_floor = today.year - max(1, recent_years)
    recent = [y for y in years if recent_year_floor <= y <= today.year]
    if recent:
        return True, str(max(recent))
    return False, ""


def _recent_keyword_evidence(
    pages: list,
    keywords: list[str],
    recent_years: int,
) -> tuple[bool, list[str], str]:
    """同一ページ内でキーワードと直近日付が共存するEvidenceを探す。"""
    saw_hits: list[str] = []
    for p in pages:
        page_text = _all_text_from_pages([p])
        page_hits = _hit_keywords(_normalize(page_text), keywords)
        if not page_hits:
            continue
        saw_hits.extend(page_hits)
        recent, evidence = _contains_recent_date(page_text, recent_years)
        if recent:
            url = getattr(p, "url", "")
            detail = f"直近日付Evidence:{evidence}"
            if url:
                detail += f" / {url}"
            return True, list(dict.fromkeys(page_hits)), detail

    if saw_hits:
        return False, list(dict.fromkeys(saw_hits)), "対象KWあり／直近日付未確認"
    return False, [], "対象KW未確認"


_EXHIBITION_CONTEXT_RE = re.compile(
    r"展示会|見本市|exhibition|expo|trade\s*show|ブース|(?:技術|産業|国際|製造|電子|機械|材料)[^。\n]{0,20}フェア",
    re.IGNORECASE,
)
_EXHIBITION_ACTION_RE = re.compile(
    r"出展|ブース出展|exhibit(?:ion|or|ing)?|ご来場[^。\n]{0,80}(?:ありがとう|御礼)",
    re.IGNORECASE,
)
_EXHIBITION_NAME_HINT_RE = re.compile(
    r"(?:「[^」]{2,80}」|『[^』]{2,80}』|[\"“][^\"”]{2,80}[\"”]|"
    r"[A-Za-z][A-Za-z0-9+.'&’・\- ]{2,50}(?:20\d{2}|[’']?\d{2})|"
    r"[A-Za-z0-9０-９一-龥ぁ-んァ-ヶ・＆&+\-／/（）()'’ ]{2,70}(?:展示会|見本市|フェア|ショー|展))"
    r"\s*(?:に|へ)?\s*出展",
    re.IGNORECASE,
)
_UPCOMING_ACTION_RE = re.compile(
    r"出展予定|出展します|出展いたします|出展致します|出展のお知らせ|出展のご案内|出展決定|開催予定|will\s+exhibit",
    re.IGNORECASE,
)
_PAST_ACTION_RE = re.compile(
    r"出展しました|出展いたしました|出展致しました|出展報告|出展実績|出展終了|出展してまいりました|exhibited|"
    r"ご来場[^。\n]{0,80}(?:ありがとうございました|ありがとう|御礼)",
    re.IGNORECASE,
)
_EVENT_DATE_LABEL_RE = re.compile(r"開催日|会期|開催期間|日時|開催日時|event\s*date", re.IGNORECASE)
_PUBLISH_DATE_LABEL_RE = re.compile(r"投稿日|公開日|掲載日|更新日|配信日|publish(?:ed)?", re.IGNORECASE)


def _is_exhibition_context(text: str) -> bool:
    """『出店』単独を除外しつつ、固有イベント名＋出展表現も許容する。"""
    if not _EXHIBITION_ACTION_RE.search(text):
        return False
    return bool(_EXHIBITION_CONTEXT_RE.search(text) or _EXHIBITION_NAME_HINT_RE.search(text))


def _is_publish_date_context(text: str, start: int, end: int) -> bool:
    around = text[max(0, start - 48):end + 20]
    return bool(_PUBLISH_DATE_LABEL_RE.search(around))


def _date_context_score(text: str, start: int, end: int) -> int:
    """展示会開催日に近い日付ほど小さい値。公開日は強く減点する。"""
    action_positions = [m.start() for m in _EXHIBITION_ACTION_RE.finditer(text)]
    context_positions = [m.start() for m in _EXHIBITION_CONTEXT_RE.finditer(text)]
    positions = action_positions + context_positions
    distance = min((abs(start - pos) for pos in positions), default=5000)
    before = text[max(0, start - 48):end + 12]
    score = distance
    if _EVENT_DATE_LABEL_RE.search(before):
        score -= 1000
    if _PUBLISH_DATE_LABEL_RE.search(before):
        score += 2000
    return score


def _choose_event_date(text: str, candidates: list[tuple[date, int, int]]) -> tuple[date, int] | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda item: (_date_context_score(text, item[1], item[2]), item[0]))
    chosen = ranked[0]
    return chosen[0], chosen[1]


def _clean_event_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n|｜-‐–—:：")
    value = re.sub(r"^(?:展示会)?(?:出展|開催)(?:のお知らせ|のご案内)?[「『\"“]?", "", value)
    value = value.strip("「」『』\"“” ")
    return value[:80]


def _event_name_from_text(text: str, focus_pos: int) -> str:
    start = max(0, focus_pos - 300)
    end = min(len(text), focus_pos + 360)
    snippet = text[start:end]
    local_focus = focus_pos - start

    quoted: list[tuple[int, str]] = []
    for pat in [r"「([^」]{2,80})」", r"『([^』]{2,80})』", r"[\"“]([^\"”]{2,80})[\"”]"]:
        for match in re.finditer(pat, snippet):
            candidate = _clean_event_name(match.group(1))
            if not candidate:
                continue
            # 出展語近傍の引用名は、Smart Sensing等のように『展』を含まなくても許容する。
            distance = abs(match.start() - local_focus)
            if distance <= 260:
                quoted.append((distance, candidate))
    if quoted:
        quoted.sort(key=lambda item: (item[0], len(item[1])))
        return quoted[0][1]

    before_action = re.search(
        r"([A-Za-z][A-Za-z0-9+.'&’・\- ]{1,50}(?:20\d{2}|[’']?\d{2})?)\s*(?:に|へ)?出展",
        snippet,
        re.IGNORECASE,
    )
    if before_action:
        candidate = _clean_event_name(before_action.group(1))
        if len(candidate) >= 3:
            return candidate

    # 『第32回 日本国際工作機械見本市(JIMTOF2024)』等を優先する。
    explicit = re.search(
        r"(第\s*\d+\s*回[^。]{2,70}?(?:展示会|見本市|フェア|ショー|展)(?:\([^)]{1,30}\))?)",
        snippet,
        re.IGNORECASE,
    )
    if explicit:
        candidate = _clean_event_name(explicit.group(1))
        if candidate:
            return candidate

    event_matches: list[tuple[int, int, str]] = []
    for match in re.finditer(
        r"([A-Za-z0-9０-９一-龥ぁ-んァ-ヶ・＆&+\-／/（）()'’ ]{2,55}(?:展示会|見本市|フェア(?:20\d{2})?|ショー(?:20\d{2})?|展(?:20\d{2})?))",
        snippet,
        re.IGNORECASE,
    ):
        candidate = _clean_event_name(match.group(1))
        if candidate and candidate not in ("展示会", "出展"):
            event_matches.append((abs(match.start() - local_focus), len(candidate), candidate))
    if event_matches:
        event_matches.sort(key=lambda item: (item[0], item[1]))
        return event_matches[0][2]
    return ""


def _extract_event_name(text: str, focus_pos: int, title: str = "") -> str:
    """タイトルを優先し、次に開催日/出展語近傍から展示会名を抽出する。"""
    if title and _is_exhibition_context(title):
        name = _event_name_from_text(title, max(0, len(title) // 2))
        if name:
            return name
    return _event_name_from_text(text, focus_pos)


def _exhibition_evidence(
    pages: list,
    keywords: list[str],
    mode: str,
    recent_years: int = 2,
    company_name: str = "",
) -> tuple[bool, list[str], str]:
    """展示会予定/実績を開催日・対象会社・展示会名のEvidence付きで判定する。

    - 『出店』単独は展示会として扱わない。
    - 日付周辺のローカル文脈で予定/実績を判別し、別イベント同士の混線を防ぐ。
    - 過去の『出展します』告知は実績とはみなさず『過去出展告知（実績未確認）』として残す。
    - 対象会社名がページ内で確認できない場合は参考候補として出すがearned=False。
    """
    today = date.today()
    cutoff = today - timedelta(days=max(1, recent_years) * 365)
    future_limit = today + timedelta(days=730)
    saw_context = False
    reference_candidates: list[str] = []
    past_announcement_candidates: list[tuple[date, str]] = []
    candidates: list[tuple[date, str, str, str]] = []

    for p in pages:
        page_text = _all_text_from_pages([p])
        if not _is_exhibition_context(page_text):
            continue
        saw_context = True
        title = str(getattr(p, "title", "") or "").strip()
        url = str(getattr(p, "url", "") or "").strip()
        direct_company = _company_mentioned(company_name, page_text) if company_name else True

        for event_date, start, end in _extract_full_dates_with_spans(page_text):
            if _is_publish_date_context(page_text, start, end):
                continue
            local = page_text[max(0, start - 420):min(len(page_text), end + 520)]
            if not _is_exhibition_context(local):
                continue

            local_upcoming = bool(_UPCOMING_ACTION_RE.search(local))
            local_past = bool(_PAST_ACTION_RE.search(local))
            if mode == "upcoming":
                if not (today <= event_date <= future_limit and local_upcoming):
                    continue
            else:
                if not (cutoff <= event_date < today):
                    continue
                if not local_past:
                    if local_upcoming:
                        event_name = _extract_event_name(page_text, start, title=title)
                        parts = [f"過去出展告知:{event_date.isoformat()}"]
                        parts.append(f"展示会:{event_name}" if event_name else "展示会名未抽出")
                        parts.append("実績未確認")
                        parts.append("対象会社:一致" if direct_company else "対象会社:一致未確認")
                        if url:
                            parts.append(url)
                        past_announcement_candidates.append((event_date, " / ".join(parts)))
                    continue

            event_name = _extract_event_name(page_text, start, title=title)
            kind = "出展予定" if mode == "upcoming" else "出展実績"
            parts = [f"{kind}:{event_date.isoformat()}"]
            parts.append(f"展示会:{event_name}" if event_name else "展示会名未抽出")
            parts.append("対象会社:一致" if direct_company else "対象会社:一致未確認")
            if url:
                parts.append(url)
            detail = " / ".join(parts)

            if direct_company:
                candidates.append((event_date, event_name, url, detail))
            else:
                reference_candidates.append("参考候補 / " + detail)

    if candidates:
        chosen = min(candidates, key=lambda x: x[0]) if mode == "upcoming" else max(candidates, key=lambda x: x[0])
        return True, [], chosen[3]

    if reference_candidates:
        return False, [], reference_candidates[0]

    if mode == "recent" and past_announcement_candidates:
        chosen = max(past_announcement_candidates, key=lambda item: item[0])
        return False, [], chosen[1]

    if saw_context:
        label = "将来の開催日＋出展予定表現" if mode == "upcoming" else f"直近{recent_years}年の開催日＋出展後Evidence"
        return False, [], f"展示会文脈あり／{label}未確認"
    return False, [], "展示会Evidence未確認"


def _contact_department_evidence(
    pages: list,
    department_keywords: list[str],
) -> tuple[bool, list[str], str]:
    """問い合わせ手段と営業対象になり得る部署情報の両方を確認する。"""
    if not _has_contact(pages):
        return False, [], "連絡手段未確認"
    all_text = _normalize(_all_text_from_pages(pages))
    hits = _hit_keywords(all_text, department_keywords)
    if not hits:
        return False, [], "連絡手段あり／部署情報未確認"
    return True, list(dict.fromkeys(hits)), "連絡手段＋部署情報を確認"


# ════════════════════════════════════════════════════════
#  軸ごとの判定
# ════════════════════════════════════════════════════════

def _evaluate_axis(
    axis: dict[str, Any],
    pages: list,
    all_text: str,
    company_name: str = "",
) -> AxisResult:
    """1評価軸または調査項目を判定してAxisResultを返す"""
    ax_id      = axis.get("id", "")
    ax_name    = axis.get("name", ax_id)
    ax_points  = int(axis.get("points", 0))
    detection  = axis.get("detection", "keyword_any")
    keywords   = axis.get("keywords", [])
    patterns   = axis.get("patterns", [])
    url_sigs   = axis.get("url_signals", [])
    min_hits   = axis.get("min_hits", 1)
    tgt_pages  = axis.get("target_pages", [])

    text_lower = _normalize(all_text)
    earned     = False
    hits: list[str] = []
    detail     = ""

    if detection == "keyword_any":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) > 0

    elif detection == "keyword_count":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) >= min_hits

    elif detection == "keyword_and_pattern":
        hits = _hit_keywords(text_lower, keywords)
        pat_hit = _hit_patterns(all_text, patterns)
        url_hit = _url_exists(pages, url_sigs) if url_sigs else False
        earned = len(hits) > 0 or pat_hit or url_hit
        details = []
        if pat_hit:
            details.append("型番/スペックパターン検出")
        if url_hit:
            details.append("対象URL検出")
        detail = " / ".join(details)

    elif detection == "keyword_groups_all":
        groups = axis.get("keyword_groups", []) or []
        group_hits: list[list[str]] = []
        for group in groups:
            gh = _hit_keywords(text_lower, group)
            group_hits.append(gh)
            hits.extend(gh)
        earned = bool(groups) and all(group_hits)
        detail = " / ".join(
            f"G{i + 1}:{len(gh)}件" for i, gh in enumerate(group_hits)
        )

    elif detection == "keyword_any_on_page":
        target = _pages_with_path(pages, tgt_pages) if tgt_pages else pages
        if target:
            page_text = _normalize(_all_text_from_pages(target))
            hits = _hit_keywords(page_text, keywords)
            earned = len(hits) > 0
            detail = f"対象ページ{len(target)}件"
        else:
            detail = "対象ページなし"

    elif detection == "recent_keyword_any":
        earned, hits, detail = _recent_keyword_evidence(
            pages,
            keywords,
            int(axis.get("recent_years", 2)),
        )

    elif detection == "recent_keyword_any_on_page":
        target = _pages_with_path(pages, tgt_pages) if tgt_pages else pages
        if target:
            earned, hits, detail = _recent_keyword_evidence(
                target,
                keywords,
                int(axis.get("recent_years", 2)),
            )
        else:
            detail = "対象ページなし"

    elif detection == "exhibition_upcoming":
        earned, hits, detail = _exhibition_evidence(
            pages,
            keywords,
            "upcoming",
            int(axis.get("recent_years", 2)),
            company_name=company_name,
        )

    elif detection == "exhibition_recent":
        earned, hits, detail = _exhibition_evidence(
            pages,
            keywords,
            "recent",
            int(axis.get("recent_years", 2)),
            company_name=company_name,
        )

    elif detection == "contact_department":
        earned, hits, detail = _contact_department_evidence(pages, keywords)

    elif detection == "regex":
        pattern = axis.get("pattern", "")
        try:
            m = re.search(pattern, all_text) if pattern else None
            earned = m is not None
            if earned:
                detail = f"正規表現マッチ: {m.group()[:50]}"
        except re.error as e:
            detail = f"正規表現エラー: {e}"

    elif detection == "keyword_all":
        hits = _hit_keywords(text_lower, keywords)
        earned = len(hits) >= len(keywords)
        detail = f"{len(hits)}/{len(keywords)}キーワードヒット"

    elif detection == "url_exists":
        earned = _url_exists(pages, url_sigs)
        if earned:
            detail = "対象URL検出"

    elif detection == "contact":
        earned = _has_contact(pages)
        if earned:
            detail = "メール/フォーム検出"

    elif detection == "manual":
        earned = False
        detail = axis.get("unverified_text", "要確認")

    return AxisResult(
        id=ax_id,
        name=ax_name,
        points=ax_points,
        earned=earned,
        score=ax_points if earned else 0,
        hit_keywords=list(dict.fromkeys(hits))[:5],
        detail=detail,
    )


# ════════════════════════════════════════════════════════
#  ProfileScorer クラス
# ════════════════════════════════════════════════════════

class ProfileScorer:
    """YAMLプロファイルの scoring_axes に基づいてスコアリングする。"""

    def __init__(self, profile) -> None:
        self.profile = profile
        self.axes_defs = getattr(profile, "scoring_axes", [])
        self.report_defs = getattr(profile, "report_fields", [])
        self.score_cap = getattr(profile, "score_cap", 100)
        self.score_sum_max = getattr(profile, "score_sum_max", 100)
        self.threshold_excellent = getattr(profile, "threshold_excellent", 70)
        self.threshold_good = getattr(profile, "threshold_good", 50)

    def score(
        self,
        company_name: str,
        company_url: str,
        parsed_pages: list,
    ) -> ProfileScore:
        all_text = _all_text_from_pages(parsed_pages)
        axes_results: list[AxisResult] = []

        for axis_def in self.axes_defs:
            axes_results.append(
                _evaluate_axis(axis_def, parsed_pages, all_text, company_name=company_name)
            )

        report_results: list[AxisResult] = []
        for report_def in self.report_defs:
            report_def = dict(report_def)
            report_def["points"] = 0
            report_results.append(
                _evaluate_axis(report_def, parsed_pages, all_text, company_name=company_name)
            )

        raw_score = sum(ax.score for ax in axes_results)

        bonus_total = 0
        bonus_rules = getattr(self.profile, "bonus_rules", []) or []
        for rule in bonus_rules:
            conditions = rule.get("condition", [])
            bonus = rule.get("bonus", 0)
            if all(kw.lower() in _normalize(all_text) for kw in conditions):
                bonus_total += bonus
                log.debug(f"  ボーナス適用: {rule.get('name','')} +{bonus}点")

        raw_score += bonus_total
        total_score = min(raw_score, self.score_cap)
        if raw_score <= -100:
            total_score = raw_score

        if hasattr(self.profile, "judge"):
            judgment = self.profile.judge(total_score)
        elif total_score >= self.threshold_excellent:
            judgment = "◎"
        elif total_score >= self.threshold_good:
            judgment = "○"
        elif total_score > 0:
            judgment = "△"
        else:
            judgment = "－"

        return ProfileScore(
            company_name=company_name,
            company_url=company_url,
            profile_name=self.profile.name,
            total_score=total_score,
            raw_score=raw_score,
            score_cap=self.score_cap,
            judgment=judgment,
            axes=axes_results,
            report_fields=report_results,
        )


def create_scorer_from_profile(profile) -> ProfileScorer:
    """Profile オブジェクトから ProfileScorer を生成"""
    return ProfileScorer(profile)
