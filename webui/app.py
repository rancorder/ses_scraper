"""
webui/app.py - SES スクレイピング管理UI バックエンド
=====================================================
FastAPI + SSE（Server-Sent Events）でリアルタイム進捗を配信する。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import logging
import psutil
import smtplib
import email.mime.text
import email.mime.multipart
import uvicorn
from fastapi import FastAPI, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ── パス設定 ─────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

log = logging.getLogger(__name__)
app = FastAPI(title="SES Scraper UI")

# ── 状態管理 ─────────────────────────────────────────────────────
class ScraperState:
    def __init__(self):
        self.is_running      = False
        self.current_profile = ""
        self.current_step    = ""       # "scraping" / "crawling" / "scoring" / "uploading"
        self.step_label      = ""       # 表示用ラベル
        self.total_companies = 0
        self.done_companies  = 0
        self.excellent       = 0
        self.good            = 0
        self.errors          = 0
        self.start_time: float | None  = None
        self.eta_seconds: float | None = None
        self.log_lines: list[str]      = []
        self.history: list[dict]       = []
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.last_active: float = time.monotonic()

    def reset(self):
        self.is_running      = False
        self.current_profile = ""
        self.current_step    = ""
        self.step_label      = ""
        self.total_companies = 0
        self.done_companies  = 0
        self.excellent       = 0
        self.good            = 0
        self.errors          = 0
        self.start_time      = None
        self.eta_seconds     = None
        self.log_lines       = []
        self.process         = None

    def add_log(self, line: str):
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_lines.append(f"[{ts}] {line}")
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        # ログからパース
        self._parse_log(line)

    def _parse_log(self, line: str):
        """ログ行から進捗情報を抽出"""
        # 総社数
        import re
        m = re.search(r"スクリーニング開始[:：]\s*(\d+)\s*社", line)
        if m:
            self.total_companies = int(m.group(1))

        # クロール完了
        m = re.search(r"クロール完了[:：]\s*(\d+)/(\d+)", line)
        if m:
            self.done_companies  = int(m.group(1))
            self.total_companies = int(m.group(2))
            self.current_step    = "crawling"
            self.step_label      = "クロール中"
            self._calc_eta()

        # KWスキャン進捗
        m = re.search(r"KWスキャン\s+(\d+)/(\d+)社", line)
        if m:
            self.done_companies  = int(m.group(1))
            self.total_companies = int(m.group(2))
            self.current_step    = "scanning"
            self.step_label      = "キーワード分析中"
            self._calc_eta()

        # キーワードヒット
        m = re.search(r"発注ヒット[:：](\d+)", line)
        if m:
            self.excellent = int(m.group(1))
        m = re.search(r"供給ヒット[:：](\d+)", line)
        if m:
            self.good = int(m.group(1))

        # 詳細並列取得
        m = re.search(r"詳細並列取得[:：]\s*(\d+)\s*社", line)
        if m:
            self.done_companies  = int(m.group(1))
            self.current_step    = "scraping"
            self.step_label      = "企業情報収集中"

        # 一覧ページ
        if "一覧 p" in line:
            self.current_step  = "scraping"
            self.step_label    = "IPROSスクレイピング中"

        # エラー
        if "クロール失敗" in line or "エラー" in line.lower():
            m = re.search(r"クロール失敗[:：](\d+)", line)
            if m:
                self.errors = int(m.group(1))

    def _calc_eta(self):
        """残り時間を推定"""
        if not self.start_time or self.done_companies == 0:
            return
        elapsed = time.monotonic() - self.start_time
        rate    = self.done_companies / elapsed  # 社/秒
        if rate > 0 and self.total_companies > 0:
            remaining = (self.total_companies - self.done_companies) / rate
            self.eta_seconds = remaining

    def to_dict(self) -> dict:
        with self._lock:
            elapsed = int(time.monotonic() - self.start_time) if self.start_time else 0
            pct = int(self.done_companies / self.total_companies * 100) \
                  if self.total_companies > 0 else 0
            return {
                "is_running":       self.is_running,
                "current_profile":  self.current_profile,
                "step":             self.current_step,
                "step_label":       self.step_label or ("実行中" if self.is_running else "停止中"),
                "total":            self.total_companies,
                "done":             self.done_companies,
                "percent":          pct,
                "excellent":        self.excellent,
                "good":             self.good,
                "errors":           self.errors,
                "elapsed_sec":      elapsed,
                "eta_sec":          int(self.eta_seconds) if self.eta_seconds else None,
                "log_tail":         self.log_lines[-30:],
            }


# ── ジョブキュー ─────────────────────────────────────────────────
import queue as _queue

class JobQueue:
    """セッションをFIFOで管理するジョブキュー"""
    def __init__(self):
        self._queue: list[str] = []   # sid のリスト
        self._lock  = threading.Lock()

    def enqueue(self, sid: str):
        with self._lock:
            if sid not in self._queue:
                self._queue.append(sid)

    def dequeue(self, sid: str):
        with self._lock:
            if sid in self._queue:
                self._queue.remove(sid)

    def position(self, sid: str) -> int:
        """0=実行中, 1以上=待ち位置"""
        with self._lock:
            try:
                return self._queue.index(sid)
            except ValueError:
                return -1

    def is_running(self, sid: str) -> bool:
        with self._lock:
            return self._queue[0] == sid if self._queue else False

    def size(self) -> int:
        with self._lock:
            return len(self._queue)


job_queue = JobQueue()

# ── セッション管理 ───────────────────────────────────────────────
sessions: dict[str, ScraperState] = {}
sessions_lock = threading.Lock()

def get_session(sid: str) -> ScraperState:
    """セッションIDに対応するScraperStateを返す（なければ新規作成）"""
    with sessions_lock:
        if sid not in sessions:
            sessions[sid] = ScraperState()
        sessions[sid].last_active = time.monotonic()
        return sessions[sid]

def cleanup_sessions():
    """1時間以上アクセスのないセッションを削除"""
    while True:
        time.sleep(600)  # 10分ごとにチェック
        now = time.monotonic()
        with sessions_lock:
            expired = [sid for sid, st in sessions.items()
                       if now - st.last_active > 3600 and not st.is_running]
            for sid in expired:
                del sessions[sid]

threading.Thread(target=cleanup_sessions, daemon=True).start()

# 後方互換: グローバルstate（旧コード用ダミー）
state = ScraperState()

# アップロード一時保存ディレクトリ
UPLOAD_DIR = PROJECT_DIR / "webui" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = PROJECT_DIR / "company_analyzer" / "output"

# ── メール通知設定 ────────────────────────────────────────────────
SMTP_CONFIG = {
    "host":     os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "port":     int(os.environ.get("SMTP_PORT", "587")),
    "user":     os.environ.get("SMTP_USER", ""),
    "password": os.environ.get("SMTP_PASS", ""),
    "from":     os.environ.get("SMTP_FROM", ""),
}


def send_completion_email(
    to: str,
    profile: str,
    total: int,
    excellent: int,
    good: int,
    elapsed: int,
    filename: str = "",
) -> bool:
    """スクレイピング完了通知メールを送信"""
    if not to or not SMTP_CONFIG["user"]:
        return False
    try:
        msg = email.mime.multipart.MIMEMultipart()
        msg["From"]    = SMTP_CONFIG["from"] or SMTP_CONFIG["user"]
        msg["To"]      = to
        msg["Subject"] = f"[SES Scraper] {profile} スクレイピング完了"

        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒"
        body = f"""スクレイピングが完了しました。

