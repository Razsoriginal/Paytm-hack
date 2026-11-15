"""Typed mock tools over the simulated DB. Agents never call these directly: the action
executor does, after the policy engine has said `execute`."""
import inspect
import random
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, create_model
from sqlalchemy.orm import Session

from .config import settings
from .db import (
	BookingItem, Booking, Case, Customer, Device, Inventory, KhataEntry, Merchant, Notification,
	Offer, OfferDelivery, Refund, Settlement, Transaction, User, Wallet, now,
)


class ToolError(Exception):
	def __init__(self, code: str, message: str = "", retryable: bool = False):
		super().__init__(message or code)
		self.code, self.retryable = code, retryable


@dataclass
class ToolSpec:
	name: str
	fn: Callable[..., dict]
	args_model: type[BaseModel]
	side_effect: bool
	money: str  # "" | "debit" (takes the user's money) | "credit" (returns money)


REGISTRY: dict[str, ToolSpec] = {}
_flaky = random.Random(settings.SEED_RANDOM)


def tool(side_effect: bool = False, money: str = ""):
	def deco(fn):
		fields = {
			n: (p.annotation, ... if p.default is inspect.Parameter.empty else p.default)
			for n, p in list(inspect.signature(fn).parameters.items())[1:]
		}
		model = create_model(f"{fn.__name__}_args", __config__=ConfigDict(extra="forbid"), **fields)
		REGISTRY[fn.__name__] = ToolSpec(fn.__name__, fn, model, side_effect, money)
		return fn
	return deco


def call(s: Session, name: str, args: dict) -> dict:
	"""Validate args against the tool's schema, then run it. Raises ToolError on failure."""
	spec = REGISTRY[name]
	parsed = spec.args_model(**args)
	if spec.side_effect and _flaky.random() < settings.MOCK_PARTNER_FAILURE_RATE:
		raise ToolError("partner_timeout", "Simulated partner timeout", retryable=True)
	return spec.fn(s, **parsed.model_dump())


def _inv(i: Inventory, units: int = 1) -> dict:
	return {"inventory_id": i.id, "kind": i.kind, "name": i.name, "origin": i.origin,
		"dest": i.dest, "price": i.price, "duration_min": i.duration_min, "rating": i.rating,
		"available": i.available, "tags": i.tags}


def _search(s: Session, kind: str, origin: str, dest: str, max_price: float | None) -> dict:
	q = s.query(Inventory).filter(Inventory.kind == kind, Inventory.available > 0,
		Inventory.dest == dest)
	if origin:
		q = q.filter(Inventory.origin == origin)
	if max_price:
		q = q.filter(Inventory.price <= max_price)
	return {"results": [_inv(i) for i in q.order_by(Inventory.price).all()]}


# ---------------- consumer ----------------
@tool()
def search_buses(s, origin: str, dest: str, max_price: float | None = None) -> dict:
	return _search(s, "bus", origin, dest, max_price)


@tool()
def search_trains(s, origin: str, dest: str, max_price: float | None = None) -> dict:
	return _search(s, "train", origin, dest, max_price)


@tool()
def search_flights(s, origin: str, dest: str, max_price: float | None = None) -> dict:
	return _search(s, "flight", origin, dest, max_price)


@tool()
def search_hotels(s, city: str, max_price: float | None = None) -> dict:
	return _search(s, "hotel", "", city, max_price)


@tool()
def get_quote(s, inventory_id: int, at_checkout: bool = False) -> dict:
	"""Live price and availability. A planted `price_jump` item reprices once at checkout."""
	i = s.get(Inventory, inventory_id)
	if not i:
		raise ToolError("not_found")
	if at_checkout and i.fail_mode == "price_jump":
		i.price, i.fail_mode = round(i.price * 1.18), ""
	return _inv(i)


@tool()
def get_wallet_balance(s, user_id: int) -> dict:
	w = s.get(Wallet, user_id)
	if not w:
		raise ToolError("not_found")
	return {"user_id": user_id, "balance": w.balance}


