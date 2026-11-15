"""Workflows: C1 trip bundle, C2 care, B1 bridge, W1 money not received, W2 collect dues.
Agents here only build ProposedActions; services.execute runs them after the policy check."""
import random
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import privacy, tools
from ..adapters.memory import memory
from ..config import settings
from ..db import (
	Approval, Booking, BookingItem, Case, Customer, ExperimentAssignment, ImpactEvent, Inventory,
	KhataEntry, Merchant, Offer, OfferDelivery, Refund, Transaction, User, Wallet, now,
)
from ..policy import ProposedAction as PA
from ..policy import payload_hash
from ..services import (
	Step, close_case, create_approval, escalate, execute, impact, inbox, run_log, run_saga,
)

SEARCH = {"bus": "search_buses", "train": "search_trains", "flight": "search_flights"}


def out(reply: str, case: Case | None = None, approval: Approval | None = None, **extra) -> dict:
	return {"reply": reply, "case_id": case.id if case else None,
		"status": case.status if case else "", "approval": approval.as_dict() if approval else None,
		**extra}


# ======================= C1: trip bundle =======================
def plan_trip(s: Session, user_id: int, c: dict, exclude: set[int] = frozenset()) -> list[dict]:
	"""Deterministic planner: ranks real fixture rows. Returns [best, alternative]."""
	prefs = {p.category: p.value for p in privacy.preferences(s, user_id)}
	nights, trav = c.get("nights") or 1, c.get("travellers") or 1
	modes = [c["mode"]] if c.get("mode") else list(SEARCH)
	legs = [r for m in modes for r in tools.call(s, SEARCH[m],
		{"origin": c["origin"], "dest": c["dest"]})["results"] if r["inventory_id"] not in exclude]
	hotels = [h for h in tools.call(s, "search_hotels", {"city": c["dest"]})["results"]
		if h["inventory_id"] not in exclude] if nights else [None]
	if not legs or not hotels:
		return []
	wp = 0.6 if prefs.get("budget") == "low" else 0.4

	def rank(rows, bonus):
		hi = max(r["price"] for r in rows) or 1
		dmax = max(r["duration_min"] for r in rows) or 1
		return sorted(rows, key=lambda r: wp * r["price"] / hi + 0.2 * r["duration_min"] / dmax
			+ 0.2 * (5 - r["rating"]) / 5 - bonus(r))

	legs = rank(legs, lambda r: 0.15 if prefs.get("travel_mode") == r["kind"] else 0)
	if nights:
		hotels = rank(hotels, lambda r: (0.1 if prefs.get("budget") == "low" and "budget" in
			r["tags"] else 0) + (0.05 if prefs.get("cuisine") == "veg" and "veg" in r["tags"] else 0))

	def build(leg, hotel):
		def item(r, units):
			return {"inventory_id": r["inventory_id"], "kind": r["kind"], "name": r["name"],
				"unit_price": r["price"], "units": units, "price": r["price"] * units}
		items = [item(leg, trav)]
		if hotel:
			items.append(item(hotel, nights))
			back = s.query(Inventory).filter(Inventory.kind == leg["kind"], Inventory.available > 0,
				Inventory.origin == c["dest"], Inventory.dest == c["origin"]).order_by(
				Inventory.price).first()
			if back and back.id not in exclude:
				items.append(item(tools._inv(back), trav))
		total = sum(i["price"] for i in items)
		why = [f"{leg['name']} ranks best on price, time and rating"]
		if prefs.get("travel_mode") == leg["kind"]:
			why.append(f"you prefer {leg['kind']} travel")
		if hotel and prefs.get("budget") == "low":
			why.append("kept the stay within a low budget")
		if c.get("budget") and total > c["budget"]:
			why.append(f"cheapest fit is ₹{total - c['budget']:.0f} over your budget")
		return {"user_id": user_id, "origin": c["origin"], "dest": c["dest"], "date": c["date"],
			"nights": nights if hotel else 0, "travellers": trav, "items": items, "total": total,
			"why": "; ".join(why)}

	plans = [build(l, h) for l in legs[:3] for h in hotels[:2]]
	if c.get("budget"):
		plans.sort(key=lambda p: p["total"] > c["budget"])  # stable: in-budget first
	best = plans[0]
	alt = next((p for p in plans[1:] if p["items"][0]["kind"] != best["items"][0]["kind"]),
		plans[1] if len(plans) > 1 else None)
	return [best] + ([alt] if alt else [])


