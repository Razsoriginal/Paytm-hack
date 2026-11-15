"""M1: inventory by chat, voice or upload."""
from sqlalchemy.orm import Session

from .. import bills, inventory, tools  # noqa: F401  (inventory registers its tools)
from ..adapters.docai import docai
from ..config import settings
from ..db import Approval, Case, Merchant, StockMovement, Upload
from ..policy import ProposedAction as PA
from ..services import (
	Step, close_case, create_approval, execute, impact, open_case, run_log, run_saga,
)
from .flows import out

LINE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
	"type": "object", "properties": {k: {"type": "string"} for k in (
		"name", "qty", "unit", "rate", "amount")}}}}}
T1_NOTE = ("On the Soundbox plan I can answer stock questions by voice, but I cannot change "
	"stock. Upgrade to the billing plan to add and adjust stock.")


def _g(n: float) -> str:
	return f"{n:g}"


def handle(s: Session, m: Merchant, intent, case: Case) -> dict:
	action = intent.action or "query"
	base = dict(actor_type="merchant", actor_id=m.id, case_id=case.id, agent="Stock")
	run_log(s, case.id, "Stock", "detect", f"Stock request: {action}")

	def write(tool, key, amount=0, **args):
		r = execute(s, PA(tool=tool, args={"merchant_id": m.id, **args}, amount=amount,
			idempotency_key=f"case{case.id}:{key}", why="Merchant asked in chat", **base))
		close_case(s, case)
		if r.decision.reason == "suggest_only":
			return None, out(T1_NOTE, case)
		if r.outcome == "needs_approval":
			return None, out("That is a large stock change. Upload the supplier invoice and I "
				"will ask you to approve it once.", case)
		if not r.ok:
			return None, out(f"I could not do that: {r.error.replace('_', ' ')}.", case)
		return r.result, None

	if action == "add" and intent.product and intent.qty:
		res, stop = write("add_product", "add", amount=intent.qty * (intent.cost or 0),
			name=intent.product, qty=intent.qty, cost=intent.cost, price=intent.price)
		if stop:
			return stop
		impact(s, "stock_added", "merchant", m.id, intent.qty * (intent.cost or 0))
		return out(f"Added {_g(intent.qty)} {res['name']}. You now have {_g(res['stock'])}. "
			f"Say 'undo' within {settings.STOCK_UNDO_MINUTES} minutes to reverse this.", case,
			undo=res["movement_id"])
	if action == "damaged" and intent.qty:
		p = inventory.find_product(s, m.id, intent.product) if intent.product else None
		if not p:  # "mark 5 as damaged" right after talking about a product
			last = (s.query(StockMovement).filter_by(merchant_id=m.id)
				.filter(StockMovement.reason != "sale").order_by(StockMovement.id.desc()).first())
			p = s.get(inventory.Product, last.product_id) if last else None
		if not p:
			close_case(s, case)
			return out("Which product is damaged? For example: mark 5 Parle-G as damaged.", case)
		res, stop = write("adjust_stock", "damaged", product_id=p.id, delta=-intent.qty,
			reason="damaged")
		if stop:
			return stop
		return out(f"Marked {_g(intent.qty)} {res['name']} as damaged. {_g(res['stock'])} left. "
			"Say 'undo' to reverse this.", case, undo=res["movement_id"])
	if action == "undo":
		res, stop = write("undo_stock_change", "undo")
		if stop:
			return stop
		return out(f"Undone. {res['name']} is back to {_g(res['stock'])}.", case)

	close_case(s, case)
	if action == "low":
		r = tools.call(s, "get_low_stock", {"merchant_id": m.id})
		if not r["products"]:
			return out("Nothing is running low.", case)
		lines = [f"{p['name']}: {_g(p['stock'])} left" + (f", about {p['days_left']} days"
			if p["days_left"] is not None else "") + f", reorder {p['reorder_qty']}"
			for p in r["products"]]
		impact(s, "low_stock_flagged", "merchant", m.id, count=len(lines))
		return out(f"{len(lines)} items are running low. " + "; ".join(lines) + f". "
			f"{r['assumption']}.", case, table={"columns": ["Item", "In stock", "Days left",
			"Reorder"], "rows": [[p["name"], p["stock"], p["days_left"] if p["days_left"] is not
			None else "–", p["reorder_qty"]] for p in r["products"]]})
	if action == "dead":
		r = tools.call(s, "get_dead_stock", {"merchant_id": m.id})
		if not r["products"]:
			return out(f"Everything in stock has sold in the last {r['days']} days.", case)
		return out(f"{len(r['products'])} items have not sold in {r['days']} days: " + ", ".join(
			f"{p['name']} (₹{p['value']} tied up)" for p in r["products"]) + ". An offer could "
			"help move them.", case)
	if action == "margin":
		r = tools.call(s, "get_margin_report", {"merchant_id": m.id})
		top = r["products"][:3]
		return out("Best earners in the last 30 days: " + ", ".join(
			f"{p['name']} ₹{p['profit_30d']} at {p['margin_pct']}% margin" for p in top) + ".", case)
	r = tools.call(s, "list_products", {"merchant_id": m.id, "query": intent.product})
	if intent.product and not r["products"]:
		return out(f"I do not have '{intent.product}' in your stock list.", case)
	if intent.product:
		p = r["products"][0]
		left = f", about {p['days_left']} days at the current pace" if p["days_left"] else ""
		return out(f"{p['name']}: {_g(p['stock'])} {p['unit']} left{left}.", case)
	low = sum(1 for p in r["products"] if p["low"])
	return out(f"You have {len(r['products'])} products, {low} running low. Open the Stock tab "
		"for the full list.", case)


