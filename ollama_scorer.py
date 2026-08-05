"""
ollama_scorer.py - Ollama（ローカルLLM）によるSES企業スコアリング
=================================================================
Ollama APIを叩いてスコア・判定理由・営業トーク案を生成する。
3.4bモデル（gemma3:4b / llama3.2:3b）対応。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class OllamaResult:
    """Ollamaの判定結果"""
    client_score: int        = 0    # 発注側スコア（0-100）
    partner_score: int       = 0    # 供給側スコア（0-100）
    reason: str              = ""   # 判定理由
    sales_talk: str          = ""   # 営業トーク案
    error: Optional[str]     = None # エラーがあれば
    elapsed_sec: float       = 0.0  # 処理時間


def _truncate_text(text: str, max_chars: int = 1500) -> str:
    """長すぎるテキストを3.4bモデルのコンテキストに収まるよう切り詰める"""
    if len(text) <= max_chars:
        return text
    # 先頭・中間・末尾を均等に残す（重要情報を逃さないため）
    chunk = max_chars // 3
    return (
        text[:chunk]
        + "\n...(中略)...\n"
        + text[len(text)//2 - chunk//2 : len(text)//2 + chunk//2]
        + "\n...(中略)...\n"
        + text[-chunk:]
    )


def _parse_json_response(raw: str) -> dict:
    """LLMレスポンスからJSONを抽出してパース（余分なテキストを除去）"""
    # コードブロック除去
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    # JSON部分だけ抽出
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"JSONが見つかりません: {raw[:200]}")

    return json.loads(match.group())


def _clamp(val, lo: int = 0, hi: int = 100) -> int:
    """スコアを0-100に収める"""
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return 0


class OllamaScorer:
    """
    Ollamaに企業サイトテキストを投げてSESスコアを取得するクラス。

    使い方:
        scorer = OllamaScorer()
        if scorer.is_available():
            result = scorer.score("株式会社ABC", "採用強化中...システム開発を...")
    """

    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "gemma3:4b", timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.timeout  = timeout
        self._api_url = f"{self.base_url}/api/generate"

    def is_available(self) -> bool:
        """Ollamaが起動しているか確認"""
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def get_available_models(self) -> list[str]:
        """インストール済みモデル一覧を取得"""
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def score(self, company_name: str, site_text: str,
              prompt_template: str | None = None) -> OllamaResult:
        """
        企業サイトテキストをOllamaで評価してOllamaResultを返す。

        Args:
            company_name:     企業名
            site_text:        クロールで取得したサイトテキスト（結合済み）
            prompt_template:  カスタムプロンプト（Noneで ses_config のデフォルト使用）
        """
        from ses_config import SCORING_PROMPT

        start = time.monotonic()

        # テキスト切り詰め（3.4bのコンテキスト制限対応）
        truncated = _truncate_text(site_text, max_chars=1500)

        # プロンプト組み立て
        template = prompt_template or SCORING_PROMPT
        prompt = template.format(
            company_name=company_name,
            site_text=truncated,
        )

        try:
            response = httpx.post(
                self._api_url,
                json={
                    "model":  self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,     # 再現性を高めるため低め
                        "num_predict": 512,
                        "top_p": 0.9,
                    },
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw_text = response.json().get("response", "")

        except httpx.TimeoutException:
            log.warning(f"  Ollama タイムアウト: {company_name}")
            return OllamaResult(error="timeout", elapsed_sec=time.monotonic() - start)

        except httpx.HTTPStatusError as e:
            log.warning(f"  Ollama HTTPエラー ({e.response.status_code}): {company_name}")
            return OllamaResult(error=f"http_{e.response.status_code}",
                                elapsed_sec=time.monotonic() - start)

        except Exception as e:
            log.warning(f"  Ollama エラー: {company_name} → {e}")
            return OllamaResult(error=str(e), elapsed_sec=time.monotonic() - start)

        # JSON解析
        try:
            data = _parse_json_response(raw_text)
            return OllamaResult(
                client_score  = _clamp(data.get("client_score", 0)),
                partner_score = _clamp(data.get("partner_score", 0)),
                reason        = str(data.get("reason", "")).strip(),
                sales_talk    = str(data.get("sales_talk", "")).strip(),
                elapsed_sec   = time.monotonic() - start,
            )
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"  JSONパース失敗: {company_name} → {e}")
            log.debug(f"  RAW: {raw_text[:300]}")
            return OllamaResult(error=f"parse_error: {e}",
                                elapsed_sec=time.monotonic() - start)


# ── モジュールレベルのシングルトン ─────────────────────────────
def create_scorer() -> OllamaScorer:
    """ses_configの設定でOllamaScorerを生成"""
    from ses_config import OLLAMA_CFG
    return OllamaScorer(
        base_url=OLLAMA_CFG.base_url,
        model=OLLAMA_CFG.model,
        timeout=OLLAMA_CFG.timeout,
    )
