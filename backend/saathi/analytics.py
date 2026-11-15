"""C5: deterministic spend analytics. The language model picks a tool and explains the
result; every figure in an answer is produced here, by SQL and plain arithmetic."""
import calendar
import math
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from .adapters.llm import SPEND_CATEGORIES
from .config import settings
from .db import Budget, CategoryCorrection, Goal, Merchant, Transaction, now
from .tools import ToolError, tool

SPEND_KINDS = ("payment", "bill", "recharge", "booking")
BY_KIND = {"bill": "bills", "recharge": "recharge", "booking": "travel"}
BY_MERCHANT = {"restaurant": "food", "tea-stall": "food", "kirana": "groceries",
	"pharmacy": "pharmacy", "salon": "personal care", "mobile-shop": "shopping",
	"travel-agent": "travel"}
SAVINGS_TARGET_PCT = 10  # assumption behind a proposed budget; the user can change any line


def categorise(s: Session, user_id: int, kind: str, merchant: Merchant | None,
		merchant_name: str = "") -> str:
	"""Rules first. A correction the user made for a merchant always wins."""
	name = merchant.name if merchant else merchant_name
	fix = s.query(CategoryCorrection).filter_by(user_id=user_id, merchant_name=name).first()
	if fix:
		return fix.category
	if kind in BY_KIND:
		return BY_KIND[kind]
	return BY_MERCHANT.get(merchant.category, "other") if merchant else "other"


def month_start(d: datetime, back: int = 0) -> datetime:
	y, m = d.year, d.month - back
	while m < 1:
		y, m = y - 1, m + 12
	return datetime(y, m, 1)


def period_range(period: str | None) -> tuple[datetime, datetime, str]:
	t = now()
	this = month_start(t)
	if period in (None, "", "this_month"):
		return this, t, "this month so far"
	if period == "last_month":
		return month_start(t, 1), this, "last month"
	if period == "last_3_months":
		return month_start(t, 3), this, "the last 3 full months"
	try:
		start = datetime.strptime(period, "%Y-%m")
	except ValueError as e:
		raise ToolError("invalid_period") from e
	return start, month_start(start + timedelta(days=32)), start.strftime("%B %Y")


def _rows(s: Session, user_id: int, start: datetime, end: datetime,
		category: str | None = None) -> list[Transaction]:
	q = s.query(Transaction).filter(Transaction.user_id == user_id,
		Transaction.kind.in_(SPEND_KINDS), Transaction.status == "success",
		Transaction.order_state == "fulfilled", Transaction.created_at >= start,
		Transaction.created_at < end)
	if category:
		q = q.filter(Transaction.category == category)
	return q.all()


def _by_category(rows) -> list[dict]:
	agg = defaultdict(lambda: [0.0, 0])
	for t in rows:
		agg[t.category or "other"][0] += t.amount
		agg[t.category or "other"][1] += 1
	return sorted(({"category": c, "total": round(v[0]), "count": v[1]} for c, v in agg.items()),
		key=lambda r: -r["total"])


@tool()
def spend_summary(s, user_id: int, period: str | None = None, category: str | None = None) -> dict:
	start, end, label = period_range(period)
	rows = _rows(s, user_id, start, end, category)
	return {"period": label, "category": category, "total": round(sum(t.amount for t in rows)),
		"count": len(rows), "by_category": _by_category(rows)}


@tool()
def spend_trend(s, user_id: int, category: str | None = None, months: int = 4) -> dict:
	t, out = now(), []
	for back in range(months - 1, -1, -1):
		start = month_start(t, back)
		end = month_start(t, back - 1) if back else t
		rows = _rows(s, user_id, start, end, category)
		out.append({"month": start.strftime("%b %Y"), "total": round(sum(x.amount for x in rows))})
	return {"category": category, "months": out}


@tool()
def compare_periods(s, user_id: int, a: str = "this_month", b: str = "last_month") -> dict:
	"""Compares like with like: when `a` is the month in progress, `b` is cut at the same day."""
	sa, ea, la = period_range(a)
	sb, eb, lb = period_range(b)
	day = None
	if a in (None, "", "this_month"):
		eb, day = min(eb, sb + (ea - sa)), (ea - sa).days + 1
		lb += f" up to day {day}"
	ra, rb = _rows(s, user_id, sa, ea), _rows(s, user_id, sb, eb)
	ta, tb = round(sum(t.amount for t in ra)), round(sum(t.amount for t in rb))
	ca = {r["category"]: r["total"] for r in _by_category(ra)}
	cb = {r["category"]: r["total"] for r in _by_category(rb)}
	cats = sorted(set(ca) | set(cb), key=lambda c: -abs(ca.get(c, 0) - cb.get(c, 0)))
	return {"a": {"period": la, "total": ta}, "b": {"period": lb, "total": tb, "up_to_day": day},
		"change": ta - tb,
		"change_pct": round(100 * (ta - tb) / tb) if tb else None,
		"by_category": [{"category": c, "a": ca.get(c, 0), "b": cb.get(c, 0),
			"change": ca.get(c, 0) - cb.get(c, 0)} for c in cats]}


@tool()
def top_merchants(s, user_id: int, period: str | None = None, limit: int = 5) -> dict:
	start, end, label = period_range(period)
	agg = defaultdict(lambda: [0.0, 0])
	for t in _rows(s, user_id, start, end):
		agg[t.merchant_name or "Unknown"][0] += t.amount
		agg[t.merchant_name or "Unknown"][1] += 1
	rows = sorted(({"merchant": m, "total": round(v[0]), "count": v[1]} for m, v in agg.items()),
		key=lambda r: -r["total"])
	return {"period": label, "merchants": rows[:limit]}