■ プロファイル: {profile}
■ 解析企業数:   {total} 社
■ ◎（最優先）: {excellent} 社
■ ○（優先）:   {good} 社
■ 所要時間:     {elapsed_str}
■ ファイル名:   {filename}

WebUI: http://162.43.76.232:8000
"""
        msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as smtp:
            smtp.starttls()
            smtp.login(SMTP_CONFIG["user"], SMTP_CONFIG["password"])
            smtp.send_message(msg)

        print(f"[メール] 完了通知送信: {to}")
        return True
    except Exception as e:
        print(f"[メール] 送信失敗: {e}")
        return False



# ── VPS リソース取得 ──────────────────────────────────────────────

def get_system_info() -> dict:
    cpu    = psutil.cpu_percent(interval=0.5)
    mem    = psutil.virtual_memory()
    disk   = psutil.disk_usage(str(PROJECT_DIR))

    # 並列可能数の推定（メモリベース）
    # Playwright 1プロセス ≈ 150MB、クロール1並列 ≈ 30MB
    free_mb         = mem.available / 1024 / 1024
    safe_free_mb    = free_mb * 0.8          # 80%まで使う
    playwright_par  = max(1, int(safe_free_mb / 150))   # ブラウザ並列数
    crawl_par       = max(1, int(safe_free_mb / 30))    # クロール並列数

    return {
        "cpu_percent":      cpu,
        "mem_total_mb":     int(mem.total     / 1024 / 1024),
        "mem_used_mb":      int(mem.used      / 1024 / 1024),
        "mem_free_mb":      int(mem.available / 1024 / 1024),
        "mem_percent":      mem.percent,
        "disk_total_gb":    round(disk.total  / 1024**3, 1),
        "disk_used_gb":     round(disk.used   / 1024**3, 1),
        "disk_free_gb":     round(disk.free   / 1024**3, 1),
        "disk_percent":     disk.percent,
        "playwright_parallel": min(playwright_par, 5),
        "crawl_parallel":      min(crawl_par,      20),
    }


# ── 利用可能プロファイル取得 ──────────────────────────────────────

def get_profiles() -> list[dict]:
    profiles_dir = PROJECT_DIR / "config" / "profiles"
    result = []
    if profiles_dir.exists():
        try:
            import yaml
            for f in sorted(profiles_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                    axes = data.get("scoring_axes", [])
                    result.append({
                        "slug":        f.stem,
                        "name":        data.get("name", f.stem),
                        "description": data.get("description", ""),
                        "axes_count":  len(axes),
                        "keywords":    data.get("keywords_search", []),
                    })
                except Exception:
                    pass
        except ImportError:
            pass
    return result


# ── 実行履歴取得 ──────────────────────────────────────────────────

def get_history() -> list[dict]:
    output_dir = PROJECT_DIR / "company_analyzer" / "output"
    result = []
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.xlsx"), reverse=True)[:20]:
            stat = f.stat()
            result.append({
                "filename":   f.name,
                "size_kb":    round(stat.st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y/%m/%d %H:%M"),
            })
    return result


# ── スクレイピング実行 ────────────────────────────────────────────

def _run_scraper(profile: str, keywords: list[str], max_pages: int, no_ai: bool):
    """バックグラウンドスレッドで ses_run.py を実行"""
    state.is_running      = True
    state.current_profile = profile
    state.start_time      = time.monotonic()
    state.log_lines       = []

    cmd = [
        sys.executable,
        str(PROJECT_DIR / "ses_run.py"),
        "--profile", profile,
        "--max-pages", str(max_pages),
    ]
    if keywords:
        cmd += ["--keyword"] + keywords
    if no_ai:
        cmd.append("--no-ai")

    state.add_log(f"実行コマンド: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_DIR),
            bufsize=1,
        )
        state.process = proc

        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if line:
                state.add_log(line)
            if not state.is_running:
                proc.terminate()
                break

        proc.wait()
        exit_code = proc.returncode

        if exit_code == 0:
            state.add_log("✅ 完了しました")
            elapsed = int(time.monotonic() - state.start_time)
            state.history.insert(0, {
                "profile":    profile,
                "elapsed":    elapsed,
                "total":      state.total_companies,
                "excellent":  state.excellent,
                "good":       state.good,
                "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
                "status":     "完了",
            })
        else:
            state.add_log(f"❌ エラー終了 (exit: {exit_code})")
            state.history.insert(0, {
                "profile":    profile,
                "elapsed":    0,
                "total":      0,
                "excellent":  0,
                "good":       0,
                "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
                "status":     "エラー",
            })

    except Exception as e:
        state.add_log(f"❌ 例外: {e}")
    finally:
        state.is_running = False
        state.step_label = "停止中"
        state.process    = None


def _run_scraper_multi(profile: str, keywords: list[str], max_pages: int, no_ai: bool, sources: list[str]):
    """複数ソース横断スクレイピング → ses_pipeline に流す"""
    import asyncio

    state.is_running      = True
    state.current_profile = profile
    state.start_time      = time.monotonic()
    state.log_lines       = []

    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from profile_loader import load_profile
        prof     = load_profile(profile) if profile else None
        kw_list  = keywords or (prof.keywords_search if prof and prof.keywords_search else [])

        if not kw_list:
            state.add_log("❌ 検索キーワードがありません")
            return

        all_companies = []
        seen_keys: set[str] = set()

        def _merge(companies, source_name):
            added = 0
            for c in companies:
                d = c.to_dict() if hasattr(c, "to_dict") else c
                url  = str(d.get("公式サイト", "") or "").strip().rstrip("/")
                name = str(d.get("会社名", "") or "").strip()
                key  = url if url.startswith("http") else name
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_companies.append(d)
                    added += 1
            state.add_log(f"  [{source_name}] +{added}社（累計: {len(all_companies)}）")

        # ── IPROS ──
        if "ipros" in sources:
            state.step_label = "IPROS スクレイピング中"
            state.add_log(f"[IPROS] 開始: {kw_list} / {max_pages}ページ")
            try:
                from ses_run import run_ipros_scrape
                import threading
                stop_ev = threading.Event()
                raw = run_ipros_scrape(kw_list, max_pages, 5, stop_ev)
                _merge(raw, "IPROS")
            except Exception as e:
                state.add_log(f"❌ IPROS エラー: {e}")

        # ── Metoree ──
        if "metoree" in sources:
            state.step_label = "Metoree スクレイピング中"
            state.add_log(f"[Metoree] 開始: {kw_list} / {max_pages}ページ")
            try:
                from scraper.metoree import scrape_metoree_async
                raw = asyncio.run(scrape_metoree_async(kw_list, max_pages=max_pages))
                _merge(raw, "Metoree")
            except Exception as e:
                state.add_log(f"❌ Metoree エラー: {e}")

        # ── アペルザ ──
        if "aperza" in sources:
            state.step_label = "アペルザ スクレイピング中"
            state.add_log(f"[アペルザ] 開始: {kw_list} / {max_pages}ページ")
            try:
                from scraper.aperza import scrape_aperza_async
                raw = asyncio.run(scrape_aperza_async(kw_list, max_pages=max_pages))
                _merge(raw, "アペルザ")
            except Exception as e:
                state.add_log(f"❌ アペルザ エラー: {e}")

        state.add_log(f"収集完了: {len(all_companies)} 社")
        state.total_companies = len(all_companies)

        if not all_companies:
            state.add_log("❌ 企業が1社も収集できませんでした")
            return

        # ses_run の to_ses_input 相当（住所・電話引き継ぎ）
        ses_input = []
        seen2: set[str] = set()
        for d in all_companies:
            url  = str(d.get("公式サイト", "") or "").strip().rstrip("/")
            if not url or not url.startswith("http"):
                url = str(d.get("詳細URL", "") or "").strip().rstrip("/")
            if not url or not url.startswith("http"):
                continue
            if url in seen2:
                continue
            seen2.add(url)
            ses_input.append({
                "name":    str(d.get("会社名", url)).strip(),
                "url":     url,
                "住所":    str(d.get("住所", "") or "").strip(),
                "電話":    str(d.get("電話", "") or "").strip(),
                "keyword": str(d.get("検索キーワード", "") or "").strip(),
                "source":  str(d.get("ソース", "") or "").strip(),
            })

        state.add_log(f"公式URL保有: {len(ses_input)} 社 → スクリーニング開始")
        state.step_label = "スクリーニング中"

        from ses_pipeline import run_ses_pipeline
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        src_tag = "+".join(sources)
        results = asyncio.run(run_ses_pipeline(
            companies     = ses_input,
            output_prefix = f"{profile}_{src_tag}_{ts}",
            concurrency   = 2,
            use_ollama    = not no_ai,
            profile       = prof,
        ))

        excellent = sum(1 for r in results if r.judgment == "◎")
        good      = sum(1 for r in results if r.judgment == "○")
        elapsed   = int(time.monotonic() - state.start_time)

        state.add_log(f"✅ 完了: ◎{excellent}社 ○{good}社")
        state.excellent = excellent
        state.good      = good
        state.history.insert(0, {
            "profile":     f"{profile}（{src_tag}）",
            "elapsed":     elapsed,
            "total":       len(results),
            "excellent":   excellent,
            "good":        good,
            "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "status":      "完了",
        })

    except Exception as e:
        import traceback
        state.add_log(f"❌ エラー: {e}")
        state.add_log(traceback.format_exc()[:300])
    finally:
        state.is_running = False
        state.step_label = "停止中"


# ── API エンドポイント ────────────────────────────────────────────

@app.get("/api/status")
async def get_status(sid: str = "default"):
    data = get_session(sid).to_dict()
    data["queue_position"] = job_queue.position(sid)
    data["queue_size"]     = job_queue.size()
    return JSONResponse(data)


@app.get("/api/system")
async def get_system():
    return JSONResponse(get_system_info())


@app.get("/api/profiles")
async def api_profiles():
    return JSONResponse(get_profiles())


@app.get("/api/profiles/{slug}")
async def api_profile_get(slug: str):
    """プロファイルの全データを返す（編集フォーム用）"""
    try:
        import yaml
    except ImportError:
        return JSONResponse({"error": "pyyaml未インストール"}, status_code=500)

    path = PROJECT_DIR / "config" / "profiles" / f"{slug}.yaml"
    if not path.exists():
        return JSONResponse({"error": "見つかりません"}, status_code=404)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["slug"] = slug
    return JSONResponse(data)


@app.post("/api/profiles")
async def api_profile_create(body: dict):
    """新規プロファイルを作成してYAMLに保存"""
    try:
        import yaml
    except ImportError:
        return JSONResponse({"error": "pyyaml未インストール"}, status_code=500)

    slug = body.get("slug", "").strip().replace(" ", "_").replace("/", "_")
    if not slug:
        return JSONResponse({"error": "slugが必要です"}, status_code=400)

    # 英数字・アンダースコアのみ許可
    import re
    if not re.match(r'^[a-zA-Z0-9_\-]+$', slug):
        return JSONResponse({"error": "slugは英数字・_・-のみ使用可能です"}, status_code=400)

    profiles_dir = PROJECT_DIR / "config" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{slug}.yaml"

    if path.exists():
        return JSONResponse({"error": f"'{slug}' はすでに存在します"}, status_code=409)

    _save_profile_yaml(path, body)
    return JSONResponse({"status": "created", "slug": slug})


@app.put("/api/profiles/{slug}")
async def api_profile_update(slug: str, body: dict):
    """既存プロファイルを上書き保存"""
    try:
        import yaml
    except ImportError:
        return JSONResponse({"error": "pyyaml未インストール"}, status_code=500)

    path = PROJECT_DIR / "config" / "profiles" / f"{slug}.yaml"
    if not path.exists():
        return JSONResponse({"error": "見つかりません"}, status_code=404)

    # バックアップ
    backup = path.with_suffix(".yaml.bak")
    import shutil
    shutil.copy2(path, backup)

    _save_profile_yaml(path, body)
    return JSONResponse({"status": "updated", "slug": slug})


def _save_profile_yaml(path: Path, body: dict):
    """フォームデータをYAMLに整形して保存"""
    import yaml

    # 評価軸を整形
    axes = []
    for ax in body.get("scoring_axes", []):
        kws = ax.get("keywords", [])
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.replace("、", ",").split(",") if k.strip()]
        axes.append({
            "id":          ax.get("id") or ax.get("name", "").lower().replace(" ", "_"),
            "name":        ax.get("name", ""),
            "points":      int(ax.get("points", 10)),
            "description": ax.get("description", ""),
            "detection":   ax.get("detection", "keyword_any"),
            "keywords":    kws,
        })

    # 検索KWを整形
    search_kws = body.get("keywords_search", [])
    if isinstance(search_kws, str):
        search_kws = [k.strip() for k in search_kws.replace("、", ",").split(",") if k.strip()]

    data = {
        "name":           body.get("name", ""),
        "description":    body.get("description", ""),
        "keywords_search": search_kws,
        "scoring_axes":   axes,
        "score_cap":      100,
        "score_sum_max":  sum(ax["points"] for ax in axes) or 100,
        "thresholds": {
            "excellent": int(body.get("threshold_excellent", 60)),
            "good":      int(body.get("threshold_good",      40)),
        },
        "ai_instruction": body.get("ai_instruction", ""),
    }

    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )



# ── SMTP設定保存・取得 ───────────────────────────────────────────
SMTP_CONFIG_FILE = PROJECT_DIR / "config" / "smtp.json"

@app.get("/api/smtp-config")
async def get_smtp_config():
    if SMTP_CONFIG_FILE.exists():
        import json as _json
        data = _json.loads(SMTP_CONFIG_FILE.read_text())
        data.pop("password", None)  # パスワードは返さない
        return JSONResponse(data)
    return JSONResponse({})

@app.post("/api/smtp-config")
async def save_smtp_config(body: dict):
    SMTP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    # 既存設定とマージ（パスワードが空の場合は上書きしない）
    existing = {}
    if SMTP_CONFIG_FILE.exists():
        existing = _json.loads(SMTP_CONFIG_FILE.read_text())
    for k, v in body.items():
        if k == "password" and not v:
            continue
        existing[k] = v
    SMTP_CONFIG_FILE.write_text(_json.dumps(existing, ensure_ascii=False))
    # グローバルSMTP_CONFIGも更新
    for k in ["host", "port", "user", "from"]:
        if k in existing:
            SMTP_CONFIG[k] = existing[k]
    if "password" in existing:
        SMTP_CONFIG["password"] = existing["password"]
    return JSONResponse({"status": "saved"})

@app.post("/api/smtp-test")
async def test_smtp(body: dict):
    to = body.get("to", "")
    if not to:
        return JSONResponse({"error": "送信先が必要です"}, status_code=400)
    ok = send_completion_email(to, "テスト", 10, 3, 5, 120, "test.xlsx")
    return JSONResponse({"status": "ok" if ok else "error"})


# ── Excelダウンロード ─────────────────────────────────────────────
@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """出力Excelファイルをダウンロード"""
    # パストラバーサル対策
    safe_name = Path(filename).name
    path = OUTPUT_DIR / safe_name
    if not path.exists() or path.suffix not in (".xlsx", ".xls"):
        return JSONResponse({"error": "ファイルが見つかりません"}, status_code=404)
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=safe_name,
    )


# ── Excelアップロード → 列プレビュー ─────────────────────────────
@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    """Excelをアップロードし、列情報とURLプレビューを返す"""
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        return JSONResponse({"error": "Excel/CSVファイルのみ対応"}, status_code=400)

    save_path = UPLOAD_DIR / f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    content = await file.read()
    save_path.write_bytes(content)

    try:
        import pandas as pd
        if file.filename.endswith(".csv"):
            for enc in ["utf-8", "cp932", "shift_jis"]:
                try:
                    df = pd.read_csv(save_path, dtype=str, encoding=enc).fillna("")
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            else:
                df = pd.read_csv(save_path, dtype=str, encoding="utf-8", errors="replace").fillna("")
        else:
            df = pd.read_excel(save_path, dtype=str).fillna("")

        cols = list(df.columns)
        total_rows = len(df)

        # URL列を自動検出（httpで始まる値が多い列）
        url_col_guess = None
        name_col_guess = None
        url_candidates = []

        # URL列キーワード（ヘッダー名でURL列を優先判定）
        URL_KEYWORDS  = ["url", "URL", "サイト", "ホームページ", "HP", "リンク", "website", "web"]
        NAME_KEYWORDS = ["企業名", "会社名", "社名", "法人名", "company", "name", "会社", "企業"]

        for col in cols:
            vals = df[col].dropna().astype(str)
            col_lower = col.lower()

            # URL列：ヘッダー名マッチを最優先
            if any(kw.lower() in col_lower for kw in [u.lower() for u in URL_KEYWORDS]):
                http_count = vals.str.startswith("http").sum()
                url_candidates.append({"col": col, "http_count": int(http_count) + 1000})  # ヘッダー名マッチに加点
                continue

            # URL列：内容でhttpが多い列
            http_count = vals.str.startswith("http").sum()
            if http_count > 0:
                url_candidates.append({"col": col, "http_count": int(http_count)})

            # 会社名列：ヘッダー名マッチを最優先
            if any(kw.lower() in col_lower for kw in [n.lower() for n in NAME_KEYWORDS]):
                name_col_guess = col
                continue

            # 会社名列：内容から推定（株式会社・有限会社が多い列）
            if name_col_guess is None:
                corp_count = vals.str.contains("株式会社|有限会社|合同会社|㈱|㈲", regex=True).sum()
                if corp_count >= max(3, len(df) * 0.1):
                    name_col_guess = col

        url_candidates.sort(key=lambda x: -x["http_count"])
        if url_candidates:
            url_col_guess = url_candidates[0]["col"]

        # プレビュー（最初の5件のURL）
        preview_urls = []
        if url_col_guess:
            preview_urls = df[url_col_guess].dropna().str.strip()
            preview_urls = [u for u in preview_urls if u.startswith("http")][:5]

        return JSONResponse({
            "saved_path": str(save_path),
            "columns":    cols,
            "total_rows": total_rows,
            "url_col_guess":  url_col_guess,
            "name_col_guess": name_col_guess,
            "url_candidates": url_candidates[:5],
            "preview_urls":   preview_urls,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── クレンジング実行（Excelアップロード版） ───────────────────────
@app.post("/api/start-cleanse")
async def start_cleanse(body: dict, background_tasks: BackgroundTasks):
    """アップロードしたExcelのURLをクロールして評価"""
    sid = body.get("sid", "default")
    st  = get_session(sid)
    if st.is_running:
        return JSONResponse({"error": "実行中です"}, status_code=400)

    saved_path = body.get("saved_path", "")
    url_col    = body.get("url_col", "")
    name_col   = body.get("name_col", "")
    profile    = body.get("profile", "ses")

    if not saved_path or not url_col:
        return JSONResponse({"error": "ファイルパスとURL列が必要です"}, status_code=400)

    st.reset()
    background_tasks.add_task(_run_cleanse_session, sid, saved_path, url_col, name_col, profile)
    return JSONResponse({"status": "started"})


def _run_cleanse(saved_path: str, url_col: str, name_col: str, profile: str):
    """クレンジング（クロール→キーワードスコアリング）をバックグラウンドで実行"""
    import pandas as pd

    state.is_running      = True
    state.current_profile = profile
    state.start_time      = time.monotonic()
    state.log_lines       = []
    state.step_label      = "クレンジング準備中"

    try:
        # ── ファイル読み込み（エンコーディング自動判別）──────────────
        path = Path(saved_path)
        if path.suffix.lower() == ".csv":
            for enc in ["utf-8", "cp932", "shift_jis"]:
                try:
                    df = pd.read_csv(path, dtype=str, encoding=enc).fillna("")
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            else:
                df = pd.read_csv(path, dtype=str, encoding="utf-8", errors="replace").fillna("")
        else:
            df = pd.read_excel(path, dtype=str).fillna("")

        # ── 列名の自動検出 ────────────────────────────────────────
        def _find_col(candidates):
            for col in df.columns:
                if any(k in col for k in candidates):
                    return col
            return None

        addr_col  = _find_col(["住所", "所在地", "address", "Address"])
        phone_col = _find_col(["電話", "TEL", "tel", "Phone", "phone", "電話番号"])
        sales_col = _find_col(["売上", "売上高", "Sales", "sales", "売上区分"])
        src_col   = _find_col(["ソース", "source", "Source", "掲載サイト"])

        state.add_log(f"列検出: URL={url_col} / 名前={name_col or 'なし'} / "
                      f"住所={addr_col or 'なし'} / 電話={phone_col or 'なし'} / "
                      f"売上={sales_col or 'なし'}")

        # ── URL有効行のみ抽出（インデックス保持）────────────────────
        if url_col not in df.columns:
            state.add_log(f"❌ URL列 '{url_col}' が見つかりません")
            return

        df["_url_clean"] = df[url_col].str.strip()
        df_valid = df[df["_url_clean"].str.startswith("http", na=False)].copy().reset_index(drop=True)

        companies = []
        seen = set()
        for _, row in df_valid.iterrows():
            url = row["_url_clean"].rstrip("/")
            if not url or url in seen:
                continue
            seen.add(url)
            name = str(row.get(name_col, "") or "").strip() if name_col and name_col in df.columns else ""
            companies.append({
                "name":    name or url,
                "url":     url,
                "住所":    str(row.get(addr_col,  "") or "").strip() if addr_col  else "",
                "電話":    str(row.get(phone_col, "") or "").strip() if phone_col else "",
                "keyword": str(row.get(sales_col, "") or "").strip() if sales_col else "",
                "source":  str(row.get(src_col,   "") or "").strip() if src_col   else "CSVアップロード",
            })

        state.add_log(f"読み込み完了: {len(companies)} 社（URL有効）")
        state.total_companies = len(companies)

        if not companies:
            state.add_log("❌ 有効なURLが見つかりません")
            return

        # ses_pipeline を直接呼び出し（IPROSスキップ）
        from ses_pipeline import run_ses_pipeline
        from profile_loader import load_profile

        prof = load_profile(profile) if profile else None
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_prefix = f"cleanse_{profile}_{ts}"

        import asyncio
        results = asyncio.run(run_ses_pipeline(
            companies     = companies,
            output_prefix = output_prefix,
            concurrency   = 2,
            use_ollama    = False,  # キーワード判定のみ
            source_path   = saved_path,
            url_col       = url_col,
            profile       = prof,
        ))

        excellent = sum(1 for r in results if r.judgment == "◎")
        good      = sum(1 for r in results if r.judgment == "○")
        elapsed   = int(time.monotonic() - state.start_time)

        state.add_log(f"✅ クレンジング完了: ◎{excellent}社 ○{good}社")
        state.excellent = excellent
        state.good      = good
        state.history.insert(0, {
            "profile":     f"{profile}（クレンジング）",
            "elapsed":     elapsed,
            "total":       len(results),
            "excellent":   excellent,
            "good":        good,
            "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "status":      "完了",
        })

    except Exception as e:
        import traceback
        state.add_log(f"❌ エラー: {e}")
        state.add_log(traceback.format_exc()[:300])
    finally:
        state.is_running = False
        state.step_label = "停止中"


# ── Google検索実行 ────────────────────────────────────────────────
@app.post("/api/start-google")
async def start_google(body: dict, background_tasks: BackgroundTasks):
    """Google検索で企業URLを収集してスクリーニング"""
    if state.is_running:
        return JSONResponse({"error": "実行中です"}, status_code=400)

    profile   = body.get("profile", "ses")
    queries   = body.get("queries", [])
    max_pages = int(body.get("max_pages", 3))
    suffix    = body.get("suffix", "会社 企業")

    if not queries:
        return JSONResponse({"error": "検索クエリが必要です"}, status_code=400)

    state.reset()
    background_tasks.add_task(_run_google_scrape, profile, queries, max_pages, suffix)
    return JSONResponse({"status": "started"})


def _run_google_scrape(profile: str, queries: list, max_pages: int, suffix: str):
    """Google検索 → クロール → スコアリング"""
    state.is_running      = True
    state.current_profile = profile
    state.start_time      = time.monotonic()
    state.log_lines       = []
    state.step_label      = "Google検索中"

    try:
        import asyncio
        sys.path.insert(0, str(PROJECT_DIR))
        from scraper.google_search import search_google_async

        state.add_log(f"[Google] 検索開始: {queries}")
        companies = asyncio.run(search_google_async(
            queries   = queries,
            max_pages = max_pages,
            suffix    = suffix,
        ))

        state.add_log(f"[Google] 収集完了: {len(companies)} 社")

        if not companies:
            state.add_log("❌ 企業が1社も収集できませんでした")
            return

        # ses_pipeline に流す
        from ses_pipeline import run_ses_pipeline
        from profile_loader import load_profile

        prof = load_profile(profile) if profile else None
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")

        results = asyncio.run(run_ses_pipeline(
            companies     = companies,
            output_prefix = f"google_{profile}_{ts}",
            concurrency   = 2,
            use_ollama    = False,
            profile       = prof,
        ))

        excellent = sum(1 for r in results if r.judgment == "◎")
        good      = sum(1 for r in results if r.judgment == "○")
        elapsed   = int(time.monotonic() - state.start_time)

        state.add_log(f"✅ 完了: ◎{excellent}社 ○{good}社")
        state.excellent = excellent
        state.good      = good
        state.history.insert(0, {
            "profile":     f"{profile}（Google）",
            "elapsed":     elapsed,
            "total":       len(results),
            "excellent":   excellent,
            "good":        good,
            "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "status":      "完了",
        })

    except Exception as e:
        import traceback
        state.add_log(f"❌ エラー: {e}")
        state.add_log(traceback.format_exc()[:300])
    finally:
        state.is_running = False
        state.step_label = "停止中"


@app.get("/api/history")
async def api_history(sid: str = "default"):
    files = get_history()
    st = get_session(sid)
    return JSONResponse({"files": files, "runs": st.history[:20]})


@app.post("/api/start")
async def start_scraping(body: dict, background_tasks: BackgroundTasks):
    sid = body.get("sid", "default")
    st  = get_session(sid)
    if st.is_running:
        return JSONResponse({"error": "実行中です"}, status_code=400)

    profile   = body.get("profile", "ses")
    keywords  = body.get("keywords", [])
    max_pages = int(body.get("max_pages", 5))
    no_ai     = bool(body.get("no_ai", True))
    sources      = body.get("sources", ["ipros"])
    notify_email = body.get("notify_email", "")

    st.reset()
    job_queue.enqueue(sid)
    background_tasks.add_task(_queued_run, sid, profile, keywords, max_pages, no_ai, sources, notify_email)
    pos = job_queue.position(sid)
    return JSONResponse({"status": "queued" if pos > 0 else "started", "queue_position": pos})


@app.post("/api/stop")
async def stop_scraping(body: dict = {}):
    sid = body.get("sid", "default") if body else "default"
    st  = get_session(sid)
    if not st.is_running:
        return JSONResponse({"error": "実行していません"}, status_code=400)
    st.is_running = False
    if st.process:
        st.process.terminate()
    st.add_log("⛔ 停止要求を受け付けました")
    return JSONResponse({"status": "stopping"})


@app.get("/api/events")
async def sse_events(sid: str = "default"):
    """Server-Sent Events でリアルタイム進捗を配信（セッション別）"""
    async def generate() -> AsyncGenerator[str, None]:
        last_log_count = 0
        while True:
            st = get_session(sid)
            data = st.to_dict()
            new_logs = st.log_lines[last_log_count:]
            last_log_count = len(st.log_lines)
            data["new_logs"]       = new_logs
            data["queue_position"] = job_queue.position(sid)
            data["queue_size"]     = job_queue.size()
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── セッション対応ラッパー関数 ───────────────────────────────────

def _queued_run(sid: str, profile: str, keywords: list, max_pages: int, no_ai: bool, sources: list, notify_email: str = ''):
    """キュー待ちしてから実行する"""
    st = get_session(sid)
    # 自分の前のジョブが終わるまで待つ
    while True:
        pos = job_queue.position(sid)
        if pos == 0:
            break
        if pos < 0:
            return  # キューから消えた（停止された）
        st.step_label = f"待機中（{pos}件が先に実行中）"
        st.add_log(f"⏳ キュー待ち: {pos}件が先に実行中です...")
        time.sleep(10)
    try:
        _run_scraper_multi_session(sid, profile, keywords, max_pages, no_ai, sources, notify_email)
    finally:
        job_queue.dequeue(sid)


def _run_scraper_multi_session(sid: str, profile: str, keywords: list, max_pages: int, no_ai: bool, sources: list, notify_email: str = ''):
    """セッション別スクレイピング実行"""
    st = get_session(sid)
    import asyncio
    import logging as _logging

    # logging → st.add_log() に橋渡し
    class _SessionHandler(_logging.Handler):
        def emit(self, record):
            try:
                st.add_log(self.format(record))
            except Exception:
                pass
    _handler = _SessionHandler()
    _handler.setFormatter(_logging.Formatter("%(message)s"))
    _root = _logging.getLogger()
    _prev_level = _root.level
    _root.setLevel(_logging.INFO)
    _root.addHandler(_handler)

    st.is_running      = True
    st.current_profile = profile
    st.start_time      = time.monotonic()
    st.log_lines       = []

    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from profile_loader import load_profile
        prof    = load_profile(profile) if profile else None
        kw_list = keywords or (prof.keywords_search if prof and prof.keywords_search else [])

        if not kw_list:
            st.add_log("❌ 検索キーワードがありません")
            return

        all_companies = []
        seen_keys: set[str] = set()

        def _merge(companies, source_name):
            added = 0
            for c in companies:
                d = c.to_dict() if hasattr(c, "to_dict") else c
                url  = str(d.get("公式サイト", "") or "").strip().rstrip("/")
                name = str(d.get("会社名", "") or "").strip()
                key  = url if url.startswith("http") else name
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_companies.append(d)
                    added += 1
            st.add_log(f"  [{source_name}] +{added}社（累計: {len(all_companies)}）")

        if "ipros" in sources:
            st.step_label = "IPROS スクレイピング中"
            st.add_log(f"[IPROS] 開始: {kw_list} / {max_pages}ページ")
            try:
                from ses_run import run_ipros_scrape
                import threading as _th
                stop_ev = _th.Event()
                raw = run_ipros_scrape(kw_list, max_pages, 5, stop_ev)
                _merge(raw, "IPROS")
            except Exception as e:
                st.add_log(f"❌ IPROS エラー: {e}")

        if "metoree" in sources:
            st.step_label = "Metoree スクレイピング中"
            st.add_log(f"[Metoree] 開始: {kw_list} / {max_pages}ページ")
            try:
                from scraper.metoree import scrape_metoree_async
                raw = asyncio.run(scrape_metoree_async(kw_list, max_pages=max_pages))
                _merge(raw, "Metoree")
            except Exception as e:
                st.add_log(f"❌ Metoree エラー: {e}")

        if "aperza" in sources:
            st.step_label = "アペルザ スクレイピング中"
            st.add_log(f"[アペルザ] 開始: {kw_list} / {max_pages}ページ")
            try:
                from scraper.aperza import scrape_aperza_async
                raw = asyncio.run(scrape_aperza_async(kw_list, max_pages=max_pages))
                _merge(raw, "アペルザ")
            except Exception as e:
                st.add_log(f"❌ アペルザ エラー: {e}")

        st.add_log(f"収集完了: {len(all_companies)} 社")
        st.total_companies = len(all_companies)

        if not all_companies:
            st.add_log("❌ 企業が1社も収集できませんでした")
            return

        ses_input = []
        seen2: set[str] = set()
        for d in all_companies:
            url = str(d.get("公式サイト", "") or "").strip().rstrip("/")
            if not url or not url.startswith("http"):
                url = str(d.get("詳細URL", "") or "").strip().rstrip("/")
            if not url or not url.startswith("http"):
                continue
            if url in seen2:
                continue
            seen2.add(url)
            ses_input.append({
                "name":    str(d.get("会社名", url)).strip(),
                "url":     url,
                "住所":    str(d.get("住所", "") or "").strip(),
                "電話":    str(d.get("電話", "") or "").strip(),
                "keyword": str(d.get("検索キーワード", "") or "").strip(),
                "source":  str(d.get("ソース", "") or "").strip(),
            })

        st.add_log(f"公式URL保有: {len(ses_input)} 社 → スクリーニング開始")
        st.step_label = "スクリーニング中"

        from ses_pipeline import run_ses_pipeline
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        src_tag = "+".join(sources)
        results = asyncio.run(run_ses_pipeline(
            companies     = ses_input,
            output_prefix = f"{profile}_{src_tag}_{ts}",
            concurrency   = 2,
            use_ollama    = not no_ai,
            profile       = prof,
        ))

        excellent = sum(1 for r in results if r.judgment == "◎")
        good      = sum(1 for r in results if r.judgment == "○")
        elapsed   = int(time.monotonic() - st.start_time)

        fname = f"{profile}_{src_tag}_{ts}.xlsx"
        st.add_log(f"✅ 完了: ◎{excellent}社 ○{good}社")
        st.excellent = excellent
        st.good      = good
        st.history.insert(0, {
            "profile":     f"{profile}（{src_tag}）",
            "elapsed":     elapsed,
            "total":       len(results),
            "excellent":   excellent,
            "good":        good,
            "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "status":      "完了",
        })

        # メール通知
        if notify_email:
            ok = send_completion_email(notify_email, profile, len(results), excellent, good, elapsed, fname)
            st.add_log(f"📧 メール通知: {'送信完了' if ok else '送信失敗'} → {notify_email}")

    except Exception as e:
        import traceback
        st.add_log(f"❌ エラー: {e}")
        st.add_log(traceback.format_exc()[:300])
    finally:
        _root.removeHandler(_handler)
        _root.setLevel(_prev_level)
        st.is_running = False
        st.step_label = "停止中"


def _run_cleanse_session(sid: str, saved_path: str, url_col: str, name_col: str, profile: str):
    """セッション別クレンジング実行"""
    import asyncio, pandas as pd
    import logging as _logging
    st = get_session(sid)

    class _SessionHandler2(_logging.Handler):
        def emit(self, record):
            try:
                st.add_log(self.format(record))
            except Exception:
                pass
    _handler2 = _SessionHandler2()
    _handler2.setFormatter(_logging.Formatter("%(message)s"))
    _root2 = _logging.getLogger()
    _prev_level2 = _root2.level
    _root2.setLevel(_logging.INFO)
    _root2.addHandler(_handler2)

    st.is_running      = True
    st.current_profile = profile
    st.start_time      = time.monotonic()
    st.log_lines       = []
    st.step_label      = "クレンジング準備中"

    try:
        path = Path(saved_path)
        if path.suffix.lower() == ".csv":
            for enc in ["utf-8", "cp932", "shift_jis"]:
                try:
                    df = pd.read_csv(path, dtype=str, encoding=enc).fillna("")
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            else:
                df = pd.read_csv(path, dtype=str, encoding="utf-8", errors="replace").fillna("")
        else:
            df = pd.read_excel(path, dtype=str).fillna("")

        def _find_col(candidates):
            for col in df.columns:
                if any(k in col for k in candidates):
                    return col
            return None

        addr_col  = _find_col(["住所", "所在地", "address", "Address"])
        phone_col = _find_col(["電話", "TEL", "tel", "Phone", "phone", "電話番号"])
        sales_col = _find_col(["売上", "売上高", "Sales", "sales", "売上区分"])
        src_col   = _find_col(["ソース", "source", "Source", "掲載サイト"])

        st.add_log(f"列検出: URL={url_col} / 名前={name_col or 'なし'} / "
                   f"住所={addr_col or 'なし'} / 電話={phone_col or 'なし'} / "
                   f"売上={sales_col or 'なし'}")

        if url_col not in df.columns:
            st.add_log(f"❌ URL列 '{url_col}' が見つかりません")
            return

        df["_url_clean"] = df[url_col].str.strip()
        df_valid = df[df["_url_clean"].str.startswith("http", na=False)].copy().reset_index(drop=True)

        companies = []
        seen = set()
        for _, row in df_valid.iterrows():
            url = row["_url_clean"].rstrip("/")
            if not url or url in seen:
                continue
            seen.add(url)
            name = str(row.get(name_col, "") or "").strip() if name_col and name_col in df.columns else ""
            companies.append({
                "name":    name or url,
                "url":     url,
                "住所":    str(row.get(addr_col,  "") or "").strip() if addr_col  else "",
                "電話":    str(row.get(phone_col, "") or "").strip() if phone_col else "",
                "keyword": str(row.get(sales_col, "") or "").strip() if sales_col else "",
                "source":  str(row.get(src_col,   "") or "").strip() if src_col   else "CSVアップロード",
            })

        st.add_log(f"読み込み完了: {len(companies)} 社（URL有効）")
        st.total_companies = len(companies)

        if not companies:
            st.add_log("❌ 有効なURLが見つかりません")
            return

        from ses_pipeline import run_ses_pipeline
        from profile_loader import load_profile
        prof = load_profile(profile) if profile else None
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")

        results = asyncio.run(run_ses_pipeline(
            companies     = companies,
            output_prefix = f"cleanse_{profile}_{ts}",
            concurrency   = 2,
            use_ollama    = False,
            profile       = prof,
            source_path   = saved_path,
            url_col       = url_col,
        ))

        excellent = sum(1 for r in results if r.judgment == "◎")
        good      = sum(1 for r in results if r.judgment == "○")
        elapsed   = int(time.monotonic() - st.start_time)

        st.add_log(f"✅ クレンジング完了: ◎{excellent}社 ○{good}社")
        st.excellent = excellent
        st.good      = good
        st.history.insert(0, {
            "profile":     f"{profile}（クレンジング）",
            "elapsed":     elapsed,
            "total":       len(results),
            "excellent":   excellent,
            "good":        good,
            "finished_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
            "status":      "完了",
        })

    except Exception as e:
        import traceback
        st.add_log(f"❌ エラー: {e}")
        st.add_log(traceback.format_exc()[:300])
    finally:
        _root2.removeHandler(_handler2)
        _root2.setLevel(_prev_level2)
        st.is_running = False
        st.step_label = "停止中"


# ── メインHTMLを返す ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html が見つかりません</h1>")


# ── 起動 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"起動中: http://0.0.0.0:{port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)