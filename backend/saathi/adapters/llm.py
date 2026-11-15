"""LLM adapter. The LLM only ever returns a schema-validated proposal (`Intent`); it cannot
call a tool. Chain: Sarvam → any other configured LLM → scripted mock, so the demo never dies."""
import json
import re
from datetime import date, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import settings

CITIES = ["Mumbai", "Pune", "Delhi", "Jaipur", "Bengaluru", "Goa"]
CITY_ALIASES = {"bombay": "Mumbai", "bangalore": "Bengaluru", "blr": "Bengaluru",
	"dilli": "Delhi", "मुंबई": "Mumbai", "गोवा": "Goa", "पुणे": "Pune", "दिल्ली": "Delhi",
	"जयपुर": "Jaipur"}
ALLOWED_PREF_CATEGORIES = {"budget", "travel_mode", "cuisine", "pace", "price_sensitivity",
	"time_slot"}

IntentName = Literal[
	"trip", "recharge", "bill", "payment_issue", "refund_status", "wallet", "offers",
	"money_not_received", "collect_dues", "device_issue", "win_back", "loan", "summary",
	"approve", "reject", "smalltalk", "unknown",
]


class Intent(BaseModel):
	"""The only thing an LLM is allowed to hand back. Anything else is rejected."""
	intent: IntentName
	confidence: float = Field(ge=0, le=1)
	origin: str | None = None
	dest: str | None = None
	date: str | None = None  # ISO date
	nights: int | None = Field(default=None, ge=0, le=30)
	budget: float | None = Field(default=None, ge=0)
	travellers: int = Field(default=1, ge=1, le=9)
	mode: Literal["bus", "train", "flight"] | None = None
	operator: str | None = None
	biller: str | None = None
	amount: float | None = Field(default=None, ge=0)
	stated_preferences: dict[str, str] = Field(default_factory=dict)
	risk: list[str] = Field(default_factory=list)

	@field_validator("stated_preferences")
	@classmethod
	def _allowlist(cls, v: dict[str, str]) -> dict[str, str]:
		# Sensitive categories can never enter through the model: drop anything off the list.
		return {k: str(x) for k, x in v.items() if k in ALLOWED_PREF_CATEGORIES}

	@field_validator("origin", "dest")
	@classmethod
	def _city(cls, v: str | None) -> str | None:
		if not v:
			return None
		k = v.strip().lower()
		return CITY_ALIASES.get(k) or next((c for c in CITIES if c.lower() == k), v.strip().title())


SYSTEM = (
	"You are the understanding layer of Saathi, an assistant for Paytm users and merchants. "
	"Messages may be Hindi, Hinglish, Marathi, Kannada or English. Reply with ONE JSON object "
	"and nothing else, matching this schema: " + json.dumps(Intent.model_json_schema()) + " "
	"Never invent prices or availability. Only fill fields the message supports. "
	"stated_preferences may only use these keys: " + ", ".join(sorted(ALLOWED_PREF_CATEGORIES)) + ". "
	"Add 'dispute' or 'fraud' to risk if the user denies making a payment or reports fraud."
)


def parse_intent(raw: str) -> Intent:
	"""Schema-validate model output. Raises ValueError for anything that does not fit."""
	m = re.search(r"\{.*\}", raw or "", re.S)
	if not m:
		raise ValueError("no_json_in_llm_output")
	try:
		return Intent.model_validate_json(m.group(0))
	except ValidationError as e:
		raise ValueError("llm_output_failed_schema") from e


# ---------------- scripted mock ----------------
def _find_cities(text: str) -> list[tuple[int, str]]:
	low, found = text.lower(), []
	for name in [*CITIES, *CITY_ALIASES]:
		for m in re.finditer(re.escape(name.lower()), low):
			found.append((m.start(), CITY_ALIASES.get(name, name)))
	return sorted(found)


