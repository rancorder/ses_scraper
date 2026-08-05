"""
ses_config.py - SES事業スクリーニング専用設定
==============================================
発注側（SESエンジニアを受け入れたい企業）と
供給側（SES事業を始めたい・拡大したい企業）の両方を判定する。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

# ── パス ──────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "company_analyzer" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Ollama設定 ─────────────────────────────────────────────────
@dataclass
class OllamaConfig:
    base_url: str  = "http://localhost:11434"
    model: str     = "gemma3:4b"       # ollama pull gemma3:4b / llama3.2:3b
    timeout: int   = 60                # 秒（3.4bは軽量なので十分）
    max_tokens: int = 512

OLLAMA_CFG = OllamaConfig()

# ── スコアリング閾値 ───────────────────────────────────────────
@dataclass
class SesScoreConfig:
    # 発注側・供給側それぞれの営業候補閾値
    client_threshold: int   = 50   # 発注側スコア（受け入れたい企業）
    partner_threshold: int  = 50   # 供給側スコア（SES事業者）
    # どちらかがこの点以上なら出力対象
    output_threshold: int   = 40

SES_SCORE_CFG = SesScoreConfig()

# ── 発注側キーワード（SESエンジニアを受け入れたい企業） ──────────
CLIENT_KEYWORDS: dict[str, list[str]] = {
    # システム開発・ITプロジェクトを多数抱える
    "it_projects": [
        "システム開発", "アプリ開発", "ソフトウェア開発",
        "デジタル化", "DX推進", "IT化", "基幹システム",
        "社内SE", "情報システム部", "IT部門",
    ],
    # 外注・BPを積極活用している
    "outsourcing": [
        "外注", "業務委託", "BP", "ビジネスパートナー",
        "パートナー企業募集", "協力会社", "フリーランス歓迎",
        "外部エンジニア", "常駐", "客先常駐",
    ],
    # 採用・リソース不足のシグナル
    "recruitment_signal": [
        "エンジニア採用", "IT人材", "即戦力", "中途採用",
        "開発チーム拡大", "増員", "リソース不足",
        "エンジニア不足", "人手不足", "開発力強化",
    ],
    # IT投資・成長中企業
    "growth_signal": [
        "資金調達", "事業拡大", "新規事業", "スタートアップ",
        "急成長", "上場", "IPO", "シリーズ",
    ],
}

# ── 供給側キーワード（SES事業を始めたい・拡大したい企業） ─────────
PARTNER_KEYWORDS: dict[str, list[str]] = {
    # SES事業の直接記述
    "ses_direct": [
        "SES", "エスイーエス", "システムエンジニアリングサービス",
        "SE派遣", "エンジニア派遣", "技術者派遣",
        "常駐型開発", "常駐支援", "エンジニアリングサービス",
    ],
    # IT系受託・人月ビジネス
    "it_contract": [
        "受託開発", "請負開発", "ソフトウェア受託",
        "システム受託", "開発代行", "ITアウトソーシング",
        "オフショア", "ラボ型", "ラボ開発",
    ],
    # 技術者を抱えている
    "engineers": [
        "エンジニアチーム", "技術者集団", "開発部隊",
        "エンジニア在籍", "即戦力エンジニア", "フリーランス",
        "個人事業主", "業務委託エンジニア",
    ],
    # 新規SES参入シグナル
    "expansion_signal": [
        "事業拡大", "新サービス", "新規参入", "多角化",
        "IT事業部", "システム事業部", "デジタル事業",
        "技術支援", "ITコンサル",
    ],
    # 採用で技術者を増やしている
    "hiring_engineers": [
        "エンジニア募集", "プログラマー募集", "SE募集",
        "バックエンドエンジニア", "フロントエンドエンジニア",
        "インフラエンジニア", "クラウドエンジニア",
        "Javaエンジニア", "Pythonエンジニア",
    ],
}

# ── クロール対象パス（SES情報が載りやすいページ）────────────────
SES_TARGET_PATHS = [
    "/", "/service", "/services", "/business",
    "/partner", "/partners", "/recruit", "/careers", "/career",
    "/company", "/about", "/news", "/blog",
    "/it", "/ses", "/engineer", "/outsourcing",
]

# ── Ollamaへのプロンプトテンプレート ──────────────────────────
SCORING_PROMPT = """
あなたは日本のIT企業の営業担当アシスタントです。
以下の企業サイトのテキストを読み、JSON形式で回答してください。

【企業名】{company_name}
【サイトテキスト】
{site_text}

以下のJSON形式のみで回答してください（他の文字は一切不要）：
{{
  "client_score": <0-100の整数。SESエンジニアを受け入れたい・外注したいニーズがある度合い>,
  "partner_score": <0-100の整数。SES事業者として協業できる可能性の度合い>,
  "reason": "<判定根拠を1〜2文の日本語で>",
  "sales_talk": "<この企業への営業トーク案を2〜3文の日本語で。具体的に>"
}}
"""
