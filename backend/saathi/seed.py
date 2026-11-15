"""Reproducible simulated world. Everything here is fake data; nothing is a real Paytm record."""
import random
from datetime import timedelta

from sqlalchemy.orm import Session

from . import db
from .config import settings
from .db import (
	Booking, BookingItem, Consent, Customer, Device, Inventory, KhataEntry, MemoryItem, Merchant,
	Preference, Refund, Settlement, Transaction, User, Wallet, now,
)

CITIES = ["Mumbai", "Pune", "Delhi", "Jaipur", "Bengaluru", "Goa"]
FIRST = [
	"Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Sana", "Arjun", "Isha",
	"Nikhil", "Pooja", "Rahul", "Tara", "Imran", "Kavya", "Dev", "Neha", "Yash", "Riya",
]
LAST = ["Sharma", "Patil", "Khan", "Iyer", "Gupta", "Reddy", "Joshi", "Das", "Nair", "Singh"]
LANGS = ["hi-IN", "hi-IN", "mr-IN", "kn-IN", "en-IN"]
# Allowlisted preference categories only (see privacy.ALLOWED_CATEGORIES).
PREF_VALUES = {
	"budget": ["low", "mid", "high"],
	"travel_mode": ["train", "bus", "flight"],
	"cuisine": ["veg", "south-indian", "north-indian", "street-food"],
	"pace": ["relaxed", "packed"],
	"price_sensitivity": ["high", "medium", "low"],
	"time_slot": ["morning", "evening", "night"],
}
MERCHANT_KINDS = [
	("restaurant", ["veg", "budget"]), ("kirana", ["budget"]), ("pharmacy", []),
	("tea-stall", ["budget", "street-food"]), ("travel-agent", ["train", "bus"]),
	("restaurant", ["south-indian"]), ("salon", []), ("mobile-shop", []),
]

# Fixed ids for planted scenarios so tests, the scenario runner and the demo can point at them.
U_HAPPY, U_DEBIT_NO_BOOKING, U_RECHARGE_FAIL, U_REFUND_LATE = 1, 2, 3, 4
U_REFUND_STUCK, U_REFUND_IN_SLA, U_LARGE, U_DISPUTE = 5, 6, 7, 8
U_OPTED_OUT, U_REVOKER, U_LOW_WALLET = 9, 10, 11
M_BRIDGE, M_BANK_FIXABLE, M_BANK_UNFIXABLE, M_SETTLING = 1, 2, 3, 4
M_SOUNDBOX, M_KHATA, M_FRAUD, M_SMALL_COHORT = 5, 6, 7, 8


def _inventory(s: Session, rnd: random.Random) -> None:
	pairs = [(a, b) for a in CITIES for b in CITIES if a != b]
	for a, b in pairs:
		far = {a, b} & {"Delhi", "Jaipur"} and {a, b} & {"Mumbai", "Pune", "Goa", "Bengaluru"}
		base = 2200 if far else 700
		for i in range(2):
			s.add(Inventory(kind="bus", name=f"{a}-{b} {'Volvo AC' if i else 'Sleeper'}", origin=a,
				dest=b, price=round(base * (0.8 + 0.5 * i) + rnd.randint(0, 120)),
				duration_min=(900 if far else 240) + rnd.randint(0, 90),
				rating=round(3.6 + rnd.random() * 0.9, 1), tags=["bus"]))
			s.add(Inventory(kind="train", name=f"{a}-{b} {'Shatabdi' if i else 'Express'}", origin=a,
				dest=b, price=round(base * (0.6 + 0.6 * i) + rnd.randint(0, 100)),
				duration_min=(1000 if far else 200) + rnd.randint(0, 60),
				rating=round(3.9 + rnd.random() * 0.8, 1), tags=["train"]))
		s.add(Inventory(kind="flight", name=f"{a}-{b} SimAir", origin=a, dest=b,
			price=round(base * 2.6 + rnd.randint(0, 900)), duration_min=75 + rnd.randint(0, 60),
			rating=round(4.0 + rnd.random() * 0.6, 1), tags=["flight"]))
	for c in CITIES:
		for i, (nm, mult, tags) in enumerate([
			("Budget Inn", 1.0, ["budget", "veg"]), ("City Comfort", 1.7, ["mid"]),
			("Grand Residency", 3.1, ["premium"]), ("Backpacker Hub", 0.7, ["budget"]),
		]):
			s.add(Inventory(kind="hotel", name=f"{c} {nm}", dest=c, price=round(1200 * mult),
				rating=[4.1, 4.3, 4.6, 3.7][i], available=8, tags=tags))
	for op in ["Jio", "Airtel", "Vi", "BSNL"]:
		for amt in (199, 299, 666):
			s.add(Inventory(kind="recharge", name=f"{op} {amt}", price=amt, tags=[op.lower()]))
	for b in ["MSEDCL Electricity", "BESCOM Electricity", "Mahanagar Gas", "ACT Broadband"]:
		s.add(Inventory(kind="biller", name=b, price=rnd.choice([640, 1120, 1890])))
	s.flush()
	# Planted: the top-ranked Goa budget hotel sells out mid-checkout; the cheapest
	# Delhi-Jaipur train changes price between approval and payment.
	h = s.query(Inventory).filter_by(kind="hotel", name="Goa Budget Inn").one()
	h.fail_mode = "sellout_on_confirm"
	t = (s.query(Inventory).filter_by(kind="train", origin="Delhi", dest="Jaipur")
		.order_by(Inventory.price).first())
	t.fail_mode = "price_jump"