def trip_request(s: Session, user: User, intent, case: Case) -> dict:
	c = dict(case.state.get("constraints") or {})
	for k in ("origin", "dest", "date", "nights", "budget", "travellers", "mode"):
		v = getattr(intent, k, None)
		if v and (k != "travellers" or v > 1 or k not in c):
			c[k] = v
	c.setdefault("origin", user.city)
	asked = case.state.get("asked", 0)
	missing = [k for k in ("dest", "date") if not c.get(k)]
	if c.get("dest") == c.get("origin"):
		missing = ["dest"]
	case.state = {"constraints": c, "asked": asked + (1 if missing else 0)}
	if missing and asked < 2:
		q = {"dest": "Where would you like to go?", "date": "When do you want to travel?"}
		case.status = "waiting"
		run_log(s, case.id, "Concierge", "decide", f"Missing {missing[0]}: asking one question")
		return out(q[missing[0]], case)
	if missing:
		return out("I still need the destination and date. Please share them together.", case)
	plans = plan_trip(s, user.id, c)
	if not plans:
		close_case(s, case)
		return out(f"I could not find anything from {c['origin']} to {c['dest']}.", case)
	return _offer_plan(s, case, plans, "Here is the best plan I found.")


def _offer_plan(s, case: Case, plans: list[dict], lead: str) -> dict:
	if not plans:
		return _esc(s, case, "No bookable plan left for this trip", "no_replacement")
	best = plans[0]
	a = create_approval(s, "consumer", best["user_id"], "trip",
		f"Trip {best['origin']} → {best['dest']} ₹{best['total']:.0f}", best, case.id)
	case.status = "waiting"
	case.state = {**case.state, "alternative": plans[1] if len(plans) > 1 else None}
	run_log(s, case.id, "Concierge", "decide",
		f"Planned {len(best['items'])} items, ₹{best['total']:.0f}. {best['why']}")
	return out(f"{lead} Total ₹{best['total']:.0f}. Why: {best['why']}. Approve to pay once "
		"from your wallet.", case, a, alternative=case.state["alternative"])


