"""Booking management, bill upload, spend insights and inventory."""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func

from saathi import bills, changequote, policy, privacy, seed, services, tools
from saathi.adapters import docai as docai_mod
from saathi.adapters.docai import DocumentExtractor, make_pdf
from saathi.agents import manager, stock
from saathi.api import policy_violations
from saathi.config import settings
from saathi.db import (
	Action, AuditLog, BillPayment, BookingItem, Case, Cohort, Escalation, Inventory, Merchant,
	Product, StockMovement, Transaction, Upload, Wallet, now,
)
from saathi.policy import ProposedAction as PA

U = seed.U_BILLS
M = seed.M_INVENTORY


def bal(s, uid=U):
	s.expire_all()
	return s.get(Wallet, uid).balance


def sample(name: str) -> bytes:
	return (Path(settings.DATA_DIR) / "samples" / name).read_bytes()


def far(days: int) -> str:
	return (now() + timedelta(days=days)).date().isoformat()


def pay_bill(s, name, values=None, ticks=(), ack=False):
	r = bills.review(s, U, sample(name), name)
	c = bills.confirm(s, U, r["upload_id"], values or {}, list(ticks), ack)
	return r, c, manager.approve(s, c["approval"]["id"], True) if c.get("approval") else None


# ---------------- C3: extraction and bill payment ----------------
@pytest.mark.parametrize("name,biller,number,amount", [
	("01-electricity.pdf", "MSEDCL Electricity", "170012345678", "1,240.00"),
	("02-gas.pdf", "Mahanagar Gas", "2100456789", "640.00"),
	("03-broadband.pdf", "ACT Broadband", "55012345", "1,179.00"),
	("04-water-photo.png", "BMC Water", "MH40012345", "412.00"),
	("06-credit-card.pdf", "SimBank Credit Card", "XXXXXXXXXXXX4417", "8,450.00"),
	("07-electricity-hinglish.pdf", "MSEDCL Electricity", "170098765432", "980.00"),
])
def test_field_level_accuracy(s, name, biller, number, amount):
	r = bills.review(s, U, sample(name), name)
	got = {f["name"]: f for f in r["fields"]}
	assert (got["biller"]["value"], got["consumer_number"]["value"],
		got["amount_due"]["value"]) == (biller, number, amount)
	assert r["state"] == "clean" and all(f["confidence"] == 1.0 for f in r["fields"])
	assert got["consumer_number"]["display"].endswith(number[-4:])
	assert number[:-4] not in got["consumer_number"]["display"]  # masked in the UI


def test_confidence_is_computed_from_the_text():
	text = "Amount due: 1,240.00\nConsumer number: 98200 12345"
	assert bills.confidence("1,240.00", text, True) == 1.0
	assert bills.confidence("1,240.00", text, False) == 0.6  # printed, but fails its check
	assert bills.confidence("9820012345", text, True) == 0.6  # only matches without spaces
	assert bills.confidence("5,000.00", text, True) == 0.3  # not on the page at all


def test_low_confidence_never_pays(s):
	before = bal(s)
	r = bills.review(s, U, sample("08-blurry-photo.png"))
	assert r["state"] == "unreadable"
	c = bills.confirm(s, U, r["upload_id"], {}, [])
	assert c["state"] == "needs_confirmation" and "approval" not in c
	# Even a forged action cannot pay from an upload the user has not confirmed.
	d = policy.decide(s, PA(tool="pay_uploaded_bill", actor_type="consumer", actor_id=U,
		idempotency_key="x", amount=1240, args={"user_id": U, "upload_id": r["upload_id"],
			"biller": "MSEDCL Electricity", "consumer_number": "170012345678", "amount_due": 1240.0}))
	assert (d.outcome, d.reason) == ("blocked", "unconfirmed_extraction")
	assert bal(s) == before


def test_partial_match_must_be_confirmed(s):
	r = bills.review(s, U, sample("05-phone-spaced-number.pdf"))
	assert r["state"] == "review"
	assert bills.confirm(s, U, r["upload_id"], {}, [])["state"] == "needs_confirmation"
	c = bills.confirm(s, U, r["upload_id"], {"consumer_number": "9820012345"}, [])
	assert c["state"] == "approval"