def _find_date(low: str, today: date) -> str | None:
	if m := re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low):
		return m.group(1)
	for words, days in ((("parso", "day after"), 2), (("kal", "tomorrow", "उद्या", "कल"), 1),
			(("aaj", "today", "आज"), 0)):
		if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in words):
			return (today + timedelta(days=days)).isoformat()
	if "weekend" in low or "shanivar" in low or "saturday" in low:
		return (today + timedelta(days=(5 - today.weekday()) % 7 or 7)).isoformat()
	if "next week" in low or "agle hafte" in low:
		return (today + timedelta(days=7)).isoformat()
	return None


def mock_intent(text: str, actor_type: str = "consumer") -> Intent:
	low = text.lower()
	has = lambda *ws: any(w in low for w in ws)  # noqa: E731
	out: dict = {"confidence": 0.9, "risk": []}

	if has("fraud", "dhokha", "maine nahi kiya", "i did not make", "never made", "unauthori"):
		out["risk"].append("dispute")
	if actor_type == "merchant":
		if has("paisa nahi aaya", "paise nahi aaye", "settlement", "money not received",
				"payment nahi aaya", "पैसा नहीं आया", "पैसे आले नाहीत"):
			out["intent"] = "money_not_received"
		elif has("udhaar", "udhar", "khata", "dues", "baki", "baaki", "collect", "vasool"):
			out["intent"] = "collect_dues"
		elif has("soundbox", "machine", "device", "awaaz nahi", "band hai"):
			out["intent"] = "device_issue"
		elif has("offer", "customer wapas", "win back", "lapsed", "grahak", "naye customer"):
			out["intent"] = "win_back"
		elif has("loan", "karz", "funding", "restock"):
			out["intent"] = "loan"
		elif has("kaisa raha", "how did i do", "summary", "this week", "is hafte"):
			out["intent"] = "summary"
	else:
		cities = _find_cities(text)
		if has("refund"):
			out["intent"] = "refund_status"
		elif has("paisa kat", "paise kat", "debited", "deducted", "payment fail", "failed",
				"booking nahi hui", "not booked", "kat gaya", "kat gaye", "recharge nahi hua",
				"पैसे कट"):
			out["intent"] = "payment_issue"
		elif has("recharge"):
			out["intent"] = "recharge"
			out["operator"] = next((o for o in ("Jio", "Airtel", "Vi", "BSNL") if o.lower() in low),
				None)
		elif has("bill", "bijli", "electricity", "gas", "broadband"):
			out["intent"] = "bill"
			out["biller"] = next((b for b in ("MSEDCL Electricity", "BESCOM Electricity",
				"Mahanagar Gas", "ACT Broadband") if b.split()[0].lower() in low), None)
		elif has("balance", "wallet"):
			out["intent"] = "wallet"
		elif has("offer", "deal", "discount"):
			out["intent"] = "offers"
		elif cities or has("trip", "travel", "jaana", "jana", "ghoomne", "ticket", "hotel",
				"yatra", "book"):
			out["intent"] = "trip"
			froms = re.search(r"(\w+)\s+se\b|from\s+(\w+)", low)
			if len(cities) >= 2:
				out["origin"], out["dest"] = cities[0][1], cities[1][1]
				if froms and (f := (froms.group(1) or froms.group(2))):
					fc = next((c for _, c in cities if c.lower() == CITY_ALIASES.get(f, f).lower()),
						None)
					if fc:
						out["origin"] = fc
						out["dest"] = next(c for _, c in cities if c != fc)
			elif len(cities) == 1:
				out["dest"] = cities[0][1]
		if has("kaisa raha", "how did i do", "summary"):
			out["intent"] = "summary"

	if "intent" not in out:
		if (d := _yes_no(low)) is not None:
			out["intent"] = "approve" if d else "reject"
		elif has("hello", "hi ", "namaste", "thanks", "dhanyavad", "shukriya"):
			out["intent"] = "smalltalk"
		else:
			out["intent"], out["confidence"] = "unknown", 0.3

	out["date"] = _find_date(low, date.today())
	if m := re.search(r"(\d+)\s*(?:raat|night|din|day)", low):
		out["nights"] = int(m.group(1))
	if m := re.search(r"(?:budget|under|tak|within|₹|rs\.?)\s*₹?\s*(\d[\d,]*)\s*(k\b)?", low):
		val = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
		out["budget" if out["intent"] == "trip" else "amount"] = val
	if m := re.search(r"(\d+)\s*(?:log|people|travellers|adults|jan)", low):
		out["travellers"] = int(m.group(1))
	out["mode"] = next((k for k in ("train", "bus", "flight") if k in low), None)
	prefs = {}
	if has("veg", "shakahari"):
		prefs["cuisine"] = "veg"
	if has("sasta", "cheap", "budget", "kam paise"):
		prefs["budget"] = "low"
	if out["mode"]:
		prefs["travel_mode"] = out["mode"]
	out["stated_preferences"] = prefs
	return Intent(**out)


