"""Sarvam Document AI (Sarvam Vision): Extract for schema fields, Digitise for the OCR text.

Jobs are asynchronous: create → poll status → fetch output. Results are cached on disk by
file hash, so a repeated upload is instant. In mock mode nothing leaves the machine: the
reader only understands the seeded sample files.
"""
import hashlib
import json
import re
import struct
import time
import zlib
from pathlib import Path

import httpx

from ..config import settings

TERMINAL = {"completed", "partially_completed", "failed", "rejected"}
ALLOWED_FORM_KEYS = {"language", "output_format", "schema"}  # never language_code / "markdown"
OUTPUT_FORMATS = {"md", "html", "json"}


class DocAIError(Exception):
	pass


def _form(language: str, output_format: str | None = None, schema: dict | None = None) -> dict:
	form = {"language": language}
	if output_format:
		if output_format not in OUTPUT_FORMATS:
			raise DocAIError(f"unsupported output_format {output_format}")
		form["output_format"] = output_format
	if schema:
		form["schema"] = json.dumps(schema)
	assert set(form) <= ALLOWED_FORM_KEYS
	return form


def _find(obj, keys: set[str]):
	"""The first dict in a nested result that carries any of the schema's keys."""
	if isinstance(obj, dict):
		if keys & set(obj):
			return obj
		obj = list(obj.values())
	if isinstance(obj, list):
		for v in obj:
			if (hit := _find(v, keys)) is not None:
				return hit
	return None


# ---------------- mock reader for the seeded samples ----------------
def embedded_text(data: bytes) -> dict | None:
	"""Text carried inside a seeded sample: PDF text operators, or a PNG `ocr` text chunk."""
	if data[:4] == b"%PDF":
		parts = re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", data)
		if not parts:
			return None
		lines = [re.sub(rb"\\(.)", rb"\1", p).decode("latin-1") for p in parts]
		return {"text": "\n".join(lines)}
	if data[:8] == b"\x89PNG\r\n\x1a\n":
		pos = 8
		while pos + 8 <= len(data):
			size, kind = struct.unpack(">I4s", data[pos:pos + 8])
			body = data[pos + 8:pos + 8 + size]
			if kind == b"tEXt" and body.startswith(b"ocr\x00"):
				return json.loads(body[4:].decode("latin-1"))
			pos += 12 + size
	return None


LABELS = {
	"biller": r"(?:biller|company|issued by|jari karta)",
	"consumer_number": r"(?:consumer (?:no|number)|account (?:no|number)|upbhokta sankhya|"
		r"card (?:no|number)|mobile (?:no|number)|connection id)",
	"bill_period": r"(?:bill(?:ing)? period|bill avadhi|statement period)",
	"amount_due": r"(?:amount due|total due|net payable|bill rashi|amount payable)",
	"due_date": r"(?:due date|pay by|antim tithi)",
	"bill_number": r"(?:bill (?:no|number)|invoice (?:no|number)|bill kramank|statement (?:no|number))",
}


def mock_extract(text: str, schema: dict) -> dict:
	props = schema.get("properties", {})
	if "items" in props:  # line-item schema: "name | qty | unit | rate | amount"
		rows = []
		for line in text.splitlines():
			cells = [c.strip() for c in line.split("|")]
			if len(cells) == 5 and re.fullmatch(r"[\d.,]+", cells[1] or "x"):
				rows.append(dict(zip(["name", "qty", "unit", "rate", "amount"], cells)))
		return {"items": rows}
	out = {}
	for name in props:
		m = re.search(rf"^\s*{LABELS.get(name, re.escape(name))}\s*[:\-]\s*(.+?)\s*$", text, re.I | re.M)
		out[name] = m.group(1) if m else ""
	return out


