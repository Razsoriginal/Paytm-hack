"""Manager agent: understands the message, loads memory, routes by actor type and intent."""
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import privacy
from ..adapters.llm import llm
from ..adapters.memory import memory
from ..adapters.speech import speech
from ..db import Approval, Case, Merchant, Notification, Transaction, User, now
from ..policy import ProposedAction as PA
from ..services import audit, close_case, create_approval, decide_approval, execute, open_case, run_log
from . import flows, insights, manage, stock
from .flows import out

HELP = {
	"consumer": "I can plan and book a trip, do a recharge or bill, fix a failed payment or "
		"late refund, and show offers you opted in to. What do you need?",
	"merchant": "I can check money that has not reached you, collect Khata dues, and suggest "
		"an offer for nearby customers. What do you need?",
}
WORKFLOW = {"trip": "C1", "payment_issue": "C2", "refund_status": "C2", "win_back": "B1",
	"booking_change": "C4", "concern": "C4", "spend": "C5", "budget": "C5", "inventory": "M1",
	"offers": "B1", "money_not_received": "W1", "device_issue": "W1", "collect_dues": "W2"}


def dispatch(s: Session, a: Approval) -> dict:
	"""Continue the workflow an approval belongs to."""
	case = s.get(Case, a.case_id)
	if a.status == "used":
		return out("That was already done. Nothing was charged twice.", case)
	if a.status != "approved":
		if case and case.status == "waiting":
			close_case(s, case)
		msg = {"rejected": "Okay, cancelled. Nothing was charged.",
			"expired": "That approval expired, so I did nothing. Ask me again to get a fresh one."}
		return out(msg.get(a.status, "Nothing was done."), case)
	run_log(s, a.case_id, "Approval", "approve", f"Approved via {a.decided_via}: {a.title}")
	if a.kind == "trip":
		return flows.execute_trip(s, a)
	if a.kind == "change":
		return manage.apply(s, a)
	if a.kind == "stock_in":
		return stock.apply_upload(s, a)
	if a.kind == "bill":
		from .. import bills
		return bills.pay(s, a)
	if a.kind == "offer":
		return flows.launch_offer(s, a)
	if a.kind == "redeem":
		return flows.redeem(s, a)
	if a.kind == "bank_fix":
		return flows.fix_bank(s, a)
	if a.kind == "khata":
		return flows.run_collection(s, a.actor_id, case, a.payload, a)
	if a.kind == "payment":
		p = a.payload
		r = execute(s, PA(tool=p["tool"], args=p["args"], amount=p["amount"], actor_type="consumer",
			actor_id=a.actor_id, idempotency_key=f"appr{a.id}:pay", approval_id=a.id,
			approval_payload=p, case_id=a.case_id, agent="Concierge", why="User approved"))
		a.status = "used"
		close_case(s, case)
		if r.ok:
			return out(f"Done. ₹{p['amount']:.0f} paid. Reference {r.result['txn_id']}.", case)
		return out(f"I could not complete it ({r.error}). Nothing was charged.", case)
	return out("Nothing to do.", case)


def approve(s: Session, approval_id: int, yes: bool, via: str = "button") -> dict:
	a = decide_approval(s, approval_id, yes, via)
	res = dispatch(s, a)
	s.commit()
	return res


def _payment(s, user: User, intent, case: Case) -> dict:
	if intent.intent == "recharge":
		if not intent.operator or not intent.amount:
			close_case(s, case)
			return out("Which operator and amount? For example: Jio 299 recharge.", case)
		tool, args = "mobile_recharge", {"user_id": user.id, "operator": intent.operator,
			"amount": intent.amount}
		title = f"{intent.operator} recharge ₹{intent.amount:.0f}"
	else:
		if not intent.biller or not intent.amount:
			close_case(s, case)
			return out("Which bill and amount? For example: electricity bill 1120.", case)
		tool, args = "pay_bill", {"user_id": user.id, "biller": intent.biller,
			"amount": intent.amount}
		title = f"{intent.biller} ₹{intent.amount:.0f}"
	a = create_approval(s, "consumer", user.id, "payment", title,
		{"tool": tool, "args": args, "amount": intent.amount}, case.id)
	case.status = "waiting"
	return out(f"{title} from your wallet. Approve to pay.", case, a)


