"""C4: manage a booking. Reschedule, modify, cancel, check status, raise a concern."""
from datetime import timedelta

from sqlalchemy.orm import Session

from .. import tools
from ..db import Approval, Booking, BookingItem, Case, Transaction, User, now
from ..policy import ProposedAction as PA
from ..policy import payload_hash
from ..services import (
	Step, close_case, create_approval, escalate, execute, impact, inbox, run_log, run_saga,
)
from ..tools import ToolError
from .flows import _esc, out


def find_booking(s: Session, user_id: int, booking_id: int | None = None) -> Booking | None:
	q = s.query(Booking).filter_by(user_id=user_id, status="confirmed")
	if booking_id:
		return q.filter_by(id=booking_id).first()
	rows = [b for b in q.order_by(Booking.id.desc()).all() if b.travel_date]
	upcoming = [b for b in rows if b.travel_date >= now().date().isoformat()]
	return (upcoming or rows or [None])[0]


def describe(q: dict) -> str:
	"""Explains a quote. Every figure here is read from the quote, none is computed."""
	parts = []
	if q["action"] == "cancel":
		parts.append(f"Cancellation fee ₹{q['fee_total']:.0f}")
	else:
		if q["fee_total"]:
			parts.append(f"change fee ₹{q['fee_total']:.0f}")
		if q["fare_diff"]:
			parts.append(f"fare difference ₹{q['fare_diff']:.0f}")
	if q["pay_extra"]:
		parts.append(f"you pay ₹{q['pay_extra']:.0f}")
	elif q["refund_total"]:
		parts.append(f"refund ₹{q['refund_total']:.0f}, {q['refund_when'].lower()}")
	else:
		parts.append("no charge")
	return "; ".join(parts) + "."


def request_change(s: Session, user: User, case: Case, action: str, booking_id: int | None = None,
		new_date: str | None = None, item_id: int | None = None,
		new_inventory_id: int | None = None, units: int | None = None) -> dict:
	b = find_booking(s, user.id, booking_id)
	if not b:
		close_case(s, case)
		return out("I could not find an active booking to change. Book a trip first, or tell me "
			"the booking number.", case)
	if action == "status":
		items = s.query(BookingItem).filter_by(booking_id=b.id).all()
		close_case(s, case)
		lines = ", ".join(f"{it.details['name']} ({it.status})" for it in items)
		return out(f"Booking {b.id}: {b.title}. {lines}.", case, booking_id=b.id)
	if action == "reschedule" and not new_date:
		close_case(s, case)
		return out(f"Which date should I move booking {b.id} to? For example: reschedule "
			f"booking {b.id} to 2026-12-05.", case, booking_id=b.id)
	req = {"booking_id": b.id, "item_id": item_id}
	try:
		if action == "cancel":
			q = tools.call(s, "quote_cancellation", req)
		else:
			q = tools.call(s, "quote_change", {**req, "new_date": new_date,
				"new_inventory_id": new_inventory_id, "units": units})
	except ToolError as e:
		close_case(s, case)
		return out(f"I cannot make that change: {e}.", case, booking_id=b.id)
	payload = {**q, "request": {**req, "new_date": new_date, "new_inventory_id": new_inventory_id,
		"units": units}}
	verb = {"cancel": "Cancel", "reschedule": f"Move to {new_date}", "modify": "Change"}[q["action"]]
	a = create_approval(s, "consumer", user.id, "change", f"{verb}: {b.title}", payload, case.id)
	case.status = "waiting"
	run_log(s, case.id, "Concierge", "decide", f"Quoted {q['action']} for booking {b.id} from the "
		f"partner policy: {describe(q)}")
	return out(f"Here is what changes for booking {b.id}. {describe(q).capitalize()} Approve to "
		"go ahead.", case, a, booking_id=b.id)


