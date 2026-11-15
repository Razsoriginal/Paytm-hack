"""C5: spend questions and budgets. The intent picks a tool; replies are built only from
that tool's result, so no figure in an answer is invented or added up by a model.
Informational only: this is not financial advice."""
from sqlalchemy.orm import Session

from .. import analytics, privacy, tools  # noqa: F401  (analytics registers its tools)
from ..db import Case, User
from ..policy import ProposedAction as PA
from ..services import close_case, execute, impact, run_log
from .flows import out

NOTE = " This is information about your own spending, not financial advice."


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> dict:
	return {"columns": [label for _, label in cols],
		"rows": [[r.get(k) if r.get(k) is not None else "–" for k, _ in cols] for r in rows]}


def answer(s: Session, user: User, intent, case: Case) -> dict:
	uid = user.id
	if not privacy.consent_state(s, uid)["spend_insights"]:
		close_case(s, case)
		return out("Spend insights are switched off. Turn them on under My data to ask about "
			"your spending.", case)
	action, cat, period = intent.action or "summary", intent.category, intent.period
	base = dict(actor_type="consumer", actor_id=uid, case_id=case.id, agent="Insights")
	call = lambda name, **args: tools.call(s, name, {"user_id": uid, **args})  # noqa: E731
	table, tool_name = None, ""

	if intent.intent == "budget":
		if action == "goal" and intent.amount and intent.months:
			name = f"{intent.dest} trip" if intent.dest else "Savings goal"
			tool_name = "plan_goal"
			r = execute(s, PA(tool=tool_name, args={"user_id": uid, "name": name,
				"target": intent.amount, "months": intent.months},
				idempotency_key=f"case{case.id}:goal", why="User set a savings goal", **base)).result
			reply = (f"Goal saved: {r['name']}, ₹{r['target']} in {r['months']} months. That is "
				f"₹{r['per_month']} a month.")
		elif action == "set" and cat and intent.amount:
			tool_name = "set_budget"
			r = execute(s, PA(tool=tool_name, args={"user_id": uid, "category": cat,
				"monthly_limit": intent.amount}, idempotency_key=f"case{case.id}:budget",
				why="User set a budget", **base)).result
			reply = f"Done. Your {r['category']} budget is ₹{r['limit']} a month."
		elif action == "status":
			tool_name, r = "budget_status", call("budget_status")
			if not r["budgets"]:
				reply = "You have no budgets yet. Say 'plan a budget for me' and I will suggest one."
			else:
				reply = (f"You have used ₹{r['spent_total']} of ₹{r['limit_total']} across your "
					"budgets this month.")
				table = _table(r["budgets"], [("category", "Category"), ("spent", "Spent ₹"),
					("limit", "Budget ₹"), ("pct", "Used %")])
		else:
			tool_name, r = "propose_budget", call("propose_budget")
			if not r["lines"]:
				reply = "There is not enough history yet to suggest a budget."
			else:
				for i, line in enumerate(r["lines"]):
					execute(s, PA(tool="set_budget", args={"user_id": uid,
						"category": line["category"], "monthly_limit": line["limit"]},
						idempotency_key=f"case{case.id}:budget{i}", why="Proposed budget", **base))
				reply = (f"I have set a starting budget of ₹{r['total']} a month. {r['assumption']} "
					"For example: 'food budget 3000'.")
				table = _table(r["lines"], [("category", "Category"), ("average", "3-month average ₹"),
					("limit", "Budget ₹")])
	elif action == "compare":
		tool_name, r = "compare_periods", call("compare_periods")
		way = "less" if r["change"] < 0 else "more"
		reply = (f"You spent ₹{r['a']['total']} {r['a']['period']}, against ₹{r['b']['total']} "
			f"{r['b']['period']}. That is ₹{abs(r['change'])} {way}.")
		table = _table(r["by_category"], [("category", "Category"), ("a", "This month ₹"),
			("b", "Last month ₹"), ("change", "Change ₹")])
	elif action == "top":
		tool_name, r = "spend_summary", call("spend_summary", period=period)
		top = r["by_category"][:3]
		reply = (f"Your top categories {r['period']}: " + ", ".join(
			f"{x['category']} ₹{x['total']}" for x in top) + ".") if top else \
			f"I see no spending {r['period']}."
		table = _table(top, [("category", "Category"), ("total", "Spent ₹"), ("count", "Payments")])
	elif action == "anomalies":
		tool_name, r = "detect_anomalies", call("detect_anomalies", period=period or "last_month")
		if r["unusual"]:
			x = r["unusual"][0]
			reply = (f"{len(r['unusual'])} unusual spend {r['period']}. The largest is ₹{x['amount']} "
				f"at {x['merchant']}; your usual {x['category']} spend is about ₹{x['usual']}.")
			table = _table(r["unusual"], [("date", "Date"), ("merchant", "Where"),
				("amount", "Amount ₹"), ("usual", "Usual ₹")])
		else:
			reply = f"Nothing unusual {r['period']}."
	elif action == "forecast":
		tool_name, r = "forecast_month_end", call("forecast_month_end")
		reply = (f"You have spent ₹{r['spent']} in {r['days_elapsed']} days. At this pace you "
			f"will reach about ₹{r['projected']} by month end.")
		if r["budget"]:
			reply += (f" That is ₹{r['overshoot']} over your ₹{r['budget']} budget." if r["overshoot"]
				else f" That is inside your ₹{r['budget']} budget.")
		else:
			reply += " Set a budget and I can tell you whether you will overshoot it."
	elif action == "recurring":
		tool_name, r = "detect_recurring", call("detect_recurring")
		reply = (f"I found {len(r['recurring'])} regular payments." if r["recurring"]
			else "I found no regular payments.")
		table = _table(r["recurring"], [("merchant", "Payee"), ("amount", "Amount ₹"),
			("months", "Months seen")])
	elif action == "trend":
		tool_name, r = "spend_trend", call("spend_trend", category=cat)
		reply = f"Your {cat or 'total'} spending by month."
		table = _table(r["months"], [("month", "Month"), ("total", "Spent ₹")])
	else:
		tool_name, r = "spend_summary", call("spend_summary", period=period, category=cat)
		what = f"on {cat} " if cat else ""
		reply = f"You spent ₹{r['total']} {what}{r['period']}, across {r['count']} payments."
		if not cat:
			table = _table(r["by_category"], [("category", "Category"), ("total", "Spent ₹"),
				("count", "Payments")])
	run_log(s, case.id, "Insights", "measure", f"Answered from {tool_name.replace('_', ' ')}")
	impact(s, "spend_question", "consumer", uid, tool=tool_name)
	close_case(s, case)
	return out(reply + (NOTE if intent.intent == "spend" else ""), case, table=table,
		tool=tool_name, result=r)