def test_pay_duplicate_and_double_submit(s):
	before = bal(s)
	_, c, paid = pay_bill(s, "01-electricity.pdf")
	assert "Paid ₹1240" in paid["reply"] and bal(s) == before - 1240
	manager.approve(s, c["approval"]["id"], True)  # double submit
	assert bal(s) == before - 1240
	again = bills.review(s, U, sample("01-electricity.pdf"))
	assert again["state"] == "duplicate" and bal(s) == before - 1240
	assert bills.review(s, U, sample("09-duplicate.pdf"))["state"] == "duplicate"
	assert s.query(BillPayment).filter_by(user_id=U, bill_number="MS-2026-11-4471".replace(
		"2026-11", f"{now():%Y-%m}")).count() == 1


def test_anomaly_flagged_and_needs_acknowledgement(s):
	r = bills.review(s, U, sample("10-anomaly.pdf"))
	assert r["flags"]["anomaly"]["times"] >= settings.BILL_ANOMALY_FACTOR
	assert bills.confirm(s, U, r["upload_id"], {}, [])["state"] == "anomaly"
	assert bills.confirm(s, U, r["upload_id"], {}, [], True)["state"] == "approval"


def test_prompt_injection_in_a_bill_is_ignored(s):
	before = bal(s)
	r, _, paid = pay_bill(s, "13-injection.pdf")
	got = {f["name"]: f["value"] for f in r["fields"]}
	assert got["amount_due"] == "1,179.00" and got["biller"] == "ACT Broadband"
	assert bal(s) == before - 1179 and "50" not in paid["reply"].replace("₹1179", "")
	assert not s.query(Transaction).filter(Transaction.amount >= 50000).count()


def test_unknown_biller_escalates(s):
	before = bal(s)
	r = bills.review(s, U, sample("11-unknown-biller.pdf"))
	assert r["state"] == "escalated" and bal(s) == before
	assert s.query(Escalation).filter_by(reason="unknown_biller").count() == 1


def test_past_due_date_is_highlighted(s):
	r = bills.review(s, U, sample("12-past-due.pdf"))
	due = next(f for f in r["fields"] if f["name"] == "due_date")
	assert r["state"] == "review" and due["state"] == "confirm" and "passed" in due["issue"]


# ---------------- uploads ----------------
def test_upload_rules(s, monkeypatch):
	with pytest.raises(bills.UploadRejected):
		bills.save_upload(s, "consumer", U, "bill", b"GIF89a not allowed")
	with pytest.raises(bills.UploadRejected):
		bills.save_upload(s, "consumer", U, "bill", b"")
	monkeypatch.setattr(settings, "UPLOAD_MAX_MB", 0.0001)
	with pytest.raises(bills.UploadRejected):
		bills.save_upload(s, "consumer", U, "bill", sample("01-electricity.pdf"))


def test_metadata_stripped_and_file_deleted_after_retention(s):
	u = bills.save_upload(s, "consumer", U, "bill", sample("04-water-photo.png"))
	stored = Path(u.path)
	assert stored.is_file() and b"tEXt" not in stored.read_bytes()
	assert bills.purge_uploads(s) == 0
	u.created_at = now() - timedelta(hours=settings.UPLOAD_RETENTION_HOURS + 1)
	assert bills.purge_uploads(s) == 1 and not stored.exists() and u.deleted_at


def test_consumer_numbers_masked_in_logs(s):
	pay_bill(s, "01-electricity.pdf")
	logs = s.query(AuditLog).filter(AuditLog.tool == "pay_uploaded_bill").all()
	assert logs and all("170012345678" not in str(l.inputs) + str(l.outputs) for l in logs)
	assert all("170012345678" not in str(a.args) for a in s.query(Action).all())
	assert bills.mask("170012345678") == "••••••••5678"