def execute_trip(s: Session, a: Approval) -> dict:
	case = s.get(Case, a.case_id)
	plan, uid = a.payload, a.actor_id
	# Re-quote at checkout. Any price or availability change breaks the approval hash.
	fresh = {**plan, "items": []}
	for it in plan["items"]:
		q = tools.call(s, "get_quote", {"inventory_id": it["inventory_id"], "at_checkout": True})
		if q["available"] < 1:
			fresh["items"].append({**it, "unit_price": -1})
		else:
			fresh["items"].append({**it, "unit_price": q["price"], "price": q["price"] * it["units"]})
	fresh["total"] = sum(i["price"] for i in fresh["items"])
	if payload_hash(fresh) != a.payload_hash:
		a.status = "expired"
		run_log(s, case.id, "Compliance", "decide", "Plan changed after approval: hash mismatch")
		impact(s, "approval_hash_mismatch", "consumer", uid)
		plans = plan_trip(s, uid, case.state["constraints"])
		return _offer_plan(s, case, plans, "The price or availability changed after you "
			"approved, so I did not charge you. Here is the updated plan.")

	appr = dict(approval_id=a.id, approval_payload=plan, case_id=case.id, actor_type="consumer",
		actor_id=uid, agent="Concierge")
	steps = []
	for i, it in enumerate(plan["items"]):
		steps.append(Step(f"hold{i}",
			lambda r, it=it, i=i: PA(tool="hold_booking", idempotency_key=f"appr{a.id}:hold{i}",
				args={"user_id": uid, "inventory_id": it["inventory_id"], "units": it["units"]},
				why="Hold before paying", **appr),
			lambda r, i=i: PA(tool="cancel_booking", idempotency_key=f"appr{a.id}:cancel{i}",
				args={"item_id": r[f"hold{i}"]["item_id"]}, why="Saga rollback", **appr)))
	steps.append(Step("pay",
		lambda r: PA(tool="pay_from_wallet", idempotency_key=f"appr{a.id}:pay", amount=plan["total"],
			args={"user_id": uid, "amount": plan["total"], "purpose": f"Trip to {plan['dest']}"},
			why="One payment for the approved plan", **appr),
		lambda r: PA(tool="refund_payment", idempotency_key=f"appr{a.id}:refund",
			args={"user_id": uid, "amount": plan["total"], "txn_id": r["pay"]["txn_id"],
				"reason": "Booking could not be completed"}, why="Saga rollback", **appr)))
	for i, it in enumerate(plan["items"]):
		steps.append(Step(f"confirm{i}",
			lambda r, i=i: PA(tool="confirm_booking", idempotency_key=f"appr{a.id}:confirm{i}",
				args={"item_id": r[f"hold{i}"]["item_id"]}, why="Confirm after payment", **appr)))
	res = run_saga(s, steps)
	a.status = "used"

	if res.ok:
		ids = [res.results[f"hold{i}"]["item_id"] for i in range(len(plan["items"]))]
		items = s.query(BookingItem).filter(BookingItem.id.in_(ids)).all()
		if not all(it.status == "confirmed" for it in items):  # verify before you tell
			return _esc(s, case, "Booking state could not be verified", "unverified_state",
				plan["total"])
		b = Booking(user_id=uid, status="confirmed", total=plan["total"], plan_hash=a.payload_hash,
			title=f"{plan['origin']} → {plan['dest']} on {plan['date']}")
		s.add(b)
		s.flush()
		for it in items:
			it.booking_id = b.id
		impact(s, "trip_booked", "consumer", uid, plan["total"], booking_id=b.id)
		memory.add(s, f"user:{uid}", "booking", f"Booked {b.title} for ₹{b.total:.0f}")
		close_case(s, case)
		return out(f"Booked. {len(items)} items confirmed for ₹{plan['total']:.0f}. "
			"Your trip is in Bookings.", case, booking_id=b.id)

	if res.compensation_failed:
		return _esc(s, case, f"Rollback incomplete after {res.error}", "compensation_failed",
			plan["total"])
	if res.error in ("insufficient_balance", "daily_spend_cap"):
		close_case(s, case)
		msg = {"insufficient_balance": "your wallet balance is too low",
			"daily_spend_cap": "this would cross your daily spend limit"}[res.error]
		return out(f"I did not book anything because {msg}. Nothing was charged.", case)
	impact(s, "saga_recovered", "consumer", uid, plan["total"], error=res.error)
	bad = plan["items"][int(res.failed_step[-1])] if res.failed_step[-1].isdigit() else None
	plans = plan_trip(s, uid, case.state["constraints"], {bad["inventory_id"]} if bad else set())
	refunded = " Your payment was refunded in full." if "pay" in res.compensated else ""
	if not plans:
		return _esc(s, case, f"{res.error} and no replacement available", "no_replacement",
			plan["total"])
	return _offer_plan(s, case, plans, f"{bad['name'] if bad else 'An item'} became unavailable "
		f"during checkout, so I cancelled the rest.{refunded} Here is a replacement.")


def _esc(s, case, issue, why, amount=0, reply="I have passed this to our support team with "
		"all the details. They will get back to you shortly.") -> dict:
	e = escalate(s, case, issue, why, "Review and resolve manually", reply, amount)
	return out(reply, case, escalation_id=e.id)


