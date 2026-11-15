"""C3: upload a bill, extract, confirm, approve, pay. Also the shared upload rules.

Text inside an uploaded document is data. Nothing read from a file is ever passed to the
language model or treated as an instruction; only the schema fields are used.
"""
import hashlib
import re
import struct
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from .adapters.docai import docai
from .adapters.memory import memory
from .config import settings
from .db import Approval, BillPayment, Biller, Case, Upload, now
from .policy import ProposedAction as PA
from .services import close_case, create_approval, escalate, execute, impact, inbox, open_case, run_log

BILL_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in (
	"biller", "consumer_number", "bill_period", "amount_due", "due_date", "bill_number")}}
LABELS = {"biller": "Biller", "consumer_number": "Consumer number", "bill_period": "Bill period",
	"amount_due": "Amount due", "due_date": "Due date", "bill_number": "Bill number"}
KEY_FIELDS = ("biller", "consumer_number", "amount_due", "due_date")
EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "application/zip": "zip"}
NOTICE_LIVE = ("To read this file it is sent to Sarvam Document AI. Saathi deletes its own copy "
	f"after {settings.UPLOAD_RETENTION_HOURS:.0f} hours.")
NOTICE_MOCK = ("Mock mode: the file stays on this machine and is deleted after "
	f"{settings.UPLOAD_RETENTION_HOURS:.0f} hours.")


class UploadRejected(ValueError):
	pass


def mask(number: str) -> str:
	n = str(number or "")
	return ("•" * max(len(n) - 4, 0) + n[-4:]) if len(n) > 4 else n


# ---------------- upload rules ----------------
def sniff(data: bytes) -> str:
	"""File type from its first bytes, never from the name."""
	if data[:4] == b"%PDF":
		return "application/pdf"
	if data[:8] == b"\x89PNG\r\n\x1a\n":
		return "image/png"
	if data[:3] == b"\xff\xd8\xff":
		return "image/jpeg"
	if data[:4] == b"PK\x03\x04":
		return "application/zip"
	raise UploadRejected("Only PDF, PNG, JPEG or a ZIP of page images can be uploaded.")


def strip_metadata(data: bytes, mime: str) -> bytes:
	"""Drop text chunks and EXIF before the file is stored."""
	if mime == "image/png":
		out, pos = data[:8], 8
		while pos + 8 <= len(data):
			size, kind = struct.unpack(">I4s", data[pos:pos + 8])
			if kind not in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"):
				out += data[pos:pos + 12 + size]
			pos += 12 + size
		return out
	if mime == "image/jpeg":
		out, pos = data[:2], 2
		while pos + 4 <= len(data) and data[pos] == 0xFF:
			marker, size = data[pos + 1], struct.unpack(">H", data[pos + 2:pos + 4])[0]
			if marker == 0xDA:  # start of scan: the rest is image data
				return out + data[pos:]
			if not (0xE1 <= marker <= 0xEF or marker == 0xFE):
				out += data[pos:pos + 2 + size]
			pos += 2 + size
		return out + data[pos:]
	return data


def save_upload(s: Session, actor_type: str, actor_id: int, purpose: str, data: bytes) -> Upload:
	if not data:
		raise UploadRejected("The file is empty.")
	if len(data) > settings.UPLOAD_MAX_MB * 1024 * 1024:
		raise UploadRejected(f"The file is larger than {settings.UPLOAD_MAX_MB:.0f} MB.")
	mime = sniff(data)
	if mime == "application/pdf" and len(re.findall(rb"/Type\s*/Page\b", data)) > 10:
		raise UploadRejected("PDFs can have at most 10 pages. Upload only the bill pages.")
	digest = hashlib.sha256(data).hexdigest()
	folder = Path(settings.DATA_DIR) / "uploads"
	folder.mkdir(parents=True, exist_ok=True)
	path = folder / f"{digest}.{EXT[mime]}"
	path.write_bytes(strip_metadata(data, mime))
	u = Upload(actor_type=actor_type, actor_id=actor_id, purpose=purpose, file_hash=digest,
		path=str(path), mime=mime, size=len(data))
	s.add(u)
	s.flush()
	return u