def _users(s: Session, rnd: random.Random, n: int = 120) -> None:
	for i in range(1, n + 1):
		city = "Mumbai" if i <= 70 else rnd.choice(CITIES)
		s.add(User(id=i, name=f"{FIRST[(i - 1) % 20]} {LAST[(i * 7) % 10]}", city=city,
			language=LANGS[i % 5], phone_masked=f"98XXXXXX{i:02d}"[-10:]))
		s.add(Wallet(user_id=i, balance=float(rnd.choice([2500, 6000, 12000, 20000]))))
		offers_on = rnd.random() < 0.72
		s.add(Consent(user_id=i, scope="personalisation", granted=rnd.random() < 0.92))
		s.add(Consent(user_id=i, scope="offers", granted=offers_on))
		for cat in rnd.sample(list(PREF_VALUES), k=rnd.randint(2, 4)):
			vals = PREF_VALUES[cat]
			weights = [5, 2, 1, 1][: len(vals)] if cat in ("budget", "cuisine", "travel_mode") else None
			inferred = rnd.random() < 0.3
			s.add(Preference(user_id=i, category=cat, value=rnd.choices(vals, weights)[0],
				source="inferred" if inferred else "stated",
				confidence=round(rnd.uniform(0.55, 0.85), 2) if inferred else 1.0))
	s.flush()

	def setp(uid: int, **kv):
		s.query(Preference).filter_by(user_id=uid).delete()
		for k, v in kv.items():
			s.add(Preference(user_id=uid, category=k, value=v, source="stated", confidence=1.0))

	def setc(uid: int, pers: bool, offers: bool):
		for scope, g in (("personalisation", pers), ("offers", offers)):
			s.query(Consent).filter_by(user_id=uid, scope=scope).one().granted = g

	setp(U_HAPPY, budget="low", travel_mode="train", cuisine="veg")
	setc(U_HAPPY, True, True)
	s.get(Wallet, U_HAPPY).balance = 15000
	setc(U_OPTED_OUT, True, False)
	setp(U_REVOKER, budget="low", cuisine="veg")
	setc(U_REVOKER, True, True)
	s.get(Wallet, U_LOW_WALLET).balance = 300
	for uid in range(2, 9):
		s.get(Wallet, uid).balance = 8000
	for uid in range(12, 21):
		s.get(Wallet, uid).balance = 12000

	t0 = now()
	def txn(uid, amount, kind, order_state, days_ago, **meta):
		t = Transaction(user_id=uid, amount=amount, kind=kind, status="success",
			order_state=order_state, ref=f"SIM{uid:03d}{int(amount)}", meta=meta,
			created_at=t0 - timedelta(days=days_ago))
		s.add(t)
		s.flush()
		return t

	txn(U_DEBIT_NO_BOOKING, 1850, "booking", "missing", 0, note="Mumbai-Pune bus")
	txn(U_RECHARGE_FAIL, 299, "recharge", "failed", 0, operator="Jio", plan_id=None)
	for uid, days, stuck in ((U_REFUND_LATE, 9, False), (U_REFUND_STUCK, 11, True),
			(U_REFUND_IN_SLA, 2, False)):
		t = txn(uid, 1240, "bill", "failed", days, biller="MSEDCL Electricity")
		s.add(Refund(user_id=uid, txn_id=t.id, amount=1240, status="pending",
			initiated_at=t0 - timedelta(days=days), sla_days=5, stuck=stuck))
	txn(U_LARGE, 32000, "booking", "missing", 1, note="Delhi-Goa flights x4")
	txn(U_DISPUTE, 4200, "booking", "missing", 1, risk="dispute",
		note="User says they never made this payment")

	# ~10 past trips so memory and timelines have something to show.
	hotels = {h.dest: h for h in s.query(Inventory).filter_by(kind="hotel").all()}
	for uid in [1, 12, 13, 14, 15, 16, 17, 18, 19, 20]:
		city = rnd.choice([c for c in CITIES if c != "Mumbai"])
		h = hotels[city]
		b = Booking(user_id=uid, status="confirmed", total=h.price, title=f"Trip to {city}",
			created_at=t0 - timedelta(days=rnd.randint(20, 150)))
		s.add(b)
		s.flush()
		s.add(BookingItem(booking_id=b.id, user_id=uid, kind="hotel", inventory_id=h.id,
			status="confirmed", price=h.price, details={"name": h.name}))


