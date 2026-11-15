"""Deterministic policy engine. The LLM proposes; this decides.

Returns execute | needs_approval | blocked | escalate for every proposed action. Nothing in
here calls an LLM, and every violation counter the evaluation reports is derived from it.
"""
import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import (
	Approval, Cohort, Consent, ExperimentAssignment, Merchant, OfferDelivery, Offer, Transaction,
	Wallet, now,
)
from .tools import REGISTRY

Outcome = Literal["execute", "needs_approval", "blocked", "escalate"]
RISKY = {"dispute", "fraud", "suspected_fraud", "legal", "abuse"}
DEBIT_KINDS = ("booking", "payment", "recharge", "bill")

# Autonomy ladder: L0 observe · L1 suggest · L2 act within limits then notify ·
# L3 one-tap approval · L4 escalate. Keyed by actor, then tier ("*" = any), then tool.
AUTONOMY: dict[str, dict[str, dict[str, str]]] = {
	"consumer": {"*": {
		"pay_from_wallet": "L3", "pay_via_upi_sim": "L3", "mobile_recharge": "L3",
		"pay_bill": "L3", "confirm_booking": "L3", "redeem_offer": "L3",
		"hold_booking": "L2", "cancel_booking": "L2", "refund_payment": "L2",
		"retry_refund": "L2", "raise_ticket": "L2", "notify": "L2", "deliver_offer": "L2",
		"create_support_ticket": "L2", "escalate_to_human": "L2",
	}},
	"merchant": {
		"*": {
			"retry_settlement": "L2", "raise_device_ticket": "L2", "create_payment_link": "L2",
			"notify": "L2", "create_support_ticket": "L2", "escalate_to_human": "L2",
			"request_bank_detail_update": "L3", "send_khata_reminder": "L3",
			"create_offer_campaign": "L3", "start_loan_application": "L3",
		},
		# Soundbox-only merchants: low-risk actions run without a tap, by design.
		"T1": {"send_khata_reminder": "L2", "start_loan_application": "L1"},
	},
	"ops": {"*": {}},
	"system": {"*": {}},
}
_overrides: dict[str, str] = {}  # Compliance can downgrade any tool at runtime.


def set_override(tool: str, level: str | None) -> None:
	if level is None:
		_overrides.pop(tool, None)
	else:
		_overrides[tool] = level


class ProposedAction(BaseModel):
	tool: str
	args: dict[str, Any] = Field(default_factory=dict)
	actor_type: Literal["consumer", "merchant", "ops", "system"]
	actor_id: int
	idempotency_key: str
	why: str = ""
	agent: str = ""
	amount: float = 0
	confidence: float = 1.0
	risk: list[str] = Field(default_factory=list)
	case_id: int | None = None
	approval_id: int | None = None
	approval_payload: dict[str, Any] | None = None  # what the approval must be bound to
	compensation: bool = False  # saga rollback of a charge Saathi itself just made


class Decision(BaseModel):
	outcome: Outcome
	level: str
	reason: str


