"""M1: merchant inventory. Stock can never go negative, every change is a stock movement,
and manual changes can be undone for a short window."""
import math
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .db import Product, SalesLine, StockMovement, now
from .tools import ToolError, tool

# Festival months sell faster. An assumption from a fixed calendar, not a forecast.
FESTIVAL_UPLIFT = {3: 1.1, 8: 1.1, 10: 1.2, 11: 1.2}


def find_product(s: Session, merchant_id: int, name: str) -> Product | None:
	key = name.strip().lower()
	rows = s.query(Product).filter_by(merchant_id=merchant_id).all()
	return (next((p for p in rows if p.name.lower() == key), None)
		or next((p for p in rows if key and (key in p.name.lower() or p.name.lower() in key)), None))


def velocity(s: Session, product_id: int, days: int = 14) -> float:
	sold = s.query(func.coalesce(func.sum(SalesLine.qty), 0)).filter(
		SalesLine.product_id == product_id,
		SalesLine.created_at > now() - timedelta(days=days)).scalar()
	return float(sold) / days


def reorder_qty(vel: float, stock: float) -> int:
	"""Cover REORDER_COVER_DAYS of sales at the current pace, times the festival factor."""
	need = vel * settings.REORDER_COVER_DAYS * FESTIVAL_UPLIFT.get(now().month, 1.0)
	return max(math.ceil(need - stock), 0)


def _row(s: Session, p: Product) -> dict:
	vel = velocity(s, p.id)
	days = round(p.stock / vel, 1) if vel > 0 else None
	low = (days is not None and days < settings.LOW_STOCK_DEFAULT_DAYS) or p.stock <= p.reorder_level
	return {"product_id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "stock": p.stock,
		"cost": p.cost, "price": p.price, "per_day": round(vel, 1), "days_left": days, "low": low,
		"margin_pct": round(100 * (p.price - p.cost) / p.price) if p.price else 0,
		"reorder_qty": reorder_qty(vel, p.stock) if low else 0}


@tool()
def list_products(s, merchant_id: int, query: str | None = None) -> dict:
	rows = s.query(Product).filter_by(merchant_id=merchant_id).order_by(Product.name).all()
	if query:
		hit = find_product(s, merchant_id, query)
		rows = [hit] if hit else []
	return {"products": [_row(s, p) for p in rows]}


@tool()
def get_low_stock(s, merchant_id: int) -> dict:
	rows = [r for r in list_products(s, merchant_id)["products"] if r["low"]]
	uplift = FESTIVAL_UPLIFT.get(now().month, 1.0)
	return {"products": sorted(rows, key=lambda r: (r["days_left"] is None, r["days_left"] or 0)),
		"assumption": f"Reorder covers {settings.REORDER_COVER_DAYS} days at the current pace"
			+ (f", raised {round((uplift - 1) * 100)}% for the festival season" if uplift > 1 else "")}


@tool()
def get_dead_stock(s, merchant_id: int, days: int | None = None) -> dict:
	days = days or settings.DEAD_STOCK_DAYS
	out = []
	for p in s.query(Product).filter(Product.merchant_id == merchant_id, Product.stock > 0).all():
		last = s.query(func.max(SalesLine.created_at)).filter_by(product_id=p.id).scalar()
		if last is None or last < now() - timedelta(days=days):
			out.append({"product_id": p.id, "name": p.name, "stock": p.stock,
				"value": round(p.stock * p.cost),
				"days_since_sale": (now() - last).days if last else None})
	return {"days": days, "products": sorted(out, key=lambda r: -r["value"])}


@tool()
def get_margin_report(s, merchant_id: int) -> dict:
	out = []
	for p in s.query(Product).filter_by(merchant_id=merchant_id).all():
		sold = float(s.query(func.coalesce(func.sum(SalesLine.qty), 0)).filter(
			SalesLine.product_id == p.id,
			SalesLine.created_at > now() - timedelta(days=30)).scalar())
		out.append({"name": p.name, "margin_pct": round(100 * (p.price - p.cost) / p.price)
			if p.price else 0, "sold_30d": round(sold), "profit_30d": round(sold * (p.price - p.cost))})
	return {"products": sorted(out, key=lambda r: -r["profit_30d"])}


def _move(s, p: Product, delta: float, reason: str, prev: dict | None = None) -> StockMovement:
	if p.stock + delta < 0:
		raise ToolError("insufficient_stock", f"Only {p.stock:g} {p.unit} of {p.name} in stock")
	p.stock += delta
	m = StockMovement(merchant_id=p.merchant_id, product_id=p.id, delta=delta, reason=reason,
		prev=prev or {})
	s.add(m)
	s.flush()
	return m


@tool(side_effect=True)
def add_product(s, merchant_id: int, name: str, qty: float, cost: float | None = None,
		price: float | None = None, unit: str = "pc") -> dict:
	"""Stock in. Adds to an existing product by name, or creates it when cost and price are given."""
	if qty <= 0:
		raise ToolError("invalid_quantity")
	p = find_product(s, merchant_id, name)
	created = p is None
	if created:
		if cost is None or price is None:
			raise ToolError("need_price", f"{name} is new: tell me the cost and selling price")
		n = s.query(Product).filter_by(merchant_id=merchant_id).count() + 1
		p = Product(merchant_id=merchant_id, sku=f"M{merchant_id}-{n:03d}", name=name.strip().title(),
			unit=unit, cost=cost, price=price, stock=0, reorder_level=0)
		s.add(p)
		s.flush()
	prev = {"cost": p.cost, "price": p.price, "created": created}
	p.cost, p.price = cost if cost is not None else p.cost, price if price is not None else p.price
	m = _move(s, p, qty, "stock_in", prev)
	return {"movement_id": m.id, "product_id": p.id, "name": p.name, "stock": p.stock,
		"created": created, "unit": p.unit}


@tool(side_effect=True)
def adjust_stock(s, merchant_id: int, product_id: int, delta: float, reason: str) -> dict:
	p = s.query(Product).filter_by(id=product_id, merchant_id=merchant_id).first()
	if not p:
		raise ToolError("not_found")
	m = _move(s, p, delta, reason)
	return {"movement_id": m.id, "product_id": p.id, "name": p.name, "stock": p.stock,
		"unit": p.unit}


@tool(side_effect=True)
def undo_stock_change(s, merchant_id: int, movement_id: int | None = None) -> dict:
	"""Reverse a manual stock change made in the last few minutes."""
	q = s.query(StockMovement).filter(StockMovement.merchant_id == merchant_id,
		StockMovement.undone.is_(False), StockMovement.reason.notin_(("sale", "undo")))
	m = q.filter_by(id=movement_id).first() if movement_id else q.order_by(
		StockMovement.id.desc()).first()
	if not m:
		raise ToolError("nothing_to_undo", "There is no recent stock change to undo")
	if m.created_at < now() - timedelta(minutes=settings.STOCK_UNDO_MINUTES):
		raise ToolError("undo_window_passed", f"Changes can be undone for "
			f"{settings.STOCK_UNDO_MINUTES} minutes")
	p = s.get(Product, m.product_id)
	_move(s, p, -m.delta, "undo")
	if "cost" in m.prev:
		p.cost, p.price = m.prev["cost"], m.prev["price"]
	m.undone = True
	name, stock = p.name, p.stock
	if m.prev.get("created") and p.stock == 0:
		s.delete(p)
	return {"undone_movement_id": m.id, "name": name, "stock": stock}