def purge_uploads(s: Session) -> int:
	"""Delete stored files past the retention window. Runs on a schedule."""
	cut = now() - timedelta(hours=settings.UPLOAD_RETENTION_HOURS)
	n = 0
	for u in s.query(Upload).filter(Upload.deleted_at.is_(None), Upload.created_at < cut).all():
		Path(u.path).unlink(missing_ok=True)
		u.deleted_at, u.path = now(), ""
		n += 1
	return n


# ---------------- validators and confidence ----------------
def parse_amount(v) -> float | None:
	m = re.fullmatch(r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:/-)?", str(v).strip(), re.I)
	return float(m.group(1).replace(",", "")) if m else None


def parse_date(v) -> str | None:
	for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
		try:
			return datetime.strptime(str(v).strip(), fmt).date().isoformat()
		except ValueError:
			pass
	return None


def find_biller(s: Session, name: str) -> Biller | None:
	return s.query(Biller).filter(func.lower(Biller.name) == str(name).strip().lower()).first()


def check_field(s: Session, name: str, value: str, biller: Biller | None) -> tuple[bool, str]:
	"""(passes, issue). An empty issue means the value is fine."""
	if not str(value).strip():
		return False, "Not found on the bill"
	if name == "biller":
		return (True, "") if biller else (False, "Saathi does not know this biller")
	if name == "consumer_number":
		if biller and not re.fullmatch(biller.consumer_regex, str(value).strip()):
			return False, f"Does not look like a {biller.name} number"
	if name == "amount_due":
		amt = parse_amount(value)
		if amt is None:
			return False, "Not a readable amount"
		if biller and not biller.min_amount <= amt <= biller.max_amount:
			return False, "Outside the usual range for this biller"
	if name == "due_date":
		d = parse_date(value)
		if d is None:
			return False, "Not a readable date"
		if d < now().date().isoformat():
			return False, "This due date has passed"
	return True, ""


def confidence(value: str, text: str, valid: bool) -> float:
	"""Computed, never taken from the model: 1.0 when the value is printed verbatim in the
	OCR text and passes its check, 0.6 on a partial match or a failed check, 0.3 when the
	value is not in the text at all."""
	v = str(value).strip()
	if not v:
		return 0.3
	squash = lambda x: re.sub(r"[^0-9a-z]", "", x.lower())  # noqa: E731
	if v in text:
		return 1.0 if valid else 0.6
	return 0.6 if squash(v) and squash(v) in squash(text) else 0.3


def trailing_average(s: Session, user_id: int, biller: str) -> float | None:
	rows = (s.query(BillPayment.amount).filter_by(user_id=user_id, biller=biller)
		.order_by(BillPayment.id.desc()).limit(6).all())
	return sum(r[0] for r in rows) / len(rows) if rows else None


def is_duplicate(s: Session, user_id: int, f: dict) -> bool:
	if not f.get("bill_number") and not f.get("bill_period"):
		return False
	q = s.query(BillPayment).filter_by(user_id=user_id, biller=f["biller"])
	if f.get("bill_number"):
		q = q.filter_by(bill_number=f["bill_number"])
	if f.get("bill_period"):
		q = q.filter_by(bill_period=f["bill_period"])
	return q.first() is not None