@tool(side_effect=True)
def hold_booking(s, user_id: int, inventory_id: int, units: int = 1) -> dict:
	i = s.get(Inventory, inventory_id)
	if not i or i.available < 1:
		raise ToolError("sold_out", f"{i.name if i else inventory_id} is not available")
	i.available -= 1
	item = BookingItem(user_id=user_id, kind=i.kind, inventory_id=i.id, status="held",
		price=i.price * units, units=units, hold_expires_at=now() + timedelta(minutes=10),
		details={"name": i.name, "origin": i.origin, "dest": i.dest})
	s.add(item)
	s.flush()
	return {"item_id": item.id, "status": "held", "price": item.price, "name": i.name}


@tool(side_effect=True)
def confirm_booking(s, item_id: int) -> dict:
	item = s.get(BookingItem, item_id)
	if not item:
		raise ToolError("not_found")
	if item.status == "confirmed":
		return {"item_id": item.id, "status": "confirmed"}
	if item.status != "held":
		raise ToolError("not_held", f"Item is {item.status}")
	if item.hold_expires_at and item.hold_expires_at < now():
		item.status = "expired"
		raise ToolError("hold_expired")
	i = s.get(Inventory, item.inventory_id)
	if i.fail_mode == "sellout_on_confirm":
		i.fail_mode, i.available = "sold_out", 0
		raise ToolError("sold_out", f"{i.name} sold out during checkout")
	item.status = "confirmed"
	return {"item_id": item.id, "status": "confirmed", "name": i.name}


@tool(side_effect=True)
def cancel_booking(s, item_id: int) -> dict:
	item = s.get(BookingItem, item_id)
	if not item:
		raise ToolError("not_found")
	if item.status in ("held", "confirmed"):
		i = s.get(Inventory, item.inventory_id)
		if i.fail_mode != "sold_out":
			i.available += 1
		item.status = "cancelled"
	return {"item_id": item.id, "status": item.status}


@tool()
def get_booking(s, booking_id: int) -> dict:
	b = s.get(Booking, booking_id)
	if not b:
		raise ToolError("not_found")
	items = s.query(BookingItem).filter_by(booking_id=b.id).all()
	return {**b.as_dict(), "items": [it.as_dict() for it in items]}


def _txn(s, user_id, amount, kind, order_state="fulfilled", **meta) -> Transaction:
	t = Transaction(user_id=user_id, amount=amount, kind=kind, status="success",
		order_state=order_state, ref=f"SIM-{uuid.uuid4().hex[:10].upper()}", meta=meta)
	s.add(t)
	s.flush()
	return t


@tool(side_effect=True, money="debit")
def pay_from_wallet(s, user_id: int, amount: float, purpose: str) -> dict:
	w = s.get(Wallet, user_id)
	if not w or w.balance < amount:
		raise ToolError("insufficient_balance", "Wallet balance is too low")
	w.balance -= amount
	t = _txn(s, user_id, amount, "booking" if "trip" in purpose.lower() else "payment",
		purpose=purpose, method="wallet")
	return {"txn_id": t.id, "ref": t.ref, "status": "success", "balance": w.balance}


@tool(side_effect=True, money="debit")
def pay_via_upi_sim(s, user_id: int, amount: float, purpose: str,
		merchant_id: int | None = None) -> dict:
	t = _txn(s, user_id, amount, "payment", purpose=purpose, method="upi_sim")
	t.merchant_id = merchant_id
	return {"txn_id": t.id, "ref": t.ref, "status": "success"}


@tool(side_effect=True, money="credit")
def refund_payment(s, user_id: int, amount: float, txn_id: int | None = None,
		reason: str = "") -> dict:
	"""Instant refund to the wallet. Used by saga compensation and by Care."""
	w = s.get(Wallet, user_id)
	w.balance += amount
	t = _txn(s, user_id, amount, "refund", reason=reason, original_txn=txn_id)
	if txn_id and (orig := s.get(Transaction, txn_id)):
		orig.status, orig.order_state = "refunded", "refunded"
	return {"refund_txn_id": t.id, "status": "processed", "balance": w.balance}