# ---------------- Document AI adapter ----------------
class FakeHTTP:
	"""Stands in for Sarvam: records every request and walks a scripted list of statuses."""

	def __init__(self, statuses, fields=None):
		self.statuses, self.fields, self.calls, self.jobs = list(statuses), fields or {}, [], 0

	def __call__(self, method, url, **kw):
		self.calls.append((method, url, kw))
		body, text = {}, ""
		if url.endswith(("/extract", "/digitise")):
			self.jobs += 1
			body = {"job_id": f"j{self.jobs}", "status": "pending"}
		elif url.endswith("/status"):
			body = {"status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}
		elif url.endswith("/results"):
			body = {"output": {"data": self.fields}}
		elif url.endswith("/download-url"):
			body = {"method": "GET", "url": "https://files.example/out.md"}
		else:
			text = "Amount due: 500.00"
		return type("R", (), {"json": lambda self: body, "text": text})()


@pytest.fixture()
def live(monkeypatch, tmp_path):
	monkeypatch.setattr(settings, "SARVAM_MODE", "live")
	monkeypatch.setattr(settings, "SARVAM_API_KEY", "test-key")
	monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
	monkeypatch.setattr(settings, "DOC_JOB_TIMEOUT_SEC", 0.2)


def test_docai_job_lifecycle_cache_and_parameters(live):
	http = FakeHTTP(["pending", "running", "completed"], {"amount_due": "500.00"})
	ex = DocumentExtractor(http=http, sleep=lambda _: None)
	out = ex.read(b"%PDF-x", "bill.pdf", "application/pdf", bills.BILL_SCHEMA)
	assert out["status"] == "completed" and out["fields"]["amount_due"] == "500.00"
	assert "500.00" in out["text"] and not out["cached"]
	posts = [kw["data"] for m, u, kw in http.calls if m == "POST"]
	assert posts[0].keys() == {"language", "schema"}
	assert posts[1] == {"language": "en-IN", "output_format": "md"}
	assert not any("language_code" in d or d.get("output_format") == "markdown" for d in posts)
	n = len(http.calls)
	assert ex.read(b"%PDF-x", "bill.pdf", "application/pdf", bills.BILL_SCHEMA)["cached"]
	assert len(http.calls) == n  # served from the file-hash cache
	with pytest.raises(docai_mod.DocAIError):
		docai_mod._form("en-IN", "markdown")


def test_docai_timeout_retry_partial_and_failed(live):
	slow = FakeHTTP(["pending"])
	out = DocumentExtractor(http=slow, sleep=lambda _: None).read(b"%PDF-a", "a.pdf",
		"application/pdf", bills.BILL_SCHEMA)
	assert out["status"] == "failed" and slow.jobs == 2  # one retry, then give up
	part = FakeHTTP(["partially_completed"], {"biller": "ACT Broadband"})
	out = DocumentExtractor(http=part, sleep=lambda _: None).read(b"%PDF-b", "b.pdf",
		"application/pdf", bills.BILL_SCHEMA)
	assert out["status"] == "partially_completed" and out["fields"]["biller"] == "ACT Broadband"
	bad = FakeHTTP(["failed"])
	assert DocumentExtractor(http=bad, sleep=lambda _: None).read(b"%PDF-c", "c.pdf",
		"application/pdf", bills.BILL_SCHEMA)["status"] == "failed"


def test_docai_mock_reads_fixtures_only():
	ex = DocumentExtractor(http=lambda *a, **k: pytest.fail("mock mode must not call out"))
	out = ex.read(sample("02-gas.pdf"), "gas.pdf", "application/pdf", bills.BILL_SCHEMA)
	assert out["mode"] == "mock" and out["fields"]["biller"] == "Mahanagar Gas"
	other = ex.read(make_pdf(["hello"]) + b"%", "x.pdf", "application/pdf", bills.BILL_SCHEMA)
	assert other["fields"]["biller"] == ""


# ---------------- C4: booking management ----------------
def book(s, uid, text):
	r = manager.handle(s, "consumer", uid, text)
	x = manager.approve(s, r["approval"]["id"], True)
	while x.get("approval"):
		x = manager.approve(s, x["approval"]["id"], True)
	return x["booking_id"]


def test_quote_matches_the_policy_fixtures(s):
	bid = book(s, 1, f"Mumbai se Pune {far(30)} 1 raat")
	items = s.query(BookingItem).filter_by(booking_id=bid).all()
	q = tools.call(s, "quote_cancellation", {"booking_id": bid})
	for line, it in zip(q["lines"], items):
		pct = {"train": 10, "hotel": 0}[it.kind]  # more than 48 hours before departure
		assert line["fee_pct"] == pct and line["refund"] == it.price - round(it.price * pct / 100)
	assert q["refund_total"] == sum(l["refund"] for l in q["lines"])
	r = tools.call(s, "quote_change", {"booking_id": bid, "new_date": far(40)})
	fee = sum(changequote.POLICY[it.kind]["change_fee"] * it.units for it in items)
	assert (r["fee_total"], r["fare_diff"], r["pay_extra"], r["refund_total"]) == (fee, 0, fee, 0)
	assert changequote.cancel_pct("flight", 30) == 30 and changequote.cancel_pct("bus", -1) == 100


def test_reschedule_cancel_and_refund_recorded(s):
	bid = book(s, 1, f"Mumbai se Pune {far(30)} 1 raat")
	before = bal(s, 1)
	r = manager.handle(s, "consumer", 1, f"reschedule booking {bid} to {far(40)}")
	fee = r["approval"]["payload"]["pay_extra"]
	assert "Moved" in manager.approve(s, r["approval"]["id"], True)["reply"]
	assert bal(s, 1) == before - fee
	assert {it.details["date"] for it in s.query(BookingItem).filter_by(booking_id=bid)} == {far(40)}
	r = manager.handle(s, "consumer", 1, f"cancel booking {bid}")
	refund = r["approval"]["payload"]["refund_total"]
	assert "Cancelled" in manager.approve(s, r["approval"]["id"], True)["reply"]
	assert bal(s, 1) == before - fee + refund
	assert s.query(Transaction).filter_by(user_id=1, kind="refund", amount=refund).count() == 1
	assert tools.call(s, "get_booking", {"booking_id": bid})["status"] == "cancelled"


def test_changed_price_invalidates_the_approval(s):
	bid = book(s, 1, f"Mumbai se Pune {far(30)} 1 raat")
	before = bal(s, 1)
	r = manager.handle(s, "consumer", 1, f"reschedule booking {bid} to {far(40)}")
	it = s.query(BookingItem).filter_by(booking_id=bid, kind="hotel").one()
	s.get(Inventory, it.inventory_id).price += 300  # the partner reprices before approval
	s.commit()
	x = manager.approve(s, r["approval"]["id"], True)
	assert "changed" in x["reply"] and bal(s, 1) == before
	assert x["approval"]["payload"]["fare_diff"] == 300
	assert it.details["date"] == far(30)


def test_multi_item_change_rolls_back(s):
	bid = book(s, 12, f"Delhi se Jaipur {far(30)} train se 1 raat")
	before = bal(s, 12)
	r = manager.handle(s, "consumer", 12, f"reschedule to {far(40)}")
	x = manager.approve(s, r["approval"]["id"], True)
	assert "unchanged" in x["reply"] and bal(s, 12) == before
	assert {it.details["date"] for it in s.query(BookingItem).filter_by(booking_id=bid)} == {far(30)}
	assert not policy_violations(s)


def test_concern_ticket_has_evidence_and_duplicate_charge_refunds(s):
	bid = book(s, 1, f"Mumbai se Pune {far(30)} 1 raat")
	r = manager.handle(s, "consumer", 1, "poor service, I want to complain")
	t = tools.call(s, "get_ticket_status", {"ticket_id": r["ticket_id"]})
	assert t["evidence"]["booking"]["id"] == bid and t["evidence"]["payments"]
	assert s.query(Escalation).filter_by(reason="concern_needs_review").count() == 1
	before = bal(s, seed.U_DOUBLE_CHARGE)
	x = manager.handle(s, "consumer", seed.U_DOUBLE_CHARGE, "I was charged twice")
	assert "refunded" in x["reply"] and bal(s, seed.U_DOUBLE_CHARGE) == before + 1450


# ---------------- C5: spend insights ----------------
def numbers(x) -> set[str]:
	if isinstance(x, dict):
		return set().union(*(numbers(v) for v in x.values())) if x else set()
	if isinstance(x, list):
		return set().union(*(numbers(v) for v in x)) if x else set()
	return {str(abs(int(x)))} if isinstance(x, (int, float)) and not isinstance(x, bool) else set()


@pytest.mark.parametrize("question", [
	"How much did I spend on food last month?", "Compare this month to last",
	"What are my top 3 categories?", "Any unusual spends last month?",
	"Will I overshoot my budget?", "plan a budget for me", "budget status",
])
def test_every_number_in_an_answer_comes_from_a_tool(s, question):
	manager.handle(s, "consumer", U, "food budget 3000")
	r = manager.handle(s, "consumer", U, question)
	said = set(re.findall(r"\d+", r["reply"].replace(",", "")))
	allowed = numbers(r["result"]) | {"3", "10", "3000"}  # "top 3", "10% savings", the example
	assert said <= allowed, said - allowed


def test_spend_summary_matches_sql_ground_truth(s):
	r = tools.call(s, "spend_summary", {"user_id": U, "period": "last_month", "category": "food"})
	start, end, _ = __import__("saathi.analytics", fromlist=["x"]).period_range("last_month")
	truth = s.query(func.sum(Transaction.amount)).filter(Transaction.user_id == U,
		Transaction.category == "food", Transaction.status == "success",
		Transaction.order_state == "fulfilled", Transaction.created_at >= start,
		Transaction.created_at < end).scalar()
	assert r["total"] == round(truth)
	everything = tools.call(s, "spend_summary", {"user_id": U, "period": "last_month"})
	assert everything["total"] == sum(c["total"] for c in everything["by_category"])


def test_category_correction_persists(s):
	name = s.query(Transaction.merchant_name).filter_by(user_id=U, category="food").first()[0]
	before = tools.call(s, "spend_summary", {"user_id": U, "period": "last_month"})
	res = services.execute(s, PA(tool="set_category", actor_type="consumer", actor_id=U,
		idempotency_key="c1", args={"user_id": U, "merchant_name": name, "category": "groceries"}))
	assert res.ok and res.result["updated"] > 0
	assert not s.query(Transaction).filter_by(user_id=U, merchant_name=name, category="food").count()
	after = tools.call(s, "spend_summary", {"user_id": U, "period": "last_month"})
	assert after["total"] == before["total"]  # moved between categories, nothing lost
	from saathi.analytics import categorise
	m = s.query(Merchant).filter_by(name=name).first()
	assert categorise(s, U, "payment", m) == "groceries"  # remembered for the next spend


def test_budget_alert_forecast_and_consent(s):
	spent = tools.call(s, "spend_summary", {"user_id": U, "category": "groceries"})["total"]
	services.execute(s, PA(tool="set_budget", actor_type="consumer", actor_id=U,
		idempotency_key="b1", args={"user_id": U, "category": "groceries",
			"monthly_limit": float(spent)}))
	row = tools.call(s, "budget_status", {"user_id": U})["budgets"][0]
	assert row["pct"] == 100 and row["alert"]
	f = tools.call(s, "forecast_month_end", {"user_id": U})
	assert f["projected"] == round(f["spent"] / f["days_elapsed"] * f["days_in_month"])
	assert f["overshoot"] == max(f["projected"] - f["budget"], 0)
	privacy.set_consent(s, U, "spend_insights", False)
	assert "switched off" in manager.handle(s, "consumer", U, "how much did I spend")["reply"]


def test_no_spend_data_reaches_merchant_cohorts(s):
	before = {c["key"]: c["size"] for c in privacy.visible_cohorts(s, seed.M_BRIDGE)}
	manager.handle(s, "consumer", U, "plan a budget for me")
	pay_bill(s, "02-gas.pdf")
	assert {c["key"]: c["size"] for c in privacy.visible_cohorts(s, seed.M_BRIDGE)} == before
	for c in s.query(Cohort).all():  # cohorts are built from stated preferences only
		assert all(part.split(":")[0] in ("cuisine", "budget", "travel_mode")
			for part in c.key.split("+"))


# ---------------- M1: inventory ----------------
def test_stock_never_negative_and_undo_restores(s):
	p = s.query(Product).filter_by(merchant_id=M, name="Parle-G").one()
	start, cost = p.stock, p.cost
	r = manager.handle(s, "merchant", M, "Add 20 packets of Parle-G at ₹9, selling ₹11")
	assert p.stock == start + 20 and p.cost == 9 and r["undo"]
	manager.handle(s, "merchant", M, "Mark 5 Parle-G as damaged")
	assert p.stock == start + 15
	manager.handle(s, "merchant", M, "undo")
	manager.handle(s, "merchant", M, "undo")
	assert (p.stock, p.cost) == (start, cost)
	x = manager.handle(s, "merchant", M, "Mark 9999 Parle-G as damaged")
	assert "insufficient stock" in x["reply"] and p.stock == start
	assert s.query(func.min(Product.stock)).scalar() >= 0
	moves = s.query(StockMovement).filter_by(product_id=p.id).filter(
		StockMovement.reason != "sale").all()
	assert sum(m.delta for m in moves) == start  # every change left a movement


def test_undo_window_and_tier_rules(s, monkeypatch):
	manager.handle(s, "merchant", M, "Add 3 packets of Parle-G at ₹8, selling ₹10")
	monkeypatch.setattr(settings, "STOCK_UNDO_MINUTES", 0)
	assert "undo window passed" in manager.handle(s, "merchant", M, "undo")["reply"]
	t1 = seed.M_T1_STOCK
	stock0 = s.query(Product).filter_by(merchant_id=t1, name="Parle-G").one().stock
	x = manager.handle(s, "merchant", t1, "Add 20 packets of Parle-G at ₹8, selling ₹10")
	assert "Soundbox plan" in x["reply"]
	assert s.query(Product).filter_by(merchant_id=t1, name="Parle-G").one().stock == stock0
	assert "left" in manager.handle(s, "merchant", t1, "How much rice is left?")["reply"]


def test_reorder_follows_the_velocity_rule(s):
	import math

	from saathi import inventory
	low = tools.call(s, "get_low_stock", {"merchant_id": M})["products"]
	assert low and all(r["days_left"] is None or r["days_left"] < settings.LOW_STOCK_DEFAULT_DAYS
		or r["stock"] <= s.get(Product, r["product_id"]).reorder_level for r in low)
	for r in low:
		vel = inventory.velocity(s, r["product_id"])
		want = math.ceil(vel * settings.REORDER_COVER_DAYS
			* inventory.FESTIVAL_UPLIFT.get(now().month, 1.0) - r["stock"])
		assert r["reorder_qty"] == max(want, 0)


def test_stock_upload_needs_review_and_approval(s):
	m = s.get(Merchant, M)
	r = stock.review_upload(s, m, sample("21-handwritten-stock.png"))
	assert r["state"] == "review" and [i["state"] for i in r["items"]] == ["ok", "confirm"]
	r = stock.review_upload(s, m, sample("20-supplier-invoice.pdf"))
	before = {p.name: p.stock for p in s.query(Product).filter_by(merchant_id=M)}
	c = stock.confirm_upload(s, m, r["upload_id"], r["items"])
	assert c["approval"]["payload"]["total"] == sum(i["qty"] * i["rate"] for i in r["items"])
	assert {p.name: p.stock for p in s.query(Product).filter_by(merchant_id=M)} == before
	assert "Added 4 items" in manager.approve(s, c["approval"]["id"], True)["reply"]
	after = {p.name: p.stock for p in s.query(Product).filter_by(merchant_id=M)}
	assert after["Parle-G"] == before["Parle-G"] + 120 and after["Dairy Milk 10"] == 48
	assert s.query(Upload).filter_by(id=r["upload_id"]).one().status == "paid"
	assert not policy_violations(s) and s.query(Case).count()