# ---------------- upload a supplier invoice or stock list ----------------
def review_upload(s: Session, m: Merchant, data: bytes, filename: str = "stock") -> dict:
	"""Read line items from a document. Always needs review: handwriting is best effort."""
	u = bills.save_upload(s, "merchant", m.id, "stock", data)
	res = docai.read(data, filename, u.mime, LINE_SCHEMA)
	notice = bills.NOTICE_LIVE if res["mode"] == "live" else bills.NOTICE_MOCK
	if res["status"] == "failed" or not res["fields"].get("items"):
		u.status = "rejected"
		s.commit()
		return {"upload_id": u.id, "state": "unreadable", "items": [], "notice": notice,
			"reply": "I could not read any items from this file. Upload a clearer photo or PDF."}
	items = []
	for row in res["fields"]["items"]:
		qty, rate = bills.parse_amount(row.get("qty", "")), bills.parse_amount(row.get("rate", ""))
		ok = bool(row.get("name")) and qty is not None and rate is not None
		conf = min(bills.confidence(str(row.get(k, "")), res["text"], ok)
			for k in ("name", "qty", "rate"))
		known = inventory.find_product(s, m.id, row.get("name", ""))
		items.append({"name": known.name if known else row.get("name", ""), "qty": qty or 0,
			"unit": row.get("unit") or "pc", "rate": rate or 0, "confidence": conf,
			"new": known is None, "state": "ok" if conf >= settings.EXTRACTION_CONFIDENCE_AUTO
			else "confirm"})
	u.fields = {"items": items}
	s.commit()
	return {"upload_id": u.id, "state": "review", "items": items, "notice": notice,
		"reply": f"I read {len(items)} items. Check every line before I add them to your stock."}


def confirm_upload(s: Session, m: Merchant, upload_id: int, items: list[dict]) -> dict:
	u = s.query(Upload).filter_by(id=upload_id, actor_id=m.id, purpose="stock").first()
	if not u or u.status != "extracted":
		return {"state": "error", "reply": "That upload is no longer waiting for review."}
	clean = [{"name": str(i["name"]).strip(), "qty": float(i["qty"]), "unit": i.get("unit") or "pc",
		"rate": float(i["rate"])} for i in items if str(i.get("name", "")).strip()
		and float(i.get("qty") or 0) > 0]
	if not clean:
		return {"state": "invalid", "reply": "There are no valid lines to add."}
	total = sum(i["qty"] * i["rate"] for i in clean)
	case = open_case(s, "merchant", m.id, "M1", f"Stock upload {u.id}")
	payload = {"upload_id": u.id, "merchant_id": m.id, "items": clean, "total": total}
	u.confirmed, u.status = payload, "confirmed"
	a = create_approval(s, "merchant", m.id, "stock_in", f"Add {len(clean)} items worth "
		f"₹{total:.0f} to stock", payload, case.id)
	case.status = "waiting"
	s.commit()
	return {"state": "approval", "approval": a.as_dict(), "case_id": case.id,
		"reply": f"{len(clean)} items worth ₹{total:.0f}. Approve to add them to your stock."}


def apply_upload(s: Session, a: Approval) -> dict:
	case, p = s.get(Case, a.case_id), a.payload
	base = dict(actor_type="merchant", actor_id=a.actor_id, case_id=case.id, agent="Stock",
		approval_id=a.id, approval_payload=p)
	steps = []
	for i, it in enumerate(p["items"]):
		steps.append(Step(f"add{i}",
			lambda r, it=it, i=i: PA(tool="add_product", idempotency_key=f"appr{a.id}:add{i}",
				amount=it["qty"] * it["rate"], args={"merchant_id": a.actor_id, "name": it["name"],
					"qty": it["qty"], "cost": it["rate"], "price": None, "unit": it["unit"]}
					if inventory.find_product(s, a.actor_id, it["name"]) else
					{"merchant_id": a.actor_id, "name": it["name"], "qty": it["qty"],
					"cost": it["rate"], "price": round(it["rate"] * 1.2, 2), "unit": it["unit"]},
				why="Approved stock upload", **base),
			lambda r, i=i: PA(tool="undo_stock_change", idempotency_key=f"appr{a.id}:undo{i}",
				args={"merchant_id": a.actor_id, "movement_id": r[f"add{i}"]["movement_id"]},
				why="Saga rollback", **base)))
	res = run_saga(s, steps)
	a.status = "used"
	close_case(s, case)
	if not res.ok:
		return out(f"I could not add the stock ({res.error.replace('_', ' ')}), so nothing was "
			"changed.", case)
	if u := s.get(Upload, p["upload_id"]):
		u.status = "paid"
	impact(s, "stock_added", "merchant", a.actor_id, p["total"], upload=True)
	return out(f"Added {len(p['items'])} items to your stock. New products are priced at cost "
		"plus 20%; change any price in chat.", case)