# ======================= C2: failed payment / refund =======================
def care(s: Session, user: User, intent, case: Case) -> dict:
	uid = user.id
	has_refund = {r.txn_id for r in s.query(Refund).filter_by(user_id=uid).all()}
	bad = [t for t in s.query(Transaction).filter(Transaction.user_id == uid,
		Transaction.status == "success", Transaction.order_state.in_(("missing", "failed")))
		.order_by(Transaction.id.desc()).all() if t.id not in has_refund]
	pending = s.query(Refund).filter_by(user_id=uid, status="pending").all()
	base = dict(actor_type="consumer", actor_id=uid, case_id=case.id, agent="Care")
	run_log(s, case.id, "Care", "detect", f"{len(bad)} failed payment(s), {len(pending)} "
		"pending refund(s)")

	if bad:
		t = bad[0]
		risk = list(set(intent.risk) | ({t.meta["risk"]} if t.meta.get("risk") else set()))
		if t.kind == "recharge" and not risk:
			r = execute(s, PA(tool="mobile_recharge", idempotency_key=f"case{case.id}:retry{t.id}",
				args={"user_id": uid, "operator": t.meta.get("operator", ""), "amount": t.amount,
					"prepaid_txn_id": t.id}, why="Debited but recharge failed: retry", **base))
			if r.ok and s.get(Transaction, t.id).order_state == "fulfilled":
				impact(s, "consumer_auto_resolved", "consumer", uid, t.amount, issue="recharge")
				close_case(s, case)
				return out(f"Your ₹{t.amount:.0f} recharge had failed after the debit. I retried "
					"it and it is now done. You were not charged again.", case)
		r = execute(s, PA(tool="refund_payment", idempotency_key=f"case{case.id}:refund{t.id}",
			amount=t.amount, risk=risk, confidence=intent.confidence,
			args={"user_id": uid, "amount": t.amount, "txn_id": t.id,
				"reason": "Debited but order not fulfilled"}, why="Auto-refund within limit", **base))
		if r.ok and s.get(Transaction, t.id).status == "refunded":
			impact(s, "consumer_auto_resolved", "consumer", uid, t.amount, issue="debit_no_order")
			close_case(s, case)
			return out(f"₹{t.amount:.0f} was debited but the order did not go through. I have "
				f"refunded it to your wallet. New balance ₹{r.result['balance']:.0f}.", case)
		return _esc(s, case, f"₹{t.amount:.0f} debited, order {t.order_state}. {t.meta.get('note', '')}",
			r.decision.reason, t.amount, "This one needs a specialist. I have shared the full "
			"details with our support team, so you will not have to repeat yourself.")

	if pending:
		rf = pending[0]
		st = tools.call(s, "get_refund_status", {"refund_id": rf.id})
		if not st["past_sla"]:
			execute(s, PA(tool="notify", idempotency_key=f"case{case.id}:eta{rf.id}",
				args={"actor_type": "consumer", "actor_id": uid,
					"message": f"Refund of ₹{rf.amount:.0f} expected within {st['eta_days']} day(s). "
						"I will tell you when it lands."}, why="Inside SLA", **base))
			impact(s, "consumer_auto_resolved", "consumer", uid, 0, issue="refund_eta")
			close_case(s, case)
			return out(f"Your ₹{rf.amount:.0f} refund is in progress and inside the {st['sla_days']}"
				f"-day window. Expected in about {st['eta_days']} day(s). I will notify you.", case)
		r = execute(s, PA(tool="retry_refund", idempotency_key=f"case{case.id}:retryrf{rf.id}",
			amount=rf.amount, args={"refund_id": rf.id}, why="Refund past SLA", **base))
		if r.ok and tools.call(s, "get_refund_status", {"refund_id": rf.id})["status"] == "processed":
			impact(s, "consumer_auto_resolved", "consumer", uid, rf.amount, issue="refund_late")
			close_case(s, case)
			return out(f"Your ₹{rf.amount:.0f} refund was past its {st['sla_days']}-day window. I "
				"pushed it through and it is now in your wallet.", case)
		return _esc(s, case, f"Refund ₹{rf.amount:.0f} pending {st['age_days']} days, retry did "
			"not clear it", "refund_past_sla_retry_failed", rf.amount)

	close_case(s, case)
	return out("I checked your recent payments and refunds and everything looks settled.", case)


# ======================= B1: bridge =======================
def draft_offer(s: Session, m: Merchant, case: Case) -> dict:
	cohorts = privacy.visible_cohorts(s, m.id)
	run_log(s, case.id, "Personalisation", "detect", f"Found {len(cohorts)} nearby customer "
		f"group(s) of {settings.COHORT_MIN_SIZE} or more people who opted in")
	if not cohorts:
		close_case(s, case)
		return out("There are not enough nearby users who opted in to offers yet, so I cannot "
			"suggest a targeted offer. No customer details are shared with you at any point.", case)
	c = cohorts[0]
	avg = s.query(func.avg(Transaction.amount)).filter(Transaction.merchant_id == m.id,
		Transaction.kind == "sale", Transaction.created_at > now() - timedelta(days=30)).scalar()
	avg = round(avg or 200)
	treated = round(c["size"] * (100 - settings.HOLDOUT_PERCENT) / 100)
	expected = max(round(treated * 0.25), 1)
	draft = {"merchant_id": m.id, "title": f"15% off on bills above ₹{avg}", "cohort_key": c["key"],
		"discount_pct": 15.0, "min_bill": float(avg), "valid_days": 7,
		"budget_cap": float(round(expected * avg * 0.15 * 2)), "expected_redemptions": expected,
		"assumption": f"Assumes 1 in 4 of the {treated} users who get it will redeem. "
			"This is an assumption, not a measurement."}
	a = create_approval(s, "merchant", m.id, "offer", draft["title"], draft, case.id)
	case.status = "waiting"
	return out(f"{c['description']}. I suggest: {draft['title']}, valid 7 days, budget cap "
		f"₹{draft['budget_cap']:.0f}, about {expected} redemptions expected. {draft['assumption']} "
		"Approve to fund and send it.", case, a, cohorts=cohorts)