@tool()
def detect_anomalies(s, user_id: int, period: str | None = None) -> dict:
	"""A spend is unusual when it is over 3× the category's median for the 90 days before."""
	start, end, label = period_range(period)
	out = []
	for t in _rows(s, user_id, start, end):
		past = [x.amount for x in _rows(s, user_id, start - timedelta(days=90), start, t.category)]
		if len(past) >= 2 and t.amount > 3 * median(past) and t.amount >= 500:
			out.append({"merchant": t.merchant_name, "category": t.category,
				"amount": round(t.amount), "usual": round(median(past)),
				"date": t.created_at.date().isoformat()})
	return {"period": label, "count": len(out), "unusual": sorted(out, key=lambda r: -r["amount"])}


@tool()
def detect_recurring(s, user_id: int) -> dict:
	"""Same payee in at least 3 of the last 4 months, amounts within 10% of each other."""
	t = now()
	seen = defaultdict(dict)
	for x in _rows(s, user_id, month_start(t, 3), t):
		seen[x.merchant_name or "Unknown"].setdefault(x.created_at.strftime("%Y-%m"), []).append(
			x.amount)
	out = []
	for name, months in seen.items():
		firsts = [v[0] for v in months.values() if len(v) == 1]
		if len(firsts) >= 3 and max(firsts) <= 1.1 * min(firsts):
			out.append({"merchant": name, "amount": round(sum(firsts) / len(firsts)),
				"months": len(firsts)})
	return {"count": len(out), "recurring": sorted(out, key=lambda r: -r["amount"])}


@tool()
def budget_status(s, user_id: int) -> dict:
	start, end, _ = period_range("this_month")
	spent = {r["category"]: r["total"] for r in _by_category(_rows(s, user_id, start, end))}
	rows = []
	for b in s.query(Budget).filter_by(user_id=user_id).order_by(Budget.category).all():
		used = spent.get(b.category, 0)
		pct = round(100 * used / b.monthly_limit) if b.monthly_limit else 0
		rows.append({"category": b.category, "limit": round(b.monthly_limit), "spent": used,
			"pct": pct, "alert": pct >= settings.BUDGET_ALERT_PCT})
	return {"budgets": rows, "limit_total": sum(r["limit"] for r in rows),
		"spent_total": sum(r["spent"] for r in rows), "alert_pct": round(settings.BUDGET_ALERT_PCT)}


@tool()
def forecast_month_end(s, user_id: int) -> dict:
	"""Straight-line projection: spend so far ÷ days elapsed × days in the month."""
	t = now()
	start = month_start(t)
	spent = round(sum(x.amount for x in _rows(s, user_id, start, t)))
	days, total_days = t.day, calendar.monthrange(t.year, t.month)[1]
	projected = round(spent / days * total_days)
	limit = budget_status(s, user_id)["limit_total"]
	return {"spent": spent, "days_elapsed": days, "days_in_month": total_days,
		"projected": projected, "budget": limit, "overshoot": max(projected - limit, 0) if limit
		else None, "method": "straight-line from spend so far"}


@tool()
def propose_budget(s, user_id: int) -> dict:
	t = now()
	rows = _rows(s, user_id, month_start(t, 3), month_start(t))
	lines = [{"category": r["category"], "average": round(r["total"] / 3),
		"limit": max(int(math.floor(r["total"] / 3 * (100 - SAVINGS_TARGET_PCT) / 100 / 100) * 100),
			100)} for r in _by_category(rows)]
	return {"lines": lines, "total": sum(l["limit"] for l in lines),
		"savings_target_pct": SAVINGS_TARGET_PCT,
		"assumption": f"Average of the last 3 full months, less a {SAVINGS_TARGET_PCT}% savings "
			"target. Change any line you like."}


@tool(side_effect=True)
def set_budget(s, user_id: int, category: str, monthly_limit: float) -> dict:
	if category not in SPEND_CATEGORIES:
		raise ToolError("unknown_category")
	b = s.query(Budget).filter_by(user_id=user_id, category=category).first()
	if not b:
		b = Budget(user_id=user_id, category=category, monthly_limit=monthly_limit)
		s.add(b)
	b.monthly_limit = monthly_limit
	return {"category": category, "limit": round(monthly_limit)}


@tool(side_effect=True)
def plan_goal(s, user_id: int, name: str, target: float, months: int) -> dict:
	g = Goal(user_id=user_id, name=name, target=target, months=months)
	s.add(g)
	s.flush()
	return {"goal_id": g.id, "name": name, "target": round(target), "months": months,
		"per_month": math.ceil(target / months)}


@tool(side_effect=True)
def set_category(s, user_id: int, merchant_name: str, category: str) -> dict:
	"""The user corrects a category. It applies to past and future spends at that payee."""
	if category not in SPEND_CATEGORIES:
		raise ToolError("unknown_category")
	fix = s.query(CategoryCorrection).filter_by(user_id=user_id, merchant_name=merchant_name).first()
	old = fix.category if fix else None
	if not fix:
		fix = CategoryCorrection(user_id=user_id, merchant_name=merchant_name, category=category)
		s.add(fix)
	fix.category = category
	n = s.query(Transaction).filter_by(user_id=user_id, merchant_name=merchant_name).update(
		{"category": category})
	return {"merchant": merchant_name, "category": category, "previous": old, "updated": n}