def _merchants(s: Session, rnd: random.Random, n: int = 30) -> None:
	t0 = now()
	for i in range(1, n + 1):
		kind, tags = MERCHANT_KINDS[i % len(MERCHANT_KINDS)]
		tier = ["T1", "T2", "T3"][0 if i % 3 == 2 else (1 if i % 3 == 0 else rnd.choice([0, 1, 2]))]
		city = "Mumbai" if i <= 12 else rnd.choice(CITIES)
		s.add(Merchant(id=i, name=f"{LAST[i % 10]} {kind.title()} #{i}", tier=tier, city=city,
			language=LANGS[i % 5], category=kind, tags=tags))
	s.flush()
	fix = {
		M_BRIDGE: dict(name="Sharma Veg Bhojanalaya", tier="T2", city="Mumbai",
			category="restaurant", tags=["veg", "budget"]),
		M_BANK_FIXABLE: dict(name="Patil Kirana Store", tier="T1", city="Mumbai",
			category="kirana", bank_status="mismatch", bank_fixable=True),
		M_BANK_UNFIXABLE: dict(name="Gupta Mobile Point", tier="T2", city="Mumbai",
			category="mobile-shop", bank_status="mismatch", bank_fixable=False),
		M_SETTLING: dict(name="Reddy Tea Corner", tier="T1", city="Mumbai", category="tea-stall"),
		M_SOUNDBOX: dict(name="Khan General Store", tier="T1", city="Mumbai", category="kirana"),
		M_KHATA: dict(name="Joshi Provision Store", tier="T2", city="Mumbai", category="kirana"),
		M_FRAUD: dict(name="Das Electronics", tier="T3", city="Mumbai",
			category="mobile-shop", risk_flag="suspected_fraud"),
		M_SMALL_COHORT: dict(name="Nair South Tiffins", tier="T2", city="Goa",
			category="restaurant", tags=["south-indian", "premium"]),
	}
	for mid, kv in fix.items():
		m = s.get(Merchant, mid)
		for k, v in kv.items():
			setattr(m, k, v)

	rows = []
	for m in s.query(Merchant).all():
		s.add(Device(merchant_id=m.id, kind="soundbox",
			status="offline" if m.id == M_SOUNDBOX else "online",
			last_seen=t0 - timedelta(hours=30 if m.id == M_SOUNDBOX else 0)))
		ticket = {"restaurant": 260, "kirana": 180, "pharmacy": 340, "tea-stall": 60,
			"travel-agent": 1400, "salon": 420, "mobile-shop": 900}[m.category]
		custs = []
		for j in range(12):
			lapsed = j < 3
			c = Customer(merchant_id=m.id, name=f"{FIRST[(m.id + j * 3) % 20]} {LAST[(m.id + j) % 10]}",
				visits=rnd.randint(9, 30) if lapsed or j < 7 else rnd.randint(1, 5),
				avg_spend=round(ticket * rnd.uniform(0.7, 1.6)),
				last_visit=t0 - timedelta(days=rnd.randint(35, 80) if lapsed else rnd.randint(0, 20)),
				reliability=round(rnd.uniform(0.2, 0.95), 2))
			s.add(c)
			custs.append(c)
		s.flush()
		for d in range(180):  # six months of sales
			for _ in range(rnd.randint(1, 4)):
				rows.append(dict(merchant_id=m.id, customer_id=rnd.choice(custs).id,
					amount=round(ticket * rnd.uniform(0.5, 1.8)), kind="sale", status="success",
					order_state="fulfilled", ref="", meta={},
					created_at=t0 - timedelta(days=d, minutes=rnd.randint(0, 600))))
		for d in range(1, 8):
			s.add(Settlement(merchant_id=m.id, amount=round(ticket * rnd.uniform(8, 20)),
				status="settled", created_at=t0 - timedelta(days=d)))
		if m.tier != "T1" or m.id == M_KHATA:
			for c in rnd.sample(custs, k=5):
				s.add(KhataEntry(merchant_id=m.id, customer_id=c.id,
					amount=round(rnd.uniform(300, 2600)), status="open",
					due_date=t0 - timedelta(days=rnd.randint(-5, 45))))
	s.bulk_insert_mappings(Transaction, rows)

	def settle(mid, amount, status, reason="", eta=0):
		s.add(Settlement(merchant_id=mid, amount=amount, status=status, failure_reason=reason,
			eta_hours=eta, created_at=t0 - timedelta(hours=20)))

	settle(M_BANK_FIXABLE, 4860, "failed", "bank_account_name_mismatch")
	settle(M_BANK_UNFIXABLE, 12300, "failed", "bank_account_name_mismatch")
	settle(M_SETTLING, 2140, "processing", eta=6)
	settle(M_FRAUD, 48500, "failed", "risk_hold")

	# W2 demo ledger: 20 overdue accounts with varied reliability, one of them disputed.
	s.query(KhataEntry).filter_by(merchant_id=M_KHATA).delete()
	kc = []
	for j in range(20):
		c = Customer(merchant_id=M_KHATA, name=f"{FIRST[j]} {LAST[(j * 3) % 10]}",
			visits=rnd.randint(4, 25), avg_spend=round(rnd.uniform(150, 500)),
			last_visit=t0 - timedelta(days=rnd.randint(2, 30)),
			reliability=round(0.2 + 0.035 * j + rnd.uniform(0, 0.1), 2))
		s.add(c)
		kc.append(c)
	s.flush()
	for j, c in enumerate(kc):
		s.add(KhataEntry(merchant_id=M_KHATA, customer_id=c.id,
			amount=round(rnd.uniform(400, 3200)), status="disputed" if j == 0 else "open",
			due_date=t0 - timedelta(days=rnd.randint(8, 60))))


