"""
config.py - システム全体の設定・定数
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

# ── パス ──────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
OUTPUT_DIR  = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── クロール設定 ───────────────────────────────────────────────
@dataclass
class CrawlConfig:
    concurrency: int        = 5        # 同時接続数（Playwright はブラウザ起動のためaiohttp より低めに設定）
    timeout: int            = 15       # タイムアウト（秒）
    delay_per_domain: float = 1.5      # 同一ドメインへの待機（秒）
    max_retries: int        = 3        # リトライ回数
    max_pages_per_site: int = 12       # 1サイト最大取得ページ数
    respect_robots: bool    = True     # robots.txt 遵守
    user_agent: str = (
        "Mozilla/5.0 (compatible; CompanyAnalyzerBot/1.0; "
        "+https://example.com/bot)"
    )

# ── スコアリング設定 ───────────────────────────────────────────
@dataclass
class ScoreConfig:
    threshold: int = 70   # 営業候補の最低スコア

    weights: dict = field(default_factory=lambda: {
        "product_presence":   40,
        "oem_presence":       20,
        "iot_keyword":        10,
        "embedded_keyword":   10,
        "vision_keyword":     10,
        "recruitment_ai":     10,
        "recruitment_eng":     5,
        "site_activity":       5,
        "contact_quality":     5,
    })

# ── 取得対象パス ───────────────────────────────────────────────
TARGET_PATHS = [
    "/", "/products", "/product", "/solution", "/solutions",
    "/technology", "/technologies", "/tech", "/oem", "/odm",
    "/partner", "/partners", "/recruit", "/careers", "/career",
    "/news", "/blog", "/press", "/ir", "/about",
]

# ── 技術キーワード辞書 ─────────────────────────────────────────
TECH_KEYWORDS: dict[str, list[str]] = {
    "edge": [
        "エッジ", "edge computing", "edge ai", "エッジai",
        "edge device", "エッジデバイス", "エッジコンピューティング",
    ],
    "iot": [
        "iot", "internet of things", "モノのインターネット",
        "connected device", "スマートデバイス", "m2m",
    ],
    "embedded": [
        "組み込み", "embedded", "マイコン", "microcontroller",
        "rtos", "firmware", "ファームウェア", "bare metal",
        "arm cortex", "fpga", "verilog", "vhdl", "risc-v",
        "embedded linux", "yocto", "buildroot",
    ],
    "sensor": [
        "センサー", "sensor", "lidar", "radar", "加速度",
        "gyro", "ジャイロ", "温度センサ", "pressure sensor",
        "proximity", "ultrasonic",
    ],
    "vision": [
        "画像処理", "image processing", "computer vision",
        "コンピュータビジョン", "opencv", "機械視覚",
        "カメラ", "camera module", "深度カメラ", "depth camera",
        "物体検出", "object detection", "顔認識",
    ],
    "ai_ml": [
        "機械学習", "machine learning", "deep learning",
        "深層学習", "neural network", "ニューラルネット",
        "tensorflow", "pytorch", "tflite", "onnx",
        "推論エンジン", "inference", "ai加速", "npu",
    ],
    "robot": [
        "ロボット", "robot", "自律", "autonomous",
        "ros", "robot os", "産業用ロボット", "協働ロボット",
        "cobot", "アーム", "manipulator",
    ],
    "industrial": [
        "産業用", "industrial", "fa", "plc", "scada",
        "製造自動化", "工場自動化", "factory automation",
        "品質検査", "外観検査", "インライン",
    ],
}

OEM_KEYWORDS = [
    "oem", "odm", "共同開発", "受託開発", "受託製造",
    "ems", "パートナー", "partner", "協業", "協力",
    "技術提携", "license", "ライセンス",
    "アライアンス", "alliance",
]

PRODUCT_SIGNALS = [
    "型番", "品番", "仕様", "spec", "specification",
    "ラインアップ", "lineup", "製品一覧", "product list",
    "カタログ", "catalog", "データシート", "datasheet",
    "モデル番号", "model number", "sku",
]

RECRUITMENT_AI_KEYWORDS = [
    "aiエンジニア", "ai engineer", "機械学習エンジニア",
    "deep learning", "データサイエンティスト", "data scientist",
    "mlops", "llm", "生成ai", "generative ai",
]

RECRUITMENT_ENG_KEYWORDS = [
    "組み込みエンジニア", "embedded engineer",
    "firmware engineer", "fpga engineer",
    "ロボットエンジニア", "vision engineer",
    "画像処理エンジニア",
]

CRAWL_CFG = CrawlConfig()
SCORE_CFG = ScoreConfig()