@tool(side_effect=True, money="debit")
def mobile_recharge(s, user_id: int, operator: str, amount: float,
		prepaid_txn_id: int | None = None) -> dict:
	"""Recharge. With `prepaid_txn_id` it fulfils an earlier debit instead of charging again."""
	if prepaid_txn_id:
		t = s.get(Transaction, prepaid_txn_id)
		if not t or t.user_id != user_id:
			raise ToolError("not_found")
		t.order_state = "fulfilled"
		return {"txn_id": t.id, "status": "recharged", "charged": 0}
	w = s.get(Wallet, user_id)
	if w.balance < amount:
		raise ToolError("insufficient_balance")
	w.balance -= amount
	t = _txn(s, user_id, amount, "recharge", operator=operator)
	return {"txn_id": t.id, "status": "recharged", "charged": amount}


@tool(side_effect=True, money="debit")
def pay_bill(s, user_id: int, biller: str, amount: float) -> dict:
	w = s.get(Wallet, user_id)
	if w.balance < amount:
		raise ToolError("insufficient_balance")
	w.balance -= amount
	t = _txn(s, user_id, amount, "bill", biller=biller)
	return {"txn_id": t.id, "status": "paid"}


@tool()
def get_refund_status(s, refund_id: int) -> dict:
	r = s.get(Refund, refund_id)
	if not r:
		raise ToolError("not_found")
	age = (now() - r.initiated_at).days
	return {"refund_id": r.id, "status": r.status, "amount": r.amount, "age_days": age,
		"sla_days": r.sla_days, "past_sla": r.status == "pending" and age > r.sla_days,
		"eta_days": max(r.sla_days - age, 0)}


@tool(side_effect=True, money="credit")
def retry_refund(s, refund_id: int) -> dict:
	r = s.get(Refund, refund_id)
	if not r:
		raise ToolError("not_found")
	if r.status == "processed":
		return {"refund_id": r.id, "status": "processed"}
	r.retries += 1
	if not r.stuck:
		r.status = "processed"
		s.get(Wallet, r.user_id).balance += r.amount
	return {"refund_id": r.id, "status": r.status, "retries": r.retries}


@tool(side_effect=True)
def raise_ticket(s, user_id: int, summary: str) -> dict:
	return create_support_ticket(s, "consumer", user_id, summary)


# ---------------- merchant ----------------
@tool()
def get_merchant_profile(s, merchant_id: int) -> dict:
	m = s.get(Merchant, merchant_id)
	if not m:
		raise ToolError("not_found")
	return m.as_dict()


@tool()
def get_settlement_status(s, merchant_id: int) -> dict:
	q = s.query(Settlement).filter_by(merchant_id=merchant_id)
	st = (q.filter(Settlement.status != "settled").order_by(Settlement.created_at.desc()).first()
		or q.order_by(Settlement.created_at.desc()).first())
	if not st:
		raise ToolError("not_found")
	return st.as_dict()


@tool(side_effect=True)
def retry_settlement(s, settlement_id: int) -> dict:
	st = s.get(Settlement, settlement_id)
	if not st:
		raise ToolError("not_found")
	m = s.get(Merchant, st.merchant_id)
	if m.risk_flag:
		raise ToolError("risk_hold", "Settlement is on a risk hold")
	if m.bank_status != "ok":
		raise ToolError("bank_mismatch", "Bank details still do not match")
	st.status, st.failure_reason = "settled", ""
	return {"settlement_id": st.id, "status": "settled", "amount": st.amount}


@tool(side_effect=True)
def request_bank_detail_update(s, merchant_id: int, account_holder_name: str) -> dict:
	m = s.get(Merchant, merchant_id)
	if not m.bank_fixable:
		raise ToolError("manual_verification_required", "Bank needs document verification")
	m.bank_status = "ok"
	return {"merchant_id": m.id, "bank_status": "ok"}


