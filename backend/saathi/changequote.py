"""Deterministic money logic for booking changes. Fees, fare differences and refunds come
only from the partner policy fixtures below; a language model never computes or states them."""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import Booking, BookingItem, Inventory, now
from .tools import ToolError

# Per partner type: cancellation fee as (minimum hours before departure, % of amount paid),
# checked top to bottom, and a flat change fee per unit. After departure nothing is refundable.
POLICY = {
	"bus": {"cancel": [(24, 10), (4, 25), (0, 50)], "change_fee": 50},
	"train": {"cancel": [(48, 10), (12, 25), (0, 50)], "change_fee": 60},
	"flight": {"cancel": [(72, 15), (24, 30), (0, 60)], "change_fee": 400},
	"hotel": {"cancel": [(48, 0), (24, 20), (0, 50)], "change_fee": 0},
}
REFUND_WHEN = "Instantly, to your wallet"


def departure(b: Booking) -> datetime:
	return datetime.fromisoformat(b.travel_date) + timedelta(hours=9)


def hours_left(b: Booking) -> float:
	return (departure(b) - now()).total_seconds() / 3600


def cancel_pct(kind: str, hours: float) -> int:
	for min_hours, pct in POLICY[kind]["cancel"]:
		if hours >= min_hours:
			return pct
	return 100


def _booking(s: Session, booking_id: int) -> tuple[Booking, list[BookingItem]]:
	b = s.get(Booking, booking_id)
	if not b or not b.travel_date:
		raise ToolError("not_found", "Booking not found")
	items = (s.query(BookingItem).filter_by(booking_id=b.id, status="confirmed")
		.order_by(BookingItem.id).all())
	if not items:
		raise ToolError("nothing_to_change", "This booking has no active items")
	return b, items


def _finish(q: dict) -> dict:
	net = sum(l["fee"] + l["fare_diff"] - l["refund"] for l in q["lines"])
	q["fee_total"] = sum(l["fee"] for l in q["lines"])
	q["fare_diff"] = sum(l["fare_diff"] for l in q["lines"])
	q["pay_extra"] = max(net, 0)
	q["refund_total"] = max(-net, 0)
	q["refund_when"] = REFUND_WHEN if q["refund_total"] else ""
	return q


def quote_cancellation(s: Session, booking_id: int, item_id: int | None = None) -> dict:
	b, items = _booking(s, booking_id)
	if item_id:
		items = [it for it in items if it.id == item_id]
		if not items:
			raise ToolError("not_found", "That item is not active on this booking")
	hours = hours_left(b)
	lines = []
	for it in items:
		pct = cancel_pct(it.kind, hours)
		fee = round(it.price * pct / 100)
		lines.append({"item_id": it.id, "kind": it.kind, "now": f"{it.details['name']} on "
			f"{b.travel_date}", "after": "Cancelled", "paid": it.price, "fee_pct": pct, "fee": 0,
			"fare_diff": 0, "refund": it.price - fee, "cancel_fee": fee})
	q = _finish({"booking_id": b.id, "action": "cancel", "item_id": item_id or 0, "new_date": "",
		"lines": lines})
	q["fee_total"] = sum(l["cancel_fee"] for l in lines)
	return q


def quote_change(s: Session, booking_id: int, new_date: str | None = None,
		item_id: int | None = None, new_inventory_id: int | None = None,
		units: int | None = None) -> dict:
	"""Reschedule the whole booking to `new_date`, or swap / resize one item."""
	b, items = _booking(s, booking_id)
	if hours_left(b) < 0:
		raise ToolError("already_departed", "This trip has already started")
	lines = []
	if new_date:
		try:
			when = datetime.fromisoformat(new_date)
		except ValueError as e:
			raise ToolError("invalid_date", "Give the new date as YYYY-MM-DD") from e
		if when.date() <= now().date() or new_date == b.travel_date:
			raise ToolError("invalid_date", "Pick a different date in the future")
		for it in items:
			inv = s.get(Inventory, it.inventory_id)
			lines.append({"item_id": it.id, "kind": it.kind, "paid": it.price, "refund": 0,
				"now": f"{it.details['name']} on {b.travel_date}",
				"after": f"{it.details['name']} on {new_date}",
				"fee": POLICY[it.kind]["change_fee"] * it.units,
				"fare_diff": inv.price * it.units - it.price, "new_inventory_id": inv.id,
				"new_units": it.units, "new_price": inv.price * it.units})
		action = "reschedule"
	else:
		it = next((x for x in items if x.id == item_id), None)
		if not it:
			raise ToolError("not_found", "That item is not active on this booking")
		inv = s.get(Inventory, new_inventory_id or it.inventory_id)
		if not inv or inv.kind != it.kind:
			raise ToolError("invalid_option", "That option is not available for this item")
		if inv.id != it.inventory_id and inv.available < 1:
			raise ToolError("sold_out", f"{inv.name} is sold out")
		n = units or it.units
		lines.append({"item_id": it.id, "kind": it.kind, "paid": it.price, "refund": 0,
			"now": f"{it.details['name']} × {it.units}", "after": f"{inv.name} × {n}",
			"fee": POLICY[it.kind]["change_fee"] * n, "fare_diff": inv.price * n - it.price,
			"new_inventory_id": inv.id, "new_units": n, "new_price": inv.price * n})
		action = "modify"
	return _finish({"booking_id": b.id, "action": action, "item_id": item_id or 0,
		"new_date": new_date or "", "lines": lines})


def get_change_options(s: Session, booking_id: int, item_id: int) -> dict:
	_, items = _booking(s, booking_id)
	it = next((x for x in items if x.id == item_id), None)
	if not it:
		raise ToolError("not_found")
	cur = s.get(Inventory, it.inventory_id)
	q = s.query(Inventory).filter(Inventory.kind == it.kind, Inventory.dest == cur.dest,
		Inventory.origin == cur.origin, Inventory.available > 0, Inventory.id != cur.id)
	return {"item_id": it.id, "current": cur.name, "options": [
		{"inventory_id": i.id, "name": i.name, "price": i.price, "rating": i.rating}
		for i in q.order_by(Inventory.price).all()]}