def _yes_no(low: str) -> bool | None:
	from ..services import parse_voice_decision
	return parse_voice_decision(low)


# ---------------- live providers ----------------
def _openai_style(base: str, headers: dict, model: str, text: str, actor_type: str) -> str:
	r = httpx.post(f"{base.rstrip('/')}/chat/completions", headers=headers, timeout=8, json={
		"model": model, "temperature": 0,
		"messages": [{"role": "system", "content": SYSTEM},
			{"role": "user", "content": f"[actor={actor_type}] {text}"}]})
	r.raise_for_status()
	return r.json()["choices"][0]["message"]["content"]


def _providers() -> dict:
	s = settings
	return {
		"sarvam": (bool(s.SARVAM_API_KEY) and s.SARVAM_MODE == "live", lambda t, a: _openai_style(
			f"{s.SARVAM_BASE_URL}/v1", {"Authorization": f"Bearer {s.SARVAM_API_KEY}",
				"api-subscription-key": s.SARVAM_API_KEY}, s.SARVAM_LLM_MODEL, t, a)),
		"openai": (bool(s.OPENAI_API_KEY and s.OPENAI_MODEL), lambda t, a: _openai_style(
			s.OPENAI_BASE_URL, {"Authorization": f"Bearer {s.OPENAI_API_KEY}"}, s.OPENAI_MODEL, t, a)),
		"gemini": (bool(s.GEMINI_API_KEY and s.GEMINI_MODEL), lambda t, a: _openai_style(
			"https://generativelanguage.googleapis.com/v1beta/openai",
			{"Authorization": f"Bearer {s.GEMINI_API_KEY}"}, s.GEMINI_MODEL, t, a)),
	}


class LLM:
	last_provider = "mock"

	def understand(self, text: str, actor_type: str = "consumer") -> Intent:
		"""Proposal only. Invalid output from one provider falls through to the next."""
		if settings.LLM_MODE == "live":
			provs = _providers()
			for name in settings.LLM_FALLBACK_CHAIN:
				ready, fn = provs.get(name, (False, None))
				if not ready:
					continue
				try:
					intent = parse_intent(fn(text, actor_type))
					self.last_provider = name
					return intent
				except Exception:  # network, auth or schema failure: try the next one
					continue
		self.last_provider = "mock"
		return mock_intent(text, actor_type)

	def health(self) -> dict:
		if settings.LLM_MODE != "live":
			return {"mode": "mock", "ok": True, "detail": "scripted decisions"}
		try:
			self.understand("ping")
			ok = self.last_provider != "mock"
			return {"mode": "live" if ok else "mock", "ok": ok,
				"detail": f"answered by {self.last_provider}"}
		except Exception as e:
			return {"mode": "mock", "ok": False, "detail": type(e).__name__}


llm = LLM()