@tool()
def get_device_status(s, merchant_id: int) -> dict:
	d = s.query(Device).filter_by(merchant_id=merchant_id).first()
	if not d:
		raise ToolError("not_found")
	return d.as_dict()


@tool(side_effect=True)
def raise_device_ticket(s, merchant_id: int, issue: str) -> dict:
	return create_support_ticket(s, "merchant", merchant_id, f"Device: {issue}")


@tool()
def list_overdue_khata(s, merchant_id: int) -> dict:
	rows = (s.query(KhataEntry, Customer).join(Customer, Customer.id == KhataEntry.customer_id)
		.filter(KhataEntry.merchant_id == merchant_id, KhataEntry.status != "paid",
			KhataEntry.due_date < now()).all())
	return {"entries": [{"entry_id": k.id, "customer_id": c.id, "customer": c.name,
		"amount": k.amount, "days_overdue": (now() - k.due_date).days, "status": k.status,
		"reliability": c.reliability, "reminders": k.reminders} for k, c in rows]}


@tool(side_effect=True)
def create_payment_link(s, merchant_id: int, amount: float, purpose: str,
		entry_id: int | None = None) -> dict:
	from .adapters.paytm_pg import pg
	link = pg.create_link(merchant_id, amount, purpose)
	if entry_id and (k := s.get(KhataEntry, entry_id)):
		k.pay_link = link["link_url"]
	return link


@tool(side_effect=True)
def send_khata_reminder(s, entry_id: int, message: str = "") -> dict:
	k = s.get(KhataEntry, entry_id)
	if not k:
		raise ToolError("not_found")
	if k.status == "disputed":
		raise ToolError("disputed", "Disputed entries are not reminded automatically")
	if k.status == "paid":
		return {"entry_id": k.id, "status": "paid"}
	k.reminders += 1
	# Real WhatsApp/SMS stays mocked: the reminder lands in the simulated outbox.
	s.add(Notification(actor_type="customer", actor_id=k.customer_id, channel="sms_mock",
		message=message or f"Reminder: ₹{k.amount:.0f} due. Pay: {k.pay_link}",
		meta={"entry_id": k.id}))
	return {"entry_id": k.id, "status": "reminded", "reminders": k.reminders}


@tool()
def list_lapsed_customers(s, merchant_id: int, days: int = 30) -> dict:
	cut = now() - timedelta(days=days)
	rows = (s.query(Customer).filter(Customer.merchant_id == merchant_id, Customer.visits >= 8,
		Customer.last_visit < cut).all())
	return {"customers": [{"customer_id": c.id, "name": c.name, "visits": c.visits,
		"avg_spend": c.avg_spend, "days_since": (now() - c.last_visit).days} for c in rows]}


@tool(side_effect=True)
def create_offer_campaign(s, merchant_id: int, title: str, cohort_key: str, discount_pct: float,
		budget_cap: float, min_bill: float = 0, valid_days: int = 7,
		expected_redemptions: int = 0, assumption: str = "") -> dict:
	o = Offer(merchant_id=merchant_id, title=title, cohort_key=cohort_key,
		discount_pct=discount_pct, budget_cap=budget_cap, min_bill=min_bill, valid_days=valid_days,
		expected_redemptions=expected_redemptions, assumption=assumption, status="live")
	s.add(o)
	s.flush()
	return {"offer_id": o.id, "status": "live"}


@tool()
def check_loan_eligibility(s, merchant_id: int) -> dict:
	m = s.get(Merchant, merchant_id)
	sales = sum(t.amount for t in s.query(Transaction).filter(
		Transaction.merchant_id == merchant_id, Transaction.kind == "sale",
		Transaction.created_at > now() - timedelta(days=90)).all())
	ok = m.kyc_status == "verified" and not m.risk_flag and m.bank_status == "ok"
	return {"eligible": ok, "max_amount": round(sales * 0.25, -2) if ok else 0,
		"basis": "25% of the last 90 days of simulated sales"}


