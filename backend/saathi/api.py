"""HTTP API and the single-page UI."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from statistics import median

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bills, db, privacy, seed, tools
from .adapters.docai import docai
from .adapters.llm import SPEND_CATEGORIES, llm
from .adapters.memory import memory
from .adapters.paytm_pg import pg
from .adapters.speech import speech
from .agents import flows, manage, manager, stock
from .config import settings
from .db import (
	Action, AgentRun, Approval, AuditLog, Booking, BookingItem, Case, Escalation, ImpactEvent,
	KhataEntry, Merchant, Notification, Offer, Refund, User, Wallet, now,
)
from .policy import AUTONOMY, has_consent
from .policy import ProposedAction as PA
from .services import execute, inbox, open_case, resolve_escalation, run_log

app = FastAPI(title="Saathi (simulated data)")
app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"],
	allow_headers=["*"], expose_headers=["X-Saathi-Mode"])
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if (DIST / "assets").is_dir():
	app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def S():
	return db.get_session()


def rows(q):
	return [r.as_dict() for r in q]


class Chat(BaseModel):
	actor_type: str
	actor_id: int
	text: str
	language: str = ""


class Decide(BaseModel):
	approve: bool
	via: str = "button"


class ConsentIn(BaseModel):
	scope: str
	granted: bool


class PrefIn(BaseModel):
	category: str
	value: str


class RedeemIn(BaseModel):
	offer_id: int
	bill_amount: float


class Reply(BaseModel):
	reply: str


class BillConfirm(BaseModel):
	values: dict = {}
	confirmed: list[str] = []
	acknowledge_anomaly: bool = False


class ChangeIn(BaseModel):
	action: str  # reschedule | cancel | concern
	new_date: str | None = None
	item_id: int | None = None
	text: str = ""


class CategoryIn(BaseModel):
	merchant_name: str
	category: str


class StockConfirm(BaseModel):
	items: list[dict]


class UndoIn(BaseModel):
	movement_id: int | None = None


async def _file(sample: str, file: UploadFile | None) -> tuple[bytes, str]:
	"""An uploaded file, or one of the seeded samples by name."""
	if file is not None:
		return await file.read(), file.filename or "upload"
	folder = (settings.DATA_DIR / "samples").resolve()
	path = (folder / sample).resolve()
	if not sample or path.parent != folder or not path.is_file():
		raise HTTPException(400, "Choose a file to upload.")
	return path.read_bytes(), path.name


@app.get("/")
def index():
	if (DIST / "index.html").is_file():
		return FileResponse(DIST / "index.html")
	return HTMLResponse("<p>The UI is not built yet. Run <code>make ui</code>, then reload.</p>", 503)


@app.get("/api/bootstrap")
def bootstrap():
	with S() as s:
		return {"simulated": True, "users": rows(s.query(User).filter(User.id <= 20)),
			"merchants": rows(s.query(Merchant).filter(Merchant.id <= 10)),
			"partners": partners(), "scenarios": list(SCENARIOS),
			"limits": {k: getattr(settings, k) for k in ("AUTO_ACTION_LIMIT_INR",
				"ESCALATE_ABOVE_INR", "CONSUMER_DAILY_SPEND_CAP_INR", "COHORT_MIN_SIZE",
				"HOLDOUT_PERCENT", "PLAN_APPROVAL_TTL_MIN")}}


@app.get("/api/partners")
def partners():
	return {"sarvam_llm": llm.health(), "sarvam_speech": speech.health(),
		"document_ai": docai.health(), "cognee": memory.health(), "paytm_pg": pg.health()}


@app.get("/api/samples")
def samples(purpose: str = ""):
	f = settings.DATA_DIR / "samples" / "manifest.json"
	rows_ = json.loads(f.read_text()) if f.is_file() else []
	return [r for r in rows_ if not purpose or r["purpose"] == purpose]


@app.post("/api/chat")
def chat(c: Chat):
	with S() as s:
		return manager.handle(s, c.actor_type, c.actor_id, c.text, c.language)


@app.post("/api/voice")
async def voice(actor_type: str = Form(...), actor_id: int = Form(...), language: str = Form(""),
		hint: str = Form(""), audio: UploadFile | None = File(None)):
	data = await audio.read() if audio else b""
	st = speech.stt(data, language, hint)
	if not st["transcript"]:
		return {"reply": "I could not hear that. Please try again.", "stt": st}
	with S() as s:
		res = manager.handle(s, actor_type, actor_id, st["transcript"], language)
	return {**res, "stt": st}


@app.get("/api/tts")
def tts(text: str, language: str = ""):
	o = speech.tts(text, language)
	return Response(o["wav"], media_type="audio/wav", headers={"X-Saathi-Mode": o["mode"]})


@app.get("/api/approvals")
def approvals(actor_type: str, actor_id: int):
	with S() as s:
		return rows(s.query(Approval).filter_by(actor_type=actor_type, actor_id=actor_id,
			status="pending").order_by(Approval.id.desc()))


@app.post("/api/approvals/{aid}")
def decide(aid: int, d: Decide):
	with S() as s:
		if not s.get(Approval, aid):
			raise HTTPException(404)
		return manager.approve(s, aid, d.approve, d.via)


@app.get("/api/consumer/{uid}")
def consumer(uid: int):
	with S() as s:
		bookings = []
		for b in s.query(Booking).filter_by(user_id=uid).order_by(Booking.id.desc()):
			bookings.append({**b.as_dict(),
				"items": rows(s.query(BookingItem).filter_by(booking_id=b.id))})
		return {"user": s.get(User, uid).as_dict(), "wallet": s.get(Wallet, uid).balance,
			"bookings": bookings, "consents": privacy.consent_state(s, uid),
			"tickets": [tools.call(s, "get_ticket_status", {"ticket_id": c.id}) for c in
				s.query(Case).filter_by(actor_type="consumer", actor_id=uid, workflow="ticket")
				.order_by(Case.id.desc())],
			"consent_text": privacy.SCOPES,
			"preferences": rows(privacy.preferences(s, uid, for_use=False)),
			"refunds": rows(s.query(Refund).filter_by(user_id=uid)),
			"inbox": rows(s.query(Notification).filter_by(actor_type="consumer", actor_id=uid)
				.order_by(Notification.id.desc()).limit(20))}


@app.post("/api/consumer/{uid}/consent")
def consent(uid: int, c: ConsentIn):
	with S() as s:
		privacy.set_consent(s, uid, c.scope, c.granted)
		privacy.refresh_all_cohorts(s)
		s.commit()
		return privacy.consent_state(s, uid)


@app.post("/api/consumer/{uid}/prefs")
def add_pref(uid: int, p: PrefIn):
	with S() as s:
		try:
			privacy.set_preference(s, uid, p.category, p.value)
		except privacy.PreferenceRejected as e:
			raise HTTPException(400, str(e))
		s.commit()
		return {"ok": True}


@app.delete("/api/consumer/{uid}/prefs/{pid}")
def del_pref(uid: int, pid: int):
	with S() as s:
		ok = privacy.delete_preference(s, uid, pid)
		s.commit()
		return {"ok": ok}


@app.delete("/api/consumer/{uid}/prefs")
def del_all(uid: int):
	with S() as s:
		privacy.delete_all(s, uid)
		s.commit()
		return {"ok": True}


@app.post("/api/consumer/{uid}/redeem")
def redeem(uid: int, r: RedeemIn):
	with S() as s:
		if not has_consent(s, uid, "offers"):
			raise HTTPException(403, "offers consent is off")
		res = flows.redeem_request(s, uid, r.offer_id, r.bill_amount)
		s.commit()
		return res


@app.post("/api/consumer/{uid}/bills")
async def bill_upload(uid: int, sample: str = Form(""), file: UploadFile | None = File(None)):
	data, name = await _file(sample, file)
	with S() as s:
		try:
			return bills.review(s, uid, data, name)
		except bills.UploadRejected as e:
			raise HTTPException(400, str(e))


@app.post("/api/consumer/{uid}/bills/{upload_id}/confirm")
def bill_confirm(uid: int, upload_id: int, b: BillConfirm):
	with S() as s:
		return bills.confirm(s, uid, upload_id, b.values, b.confirmed, b.acknowledge_anomaly)


@app.post("/api/consumer/{uid}/bookings/{bid}/change")
def booking_change(uid: int, bid: int, c: ChangeIn):
	with S() as s:
		user = s.get(User, uid)
		case = open_case(s, "consumer", uid, "C4", f"{c.action} booking {bid}")
		if c.action == "concern":
			res = manage.concern(s, user, c.text or "Concern about this booking", case, bid)
		else:
			res = manage.request_change(s, user, case, c.action, bid, c.new_date, c.item_id)
		s.commit()
		return res


@app.get("/api/consumer/{uid}/spend")
def spend(uid: int):
	with S() as s:
		if not privacy.consent_state(s, uid)["spend_insights"]:
			return {"enabled": False}
		call = lambda name, **a: tools.call(s, name, {"user_id": uid, **a})  # noqa: E731
		recent = (s.query(db.Transaction).filter(db.Transaction.user_id == uid,
			db.Transaction.kind.in_(("payment", "bill", "recharge", "booking")),
			db.Transaction.status == "success", db.Transaction.order_state == "fulfilled")
			.order_by(db.Transaction.created_at.desc()).limit(15))
		return {"enabled": True, "summary": call("spend_summary"), "trend": call("spend_trend"),
			"budgets": call("budget_status"), "forecast": call("forecast_month_end"),
			"categories": SPEND_CATEGORIES,
			"goals": rows(s.query(db.Goal).filter_by(user_id=uid)),
			"recent": [{"merchant": t.merchant_name, "amount": round(t.amount),
				"category": t.category, "date": t.created_at.date().isoformat()} for t in recent]}


@app.post("/api/consumer/{uid}/category")
def set_category(uid: int, c: CategoryIn):
	with S() as s:
		r = execute(s, PA(tool="set_category", actor_type="consumer", actor_id=uid, agent="Insights",
			args={"user_id": uid, "merchant_name": c.merchant_name, "category": c.category},
			idempotency_key=f"cat:{uid}:{c.merchant_name}:{c.category}:{now().timestamp()}",
			why="User corrected a category"))
		if not r.ok:
			raise HTTPException(400, r.error)
		return r.result


@app.get("/api/merchant/{mid}/stock")
def stock_view(mid: int):
	with S() as s:
		m = s.get(Merchant, mid)
		return {"tier": m.tier, "can_edit": m.tier != "T1",
			"products": tools.call(s, "list_products", {"merchant_id": mid})["products"],
			"low": tools.call(s, "get_low_stock", {"merchant_id": mid}),
			"dead": tools.call(s, "get_dead_stock", {"merchant_id": mid})["products"],
			"undo_minutes": settings.STOCK_UNDO_MINUTES}


@app.post("/api/merchant/{mid}/stock/undo")
def stock_undo(mid: int, u: UndoIn):
	with S() as s:
		r = execute(s, PA(tool="undo_stock_change", actor_type="merchant", actor_id=mid,
			agent="Stock", args={"merchant_id": mid, "movement_id": u.movement_id},
			idempotency_key=f"undo:{mid}:{u.movement_id}:{now().timestamp()}", why="Undo pressed"))
		if not r.ok:
			raise HTTPException(400, r.error.replace("_", " "))
		return r.result


@app.post("/api/merchant/{mid}/stock-upload")
async def stock_upload(mid: int, sample: str = Form(""), file: UploadFile | None = File(None)):
	data, name = await _file(sample, file)
	with S() as s:
		try:
			return stock.review_upload(s, s.get(Merchant, mid), data, name)
		except bills.UploadRejected as e:
			raise HTTPException(400, str(e))


@app.post("/api/merchant/{mid}/stock-upload/{upload_id}/confirm")
def stock_confirm(mid: int, upload_id: int, c: StockConfirm):
	with S() as s:
		return stock.confirm_upload(s, s.get(Merchant, mid), upload_id, c.items)


@app.get("/api/merchant/{mid}")
def merchant(mid: int):
	with S() as s:
		m = s.get(Merchant, mid)
		auto = {**AUTONOMY["merchant"]["*"], **AUTONOMY["merchant"].get(m.tier, {})}
		out = {"merchant": m.as_dict(), "autonomy": auto,
			"cohorts": privacy.visible_cohorts(s, mid),  # counts only, never individuals
			"offers": [flows.bridge_report(s, o.id) for o in s.query(Offer).filter_by(merchant_id=mid)],
			"khata_open": s.query(KhataEntry).filter_by(merchant_id=mid, status="open").count(),
			"inbox": rows(s.query(Notification).filter_by(actor_type="merchant", actor_id=mid)
				.order_by(Notification.id.desc()).limit(20))}
		s.commit()
		return out


@app.get("/api/ops/escalations")
def escalations():
	with S() as s:
		out = []
		for e in s.query(Escalation).order_by(Escalation.id.desc()):
			age = (now() - e.created_at).total_seconds() / 60
			out.append({**e.as_dict(), "minutes_left": round(e.sla_minutes - age)})
		return out


@app.post("/api/ops/escalations/{eid}/resolve")
def resolve(eid: int, r: Reply):
	with S() as s:
		e = resolve_escalation(s, eid, r.reply)
		s.commit()
		return e.as_dict()


@app.get("/api/audit")
def audit_log(limit: int = 80):
	with S() as s:
		return rows(s.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit))


@app.get("/api/runs")
def runs(after: int = 0, limit: int = 60):
	with S() as s:
		q = s.query(AgentRun).filter(AgentRun.id > after).order_by(AgentRun.id.desc()).limit(limit)
		return rows(q)[::-1]


@app.get("/api/runs/stream")
async def stream(after: int = 0):
	async def gen():
		last = after
		while True:
			for r in runs(last):
				last = r["id"]
				yield f"data: {json.dumps(r)}\n\n"
			await asyncio.sleep(1)
	return StreamingResponse(gen(), media_type="text/event-stream")


def policy_violations(s) -> list[str]:
	"""Independent re-check of what actually happened. Must be empty."""
	bad = []
	for a in s.query(Action).filter_by(status="done"):
		if a.decision != "execute":
			bad.append(f"action {a.id} ran with decision {a.decision}")
	for o in s.query(Offer):
		for d in s.query(db.OfferDelivery).filter_by(offer_id=o.id):
			held = s.query(db.ExperimentAssignment).filter_by(experiment=f"offer:{o.id}",
				subject_id=d.user_id, group="holdout").first()
			if held:
				bad.append(f"offer {o.id} delivered to holdout user")
	for c in s.query(db.Cohort):
		pass
	from .adapters.llm import ALLOWED_PREF_CATEGORIES
	for p in s.query(db.Preference):
		if p.category not in ALLOWED_PREF_CATEGORIES:
			bad.append(f"preference {p.id} in a category that is not allowed")
	for it in s.query(BookingItem).filter(BookingItem.status == "held",
			BookingItem.booking_id.is_(None)):
		if it.hold_expires_at and it.hold_expires_at > now():
			bad.append(f"orphan hold {it.id}")
	return bad


def metrics(s) -> dict:
	ev = s.query(ImpactEvent).all()
	def n(kind):
		return sum(1 for e in ev if e.kind == kind)
	def amt(kind, grp=None):
		return sum(e.amount for e in ev if e.kind == kind and (grp is None or e.group == grp))
	def cases(wf, status=None):
		q = s.query(Case).filter(Case.workflow == wf)
		return (q.filter_by(status=status) if status else q).count()
	closed = [e for e in ev if e.kind in ("case_resolved", "case_escalated")]
	secs = [e.meta.get("seconds", 0) for e in closed if e.kind == "case_resolved"]
	c1 = [e.meta.get("seconds", 0) for e in closed if e.meta.get("workflow") == "C1"
		and e.kind == "case_resolved"]
	c2 = cases("C2", "resolved") + cases("C2", "escalated")
	appr = n("approval_approved") + n("approval_rejected") + n("approval_expired")
	offers = [flows.bridge_report(s, o.id) for o in s.query(Offer)]
	pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
	return {
		"simulated": True,
		"consumer": {"trips_booked": n("trip_booked"), "trip_value_inr": amt("trip_booked"),
			"trip_completion_pct": pct(n("trip_booked"), cases("C1")),
			"time_to_book_p50_s": round(median(c1), 2) if c1 else None,
			"saga_recoveries": n("saga_recovered"), "hash_mismatches": n("approval_hash_mismatch"),
			"auto_resolution_pct": pct(n("consumer_auto_resolved"), c2),
			"refunded_inr": amt("consumer_auto_resolved")},
		"merchant": {"auto_resolved": n("merchant_auto_resolved"),
			"khata_recovered_inr": amt("khata_recovered", "treatment"),
			"khata_holdout_inr": amt("khata_recovered", "holdout"),
			"reminders_sent": sum(e.meta.get("count", 0) for e in ev
				if e.kind == "khata_reminders_sent")},
		"features": {"bills_paid": n("bill_paid"), "bills_paid_inr": amt("bill_paid"),
			"bills_read": n("bill_extracted"),
			"extraction_auto_accept_pct": pct(sum(1 for e in ev if e.kind == "bill_extracted"
				and e.meta.get("auto")), n("bill_extracted")),
			"booking_changes": n("booking_change"), "spend_questions": n("spend_question"),
			"low_stock_flagged": sum(e.meta.get("count", 0) for e in ev
				if e.kind == "low_stock_flagged"),
			"stock_added_inr": amt("stock_added")},
		"bridge": offers[-1] if offers else None, "offers": offers,
		"approval_rate_pct": pct(n("approval_approved"), appr),
		"escalation_pct": pct(n("case_escalated"), len(closed)),
		"resolution_p50_s": round(median(secs), 2) if secs else None,
		"policy_violations": policy_violations(s),
	}


@app.get("/api/impact")
def impact_api():
	with S() as s:
		m = metrics(s)
		s.commit()
		return m


# ---------------- scenario runner ----------------
def _say(s, t, i, text, approve=None, log=None):
	r = manager.handle(s, t, i, text)
	log.append({"who": f"{t}:{i}", "said": text, "saathi": r["reply"]})
	while approve is not None and r.get("approval"):
		r = manager.approve(s, r["approval"]["id"], approve, "scenario")
		log.append({"who": f"{t}:{i}", "said": "approve" if approve else "reject",
			"saathi": r["reply"]})
		if not approve:
			break
	return r


def _c2(uid):
	return lambda s, log: _say(s, "consumer", uid, "mere paise kat gaye, refund kab aayega", log=log)


def _w1(mid):
	return lambda s, log: _say(s, "merchant", mid, "mera paisa nahi aaya", True, log)


def _b1(s, log):
	r = _say(s, "merchant", seed.M_BRIDGE, "naye customer ke liye offer banao", True, log)
	if r.get("offer_id"):
		log.append({"who": "simulation", "said": "simulated users respond",
			"saathi": json.dumps(flows.simulate_offer_response(s, r["offer_id"]))})


def _w2(s, log):
	r = _say(s, "merchant", seed.M_KHATA, "udhaar vasool karna hai", True, log)
	log.append({"who": "simulation", "said": "simulated customers pay",
		"saathi": json.dumps(flows.simulate_khata_payments(s, r["case_id"]))})


def _sample(name: str) -> bytes:
	return (settings.DATA_DIR / "samples" / name).read_bytes()


def _bill(name: str, ack: bool = False, fix: dict | None = None):
	"""Upload a seeded bill as the demo user, confirm what needs confirming, approve."""
	def run(s, log):
		uid = seed.U_BILLS
		r = bills.review(s, uid, _sample(name), name)
		log.append({"who": f"consumer:{uid}", "said": f"uploads {name}", "saathi": r["reply"]})
		if r["state"] not in ("clean", "review"):
			return
		ticks = [f["name"] for f in r["fields"] if f["state"] != "ok"]
		c = bills.confirm(s, uid, r["upload_id"], fix or {}, ticks, ack)
		log.append({"who": f"consumer:{uid}", "said": "confirms the details", "saathi": c["reply"]})
		if c.get("approval"):
			p = manager.approve(s, c["approval"]["id"], True, "scenario")
			log.append({"who": f"consumer:{uid}", "said": "approve", "saathi": p["reply"]})
	return run


def _far(days: int) -> str:
	return (now() + timedelta(days=days)).date().isoformat()


def _c4(uid: int, route: str, then: str):
	def run(s, log):
		_say(s, "consumer", uid, f"{route} {_far(25)} 1 raat", True, log)
		_say(s, "consumer", uid, then.format(date=_far(32)), True, log)
	return run


def _script(actor: str, actor_id: int, lines: list[str]):
	def run(s, log):
		for line in lines:
			_say(s, actor, actor_id, line, True, log)
	return run


def _stock_upload(s, log):
	m = s.get(Merchant, seed.M_INVENTORY)
	r = stock.review_upload(s, m, _sample("20-supplier-invoice.pdf"))
	log.append({"who": f"merchant:{m.id}", "said": "uploads a supplier invoice", "saathi": r["reply"]})
	c = stock.confirm_upload(s, m, r["upload_id"], r["items"])
	log.append({"who": f"merchant:{m.id}", "said": "confirms the lines", "saathi": c["reply"]})
	p = manager.approve(s, c["approval"]["id"], True, "scenario")
	log.append({"who": f"merchant:{m.id}", "said": "approve", "saathi": p["reply"]})


SCENARIOS = {
	"C1 happy path": lambda s, log: _say(s, "consumer", 14, "Mumbai se Pune kal 1 raat", True, log),
	"C1 sold out mid-checkout": lambda s, log: _say(s, "consumer", 1, "Mumbai se Goa kal 2 raat",
		True, log),
	"C1 price change after approval": lambda s, log: _say(s, "consumer", 12,
		"Delhi se Jaipur kal train se", True, log),
	"C1 needs one question": lambda s, log: (_say(s, "consumer", 17, "Goa jaana hai", log=log),
		_say(s, "consumer", 17, "kal", True, log)),
	"C2 debited, not booked": _c2(seed.U_DEBIT_NO_BOOKING),
	"C2 recharge failed": _c2(seed.U_RECHARGE_FAIL),
	"C2 refund past SLA": _c2(seed.U_REFUND_LATE),
	"C2 refund stuck (escalates)": _c2(seed.U_REFUND_STUCK),
	"C2 refund inside SLA": _c2(seed.U_REFUND_IN_SLA),
	"C2 large amount (escalates)": _c2(seed.U_LARGE),
	"C2 dispute (escalates)": _c2(seed.U_DISPUTE),
	"B1 offer bridge": _b1,
	"W1 bank mismatch, fixable": _w1(seed.M_BANK_FIXABLE),
	"W1 bank mismatch, escalates": _w1(seed.M_BANK_UNFIXABLE),
	"W1 settlement in progress": _w1(seed.M_SETTLING),
	"W1 soundbox offline": _w1(seed.M_SOUNDBOX),
	"W1 suspected fraud (escalates)": _w1(seed.M_FRAUD),
	"W2 collect dues": _w2,
	"C3 clear electricity bill": _bill("01-electricity.pdf"),
	"C3 photo of a water bill": _bill("04-water-photo.png"),
	"C3 number needs confirming": _bill("05-phone-spaced-number.pdf",
		fix={"consumer_number": "9820012345"}),
	"C3 Hinglish layout": _bill("07-electricity-hinglish.pdf"),
	"C3 blurry photo": _bill("08-blurry-photo.png"),
	"C3 duplicate bill (blocked)": _bill("09-duplicate.pdf"),
	"C3 double the usual amount": _bill("10-anomaly.pdf", ack=True),
	"C3 unknown biller (escalates)": _bill("11-unknown-biller.pdf"),
	"C3 past due date": _bill("12-past-due.pdf"),
	"C3 instruction hidden in the bill": _bill("13-injection.pdf"),
	"C4 reschedule a trip": _c4(14, "Mumbai se Pune", "reschedule to {date}"),
	"C4 cancel a trip": _c4(19, "Mumbai se Pune", "cancel my booking"),
	"C4 change fails and rolls back": _c4(12, "Delhi se Jaipur train se", "reschedule to {date}"),
	"C4 charged twice": _script("consumer", seed.U_DOUBLE_CHARGE, ["I was charged twice"]),
	"C5 spend questions": _script("consumer", seed.U_BILLS, [
		"How much did I spend on food last month?", "Compare this month to last",
		"Any unusual spends last month?", "plan a budget for me", "Will I overshoot my budget?"]),
	"M1 stock by chat": _script("merchant", seed.M_INVENTORY, [
		"Add 20 packets of Parle-G at ₹8, selling ₹10", "Mark 5 as damaged", "undo",
		"What is running low?"]),
	"M1 supplier invoice upload": _stock_upload,
}


@app.post("/api/scenarios/{name}")
def scenario(name: str):
	if name not in SCENARIOS:
		raise HTTPException(404)
	log = []
	with S() as s:
		SCENARIOS[name](s, log)
		s.commit()
	return {"scenario": name, "steps": log}


@app.post("/api/reset")
def reset():
	seed.seed()
	return {"ok": True}


# ---------------- scheduled proactive scans ----------------
def proactive_scan() -> int:
	"""Detect before the user asks: refunds past SLA, failed settlements, overdue Khata."""
	found = 0
	with S() as s:
		seen = {n.meta.get("scan") for n in s.query(Notification) if n.meta.get("scan")}
		def nudge(t, i, key, msg):
			nonlocal found
			if key not in seen:
				inbox(s, t, i, msg, scan=key)
				run_log(s, None, "Scanner", "detect", msg)
				found += 1
		for r in s.query(Refund).filter_by(status="pending"):
			if (now() - r.initiated_at).days > r.sla_days:
				nudge("consumer", r.user_id, f"refund{r.id}", f"Your ₹{r.amount:.0f} refund is "
					"late. Tell me 'refund' and I will chase it.")
		for st in s.query(db.Settlement).filter_by(status="failed"):
			nudge("merchant", st.merchant_id, f"settle{st.id}", f"₹{st.amount:.0f} settlement "
				"failed. Say 'paisa nahi aaya' and I will fix it.")
		s.commit()
	return found


def _purge() -> None:
	with S() as s:
		bills.purge_uploads(s)
		s.commit()


@app.on_event("startup")
def startup():
	db.init_db()
	stale = db.missing_schema()
	if stale:
		# A database from an older checkout cannot be migrated by create_all, and every
		# table here is simulated, so rebuild it from the seed instead of failing later.
		print(f"Saathi: rebuilding stale database (missing {', '.join(stale[:5])})")
		db.init_db(drop=True)
	with S() as s:
		if not s.query(User).first():
			seed.seed(s)
		elif not (settings.DATA_DIR / "samples" / "manifest.json").is_file():
			seed.write_samples()
	sch = BackgroundScheduler(daemon=True)
	sch.add_job(proactive_scan, "interval", seconds=60, next_run_time=None)
	sch.add_job(_purge, "interval", minutes=30)
	sch.start()
	proactive_scan()
