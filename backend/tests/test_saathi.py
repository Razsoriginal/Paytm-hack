import pytest
from pydantic import ValidationError

from saathi import policy, privacy, seed, services, tools
from saathi.adapters.llm import Intent, parse_intent
from saathi.agents import flows, manager
from saathi.api import policy_violations
from saathi.config import settings
from saathi.db import (
	Approval, AuditLog, BookingItem, Cohort, Escalation, MemoryItem, OfferDelivery, Preference,
	Transaction, Wallet,
)
from saathi.policy import ProposedAction as PA


def bal(s, uid):
	s.expire_all()
	return s.get(Wallet, uid).balance


def trip(s, uid, text):
	return manager.handle(s, "consumer", uid, text)


# ---------- C1 ----------
def test_trip_happy_path_and_double_submit(s):
	before = bal(s, 1)
	r = trip(s, 1, "Mumbai se Pune kal 1 raat")
	total, aid = r["approval"]["payload"]["total"], r["approval"]["id"]
	assert "Booked" in manager.approve(s, aid, True)["reply"]
	assert bal(s, 1) == before - total
	manager.approve(s, aid, True)  # double submit
	assert bal(s, 1) == before - total
	assert s.query(Transaction).filter_by(user_id=1, kind="booking").count() == 1


def test_approval_hash_mismatch_rejected(s):
	before = bal(s, 12)
	r = trip(s, 12, "Delhi se Jaipur kal train se")
	old = r["approval"]["id"]
	r2 = manager.approve(s, old, True)
	assert "changed" in r2["reply"] and bal(s, 12) == before
	assert s.get(Approval, old).status == "expired"
	assert r2["approval"]["payload_hash"] != r["approval"]["payload_hash"]
	assert "Booked" in manager.approve(s, r2["approval"]["id"], True)["reply"]


def test_tampered_payload_needs_approval(s):
	r = trip(s, 1, "Mumbai se Pune kal 1 raat")
	a = services.decide_approval(s, r["approval"]["id"], True)
	forged = {**a.payload, "total": 1.0}
	d = policy.decide(s, PA(tool="pay_from_wallet", actor_type="consumer", actor_id=1,
		args={"user_id": 1, "amount": 1.0, "purpose": "x"}, amount=1.0, idempotency_key="k",
		approval_id=a.id, approval_payload=forged))
	assert (d.outcome, d.reason) == ("needs_approval", "approval_hash_mismatch")


def test_saga_rollback_leaves_no_orphans(s):
	before = bal(s, 1)
	r = trip(s, 1, "Mumbai se Goa kal 2 raat")
	r2 = manager.approve(s, r["approval"]["id"], True)
	assert "refunded" in r2["reply"] and r2["approval"]
	assert bal(s, 1) == before
	assert s.query(BookingItem).filter_by(user_id=1, status="held").count() == 0
	assert s.query(BookingItem).filter_by(user_id=1, status="confirmed", booking_id=None).count() == 0
	assert not policy_violations(s)


def test_spend_cap_and_wallet(s, monkeypatch):
	r = trip(s, seed.U_LOW_WALLET, "Mumbai se Pune kal 1 raat")
	assert "wallet balance is too low" in manager.approve(s, r["approval"]["id"], True)["reply"]
	assert s.query(BookingItem).filter_by(user_id=seed.U_LOW_WALLET, status="held").count() == 0
	monkeypatch.setattr(settings, "CONSUMER_DAILY_SPEND_CAP_INR", 100)
	r = trip(s, 1, "Mumbai se Pune kal 1 raat")
	assert "daily spend limit" in manager.approve(s, r["approval"]["id"], True)["reply"]


def test_one_clarifying_question(s):
	assert "When" in trip(s, 13, "Goa jaana hai")["reply"]
	assert trip(s, 13, "kal")["approval"]