class DocumentExtractor:
	def __init__(self, http=None, sleep=time.sleep, poll_seconds: float = 1.5):
		self.http = http or self._http
		self.sleep, self.poll_seconds = sleep, poll_seconds

	@staticmethod
	def _http(method: str, url: str, **kw):
		headers = {"api-subscription-key": settings.SARVAM_API_KEY}
		if "amazonaws" in url or "blob.core" in url:  # pre-signed download link
			headers = {}
		r = httpx.request(method, url, headers=headers, timeout=30, **kw)
		r.raise_for_status()
		return r

	def live(self) -> bool:
		return settings.SARVAM_MODE == "live" and bool(settings.SARVAM_API_KEY)

	# -- job lifecycle --
	def _job(self, kind: str, data: bytes, filename: str, mime: str, form: dict) -> tuple[str, str]:
		base = f"{settings.SARVAM_BASE_URL}/doc-ai/v1/job"
		job = self.http("POST", f"{base}/{kind}", data=form,
			files={"file": (filename, data, mime)}).json()
		job_id, status = job["job_id"], job.get("status", "pending")
		deadline = time.monotonic() + settings.DOC_JOB_TIMEOUT_SEC
		while status not in TERMINAL:
			if time.monotonic() > deadline:
				raise TimeoutError(f"{kind} job {job_id} timed out")
			self.sleep(self.poll_seconds)
			status = self.http("GET", f"{base}/{job_id}/status").json().get("status", "pending")
		if status in ("failed", "rejected"):
			raise DocAIError(f"{kind} job {status}")
		return job_id, status

	def _run_live(self, data, filename, mime, schema, language) -> dict:
		base = f"{settings.SARVAM_BASE_URL}/doc-ai/v1/job"
		jid, st1 = self._job("extract", data, filename, mime, _form(language, schema=schema))
		raw = self.http("GET", f"{base}/{jid}/results").json()
		fields = _find(raw, set(schema.get("properties", {}))) or {}
		jid, st2 = self._job("digitise", data, filename, mime, _form(language, "md"))
		link = self.http("GET", f"{base}/{jid}/download-url").json()
		text = self.http(link.get("method", "GET"), link["url"]).text
		partial = "partially_completed" in (st1, st2)
		return {"fields": fields, "text": text, "mode": "live",
			"status": "partially_completed" if partial else "completed"}

	def read(self, data: bytes, filename: str, mime: str, schema: dict,
			language: str = "en-IN") -> dict:
		"""{fields, text, status, mode, cached}. Never raises: a failure comes back as
		status `failed` so the caller can ask for a re-upload or manual entry."""
		key = hashlib.sha256(data + json.dumps(schema, sort_keys=True).encode()).hexdigest()
		cache = Path(settings.DATA_DIR) / "docai_cache" / f"{key}.json"
		if cache.is_file():
			return {**json.loads(cache.read_text()), "cached": True}
		if self.live():
			out = None
			for _ in range(2):  # one retry, then give up
				try:
					out = self._run_live(data, filename, mime, schema, language)
					break
				except Exception:
					continue
			out = out or {"fields": {}, "text": "", "mode": "live", "status": "failed"}
		else:
			found = embedded_text(data)
			if found is None:
				out = {"fields": {}, "text": "", "mode": "mock", "status": "failed"}
			else:
				out = {"fields": found.get("extract") or mock_extract(found["text"], schema),
					"text": found["text"], "mode": "mock", "status": "completed"}
		if out["status"] != "failed":
			cache.parent.mkdir(parents=True, exist_ok=True)
			cache.write_text(json.dumps(out))
		return {**out, "cached": False}

	def health(self) -> dict:
		if not self.live():
			return {"mode": "mock", "ok": True, "detail": "reads the seeded sample files only"}
		try:
			self.http("POST", f"{settings.SARVAM_BASE_URL}/doc-ai/v1/job/upload", json={})
			return {"mode": "live", "ok": True, "detail": "Document AI reachable"}
		except Exception as e:
			return {"mode": "mock", "ok": False, "detail": f"request failed ({type(e).__name__})"}


# ---------------- tiny writers for seeded samples ----------------
def make_pdf(lines: list[str]) -> bytes:
	"""A one-page PDF with plain text lines. Enough for a readable sample bill."""
	esc = lambda x: x.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
	body = "BT /F1 12 Tf 50 780 Td 16 TL\n" + "\n".join(f"({esc(l)}) Tj T*" for l in lines) + "\nET"
	stream = body.encode("latin-1", "replace")
	objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
		b"/Resources << /Font << /F1 5 0 R >> >> >>",
		b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
	out, offsets = b"%PDF-1.4\n", []
	for i, o in enumerate(objs, 1):
		offsets.append(len(out))
		out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
	xref = len(out)
	out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
	out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
	out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
	return out


def make_png(ocr: dict, seed: int = 0) -> bytes:
	"""A small grey image standing in for a photo, with the sample's OCR text in a text chunk."""
	def chunk(kind: bytes, body: bytes) -> bytes:
		return struct.pack(">I", len(body)) + kind + body + struct.pack(">I",
			zlib.crc32(kind + body) & 0xFFFFFFFF)
	w, h = 160, 100
	rows = b"".join(b"\x00" + bytes((x * 7 + y * 13 + seed * 31) % 256 for x in range(w))
		for y in range(h))
	return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
		+ chunk(b"tEXt", b"ocr\x00" + json.dumps(ocr).encode("latin-1", "replace"))
		+ chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


docai = DocumentExtractor()