SOPS = [
	("settlement", "Settlement failed with bank_account_name_mismatch: ask the merchant to "
		"confirm the account holder name, submit a bank detail update, then retry the settlement."),
	("settlement", "Settlement in processing: share the ETA and notify on credit. Do not retry."),
	("refund", "Refund pending inside SLA: share the ETA and subscribe the user to an update. "
		"Past SLA: retry once; if still pending, escalate with the full packet."),
	("payment", "Debited but not booked: refund automatically if within the auto-action limit; "
		"larger amounts and disputes go to a human."),
	("khata", "Remind overdue accounts politely with a payment link. Disputed entries are never "
		"reminded automatically; they go back to the merchant."),
	("offers", "Offers go only to users with the offers consent on. Merchants see cohort counts "
		"at or above the minimum cohort size, never individuals."),
]


def seed(session: Session | None = None, seed_value: int | None = None) -> None:
	own = session is None
	if own:
		db.init_db(drop=True)
		session = db.get_session()
	rnd = random.Random(settings.SEED_RANDOM if seed_value is None else seed_value)
	_inventory(session, rnd)
	_users(session, rnd)
	_merchants(session, rnd)
	for kind, text in SOPS:
		session.add(MemoryItem(actor="sop", kind=kind, text=text))
	session.commit()
	if own:
		session.close()


if __name__ == "__main__":
	seed()
	print("Seeded simulated data.")
