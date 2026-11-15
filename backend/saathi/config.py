"""Settings loaded from the environment (.env at the repo root). Never log values from here."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _s(key: str, default: str = "") -> str:
	return os.environ.get(key, default).strip()


def _i(key: str, default: int) -> int:
	try:
		return int(_s(key, str(default)))
	except ValueError:
		return default


def _f(key: str, default: float) -> float:
	try:
		return float(_s(key, str(default)))
	except ValueError:
		return default


class Settings:
	def __init__(self) -> None:
		self.APP_ENV = _s("APP_ENV", "dev")
		self.BACKEND_PORT = _i("BACKEND_PORT", 8000)
		self.FRONTEND_PORT = _i("FRONTEND_PORT", 5173)
		self.DATABASE_URL = _s("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'saathi.db'}")
		self.APP_SECRET = _s("APP_SECRET", "dev-secret")
		self.CORS_ORIGINS = [o for o in _s("CORS_ORIGINS", "*").split(",") if o]

		self.SARVAM_API_KEY = _s("SARVAM_API_KEY")
		self.SARVAM_BASE_URL = _s("SARVAM_BASE_URL", "https://api.sarvam.ai")
		self.SARVAM_LLM_MODEL = _s("SARVAM_LLM_MODEL", "sarvam-105b")
		self.SARVAM_STT_MODEL = _s("SARVAM_STT_MODEL", "saaras:v3")
		self.SARVAM_TTS_MODEL = _s("SARVAM_TTS_MODEL", "bulbul:v3")
		self.SARVAM_TTS_SPEAKER = _s("SARVAM_TTS_SPEAKER", "")
		self.SARVAM_DEFAULT_LANGUAGE = _s("SARVAM_DEFAULT_LANGUAGE", "hi-IN")
		self.SARVAM_AUDIO_CACHE_DIR = _s("SARVAM_AUDIO_CACHE_DIR", str(ROOT / "data" / "audio"))

		self.LLM_FALLBACK_CHAIN = [
			x.strip() for x in _s("LLM_FALLBACK_CHAIN", "sarvam,mock").split(",") if x.strip()
		]
		self.OPENAI_API_KEY = _s("OPENAI_API_KEY")
		self.OPENAI_BASE_URL = _s("OPENAI_BASE_URL", "https://api.openai.com/v1")
		self.OPENAI_MODEL = _s("OPENAI_MODEL")
		self.GEMINI_API_KEY = _s("GEMINI_API_KEY")
		self.GEMINI_MODEL = _s("GEMINI_MODEL")
		self.ANTHROPIC_API_KEY = _s("ANTHROPIC_API_KEY")
		self.ANTHROPIC_MODEL = _s("ANTHROPIC_MODEL")

		self.COGNEE_LLM_API_KEY = _s("COGNEE_LLM_API_KEY")
		self.COGNEE_LLM_MODEL = _s("COGNEE_LLM_MODEL")
		self.COGNEE_EMBEDDING_MODEL = _s("COGNEE_EMBEDDING_MODEL")
		self.COGNEE_DATA_DIR = _s("COGNEE_DATA_DIR", str(ROOT / "data" / "cognee"))
		self.COGNEE_DATASET_MEMORY = _s("COGNEE_DATASET_MEMORY", "saathi_memory")
		self.COGNEE_DATASET_SOPS = _s("COGNEE_DATASET_SOPS", "saathi_sops")

		# PAYTM_* names are accepted as well as PAYTM_PG_*.
		self.PAYTM_PG_MID = _s("PAYTM_PG_MID") or _s("PAYTM_MID")
		self.PAYTM_PG_KEY = _s("PAYTM_PG_KEY") or _s("PAYTM_MERCHANT_KEY")
		self.PAYTM_WEBSITE = _s("PAYTM_WEBSITE", "WEBSTAGING")
		self.PAYTM_PG_BASE_URL = _s("PAYTM_PG_BASE_URL", "https://securegw-stage.paytm.in")

		# A service only runs live when its mode says so AND its key is present.
		self.SARVAM_MODE = self._mode("SARVAM_MODE", self.SARVAM_API_KEY)
		self.LLM_MODE = self._mode("LLM_MODE", self.SARVAM_API_KEY or self.OPENAI_API_KEY)
		self.COGNEE_MODE = self._mode("COGNEE_MODE", self.COGNEE_LLM_API_KEY)
		pg_default = "live" if _s("PAYTM_ENV", "mock").lower() == "staging" else "mock"
		self.PAYTM_PG_MODE = self._mode("PAYTM_PG_MODE", self.PAYTM_PG_MID and self.PAYTM_PG_KEY,
			pg_default)

		self.HOLDOUT_PERCENT = _i("HOLDOUT_PERCENT", 20)
		self.AUTO_ACTION_LIMIT_INR = _f("AUTO_ACTION_LIMIT_INR", 2000)
		self.APPROVAL_REQUIRED_ABOVE_INR = _f("APPROVAL_REQUIRED_ABOVE_INR", 2000)
		self.ESCALATE_ABOVE_INR = _f("ESCALATE_ABOVE_INR", 25000)
		self.CONFIDENCE_ESCALATION_THRESHOLD = _f("CONFIDENCE_ESCALATION_THRESHOLD", 0.5)
		self.CONSUMER_DAILY_SPEND_CAP_INR = _f("CONSUMER_DAILY_SPEND_CAP_INR", 20000)
		self.PLAN_APPROVAL_TTL_MIN = _i("PLAN_APPROVAL_TTL_MIN", 15)
		self.COHORT_MIN_SIZE = _i("COHORT_MIN_SIZE", 10)
		self.PREFERENCE_RETENTION_DAYS = _i("PREFERENCE_RETENTION_DAYS", 180)
		self.OFFER_MAX_PER_USER_PER_WEEK = _i("OFFER_MAX_PER_USER_PER_WEEK", 2)
		self.MOCK_PARTNER_FAILURE_RATE = _f("MOCK_PARTNER_FAILURE_RATE", 0.0)
		self.SEED_RANDOM = _i("SEED_RANDOM", 42)

		# Tunables with code defaults. An env override is honoured but never required.
		self.UPLOAD_MAX_MB = _f("UPLOAD_MAX_MB", 10)
		self.UPLOAD_RETENTION_HOURS = _f("UPLOAD_RETENTION_HOURS", 24)
		self.EXTRACTION_CONFIDENCE_AUTO = _f("EXTRACTION_CONFIDENCE_AUTO", 0.9)
		self.EXTRACTION_CONFIDENCE_MIN = _f("EXTRACTION_CONFIDENCE_MIN", 0.6)
		self.BILL_ANOMALY_FACTOR = _f("BILL_ANOMALY_FACTOR", 1.8)
		self.BUDGET_ALERT_PCT = _f("BUDGET_ALERT_PCT", 80)
		self.LOW_STOCK_DEFAULT_DAYS = _f("LOW_STOCK_DEFAULT_DAYS", 3)
		self.NUDGE_MAX_PER_USER_PER_DAY = _i("NUDGE_MAX_PER_USER_PER_DAY", 3)
		self.DOC_JOB_TIMEOUT_SEC = _f("DOC_JOB_TIMEOUT_SEC", 60)
		self.STOCK_UNDO_MINUTES = _i("STOCK_UNDO_MINUTES", 10)
		self.REORDER_COVER_DAYS = _i("REORDER_COVER_DAYS", 14)
		self.DEAD_STOCK_DAYS = _i("DEAD_STOCK_DAYS", 30)
		self.DATA_DIR = Path(_s("DATA_DIR", str(ROOT / "data")))

	@staticmethod
	def _mode(key: str, has_key, default: str = "live") -> str:
		"""Live when a key exists unless the mode says mock. No key always means mock."""
		mode = _s(key, default).lower()
		return "live" if mode == "live" and has_key else "mock"

	def key_report(self) -> dict[str, bool]:
		"""Presence only. Values are never returned."""
		keys = [
			"SARVAM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
			"COGNEE_LLM_API_KEY", "PAYTM_PG_MID", "PAYTM_PG_KEY",
		]
		return {k: bool(getattr(self, k)) for k in keys}


settings = Settings()