def handle(s: Session, actor_type: str, actor_id: int, text: str, language: str = "") -> dict:
	actor = s.get(User if actor_type == "consumer" else Merchant, actor_id)
	if not actor:
		return out("I could not find that account.")
	lang = language or actor.language
	english = speech.translate(text, "en-IN")["text"] if lang != "en-IN" else text
	try:
		intent = llm.understand(english, actor_type)
	except Exception:  # invalid model output is rejected, never executed
		return out("Sorry, I did not understand that. Could you say it another way?")
	who = f"{actor_type}:{actor_id}"
	audit(s, who, "message", f"intent={intent.intent} conf={intent.confidence:.2f} "
		f"via={llm.last_provider}", original_text=text, translated_text=english)
	recalled = memory.search(s, f"{'user' if actor_type == 'consumer' else 'merchant'}:{actor_id}",
		english, 3)

	pending = (s.query(Approval).filter_by(actor_type=actor_type, actor_id=actor_id,
		status="pending").order_by(Approval.id.desc()).first())
	if pending and intent.intent in ("approve", "reject"):
		res = approve(s, pending.id, intent.intent == "approve", "voice")
		return _finish(s, res, lang, intent)

	waiting = None
	if actor_type == "consumer" and not pending:
		waiting = (s.query(Case).filter_by(actor_type="consumer", actor_id=actor_id, workflow="C1",
			status="waiting").order_by(Case.id.desc()).first())
	name = intent.intent
	if waiting and name in ("trip", "unknown", "smalltalk", "approve", "reject"):
		case, name = waiting, "trip"
		case.status = "open"
	elif name in WORKFLOW or name in ("recharge", "bill"):
		if name == "offers" and actor_type == "consumer":
			n = s.query(Notification).filter_by(actor_type="consumer", actor_id=actor_id).count()
			return _finish(s, out(f"You have {n} item(s) in your inbox. Offers only show up if "
				"you have turned on the offers setting."), lang, intent)
		case = open_case(s, actor_type, actor_id, WORKFLOW.get(name, "PAY"), english[:200])
		run_log(s, case.id, "Manager", "detect", f"Understood the message as "
			f"'{name.replace('_', ' ')}', {intent.confidence:.0%} sure. Recalled {len(recalled)} "
			"memory item(s)")
	else:
		case = None

	if actor_type == "consumer":
		if intent.stated_preferences and privacy.consent_state(s, actor_id)["personalisation"]:
			for k, v in intent.stated_preferences.items():
				try:
					privacy.set_preference(s, actor_id, k, v, "stated")
				except privacy.PreferenceRejected:
					pass
		if name == "trip":
			res = flows.trip_request(s, actor, intent, case)
		elif name in ("payment_issue", "refund_status"):
			res = flows.care(s, actor, intent, case)
		elif name in ("recharge", "bill"):
			res = _payment(s, actor, intent, case)
		elif name == "booking_change":
			res = manage.request_change(s, actor, case, intent.action or "status",
				intent.booking_id, intent.date)
		elif name == "concern":
			res = manage.concern(s, actor, english, case, intent.booking_id)
		elif name in ("spend", "budget"):
			res = insights.answer(s, actor, intent, case)
		elif name == "wallet":
			res = out(f"Your wallet balance is ₹{flows.wallet(s, actor_id):.0f}.")
		elif intent.risk:
			case = open_case(s, actor_type, actor_id, "C2", english[:200])
			res = flows.care(s, actor, intent, case)
		else:
			res = out(HELP["consumer"])
	else:
		if name in ("money_not_received", "device_issue"):
			res = flows.money_not_received(s, actor, case)
		elif name == "collect_dues":
			res = flows.collect_dues(s, actor, case)
		elif name == "inventory":
			res = stock.handle(s, actor, intent, case)
		elif name in ("win_back", "offers"):
			res = flows.draft_offer(s, actor, case)
		elif name == "summary":
			def week(a, b):
				return s.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
					Transaction.merchant_id == actor_id, Transaction.kind == "sale",
					Transaction.created_at > now() - timedelta(days=a),
					Transaction.created_at <= now() - timedelta(days=b)).scalar()
			this, last = week(7, 0), week(14, 7)
			res = out(f"Sales this week ₹{this:.0f}, last week ₹{last:.0f}.")
		else:
			res = out(HELP["merchant"])
	return _finish(s, res, lang, intent)


def _finish(s: Session, res: dict, lang: str, intent) -> dict:
	s.commit()
	res["intent"] = intent.intent
	res["reply_en"] = res["reply"]
	if lang != "en-IN":
		res["reply"] = speech.translate(res["reply"], lang, "en-IN")["text"]
	res["language"] = lang
	return res
