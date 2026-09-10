"""
profile_scorer.py - プロファイル駆動スコアリングエンジン
=========================================================
YAMLで定義した評価軸・配点に基づいて企業サイトをスコアリングする。
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
    id: str
    name: str
    points: int
    earned: bool
    score: int
    hit_keywords: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class ProfileScore:
    company_name: str
    company_url: str
    profile_name: str
    total_score: int
    raw_score: int
    score_cap: int
    judgment: str
    axes: list[AxisResult] = field(default_factory=list)
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


def _normalize(text: str) -> str:
    return text.lower().replace("\u3000", " ").replace("\n", " ")


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000・･\-‐‑–—_/|｜()（）\[\]【】]", "", text.lower())


def _company_aliases(company_name: str) -> list[str]:
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
    result: list[str] = []
    for alias in aliases:
        value = _compact(alias)
        if len(value) >= 3 and value not in result:
            result.append(value)
    return result


def _company_mentioned(company_name: str, text: str) -> bool:
    haystack = _compact(text)
    return any(alias in haystack for alias in _company_aliases(company_name))


_CORPORATE_IDENTITY_RE = re.compile(
    r"株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|一般財団法人|"
    r"\b(?:inc\.?|corp\.?|corporation|co\.?\s*,?\s*ltd\.?|ltd\.?)\b",
    re.IGNORECASE,
)


def _page_identity_text(page) -> str:
    """ページ自身がどの法人を表すかを見る。footer/bodyの会社一覧は使わない。"""
    parts = [
        str(getattr(page, "title", "") or ""),
        str(getattr(page, "meta_desc", "") or ""),
    ]
    for attr in ("h1", "h2"):
        value = getattr(page, attr, []) or []
        if isinstance(value, list):
            parts.extend(str(v) for v in value[:3] if v)
    return " ".join(parts)


def _root_like_page(page) -> bool:
    url = str(getattr(page, "url", "") or "")
    path = re.sub(r"^https?://[^/]+", "", url.lower()).split("?", 1)[0]
    path = path.rstrip("/") or "/"
    return path in ("/", "/jp", "/en")


_JA_CORPORATE_IDENTITY_RE = re.compile(
    r"株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|一般財団法人"
)


def _formal_scoring_scope(pages: list, company_name: str) -> tuple[list, bool]:
    """R2正式採点の会社スコープ。

    専用企業サイト:
        ルートが対象会社本人なら同一サイト全体を使用。

    親会社・グループ共通サイト:
        ルートに別の日本法人名が明示されている場合のみ厳格モード。
        title / h1 / h2 / meta description で対象会社本人と確認できる
        ページだけ正式採点に使用する。

    footerや本文末尾のグループ会社一覧だけで本人扱いしない。
    """
    if not pages or not company_name:
        return pages, False

    root = next((p for p in pages if _root_like_page(p)), pages[0])
    root_identity = _page_identity_text(root)

    # 自社専用サイト
    if _company_mentioned(company_name, root_identity):
        return pages, False

    # ブランド名のみなど、別法人サイトと断定できない場合は過剰除外しない。
    if not _JA_CORPORATE_IDENTITY_RE.search(root_identity):
        return pages, False

    # 親会社/グループ共通サイト。
    scoped = [
        p
        for p in pages
        if _company_mentioned(company_name, _page_identity_text(p))
    ]
    return scoped, True


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
    result = []
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in path_signals:
            if sig.lower() in url_lower:
                result.append(p)
                break
    return result


def _all_text_from_pages(pages: list) -> str:
    parts: list[str] = []
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
    all_text = _all_text_from_pages(pages).lower()
    if re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", all_text):
        return True
    return any(
        word.lower() in all_text
        for word in ["お問い合わせ", "contact", "問い合わせフォーム", "inquiry"]
    )


def _url_exists(pages: list, url_signals: list[str]) -> bool:
    for p in pages:
        url_lower = getattr(p, "url", "").lower()
        for sig in url_signals:
            if sig.lower() in url_lower:
                return True
    return False


def _extract_full_dates_with_spans(text: str) -> list[tuple[date, int, int]]:
    found: dict[tuple[date, int, int], None] = {}
    patterns = [
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]
    for pat in patterns:
        for match in re.finditer(pat, text):
            try:
                parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            found[(parsed, match.start(), match.end())] = None
    return sorted(found, key=lambda item: (item[0], item[1]))


def _extract_full_dates(text: str) -> list[date]:
    return sorted({d for d, _, _ in _extract_full_dates_with_spans(text)})


def _contains_recent_date(text: str, recent_years: int) -> tuple[bool, str]:
    today = date.today()
    cutoff = today - timedelta(days=max(1, recent_years) * 365)
    full_dates = _extract_full_dates(text)
    recent_full = [d for d in full_dates if cutoff <= d <= today + timedelta(days=31)]
    if recent_full:
        return True, max(recent_full).isoformat()

    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    floor = today.year - max(1, recent_years)
    recent = [y for y in years if floor <= y <= today.year]
    if recent:
        return True, str(max(recent))
    return False, ""


def _recent_keyword_evidence(
    pages: list,
    keywords: list[str],
    recent_years: int,
) -> tuple[bool, list[str], str]:
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
    r"[A-Za-z0-9０-９一-龥ぁ-んァ-ヶー・＆&+\-／/（）()'’ ]{2,70}(?:展示会|見本市|フェア|ショー|展))"
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
_EVENT_DATE_LABEL_RE = re.compile(
    r"開催日|会期|開催期間|開催時期|日時|開催日時|event\s*date",
    re.IGNORECASE,
)
_PUBLISH_DATE_LABEL_RE = re.compile(
    r"投稿日|公開日|掲載日|更新日|配信日|publish(?:ed)?",
    re.IGNORECASE,
)
_GENERIC_EVENT_TITLE_RE = re.compile(
    r"^(?:お知らせ|ニュース|news|展示会|展示会情報|exhibition(?: information)?|"
    r"20\d{2}年?(?:展示会情報|exhibition information)?)$",
    re.IGNORECASE,
)


def _is_exhibition_context(text: str) -> bool:
    if not _EXHIBITION_ACTION_RE.search(text):
        return False
    return bool(_EXHIBITION_CONTEXT_RE.search(text) or _EXHIBITION_NAME_HINT_RE.search(text))


def _is_publish_date_context(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 24):start]
    if _EVENT_DATE_LABEL_RE.search(before):
        return False
    return bool(_PUBLISH_DATE_LABEL_RE.search(before))


def _date_context_score(text: str, start: int, end: int) -> int:
    action_positions = [m.start() for m in _EXHIBITION_ACTION_RE.finditer(text)]
    context_positions = [m.start() for m in _EXHIBITION_CONTEXT_RE.finditer(text)]
    positions = action_positions + context_positions
    distance = min((abs(start - pos) for pos in positions), default=5000)
    label_before = text[max(0, start - 32):start]
    score = distance
    if _EVENT_DATE_LABEL_RE.search(label_before):
        score -= 1000
    if _PUBLISH_DATE_LABEL_RE.search(label_before):
        score += 2000
    return score


def _choose_event_date(
    text: str,
    candidates: list[tuple[date, int, int]],
) -> tuple[date, int, int] | None:
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (_date_context_score(text, item[1], item[2]), item[0]),
    )


def _clean_event_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n|｜-‐–—:：")
    value = value.strip("「」『』\"“” ")
    return value[:120]


def _event_name_from_title(title: str) -> str:
    if not title:
        return ""
    core = re.split(r"[|｜]", title, maxsplit=1)[0].strip()
    core = re.split(r"\s+[‐–—-]\s+", core, maxsplit=1)[0].strip()

    for pat in [r"「([^」]{2,100})」", r"『([^』]{2,100})』", r"[\"“]([^\"”]{2,100})[\"”]"]:
        m = re.search(pat, core)
        if m:
            return _clean_event_name(m.group(1))

    core = re.sub(
        r"\s*(?:への?|に|へ)?\s*出展(?:の)?(?:お知らせ|ご案内|報告|実績)[。.]?\s*$",
        "",
        core,
        flags=re.IGNORECASE,
    )
    core = re.sub(
        r"\s*(?:への?|に|へ)?\s*出展(?:します|いたします|致します|しました|いたしました|致しました)?[。.]?\s*$",
        "",
        core,
        flags=re.IGNORECASE,
    )
    core = _clean_event_name(core)
    if not core or _GENERIC_EVENT_TITLE_RE.fullmatch(core) or len(core) < 3:
        return ""
    return core


def _event_name_from_text(text: str, focus_pos: int) -> str:
    start = max(0, focus_pos - 320)
    end = min(len(text), focus_pos + 380)
    snippet = text[start:end]
    local_focus = focus_pos - start

    quoted: list[tuple[int, str]] = []
    for pat in [r"「([^」]{2,100})」", r"『([^』]{2,100})』", r"[\"“]([^\"”]{2,100})[\"”]"]:
        for match in re.finditer(pat, snippet):
            candidate = _clean_event_name(match.group(1))
            if candidate:
                quoted.append((abs(match.start() - local_focus), candidate))
    if quoted:
        quoted.sort(key=lambda item: (item[0], len(item[1])))
        return quoted[0][1]

    explicit = re.search(
        r"(第\s*\d+\s*回[^。\n]{2,90}?(?:展示会|見本市|フェア|ショー|展)(?:\([^)]{1,30}\))?)",
        snippet,
        re.IGNORECASE,
    )
    if explicit:
        candidate = _clean_event_name(explicit.group(1))
        if candidate:
            return candidate

    before_action = re.search(
        r"([^。！？\n|｜]{3,100}?)(?:に|へ|への)?\s*出展(?:します|いたします|致します|しました|いたしました|致しました)",
        snippet,
        re.IGNORECASE,
    )
    if before_action:
        candidate = _clean_event_name(before_action.group(1))
        candidate = re.sub(r"^.*?(?:当社|弊社|当社は|弊社は)\s*", "", candidate)
        if len(candidate) >= 3:
            return candidate[-100:]

    event_matches: list[tuple[int, int, str]] = []
    for match in re.finditer(
        r"([A-Za-z0-9０-９一-龥ぁ-んァ-ヶー・＆&+\-／/（）()'’ ]{2,70}(?:展示会|見本市|フェア(?:20\d{2})?|ショー(?:20\d{2})?|展(?:20\d{2})?))",
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
    title_name = _event_name_from_title(title)
    if title_name:
        return title_name
    return _event_name_from_text(text, focus_pos)


def _is_generic_exhibition_listing(url: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", str(url).lower()).split("?", 1)[0]
    path = path.rstrip("/") or "/"
    if path == "/":
        return True
    patterns = [
        r"/(?:news|topics|press|release|event|events|exhibition|news_exhibition|newinformation)(?:/page/\d+)?",
        r"/(?:jp|en)/technology/event",
        r"/recruit-news(?:/recruit-news_category/[^/]+|/date/20\\d{2}/\\d{1,2})?",
        r"/20\d{2}(?:/page/\d+)?",
    ]
    return any(re.fullmatch(pattern, path) for pattern in patterns)


def _exhibition_page_quality(url: str) -> int:
    path = re.sub(r"^https?://[^/]+", "", str(url).lower()).split("?", 1)[0].rstrip("/")
    if re.search(r"/(?:jp|en)/technology/event/y20\d{2}$", path):
        return 2
    return 3


def _pick_exhibition_candidate(items: list[tuple], mode: str) -> tuple | None:
    if not items:
        return None
    best_quality = max(item[0] for item in items)
    same_quality = [item for item in items if item[0] == best_quality]
    if mode == "upcoming":
        return min(same_quality, key=lambda item: item[1])
    return max(same_quality, key=lambda item: item[1])


def _infer_exhibition_year(title: str, url: str, text: str) -> int | None:
    """年が省略された「6月10日」等を補完するための年を推定する。"""
    for source in (title, url):
        years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", str(source or ""))
        if years:
            return int(years[-1])

    full_dates = _extract_full_dates(text)
    if full_dates:
        recent = [
            d.year for d in full_dates
            if date.today().year - 2 <= d.year <= date.today().year + 1
        ]
        if recent:
            return max(recent)
    return None


def _exhibition_date_candidates(
    text: str,
    title: str = "",
    url: str = "",
) -> list[tuple[date, int, int]]:
    """完全日付に加え、ページ文脈から年を補完した月日も抽出する。"""
    full = _extract_full_dates_with_spans(text)
    found: dict[tuple[date, int, int], None] = {
        item: None for item in full
    }

    inferred_year = _infer_exhibition_year(title, url, text)
    if inferred_year is None:
        return list(found)

    for match in re.finditer(r"(?<!\d)(\d{1,2})月\s*(\d{1,2})日", text):
        # 「2026年6月10日」の月日部分を二重登録しない。
        if any(start <= match.start() < end for _, start, end in full):
            continue
        try:
            parsed = date(
                inferred_year,
                int(match.group(1)),
                int(match.group(2)),
            )
        except ValueError:
            continue
        found[(parsed, match.start(), match.end())] = None

    return sorted(found, key=lambda item: (item[0], item[1]))


def _event_date_context_tier(text: str, start: int) -> int:
    """イベント開催日らしさ。会期ラベルを最優先する。"""
    before = text[max(0, start - 48):start]
    if _EVENT_DATE_LABEL_RE.search(before):
        return 2

    positions = (
        [m.start() for m in _EXHIBITION_ACTION_RE.finditer(text)]
        + [m.start() for m in _EXHIBITION_CONTEXT_RE.finditer(text)]
    )
    distance = min((abs(start - pos) for pos in positions), default=99999)
    if distance <= 500:
        return 1
    return 0


def _exhibition_evidence(
    pages: list,
    keywords: list[str],
    mode: str,
    recent_years: int = 2,
    company_name: str = "",
) -> tuple[bool, list[str], str]:
    today = date.today()
    cutoff = today - timedelta(days=max(1, recent_years) * 365)
    future_limit = today + timedelta(days=730)

    saw_context = False
    reference_candidates: list[tuple[int, date, str]] = []
    past_announcement_candidates: list[tuple[int, date, str]] = []
    candidates: list[tuple[int, date, str]] = []

    for p in pages:
        page_text = _all_text_from_pages([p])
        if not _is_exhibition_context(page_text):
            continue

        saw_context = True
        title = str(getattr(p, "title", "") or "").strip()
        url = str(getattr(p, "url", "") or "").strip()

        # 一覧・カテゴリページそのものを展示会実績にしない。
        if _is_generic_exhibition_listing(url):
            continue

        direct_company = (
            _company_mentioned(company_name, page_text)
            if company_name else True
        )

        date_candidates = [
            item
            for item in _exhibition_date_candidates(
                page_text,
                title=title,
                url=url,
            )
            if not _is_publish_date_context(
                page_text, item[1], item[2]
            )
        ]

        for event_date, start, end in date_candidates:
            # 複数イベントを持つ年別一覧にも対応するため、日付ごとに評価する。
            local = page_text[
                max(0, start - 700):
                min(len(page_text), end + 800)
            ]

            if (
                not _is_exhibition_context(local)
                and not _is_exhibition_context(title)
            ):
                continue

            title_upcoming = bool(_UPCOMING_ACTION_RE.search(title))
            title_past = bool(_PAST_ACTION_RE.search(title))
            local_upcoming = bool(_UPCOMING_ACTION_RE.search(local))
            local_past = bool(_PAST_ACTION_RE.search(local))

            if title_upcoming and not title_past:
                local_upcoming = True
                local_past = False
            elif title_past:
                local_past = True

            # 詳細ページ > 年別一覧、かつ「会期」等の開催日文脈を優先。
            quality = (
                _exhibition_page_quality(url) * 10
                + _event_date_context_tier(page_text, start)
            )

            event_name = _extract_event_name(
                page_text,
                start,
                title=title,
            )

            if mode == "upcoming":
                if not (
                    today <= event_date <= future_limit
                    and local_upcoming
                ):
                    continue
                kind = "出展予定"

            else:
                if not (cutoff <= event_date < today):
                    continue

                # 過去の日付だが文章が「出展します」のままなら、
                # 実績確定とはせず「過去出展告知」とする。
                if not local_past:
                    if local_upcoming:
                        parts = [
                            f"過去出展告知:{event_date.isoformat()}"
                        ]
                        parts.append(
                            f"展示会:{event_name}"
                            if event_name else "展示会名未抽出"
                        )
                        parts.append("実績未確認")
                        parts.append(
                            "対象会社:一致"
                            if direct_company
                            else "対象会社:一致未確認"
                        )
                        if url:
                            parts.append(url)

                        past_announcement_candidates.append(
                            (quality, event_date, " / ".join(parts))
                        )
                    continue

                kind = "出展実績"

            parts = [f"{kind}:{event_date.isoformat()}"]
            parts.append(
                f"展示会:{event_name}"
                if event_name else "展示会名未抽出"
            )
            parts.append(
                "対象会社:一致"
                if direct_company
                else "対象会社:一致未確認"
            )
            if url:
                parts.append(url)

            detail = " / ".join(parts)

            if direct_company:
                candidates.append(
                    (quality, event_date, detail)
                )
            else:
                reference_candidates.append(
                    (
                        quality,
                        event_date,
                        "参考候補 / " + detail,
                    )
                )

    chosen = _pick_exhibition_candidate(candidates, mode)
    if chosen:
        return True, [], chosen[2]

    reference = _pick_exhibition_candidate(
        reference_candidates,
        mode,
    )
    if reference:
        return False, [], reference[2]

    if mode == "recent":
        past_announcement = _pick_exhibition_candidate(
            past_announcement_candidates,
            mode,
        )
        if past_announcement:
            return False, [], past_announcement[2]

    if saw_context:
        label = (
            "将来の開催日＋出展予定表現"
            if mode == "upcoming"
            else f"直近{recent_years}年の開催日＋出展後Evidence"
        )
        return False, [], f"展示会文脈あり／{label}未確認"

    return False, [], "展示会Evidence未確認"

def _contact_department_evidence(
    pages: list,
    department_keywords: list[str],
) -> tuple[bool, list[str], str]:
    if not _has_contact(pages):
        return False, [], "連絡手段未確認"
    all_text = _normalize(_all_text_from_pages(pages))
    hits = _hit_keywords(all_text, department_keywords)
    if not hits:
        return False, [], "連絡手段あり／部署情報未確認"
    return True, list(dict.fromkeys(hits)), "連絡手段＋部署情報を確認"


def _evaluate_axis(
    axis: dict[str, Any],
    pages: list,
    all_text: str,
    company_name: str = "",
) -> AxisResult:
    ax_id = axis.get("id", "")
    ax_name = axis.get("name", ax_id)
    ax_points = int(axis.get("points", 0))
    detection = axis.get("detection", "keyword_any")
    keywords = axis.get("keywords", [])
    patterns = axis.get("patterns", [])
    url_sigs = axis.get("url_signals", [])
    min_hits = axis.get("min_hits", 1)
    tgt_pages = axis.get("target_pages", [])

    text_lower = _normalize(all_text)
    earned = False
    hits: list[str] = []
    detail = ""

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
        detail = " / ".join(f"G{i + 1}:{len(gh)}件" for i, gh in enumerate(group_hits))

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
            pages, keywords, int(axis.get("recent_years", 2))
        )

    elif detection == "recent_keyword_any_on_page":
        target = _pages_with_path(pages, tgt_pages) if tgt_pages else pages
        if target:
            earned, hits, detail = _recent_keyword_evidence(
                target, keywords, int(axis.get("recent_years", 2))
            )
        else:
            detail = "対象ページなし"

    elif detection == "exhibition_upcoming":
        earned, hits, detail = _exhibition_evidence(
            pages, keywords, "upcoming", int(axis.get("recent_years", 2)), company_name=company_name
        )

    elif detection == "exhibition_recent":
        earned, hits, detail = _exhibition_evidence(
            pages, keywords, "recent", int(axis.get("recent_years", 2)), company_name=company_name
        )

    elif detection == "contact_department":
        earned, hits, detail = _contact_department_evidence(pages, keywords)

    elif detection == "regex":
        pattern = axis.get("pattern", "")
        try:
            match = re.search(pattern, all_text) if pattern else None
            earned = match is not None
            if earned:
                detail = f"正規表現マッチ: {match.group()[:50]}"
        except re.error as exc:
            detail = f"正規表現エラー: {exc}"

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


class ProfileScorer:
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

        formal_pages, scope_guarded = _formal_scoring_scope(parsed_pages, company_name)
        formal_text = _all_text_from_pages(formal_pages)
        axes_results = [
            _evaluate_axis(axis_def, formal_pages, formal_text, company_name=company_name)
            for axis_def in self.axes_defs
        ]
        if scope_guarded:
            for axis in axes_results:
                scope_note = "対象会社スコープ外Evidence除外"
                if axis.detail:
                    axis.detail += f" / {scope_note}"
                else:
                    axis.detail = scope_note

        # 採点外Evidenceは従来どおり全取得ページを参照する。
        # 展示会項目は内部で対象会社一致/不一致を明示する。
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
            if all(kw.lower() in _normalize(formal_text) for kw in conditions):
                bonus_total += bonus
                log.debug(f"  ボーナス適用: {rule.get('name', '')} +{bonus}点")

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
    return ProfileScorer(profile)
