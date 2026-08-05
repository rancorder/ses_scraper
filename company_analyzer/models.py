"""
models.py - システム全体で使うデータモデル（Pydantic）
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, HttpUrl, Field


class PageResult(BaseModel):
    """1ページ分のクロール結果"""
    url:          str
    status_code:  int            = 0
    html:         str            = ""
    error:        Optional[str]  = None
    elapsed_ms:   int            = 0


class SiteFeatures(BaseModel):
    """1サイト分の特徴量（feature_extractor出力）"""
    domain:             str
    pages_crawled:      list[str]           = Field(default_factory=list)

    # 検出フラグ
    product_presence:   bool                = False
    oem_presence:       bool                = False

    # キーワードカテゴリ別ヒット
    tech_keywords:      dict[str, list[str]] = Field(default_factory=dict)

    # 採用シグナル
    recruitment_ai:     bool                = False
    recruitment_eng:    bool                = False

    # コンタクト
    contact_email:      Optional[str]       = None
    has_contact_form:   bool                = False
    has_captcha:        bool                = False

    # サイト活動
    has_news:           bool                = False
    has_blog:           bool                = False
    has_press:          bool                = False

    # 製品ページURL
    product_pages:      list[str]           = Field(default_factory=list)
    oem_pages:          list[str]           = Field(default_factory=list)

    # 将来拡張用
    tech_stack:         dict[str, str]      = Field(default_factory=dict)   # e.g. {"framework": "React"}
    last_updated:       Optional[str]       = None
    llm_classification: Optional[str]       = None


class ScoredCompany(BaseModel):
    """スコアリング結果（最終出力）"""
    company_name:   str
    company_url:    str
    domain:         str
    score:          int
    is_candidate:   bool
    score_breakdown: dict[str, int]         = Field(default_factory=dict)
    detected_features: list[str]            = Field(default_factory=list)
    product_pages:  list[str]               = Field(default_factory=list)
    oem_pages:      list[str]               = Field(default_factory=list)
    tech_keywords:  dict[str, list[str]]    = Field(default_factory=dict)
    contact_email:  Optional[str]           = None
    has_contact_form: bool                  = False
    pages_crawled:  int                     = 0
    error:          Optional[str]           = None