def apply(s: Session, a: Approval) -> dict:
	case, old, uid = s.get(Case, a.case_id), a.payload, a.actor_id
	req = old["request"]
	b = s.get(Booking, old["booking_id"])
	# Re-quote. A fee tier, price or availability change breaks the approval hash.
	try:
		if old["action"] == "cancel":
			q = tools.call(s, "quote_cancellation", {"booking_id": b.id, "item_id": req["item_id"]})
		else:
			q = tools.call(s, "quote_change", req)
	except ToolError as e:
		a.status = "expired"
		close_case(s, case)
		return out(f"That change is no longer possible ({e}). Nothing was charged.", case)
	fresh = {**q, "request": req}
	if payload_hash(fresh) != a.payload_hash:
		a.status = "expired"
		impact(s, "approval_hash_mismatch", "consumer", uid)
		run_log(s, case.id, "Compliance", "decide", "Quote changed after approval: hash mismatch")
		a2 = create_approval(s, "consumer", uid, "change", a.title, fresh, case.id)
		return out("The quote changed after you approved, so I did nothing. Updated quote: "
			f"{describe(q)} Approve again to go ahead.", case, a2, booking_id=b.id)

	base = dict(approval_id=a.id, approval_payload=old, case_id=case.id, actor_type="consumer",
		actor_id=uid, agent="Concierge")
	steps = []
	if q["pay_extra"]:
		steps.append(Step("pay",
			lambda r: PA(tool="pay_from_wallet", idempotency_key=f"appr{a.id}:pay",
				amount=q["pay_extra"], args={"user_id": uid, "amount": q["pay_extra"],
					"purpose": f"Trip change for booking {b.id}"}, why="Approved change", **base),
			lambda r: PA(tool="refund_payment", idempotency_key=f"appr{a.id}:unpay",
				args={"user_id": uid, "amount": q["pay_extra"], "txn_id": r["pay"]["txn_id"],
					"reason": "Change could not be completed"}, why="Saga rollback", **base)))
	for i, l in enumerate(q["lines"]):
		if q["action"] == "cancel":
			steps.append(Step(f"line{i}", lambda r, l=l, i=i: PA(tool="cancel_booking",
				idempotency_key=f"appr{a.id}:cancel{i}", args={"item_id": l["item_id"]},
				why="Approved cancellation", **base)))
		else:
			def do(r, l=l, i=i):
				return PA(tool="apply_change", idempotency_key=f"appr{a.id}:change{i}",
					args={"item_id": l["item_id"], "new_date": q["new_date"] or b.travel_date,
						"new_inventory_id": l["new_inventory_id"], "units": l["new_units"],
						"price": l["new_price"]}, why="Approved change", **base)
			def undo(r, l=l, i=i):
				return PA(tool="apply_change", idempotency_key=f"appr{a.id}:revert{i}",
					args={"item_id": l["item_id"], **r[f"line{i}"]["previous"]},
					why="Saga rollback", **base)
			steps.append(Step(f"line{i}", do, undo))
	if q["refund_total"]:
		steps.append(Step("refund", lambda r: PA(tool="refund_payment",
			idempotency_key=f"appr{a.id}:refund", amount=q["refund_total"],
			args={"user_id": uid, "amount": q["refund_total"],
				"reason": f"{q['action']} booking {b.id}"}, why="Refund from the quote", **base)))
	res = run_saga(s, steps)
	a.status = "used"

	if not res.ok:
		if res.compensation_failed or (q["action"] == "cancel" and res.results):
			return _esc(s, case, f"Booking {b.id} {q['action']} stopped at {res.failed_step}: "
				f"{res.error}", "partial_change", q["refund_total"] or q["pay_extra"])
		impact(s, "saga_recovered", "consumer", uid, q["pay_extra"], error=res.error)
		close_case(s, case)
		back = " Your payment was refunded." if "pay" in res.compensated else ""
		return out(f"The partner could not confirm the change ({res.error.replace('_', ' ')}), so "
			f"I put everything back as it was.{back} Your booking is unchanged.", case,
			booking_id=b.id)

	# Verify the final state with a tool before saying it is done.
	state = tools.call(s, "get_booking", {"booking_id": b.id})
	changed = {l["item_id"] for l in q["lines"]}
	want = "cancelled" if q["action"] == "cancel" else "confirmed"
	ok = all(it["status"] == want and (q["action"] != "reschedule"
		or it["details"].get("date") == q["new_date"]) for it in state["items"] if it["id"] in changed)
	if not ok:
		return _esc(s, case, f"Booking {b.id} state could not be verified after {q['action']}",
			"unverified_state")
	active = [it for it in state["items"] if it["status"] == "confirmed"]
	b.total = sum(it["price"] for it in active)
	if not active:
		b.status = "cancelled"
	if q["action"] == "reschedule":
		b.travel_date = q["new_date"]
		b.title = b.title.rsplit(" on ", 1)[0] + f" on {q['new_date']}"
	msg = {"cancel": "Cancelled" if not active else "Item cancelled",
		"reschedule": f"Moved to {q['new_date']}", "modify": "Changed"}[q["action"]]
	msg += f". {describe(q).capitalize()}"
	b.timeline = [*b.timeline, {"at": now().isoformat(), "event": msg}]
	inbox(s, "consumer", uid, f"Booking {b.id}: {msg}", booking_id=b.id)
	impact(s, "booking_change", "consumer", uid, q["refund_total"], action=q["action"])
	close_case(s, case)
	return out(f"Done. {msg} A confirmation is in your inbox.", case, booking_id=b.id)