def launch_offer(s: Session, a: Approval) -> dict:
	case, d = s.get(Case, a.case_id), a.payload
	r = execute(s, PA(tool="create_offer_campaign", args=d, actor_type="merchant", actor_id=a.actor_id,
		idempotency_key=f"appr{a.id}:offer", amount=d["budget_cap"], approval_id=a.id,
		approval_payload=d, case_id=case.id, agent="Seller", why="Merchant approved the offer"))
	a.status = "used"
	if not r.ok:
		close_case(s, case)
		return out(f"I could not launch the offer ({r.error}).", case)
	oid = r.result["offer_id"]
	rnd = random.Random(settings.SEED_RANDOM + oid)
	phrase = d["cohort_key"].replace("+", " and ").replace(":", " ")
	sent = held = skipped = 0
	for uid in privacy.cohort_members(s, a.actor_id, d["cohort_key"]):
		grp = "holdout" if rnd.random() * 100 < settings.HOLDOUT_PERCENT else "treatment"
		s.add(ExperimentAssignment(experiment=f"offer:{oid}", subject_type="user", subject_id=uid,
			group=grp))
		s.flush()
		if grp == "holdout":
			held += 1
			continue
		dr = execute(s, PA(tool="deliver_offer", actor_type="consumer", actor_id=uid, case_id=case.id,
			idempotency_key=f"offer{oid}:deliver{uid}", agent="Personalisation",
			args={"offer_id": oid, "user_id": uid, "reason": "You turned on offers and your "
				f"saved preferences ({phrase}) match this shop near you."}, why="Consented match"))
		sent, skipped = sent + dr.ok, skipped + (not dr.ok)
	impact(s, "offer_launched", "merchant", a.actor_id, d["budget_cap"], offer_id=oid, sent=sent,
		holdout=held)
	close_case(s, case)
	return out(f"Offer is live. Sent to {sent} matching users who opted in; {held} held back as "
		"a comparison group so we can measure real uplift.", case, offer_id=oid, sent=sent,
		holdout=held, skipped=skipped)


def redeem_request(s: Session, user_id: int, offer_id: int, bill: float) -> dict:
	o = s.get(Offer, offer_id)
	case = _case(s, "consumer", user_id, "B1-redeem", f"Redeem offer {offer_id}")
	pay = round(bill * (1 - o.discount_pct / 100), 2)
	payload = {"user_id": user_id, "offer_id": offer_id, "bill_amount": bill, "pay": pay}
	a = create_approval(s, "consumer", user_id, "redeem", f"Pay ₹{pay:.0f} (bill ₹{bill:.0f}, "
		f"{o.discount_pct:.0f}% off)", payload, case.id)
	case.status = "waiting"
	return out(f"Bill ₹{bill:.0f}, you pay ₹{pay:.0f} after the offer. Approve to pay.", case, a)


def redeem(s: Session, a: Approval) -> dict:
	case, p = s.get(Case, a.case_id), a.payload
	r = execute(s, PA(tool="redeem_offer", actor_type="consumer", actor_id=a.actor_id,
		args={"user_id": p["user_id"], "offer_id": p["offer_id"], "bill_amount": p["bill_amount"]},
		idempotency_key=f"appr{a.id}:redeem", amount=p["pay"], approval_id=a.id, approval_payload=p,
		case_id=case.id, agent="Concierge", why="User approved the payment"))
	a.status = "used"
	close_case(s, case)
	if not r.ok:
		return out(f"I could not redeem this offer ({r.error}). Nothing was charged.", case)
	impact(s, "offer_redeemed", "consumer", a.actor_id, r.result["paid"], offer_id=p["offer_id"],
		discount=r.result["discount"])
	return out(f"Paid ₹{r.result['paid']:.0f}. You saved ₹{r.result['discount']:.0f}.", case)