def payload_hash(payload: dict) -> str:
	def norm(v):
		if isinstance(v, float):
			return round(v, 2)
		if isinstance(v, dict):
			return {k: norm(x) for k, x in sorted(v.items())}
		if isinstance(v, (list, tuple)):
			return [norm(x) for x in v]
		return v
	blob = json.dumps(norm(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(blob.encode()).hexdigest()


def has_consent(s: Session, user_id: int, scope: str) -> bool:
	c = s.query(Consent).filter_by(user_id=user_id, scope=scope).first()
	return bool(c and c.granted)


def spent_today(s: Session, user_id: int) -> float:
	start = now().replace(hour=0, minute=0, second=0, microsecond=0)
	return float(s.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
		Transaction.user_id == user_id, Transaction.kind.in_(DEBIT_KINDS),
		Transaction.status == "success", Transaction.created_at >= start).scalar())


def offers_this_week(s: Session, user_id: int) -> int:
	return s.query(OfferDelivery).filter(OfferDelivery.user_id == user_id,
		OfferDelivery.delivered_at > now() - timedelta(days=7)).count()


def level_for(s: Session, p: ProposedAction) -> str:
	if p.tool in _overrides:
		return _overrides[p.tool]
	table = AUTONOMY.get(p.actor_type, {})
	tier = "*"
	if p.actor_type == "merchant":
		m = s.get(Merchant, p.actor_id)
		tier = m.tier if m else "*"
	return table.get(tier, {}).get(p.tool) or table.get("*", {}).get(p.tool) or "L3"


def check_approval(s: Session, p: ProposedAction) -> str:
	"""'' when a valid approval covers this exact payload, otherwise the reason it does not."""
	if not p.approval_id:
		return "approval_required"
	a = s.get(Approval, p.approval_id)
	if not a or a.actor_type != p.actor_type or a.actor_id != p.actor_id:
		return "approval_not_found"
	if a.status != "approved":
		return f"approval_{a.status}"
	if a.expires_at < now():
		return "approval_expired"
	if a.payload_hash != payload_hash(p.approval_payload or {}):
		return "approval_hash_mismatch"
	return ""


def decide(s: Session, p: ProposedAction) -> Decision:
	spec = REGISTRY.get(p.tool)
	if not spec:
		return Decision(outcome="blocked", level="L0", reason="unknown_tool")
	try:
		spec.args_model(**p.args)
	except ValidationError:
		return Decision(outcome="blocked", level="L0", reason="invalid_arguments")
	if not spec.side_effect:
		return Decision(outcome="execute", level="L2", reason="read_only")
	if p.tool in ("escalate_to_human", "create_support_ticket", "raise_ticket"):
		return Decision(outcome="execute", level="L2", reason="handoff_always_allowed")

	if RISKY & set(p.risk):
		return Decision(outcome="escalate", level="L4", reason="risk:" + ",".join(sorted(p.risk)))
	if p.actor_type == "merchant" and (m := s.get(Merchant, p.actor_id)) and m.risk_flag:
		return Decision(outcome="escalate", level="L4", reason=f"risk:{m.risk_flag}")
	if p.confidence < settings.CONFIDENCE_ESCALATION_THRESHOLD:
		return Decision(outcome="escalate", level="L4", reason="low_confidence")

	# ---- consent, cohort and frequency guards (Compliance) ----
	if p.tool == "deliver_offer":
		uid = p.args["user_id"]
		if not has_consent(s, uid, "offers"):
			return Decision(outcome="blocked", level="L0", reason="no_offers_consent")
		offer = s.get(Offer, p.args["offer_id"])
		if not offer or offer.status != "live":
			return Decision(outcome="blocked", level="L0", reason="offer_not_live")
		held = s.query(ExperimentAssignment).filter_by(experiment=f"offer:{offer.id}",
			subject_type="user", subject_id=uid, group="holdout").first()
		if held:
			return Decision(outcome="blocked", level="L0", reason="holdout_user")
		if offers_this_week(s, uid) >= settings.OFFER_MAX_PER_USER_PER_WEEK:
			return Decision(outcome="blocked", level="L0", reason="frequency_cap")
	if p.tool == "redeem_offer" and not has_consent(s, p.actor_id, "offers"):
		return Decision(outcome="blocked", level="L0", reason="no_offers_consent")
	if p.tool == "create_offer_campaign":
		c = s.query(Cohort).filter_by(merchant_id=p.actor_id, key=p.args.get("cohort_key")).first()
		if not c or c.size < settings.COHORT_MIN_SIZE:
			return Decision(outcome="blocked", level="L0", reason="cohort_below_minimum")

	# ---- compensation: only ever returns money Saathi just took ----
	if p.compensation:
		if p.tool == "cancel_booking":
			return Decision(outcome="execute", level="L2", reason="saga_compensation")
		if p.tool == "refund_payment":
			orig = s.get(Transaction, p.args.get("txn_id") or 0)
			ok = (orig and orig.user_id == p.args["user_id"] and orig.status == "success"
				and p.args["amount"] <= orig.amount + 0.01)
			if ok:
				return Decision(outcome="execute", level="L2", reason="saga_compensation")
			return Decision(outcome="blocked", level="L0", reason="compensation_not_matched")

	if p.amount > settings.ESCALATE_ABOVE_INR:
		return Decision(outcome="escalate", level="L4", reason="amount_above_escalation_limit")

	# ---- consumer money checks ----
	prepaid = p.tool == "mobile_recharge" and p.args.get("prepaid_txn_id")
	if p.actor_type == "consumer" and spec.money == "debit" and not prepaid:
		if spent_today(s, p.actor_id) + p.amount > settings.CONSUMER_DAILY_SPEND_CAP_INR:
			return Decision(outcome="blocked", level="L0", reason="daily_spend_cap")
		if p.tool != "pay_via_upi_sim" and p.tool != "confirm_booking":
			w = s.get(Wallet, p.actor_id)
			if not w or w.balance < p.amount:
				return Decision(outcome="blocked", level="L0", reason="insufficient_balance")

	level = "L2" if prepaid else level_for(s, p)
	if level == "L0":
		return Decision(outcome="blocked", level=level, reason="observe_only")
	if level == "L1":
		return Decision(outcome="blocked", level=level, reason="suggest_only")
	if level == "L4":
		return Decision(outcome="escalate", level=level, reason="policy_requires_human")
	if level == "L2":
		if p.amount <= settings.AUTO_ACTION_LIMIT_INR:
			return Decision(outcome="execute", level=level, reason="within_auto_limit")
		level = "L3"  # above the auto limit an L2 action needs a tap
	why_not = check_approval(s, p)
	if not why_not:
		return Decision(outcome="execute", level=level, reason="approved")
	return Decision(outcome="needs_approval", level=level, reason=why_not)