def concern(s: Session, user: User, text: str, case: Case, booking_id: int | None = None) -> dict:
	"""Raise a concern with the evidence attached. Duplicate charges inside the auto limit are
	refunded on the spot; everything else goes to a person with the full packet."""
	b = find_booking(s, user.id, booking_id) or (s.query(Booking).filter_by(user_id=user.id)
		.order_by(Booking.id.desc()).first())
	txns = (s.query(Transaction).filter(Transaction.user_id == user.id,
		Transaction.kind == "booking", Transaction.status == "success",
		Transaction.created_at > now() - timedelta(days=30)).order_by(Transaction.id).all())
	dup = next((t2 for i, t1 in enumerate(txns) for t2 in txns[i + 1:]
		if t1.amount == t2.amount and t1.meta.get("purpose") == t2.meta.get("purpose")
		and abs((t2.created_at - t1.created_at).total_seconds()) < 86400), None)
	evidence = {"concern": text, "booking": b.as_dict() if b else None,
		"timeline": b.timeline if b else [],
		"payments": [{"id": t.id, "amount": t.amount, "at": t.created_at.isoformat()} for t in txns]}
	run_log(s, case.id, "Care", "detect", f"Concern on booking {b.id if b else 'none'}; "
		f"{'duplicate charge found' if dup else 'no duplicate charge'}")
	if dup:
		r = execute(s, PA(tool="refund_payment", actor_type="consumer", actor_id=user.id,
			case_id=case.id, agent="Care", idempotency_key=f"case{case.id}:dup{dup.id}",
			amount=dup.amount, args={"user_id": user.id, "amount": dup.amount, "txn_id": dup.id,
				"reason": "Duplicate charge"}, why="Charged twice for the same booking"))
		if r.ok and s.get(Transaction, dup.id).status == "refunded":
			impact(s, "consumer_auto_resolved", "consumer", user.id, dup.amount, issue="duplicate")
			inbox(s, "consumer", user.id, f"Duplicate charge of ₹{dup.amount:.0f} refunded.")
			close_case(s, case)
			return out(f"You were charged ₹{dup.amount:.0f} twice. I have refunded the second "
				"charge to your wallet.", case)
	t = execute(s, PA(tool="raise_ticket", actor_type="consumer", actor_id=user.id, case_id=case.id,
		agent="Care", idempotency_key=f"case{case.id}:ticket",
		args={"user_id": user.id, "summary": text[:200]}, why="Concern needs a person"))
	ticket = s.get(Case, t.result["ticket_id"])
	ticket.state = evidence
	e = escalate(s, case, f"Concern: {text[:160]}", "concern_needs_review",
		"Check the attached booking evidence and reply", "Thanks for flagging this. We are "
		"looking into it and will update you here.", dup.amount if dup else 0)
	return out(f"I have raised ticket {ticket.id} with your booking details and payment history "
		"attached, so you will not need to explain it again. You can track it under Trips.", case,
		ticket_id=ticket.id, escalation_id=e.id)