def simulate_offer_response(s: Session, offer_id: int) -> dict:
	"""SIMULATED user behaviour so the funnel has something to measure. Rates below are
	simulation parameters, not claims: treated users redeem at 30%, holdout users buy at
	full price at 12%, and 3% of treated users opt out."""
	o = s.get(Offer, offer_id)
	rnd = random.Random(settings.SEED_RANDOM * 7 + offer_id)
	for d in s.query(OfferDelivery).filter_by(offer_id=offer_id, status="delivered").all():
		x, bill = rnd.random(), round(o.min_bill * rnd.uniform(1.0, 1.6))
		if x < 0.30 and privacy.consent_state(s, d.user_id)["offers"]:
			try:
				res = tools.call(s, "redeem_offer", {"user_id": d.user_id, "offer_id": offer_id,
					"bill_amount": bill})
				impact(s, "offer_redeemed", "consumer", d.user_id, res["paid"], offer_id=offer_id,
					discount=res["discount"], simulated=True)
			except tools.ToolError:
				pass
		elif x > 0.97:
			privacy.set_consent(s, d.user_id, "offers", False)
	for e in s.query(ExperimentAssignment).filter_by(experiment=f"offer:{offer_id}",
			group="holdout").all():
		if rnd.random() < 0.12:
			bill = round(o.min_bill * rnd.uniform(1.0, 1.6))
			s.add(Transaction(user_id=e.subject_id, merchant_id=o.merchant_id, amount=bill,
				kind="sale", meta={"holdout_of": offer_id}))
			impact(s, "holdout_purchase", "consumer", e.subject_id, bill, "holdout",
				offer_id=offer_id, simulated=True)
	s.commit()
	return bridge_report(s, offer_id)


def _events(s, kind, offer_id):
	return [e for e in s.query(ImpactEvent).filter_by(kind=kind).all()
		if e.meta.get("offer_id") == offer_id]


def bridge_report(s: Session, offer_id: int) -> dict:
	o = s.get(Offer, offer_id)
	q = s.query(ExperimentAssignment).filter_by(experiment=f"offer:{offer_id}")
	n_h = q.filter_by(group="holdout").count()
	matched = q.count()
	n_t = s.query(OfferDelivery).filter_by(offer_id=offer_id).count()
	red = _events(s, "offer_redeemed", offer_id)
	hold = _events(s, "holdout_purchase", offer_id)
	opt = len(_events(s, "offer_opt_out", offer_id))
	rate_t = len(red) / n_t if n_t else 0
	rate_h = len(hold) / n_h if n_h else 0
	rev_t, rev_h = sum(e.amount for e in red), sum(e.amount for e in hold)
	inc = rev_t - (rev_h / n_h) * n_t if n_h and n_t else None
	cohort = privacy.compute_cohorts(s, o.merchant_id)
	size = next((c.size for c in cohort if c.key == o.cohort_key), 0)
	return {"offer_id": offer_id, "title": o.title, "cohort_size_now": size, "matched": matched,
		"delivered": n_t, "holdout": n_h, "redeemed": len(red), "holdout_purchases": len(hold),
		"redemption_rate": round(rate_t, 3), "holdout_rate": round(rate_h, 3),
		"uplift_points": round((rate_t - rate_h) * 100, 1), "treatment_revenue": rev_t,
		"holdout_revenue": rev_h, "incremental_inr": None if inc is None else round(inc),
		"discount_cost": o.spent, "opt_outs": opt, "opt_out_rate": round(opt / n_t, 3) if n_t else 0,
		"note": "Simulated data. Incremental ₹ = treated revenue minus what the holdout rate "
			"predicts for the same number of users."}