@tool(side_effect=True)
def start_loan_application(s, merchant_id: int, amount: float) -> dict:
	elig = check_loan_eligibility(s, merchant_id)
	if not elig["eligible"] or amount > elig["max_amount"]:
		raise ToolError("not_eligible")
	return create_support_ticket(s, "merchant", merchant_id, f"Loan application ₹{amount:.0f}")


# ---------------- bridge ----------------
@tool(side_effect=True)
def deliver_offer(s, offer_id: int, user_id: int, reason: str) -> dict:
	"""Put an offer in a consenting user's inbox with the reason they are seeing it."""
	if s.query(OfferDelivery).filter_by(offer_id=offer_id, user_id=user_id).first():
		return {"offer_id": offer_id, "user_id": user_id, "status": "already_delivered"}
	o, m = s.get(Offer, offer_id), None
	m = s.get(Merchant, o.merchant_id)
	s.add(OfferDelivery(offer_id=offer_id, user_id=user_id, reason=reason))
	s.add(Notification(actor_type="consumer", actor_id=user_id, channel="inbox",
		message=f"{o.title} at {m.name}",
		meta={"type": "offer", "offer_id": o.id, "why": reason, "merchant": m.name,
			"discount_pct": o.discount_pct, "min_bill": o.min_bill}))
	return {"offer_id": offer_id, "user_id": user_id, "status": "delivered"}


@tool(side_effect=True, money="debit")
def redeem_offer(s, user_id: int, offer_id: int, bill_amount: float) -> dict:
	o = s.get(Offer, offer_id)
	d = s.query(OfferDelivery).filter_by(offer_id=offer_id, user_id=user_id).first()
	if not o or not d or o.status != "live":
		raise ToolError("offer_not_available")
	if d.status == "redeemed":
		return {"status": "already_redeemed", "offer_id": offer_id}
	if bill_amount < o.min_bill:
		raise ToolError("below_min_bill", f"Minimum bill is ₹{o.min_bill:.0f}")
	discount = round(bill_amount * o.discount_pct / 100, 2)
	if o.spent + discount > o.budget_cap:
		o.status = "ended"
		raise ToolError("offer_budget_exhausted")
	w = s.get(Wallet, user_id)
	if w.balance < bill_amount - discount:
		raise ToolError("insufficient_balance")
	w.balance -= bill_amount - discount
	t = _txn(s, user_id, bill_amount - discount, "payment", purpose=o.title, offer_id=o.id,
		discount=discount)
	t.merchant_id = o.merchant_id
	o.spent += discount
	d.status, d.redeemed_at, d.bill_amount = "redeemed", now(), bill_amount
	return {"status": "redeemed", "paid": bill_amount - discount, "discount": discount,
		"txn_id": t.id, "offer_id": o.id}


# ---------------- shared ----------------
@tool(side_effect=True)
def create_support_ticket(s, actor_type: str, actor_id: int, summary: str) -> dict:
	c = Case(actor_type=actor_type, actor_id=actor_id, workflow="ticket", summary=summary)
	s.add(c)
	s.flush()
	return {"ticket_id": c.id, "status": "open"}


@tool(side_effect=True)
def notify(s, actor_type: str, actor_id: int, message: str, channel: str = "inbox",
		meta: dict[str, Any] = {}) -> dict:
	n = Notification(actor_type=actor_type, actor_id=actor_id, channel=channel, message=message,
		meta=meta)
	s.add(n)
	s.flush()
	return {"notification_id": n.id}


@tool(side_effect=True)
def escalate_to_human(s, packet: dict[str, Any]) -> dict:
	from .services import open_escalation
	e = open_escalation(s, packet)
	return {"escalation_id": e.id, "status": "open"}


def user_exists(s: Session, user_id: int) -> bool:
	return s.get(User, user_id) is not None