# ---------- C2 ----------
@pytest.mark.parametrize("uid,word,escalated", [
	(seed.U_DEBIT_NO_BOOKING, "refunded", False), (seed.U_RECHARGE_FAIL, "retried", False),
	(seed.U_REFUND_LATE, "pushed it through", False), (seed.U_REFUND_IN_SLA, "in progress", False),
	(seed.U_REFUND_STUCK, "support team", True), (seed.U_LARGE, "support team", True),
	(seed.U_DISPUTE, "support team", True),
])
def test_care_resolves_or_escalates(s, uid, word, escalated):
	r = manager.handle(s, "consumer", uid, "mere paise kat gaye, refund kab aayega")
	assert word in r["reply"]
	e = s.query(Escalation).filter_by(actor_id=uid).first()
	assert bool(e) == escalated
	if e:  # the packet carries full context
		assert e.packet["issue_summary"] and e.packet["why_escalated"] and "tried" in e.packet


def test_idempotent_executor(s):
	p = PA(tool="refund_payment", actor_type="consumer", actor_id=2, amount=100,
		args={"user_id": 2, "amount": 100.0}, idempotency_key="same")
	b = bal(s, 2)
	assert services.execute(s, p).ok and services.execute(s, p).replayed
	assert bal(s, 2) == b + 100


# ---------- policy ----------
def test_policy_limits_and_tiers(s):
	def d(**kw):
		return policy.decide(s, PA(idempotency_key="x", **kw)).outcome
	assert d(tool="nope", actor_type="consumer", actor_id=1) == "blocked"
	assert d(tool="pay_bill", actor_type="consumer", actor_id=1, amount=500,
		args={"user_id": 1, "biller": "b", "amount": 500.0}) == "needs_approval"
	assert d(tool="refund_payment", actor_type="consumer", actor_id=1, amount=99999,
		args={"user_id": 1, "amount": 99999.0}) == "escalate"
	assert d(tool="refund_payment", actor_type="consumer", actor_id=1, amount=10, risk=["fraud"],
		args={"user_id": 1, "amount": 10.0}) == "escalate"
	assert d(tool="refund_payment", actor_type="consumer", actor_id=1, amount=10, confidence=0.1,
		args={"user_id": 1, "amount": 10.0}) == "escalate"
	rem = dict(tool="send_khata_reminder", actor_type="merchant", args={"entry_id": 1})
	assert d(actor_id=seed.M_SOUNDBOX, **rem) == "execute"  # T1: L2
	assert d(actor_id=seed.M_KHATA, **rem) == "needs_approval"  # T2: L3
	assert d(tool="retry_settlement", actor_type="merchant", actor_id=seed.M_FRAUD,
		args={"settlement_id": 1}) == "escalate"


def test_invalid_llm_output_rejected():
	for raw in ("book it now", '{"intent":"transfer_all_money","confidence":1}',
			'{"intent":"trip","confidence":7}'):
		with pytest.raises(ValueError):
			parse_intent(raw)
	i = Intent(intent="trip", confidence=0.9, stated_preferences={"religion": "x", "budget": "low"})
	assert i.stated_preferences == {"budget": "low"}
	with pytest.raises(ValidationError):
		tools.call(None, "pay_bill", {"user_id": 1, "biller": "b", "amount": "lots"})


# ---------- privacy and bridge ----------
def launch(s):
	r = manager.handle(s, "merchant", seed.M_BRIDGE, "naye customer ke liye offer banao")
	return r, manager.approve(s, r["approval"]["id"], True)


def test_bridge_consent_holdout_and_measurement(s):
	r, x = launch(s)
	oid = x["offer_id"]
	got = {d.user_id for d in s.query(OfferDelivery).filter_by(offer_id=oid)}
	assert got and all(policy.has_consent(s, u, "offers") for u in got)
	assert seed.U_OPTED_OUT not in got
	rep = flows.simulate_offer_response(s, oid)
	assert rep["delivered"] == len(got) and rep["holdout"] > 0
	assert rep["delivered"] + rep["holdout"] == rep["matched"]
	assert "opt_out_rate" in rep and not policy_violations(s)