# ======================= W1: money not received =======================
def money_not_received(s: Session, m: Merchant, case: Case) -> dict:
	st = tools.call(s, "get_settlement_status", {"merchant_id": m.id})
	dev = tools.call(s, "get_device_status", {"merchant_id": m.id})
	run_log(s, case.id, "Diagnoser", "detect", f"Settlement {st['status']} "
		f"{st['failure_reason']}; bank {m.bank_status}; device {dev['status']}")
	amt = st["amount"]
	if m.risk_flag or st["failure_reason"] == "risk_hold":
		return _esc(s, case, f"Settlement ₹{amt:.0f} on hold ({st['failure_reason']})",
			f"risk:{m.risk_flag or 'risk_hold'}", amt, "Your settlement needs a manual review. "
			"I have sent the details to the support team and they will call you.")
	if st["status"] == "processing":
		execute(s, PA(tool="notify", actor_type="merchant", actor_id=m.id, case_id=case.id,
			idempotency_key=f"case{case.id}:eta", agent="Diagnoser", why="Settlement in progress",
			args={"actor_type": "merchant", "actor_id": m.id,
				"message": f"₹{amt:.0f} will reach your bank in about {st['eta_hours']} hours."}))
		close_case(s, case)
		impact(s, "merchant_auto_resolved", "merchant", m.id, amt, issue="settlement_eta")
		return out(f"Your ₹{amt:.0f} is on the way. It should reach your bank in about "
			f"{st['eta_hours']} hours. I will tell you when it is credited.", case)
	if st["status"] == "failed" and m.bank_status != "ok":
		payload = {"merchant_id": m.id, "settlement_id": st["id"], "amount": amt,
			"account_holder_name": m.name}
		a = create_approval(s, "merchant", m.id, "bank_fix", "Update bank name and retry "
			f"₹{amt:.0f}", payload, case.id)
		case.status = "waiting"
		return out(f"₹{amt:.0f} did not reach you because the name on your bank account does "
			f"not match. Shall I update it to '{m.name}' and send the money again? Say haan or "
			"nahi.", case, a)
	if dev["status"] == "offline":
		execute(s, PA(tool="raise_device_ticket", actor_type="merchant", actor_id=m.id,
			case_id=case.id, idempotency_key=f"case{case.id}:device", agent="Fixer",
			args={"merchant_id": m.id, "issue": "Soundbox offline"}, why="Device offline"))
		close_case(s, case)
		return out("Your payments are settled. Your Soundbox is offline, which is why you did "
			"not hear the alerts. I have raised a device ticket.", case)
	close_case(s, case)
	return out(f"Your last settlement of ₹{amt:.0f} is credited. Nothing is stuck.", case)


def fix_bank(s: Session, a: Approval) -> dict:
	case, p = s.get(Case, a.case_id), a.payload
	base = dict(actor_type="merchant", actor_id=a.actor_id, case_id=case.id, agent="Fixer",
		approval_id=a.id, approval_payload=p)
	r1 = execute(s, PA(tool="request_bank_detail_update", idempotency_key=f"appr{a.id}:bank",
		args={"merchant_id": p["merchant_id"], "account_holder_name": p["account_holder_name"]},
		why="Merchant confirmed the name", **base))
	a.status = "used"
	if r1.ok:
		r2 = execute(s, PA(tool="retry_settlement", idempotency_key=f"appr{a.id}:retry",
			args={"settlement_id": p["settlement_id"]}, why="Bank fixed: retry", **base))
		st = tools.call(s, "get_settlement_status", {"merchant_id": a.actor_id})
		if r2.ok and st["status"] == "settled":
			impact(s, "merchant_auto_resolved", "merchant", a.actor_id, p["amount"],
				issue="settlement_fixed")
			close_case(s, case)
			return out(f"Done. Bank details updated and ₹{p['amount']:.0f} is credited.", case)
	return _esc(s, case, f"Bank mismatch not fixable in one step; ₹{p['amount']:.0f} stuck",
		r1.error or "retry_failed", p["amount"], "The bank needs a document check for this. I "
		"have sent everything to the support team, so you will not need to explain it again.")