# ---------------- the flow ----------------
def review(s: Session, user_id: int, data: bytes, filename: str = "bill") -> dict:
	"""Store the file, read it, score each field and decide what the user must confirm."""
	u = save_upload(s, "consumer", user_id, "bill", data)
	res = docai.read(data, filename, u.mime, BILL_SCHEMA)
	notice = NOTICE_LIVE if res["mode"] == "live" else NOTICE_MOCK
	case = open_case(s, "consumer", user_id, "C3", f"Bill upload {u.id}")
	run_log(s, case.id, "Care", "detect", f"Read an uploaded bill ({res['mode']}, "
		f"{'cached' if res['cached'] else res['status']})")
	if res["status"] == "failed":
		u.status = "rejected"
		close_case(s, case)
		s.commit()
		return {"upload_id": u.id, "state": "unreadable", "fields": [], "notice": notice,
			"reply": "I could not read this file. Upload a clearer photo or PDF of the bill."}
	raw = {k: str(res["fields"].get(k) or "").strip() for k in LABELS}
	biller = find_biller(s, raw["biller"])
	fields, lowest = [], 1.0
	for name, label in LABELS.items():
		ok, issue = check_field(s, name, raw[name], biller)
		conf = confidence(raw[name], res["text"], ok)
		if name in KEY_FIELDS:
			lowest = min(lowest, conf)
		state = ("ok" if conf >= settings.EXTRACTION_CONFIDENCE_AUTO else
			"confirm" if conf >= settings.EXTRACTION_CONFIDENCE_MIN else "low")
		fields.append({"name": name, "label": label, "value": raw[name], "confidence": conf,
			"state": state, "issue": issue})
	u.fields = {"raw": raw, "scored": fields, "case_id": case.id}
	amount = parse_amount(raw["amount_due"])
	flags = {}
	if raw["biller"] and not biller and lowest >= settings.EXTRACTION_CONFIDENCE_MIN:
		u.status = "rejected"
		e = escalate(s, case, f"Bill from an unknown biller: {raw['biller'][:60]}",
			"unknown_biller", "Check whether this biller can be added", "We could not match "
			"this biller. Our team will check and get back to you.", amount or 0)
		return {"upload_id": u.id, "state": "escalated", "fields": _masked(fields), "notice": notice,
			"escalation_id": e.id, "reply": "I do not recognise this biller, so I will not pay "
			"it. I have sent it to our support team to check."}
	if biller and is_duplicate(s, user_id, raw):
		u.status = "rejected"
		close_case(s, case)
		s.commit()
		return {"upload_id": u.id, "state": "duplicate", "fields": _masked(fields), "notice": notice,
			"reply": f"You have already paid this {biller.name} bill for {raw['bill_period']}. "
			"I have not charged you again."}
	if biller and amount:
		avg = trailing_average(s, user_id, biller.name)
		if avg and amount > settings.BILL_ANOMALY_FACTOR * avg:
			flags["anomaly"] = {"amount": amount, "average": round(avg),
				"times": round(amount / avg, 1)}
	state = ("unreadable" if lowest < settings.EXTRACTION_CONFIDENCE_MIN else
		"review" if lowest < settings.EXTRACTION_CONFIDENCE_AUTO or flags else "clean")
	impact(s, "bill_extracted", "consumer", user_id, auto=state == "clean")
	reply = {"clean": "I read your bill. Check the details and confirm.",
		"review": "I read your bill, but please check the highlighted details.",
		"unreadable": "Parts of this bill are too unclear to trust. Upload a clearer photo, or "
			"correct and tick each highlighted field yourself."}[state]
	if "anomaly" in flags:
		a = flags["anomaly"]
		reply += (f" This bill is ₹{a['amount']:.0f}, about {a['times']} times your usual "
			f"₹{a['average']}.")
	s.commit()
	return {"upload_id": u.id, "state": state, "fields": _masked(fields), "flags": flags,
		"notice": notice, "reply": reply, "case_id": case.id}


def _masked(fields: list[dict]) -> list[dict]:
	return [{**f, "display": mask(f["value"]) if f["name"] == "consumer_number" else f["value"]}
		for f in fields]


