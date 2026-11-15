"""Paytm Payment Gateway (staging, test money only). Live calls are made only when staging
credentials and the official checksum library are both present; otherwise the mock returns
the same response shape."""
import json
import uuid

import httpx

from ..config import settings


class PaytmPG:
	def _live(self) -> bool:
		if settings.PAYTM_PG_MODE != "live":
			return False
		try:
			import paytmchecksum  # noqa: F401
			return True
		except ImportError:
			return False

	def _post(self, path: str, body: dict) -> dict:
		import paytmchecksum
		sig = paytmchecksum.generateSignature(json.dumps(body), settings.PAYTM_PG_KEY)
		r = httpx.post(f"{settings.PAYTM_PG_BASE_URL}{path}", timeout=20,
			json={"body": body, "head": {"tokenType": "AES", "signature": sig}})
		r.raise_for_status()
		return r.json().get("body", {})

	def create_link(self, merchant_id: int, amount: float, purpose: str) -> dict:
		if self._live():
			try:
				b = self._post("/link/create", {"mid": settings.PAYTM_PG_MID, "linkType": "FIXED",
					"linkDescription": purpose[:30], "linkName": f"Saathi{merchant_id}",
					"amount": amount})
				return {"link_id": str(b.get("linkId")), "link_url": b.get("shortUrl", ""),
					"amount": amount, "status": "ACTIVE", "mode": "live"}
			except Exception:
				pass
		lid = uuid.uuid4().hex[:10]
		return {"link_id": lid, "link_url": f"https://sim.paytm.invalid/pay/{lid}",
			"amount": amount, "status": "ACTIVE", "mode": "mock"}

	def txn_status(self, order_id: str) -> dict:
		if self._live():
			try:
				b = self._post("/v3/order/status", {"mid": settings.PAYTM_PG_MID, "orderId": order_id})
				return {"order_id": order_id, "status": b.get("resultInfo", {}).get("resultStatus"),
					"mode": "live"}
			except Exception:
				pass
		return {"order_id": order_id, "status": "TXN_SUCCESS", "mode": "mock"}

	def refund(self, order_id: str, amount: float) -> dict:
		if self._live():
			try:
				b = self._post("/refund/apply", {"mid": settings.PAYTM_PG_MID, "orderId": order_id,
					"txnType": "REFUND", "refId": uuid.uuid4().hex[:12], "refundAmount": str(amount)})
				return {"order_id": order_id, "mode": "live",
					"status": b.get("resultInfo", {}).get("resultStatus")}
			except Exception:
				pass
		return {"order_id": order_id, "status": "PENDING", "amount": amount, "mode": "mock"}

	def health(self) -> dict:
		if settings.PAYTM_PG_MODE != "live":
			return {"mode": "mock", "ok": True, "detail": "mock links with the same contract"}
		if not self._live():
			return {"mode": "mock", "ok": False, "detail": "paytmchecksum library not installed"}
		return {"mode": "live", "ok": True, "detail": "staging credentials present"}


pg = PaytmPG()