# ======================= W2: collect dues =======================
def collect_dues(s: Session, m: Merchant, case: Case) -> dict:
	rows = tools.call(s, "list_overdue_khata", {"merchant_id": m.id})["entries"]
	disputed = [r for r in rows if r["status"] == "disputed"]
	rows = sorted([r for r in rows if r["status"] == "open"],
		key=lambda r: -(r["amount"] * r["reliability"]))
	if not rows:
		close_case(s, case)
		return out("No overdue Khata accounts right now.", case)
	rnd = random.Random(settings.SEED_RANDOM + case.id)
	entries = []
	for r in rows:
		grp = "holdout" if rnd.random() * 100 < settings.HOLDOUT_PERCENT else "treatment"
		s.add(ExperimentAssignment(experiment=f"khata:{case.id}", subject_type="entry",
			subject_id=r["entry_id"], group=grp))
		if grp == "treatment":
			entries.append({"entry_id": r["entry_id"], "customer": r["customer"],
				"amount": r["amount"], "days_overdue": r["days_overdue"]})
	plan = {"merchant_id": m.id, "entries": entries, "total": sum(e["amount"] for e in entries)}
	run_log(s, case.id, "Collector", "decide", f"{len(entries)} reminders planned, "
		f"{len(rows) - len(entries)} held out, {len(disputed)} disputed left for the merchant")
	note = (f" {len(disputed)} disputed entry is left for you to sort out directly."
		if disputed else "")
	if m.tier == "T1":  # low-risk action, L2 for Soundbox-only merchants
		res = run_collection(s, m.id, case, plan, None)
		res["reply"] += note
		return res
	a = create_approval(s, "merchant", m.id, "khata", f"Send {len(entries)} reminders for "
		f"₹{plan['total']:.0f}", plan, case.id)
	case.status = "waiting"
	return out(f"{len(rows)} accounts are overdue. I will send polite reminders with pay links "
		f"to {len(entries)} of them for ₹{plan['total']:.0f}, best chances first.{note} "
		"Approve once and I will handle the rest.", case, a, plan=plan)


def run_collection(s: Session, mid: int, case: Case, plan: dict, a: Approval | None) -> dict:
	base = dict(actor_type="merchant", actor_id=mid, case_id=case.id, agent="Collector",
		approval_id=a.id if a else None, approval_payload=plan if a else None)
	sent = 0
	for e in plan["entries"]:
		k = f"case{case.id}:e{e['entry_id']}"
		l = execute(s, PA(tool="create_payment_link", idempotency_key=f"{k}:link",
			amount=e["amount"], args={"merchant_id": mid, "amount": e["amount"],
				"purpose": "Khata dues", "entry_id": e["entry_id"]}, why="Pay link", **base))
		if l.ok:
			r = execute(s, PA(tool="send_khata_reminder", idempotency_key=f"{k}:remind",
				args={"entry_id": e["entry_id"]}, why="Approved reminder plan", **base))
			sent += r.ok
	if a:
		a.status = "used"
	impact(s, "khata_reminders_sent", "merchant", mid, plan["total"], count=sent, case_id=case.id)
	close_case(s, case)
	return out(f"Sent {sent} reminders with payment links. I will update your ledger as "
		"payments come in.", case, sent=sent)


def simulate_khata_payments(s: Session, case_id: int) -> dict:
	"""SIMULATED customer behaviour: reminded accounts pay with probability 0.7 × reliability,
	held-out accounts with 0.25 × reliability."""
	rnd = random.Random(settings.SEED_RANDOM * 3 + case_id)
	stats = {"treatment": [0, 0, 0.0, 0.0], "holdout": [0, 0, 0.0, 0.0]}  # n, paid, due, recovered
	for x in s.query(ExperimentAssignment).filter_by(experiment=f"khata:{case_id}").all():
		k = s.get(KhataEntry, x.subject_id)
		c = s.get(Customer, k.customer_id)
		st = stats[x.group]
		st[0] += 1
		st[2] += k.amount
		if k.status == "open" and rnd.random() < c.reliability * (0.7 if x.group == "treatment"
				else 0.25):
			k.status, k.paid_at = "paid", now()
			s.add(Transaction(merchant_id=k.merchant_id, customer_id=c.id, amount=k.amount,
				kind="khata"))
			impact(s, "khata_recovered", "merchant", k.merchant_id, k.amount, x.group,
				case_id=case_id, simulated=True)
		if k.status == "paid":
			st[1] += 1
			st[3] += k.amount
	s.commit()
	t, h = stats["treatment"], stats["holdout"]
	rt, rh = (t[3] / t[2] if t[2] else 0), (h[3] / h[2] if h[2] else 0)
	return {"case_id": case_id, "reminded": t[0], "reminded_paid": t[1], "recovered_inr": t[3],
		"holdout": h[0], "holdout_paid": h[1], "holdout_recovered_inr": h[3],
		"recovery_rate": round(rt, 3), "holdout_recovery_rate": round(rh, 3),
		"incremental_inr": round(t[3] - rh * t[2]), "note": "Simulated data."}


def _case(s, actor_type, actor_id, workflow, summary) -> Case:
	from ..services import open_case
	return open_case(s, actor_type, actor_id, workflow, summary)


def wallet(s: Session, user_id: int) -> float:
	return s.get(Wallet, user_id).balance
