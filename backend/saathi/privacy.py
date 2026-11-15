"""Preference store, consent and cohorts. Design reference: India's DPDP Act (consent that is
specific and revocable, purpose limitation, erasure, data minimisation)."""
from datetime import timedelta

from sqlalchemy.orm import Session

from .adapters.llm import ALLOWED_PREF_CATEGORIES
from .adapters.memory import memory
from .config import settings
from .db import Cohort, Consent, Merchant, OfferDelivery, Preference, User, now
from .services import audit, impact

SCOPES = {
	"personalisation": "Use what I tell Saathi to tailor my own plans.",
	"offers": "Match me, without sharing who I am, to offers from nearby shops. Off by default.",
	"spend_insights": "Show me insights about my own spending. Never shared with shops.",
}
DEFAULT_ON = {"spend_insights"}
# Never stored and never inferred, whatever the source.
SENSITIVE_HINTS = ("health", "religio", "caste", "politic", "sexual", "medical", "disab")


class PreferenceRejected(ValueError):
	pass


def consent_state(s: Session, user_id: int) -> dict[str, bool]:
	rows = {c.scope: c.granted for c in s.query(Consent).filter_by(user_id=user_id).all()}
	return {scope: rows.get(scope, scope in DEFAULT_ON) for scope in SCOPES}


def set_consent(s: Session, user_id: int, scope: str, granted: bool) -> None:
	"""Takes effect immediately: every read path checks consent at the moment of use."""
	if scope not in SCOPES:
		raise ValueError("unknown_scope")
	c = s.query(Consent).filter_by(user_id=user_id, scope=scope).first()
	if not c:
		c = Consent(user_id=user_id, scope=scope)
		s.add(c)
	was = c.granted
	c.granted, c.updated_at = granted, now()
	audit(s, f"consumer:{user_id}", f"consent_{scope}_{'on' if granted else 'off'}", outcome="ok")
	if scope == "offers" and was and not granted:
		recent = s.query(OfferDelivery).filter(OfferDelivery.user_id == user_id,
			OfferDelivery.delivered_at > now() - timedelta(days=14)).first()
		impact(s, "offer_opt_out", "consumer", user_id, offer_id=recent.offer_id if recent else None)
	s.flush()


def set_preference(s: Session, user_id: int, category: str, value: str, source: str = "stated",
		confidence: float = 1.0) -> Preference:
	cat = category.strip().lower()
	if cat not in ALLOWED_PREF_CATEGORIES or any(h in f"{cat} {value}".lower()
			for h in SENSITIVE_HINTS):
		raise PreferenceRejected(f"category_not_allowed:{cat}")
	if source not in ("stated", "inferred"):
		raise PreferenceRejected("bad_source")
	cur = s.query(Preference).filter_by(user_id=user_id, category=cat).first()
	if cur and cur.source == "stated" and source == "inferred":
		return cur  # what the user said beats what we guessed
	if not cur:
		cur = Preference(user_id=user_id, category=cat)
		s.add(cur)
	cur.value, cur.source, cur.created_at = value, source, now()
	cur.confidence = 1.0 if source == "stated" else min(confidence, 0.95)
	memory.add(s, f"user:{user_id}", "preference", f"{cat} = {value} ({source})")
	s.flush()
	return cur


def preferences(s: Session, user_id: int, for_use: bool = True) -> list[Preference]:
	"""`for_use=True` returns nothing unless the personalisation consent is on."""
	if for_use and not consent_state(s, user_id)["personalisation"]:
		return []
	return s.query(Preference).filter_by(user_id=user_id).all()


def delete_preference(s: Session, user_id: int, pref_id: int) -> bool:
	p = s.query(Preference).filter_by(id=pref_id, user_id=user_id).first()
	if not p:
		return False
	memory.forget(s, f"user:{user_id}", contains=f"{p.category} = ")
	s.delete(p)
	audit(s, f"consumer:{user_id}", "preference_deleted", inputs={"category": p.category})
	s.flush()
	refresh_all_cohorts(s)
	return True


def delete_all(s: Session, user_id: int) -> None:
	"""Erase preferences and memory, then recompute cohorts so the user is out of every count."""
	s.query(Preference).filter_by(user_id=user_id).delete()
	memory.forget(s, f"user:{user_id}")
	audit(s, f"consumer:{user_id}", "all_preferences_deleted", outcome="ok")
	s.flush()
	refresh_all_cohorts(s)


def purge_expired(s: Session) -> int:
	cut = now() - timedelta(days=settings.PREFERENCE_RETENTION_DAYS)
	n = s.query(Preference).filter(Preference.created_at < cut).delete()
	if n:
		refresh_all_cohorts(s)
	return n


# ---------------- cohorts ----------------
def _cohort_defs(m: Merchant) -> list[tuple[str, dict[str, str], str]]:
	"""(key, required preferences, human phrase) for this merchant's category."""
	tags, out = set(m.tags or []), []
	cuisine = next((t for t in ("veg", "south-indian", "north-indian", "street-food") if t in tags),
		None)
	if m.category in ("restaurant", "tea-stall") and cuisine:
		low = "budget" in tags
		if low:
			out.append((f"cuisine:{cuisine}+budget:low", {"cuisine": cuisine, "budget": "low"},
				f"prefer budget {cuisine} meals"))
		out.append((f"cuisine:{cuisine}", {"cuisine": cuisine}, f"prefer {cuisine} food"))
	if m.category == "travel-agent":
		for mode in ("train", "bus"):
			out.append((f"travel_mode:{mode}", {"travel_mode": mode}, f"prefer {mode} travel"))
	out.append(("budget:low", {"budget": "low"}, "look for low prices"))
	return out


def cohort_members(s: Session, merchant_id: int, key: str) -> list[int]:
	"""INTERNAL ONLY. User ids never leave the backend and are never shown to a merchant."""
	m = s.get(Merchant, merchant_id)
	need = next((d[1] for d in _cohort_defs(m) if d[0] == key), None)
	if need is None:
		return []
	out = []
	for u in s.query(User).filter_by(city=m.city).all():
		if not consent_state(s, u.id)["offers"]:
			continue
		have = {p.category: p.value for p in s.query(Preference).filter_by(user_id=u.id).all()}
		if all(have.get(k) == v for k, v in need.items()):
			out.append(u.id)
	return out


def compute_cohorts(s: Session, merchant_id: int) -> list[Cohort]:
	m = s.get(Merchant, merchant_id)
	s.query(Cohort).filter_by(merchant_id=merchant_id).delete()
	rows = []
	for key, _, phrase in _cohort_defs(m):
		n = len(cohort_members(s, merchant_id, key))
		c = Cohort(merchant_id=merchant_id, key=key, size=n,
			description=f"{n} nearby users {phrase}")
		s.add(c)
		rows.append(c)
	s.flush()
	return rows


def visible_cohorts(s: Session, merchant_id: int) -> list[dict]:
	"""What a merchant may see: counts at or above COHORT_MIN_SIZE, no identifiers."""
	rows = compute_cohorts(s, merchant_id)
	return [{"key": c.key, "size": c.size, "description": c.description}
		for c in sorted(rows, key=lambda c: -c.size) if c.size >= settings.COHORT_MIN_SIZE]


def refresh_all_cohorts(s: Session) -> None:
	for (mid,) in s.query(Cohort.merchant_id).distinct().all():
		compute_cohorts(s, mid)