def test_consent_revoked_excludes_everywhere(s):
	key = privacy.visible_cohorts(s, seed.M_BRIDGE)[0]["key"]
	assert seed.U_REVOKER in privacy.cohort_members(s, seed.M_BRIDGE, key)
	privacy.set_consent(s, seed.U_REVOKER, "offers", False)  # revoked mid-way
	assert seed.U_REVOKER not in privacy.cohort_members(s, seed.M_BRIDGE, key)
	_, x = launch(s)
	assert not s.query(OfferDelivery).filter_by(user_id=seed.U_REVOKER).count()
	d = policy.decide(s, PA(tool="deliver_offer", actor_type="consumer", actor_id=seed.U_REVOKER,
		idempotency_key="z", args={"offer_id": x["offer_id"], "user_id": seed.U_REVOKER, "reason": "r"}))
	assert (d.outcome, d.reason) == ("blocked", "no_offers_consent")
	privacy.set_consent(s, 1, "personalisation", False)
	assert privacy.preferences(s, 1) == []


def test_deletion_removes_from_memory_and_cohorts(s):
	privacy.set_preference(s, seed.U_REVOKER, "budget", "low")
	size = next(c["size"] for c in privacy.visible_cohorts(s, seed.M_BRIDGE) if c["key"] == "budget:low")
	privacy.delete_all(s, seed.U_REVOKER)
	assert not s.query(Preference).filter_by(user_id=seed.U_REVOKER).count()
	assert not s.query(MemoryItem).filter_by(actor=f"user:{seed.U_REVOKER}").count()
	now_size = s.query(Cohort).filter_by(merchant_id=seed.M_BRIDGE, key="budget:low").one().size
	assert now_size == size - 1


def test_small_cohorts_never_exposed_and_no_sensitive_categories(s):
	assert all(c["size"] >= settings.COHORT_MIN_SIZE
		for c in privacy.visible_cohorts(s, seed.M_SMALL_COHORT))
	r = manager.handle(s, "merchant", seed.M_SMALL_COHORT, "offer banao")
	assert r["approval"] is None
	d = policy.decide(s, PA(tool="create_offer_campaign", actor_type="merchant",
		actor_id=seed.M_SMALL_COHORT, idempotency_key="c", args={"merchant_id": seed.M_SMALL_COHORT,
			"title": "t", "cohort_key": "cuisine:south-indian", "discount_pct": 10.0, "budget_cap": 100.0}))
	assert (d.outcome, d.reason) == ("blocked", "cohort_below_minimum")
	for cat in ("religion", "health", "caste", "politics"):
		with pytest.raises(privacy.PreferenceRejected):
			privacy.set_preference(s, 1, cat, "x")


# ---------- merchant ----------
def test_w1_branches(s):
	def ask(mid):
		return manager.handle(s, "merchant", mid, "mera paisa nahi aaya")
	assert "credited" in manager.handle(s, "merchant", seed.M_BANK_FIXABLE, "haan")["reply"] \
		if ask(seed.M_BANK_FIXABLE)["approval"] else False
	ask(seed.M_BANK_UNFIXABLE)
	assert "support team" in manager.handle(s, "merchant", seed.M_BANK_UNFIXABLE, "haan")["reply"]
	assert "on the way" in ask(seed.M_SETTLING)["reply"]
	assert "manual review" in ask(seed.M_FRAUD)["reply"]


def test_w2_collects_with_holdout_and_skips_disputes(s):
	r = manager.handle(s, "merchant", seed.M_KHATA, "udhaar vasool karna hai")
	assert "disputed" in r["reply"]
	x = manager.approve(s, r["approval"]["id"], True)
	rep = flows.simulate_khata_payments(s, x["case_id"])
	assert x["sent"] == rep["reminded"] > 0 and rep["holdout"] > 0


def test_audit_log_is_append_only(s):
	manager.handle(s, "consumer", 2, "paise kat gaye")
	assert s.query(AuditLog).count() > 0
	with pytest.raises(Exception):
		s.execute(AuditLog.__table__.delete())
		s.commit()