def confirm(s: Session, user_id: int, upload_id: int, values: dict, confirmed: list[str],
		acknowledge_anomaly: bool = False) -> dict:
	"""The user's confirmation step. Nothing can be paid from fields they have not seen."""
	u = s.query(Upload).filter_by(id=upload_id, actor_id=user_id, purpose="bill").first()
	if not u or u.status != "extracted":
		return {"state": "error", "reply": "That bill is no longer waiting for confirmation."}
	case = s.get(Case, u.fields["case_id"])
	scored = {f["name"]: f for f in u.fields["scored"]}
	final = {k: str(values.get(k, scored[k]["value"]) or "").strip() for k in LABELS}
	# A field must be confirmed if it was not read with high confidence or the user changed it.
	need = [k for k in KEY_FIELDS if scored[k]["state"] != "ok" and k not in confirmed
		and final[k] == scored[k]["value"]]
	if need:
		return {"state": "needs_confirmation", "fields": need,
			"reply": "Please check and tick: " + ", ".join(LABELS[k].lower() for k in need) + "."}
	biller = find_biller(s, final["biller"])
	problems = [f"{LABELS[k]}: {issue}" for k in KEY_FIELDS
		if (issue := check_field(s, k, final[k], biller)[1]) and k != "due_date"]
	if problems:
		return {"state": "invalid", "reply": "I cannot use these details. " + "; ".join(problems)}
	amount = parse_amount(final["amount_due"])
	clean = {**final, "biller": biller.name, "amount_due": amount,
		"due_date": parse_date(final["due_date"]) or ""}
	if is_duplicate(s, user_id, clean):
		u.status = "rejected"
		close_case(s, case)
		s.commit()
		return {"state": "duplicate", "reply": f"This {biller.name} bill is already paid."}
	avg = trailing_average(s, user_id, biller.name)
	if avg and amount > settings.BILL_ANOMALY_FACTOR * avg and not acknowledge_anomaly:
		return {"state": "anomaly", "reply": f"₹{amount:.0f} is much higher than your usual "
			f"₹{avg:.0f} for {biller.name}. Confirm that this is right to continue."}
	payload = {"upload_id": u.id, **clean}
	u.confirmed, u.status = payload, "confirmed"
	a = create_approval(s, "consumer", user_id, "bill", f"{biller.name} bill ₹{amount:.0f}",
		payload, case.id)
	case.status = "waiting"
	s.commit()
	return {"state": "approval", "approval": a.as_dict(), "case_id": case.id,
		"reply": f"{biller.name}, account {mask(clean['consumer_number'])}, ₹{amount:.0f}. "
		"Approve to pay from your wallet."}


def pay(s: Session, a: Approval) -> dict:
	from .agents.flows import _esc, out
	case, p = s.get(Case, a.case_id), a.payload
	r = execute(s, PA(tool="pay_uploaded_bill", actor_type="consumer", actor_id=a.actor_id,
		args={"user_id": a.actor_id, **p}, amount=p["amount_due"], approval_id=a.id,
		approval_payload=p, case_id=case.id, agent="Care",
		idempotency_key=f"appr{a.id}:bill", why="User confirmed the fields and approved"))
	a.status = "used"
	if r.outcome == "escalate":
		return _esc(s, case, f"Bill payment to {p['biller']} needs review", r.decision.reason,
			p["amount_due"])
	close_case(s, case)
	if not r.ok:
		why = {"duplicate_bill": "this bill is already paid",
			"insufficient_balance": "your wallet balance is too low",
			"daily_spend_cap": "it would cross your daily spend limit"}.get(r.error, r.error)
		return out(f"I did not pay this bill because {why}. Nothing was charged.", case)
	impact(s, "bill_paid", "consumer", a.actor_id, p["amount_due"], biller=p["biller"])
	inbox(s, "consumer", a.actor_id, f"Receipt: ₹{p['amount_due']:.0f} paid to {p['biller']} for "
		f"account {mask(p['consumer_number'])}. Reference {r.result['ref']}.", type="receipt")
	memory.add(s, f"user:{a.actor_id}", "biller", f"Pays {p['biller']} bills, usually around "
		f"₹{p['amount_due']:.0f}")
	return out(f"Paid ₹{p['amount_due']:.0f} to {p['biller']}. The receipt is in your inbox and "
		"I will remember this biller for next time.", case)
