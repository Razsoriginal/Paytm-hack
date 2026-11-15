"""Schema for the simulated Paytm world plus Saathi's own state."""
from datetime import datetime, timezone

from sqlalchemy import (
	JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
	create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import ROOT, settings


def now() -> datetime:
	return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
	def as_dict(self) -> dict:
		out = {}
		for c in self.__table__.columns:
			v = getattr(self, c.name)
			out[c.name] = v.isoformat() if isinstance(v, datetime) else v
		return out


def _pk() -> Mapped[int]:
	return mapped_column(Integer, primary_key=True, autoincrement=True)


def _ts() -> Mapped[datetime]:
	return mapped_column(DateTime, default=now)


# ---- consumers ----
class User(Base):
	__tablename__ = "users"
	id: Mapped[int] = _pk()
	name: Mapped[str] = mapped_column(String)
	language: Mapped[str] = mapped_column(String, default="hi-IN")
	city: Mapped[str] = mapped_column(String)
	phone_masked: Mapped[str] = mapped_column(String, default="")


class Consent(Base):
	__tablename__ = "consents"
	__table_args__ = (UniqueConstraint("user_id", "scope"),)
	id: Mapped[int] = _pk()
	user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
	scope: Mapped[str] = mapped_column(String)  # personalisation | offers
	granted: Mapped[bool] = mapped_column(Boolean, default=False)
	updated_at: Mapped[datetime] = _ts()


class Preference(Base):
	__tablename__ = "preferences"
	id: Mapped[int] = _pk()
	user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
	category: Mapped[str] = mapped_column(String)
	value: Mapped[str] = mapped_column(String)
	source: Mapped[str] = mapped_column(String, default="stated")  # stated | inferred
	confidence: Mapped[float] = mapped_column(Float, default=1.0)
	scope: Mapped[str] = mapped_column(String, default="personalisation")
	created_at: Mapped[datetime] = _ts()


class Wallet(Base):
	__tablename__ = "wallets"
	user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
	balance: Mapped[float] = mapped_column(Float, default=0)


class Booking(Base):
	__tablename__ = "bookings"
	id: Mapped[int] = _pk()
	user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
	status: Mapped[str] = mapped_column(String, default="pending")
	total: Mapped[float] = mapped_column(Float, default=0)
	plan_hash: Mapped[str] = mapped_column(String, default="")
	title: Mapped[str] = mapped_column(String, default="")
	created_at: Mapped[datetime] = _ts()


class BookingItem(Base):
	__tablename__ = "booking_items"
	id: Mapped[int] = _pk()
	booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
	user_id: Mapped[int] = mapped_column(Integer, index=True)
	kind: Mapped[str] = mapped_column(String)
	inventory_id: Mapped[int] = mapped_column(Integer)
	status: Mapped[str] = mapped_column(String, default="held")  # held|confirmed|cancelled|expired
	price: Mapped[float] = mapped_column(Float)
	units: Mapped[int] = mapped_column(Integer, default=1)
	hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
	details: Mapped[dict] = mapped_column(JSON, default=dict)


class Refund(Base):
	__tablename__ = "refunds"
	id: Mapped[int] = _pk()
	user_id: Mapped[int] = mapped_column(Integer, index=True)
	txn_id: Mapped[int] = mapped_column(Integer)
	amount: Mapped[float] = mapped_column(Float)
	status: Mapped[str] = mapped_column(String, default="pending")  # pending|processed|failed
	initiated_at: Mapped[datetime] = _ts()
	sla_days: Mapped[int] = mapped_column(Integer, default=5)
	retries: Mapped[int] = mapped_column(Integer, default=0)
	stuck: Mapped[bool] = mapped_column(Boolean, default=False)  # planted: retry cannot fix it


# ---- provider fixtures ----
class Inventory(Base):
	__tablename__ = "inventory"
	id: Mapped[int] = _pk()
	kind: Mapped[str] = mapped_column(String, index=True)  # bus|train|flight|hotel|recharge|biller
	name: Mapped[str] = mapped_column(String)
	origin: Mapped[str] = mapped_column(String, default="")
	dest: Mapped[str] = mapped_column(String, default="")  # hotels: city
	price: Mapped[float] = mapped_column(Float)
	duration_min: Mapped[int] = mapped_column(Integer, default=0)
	rating: Mapped[float] = mapped_column(Float, default=4.0)
	available: Mapped[int] = mapped_column(Integer, default=20)
	tags: Mapped[list] = mapped_column(JSON, default=list)
	fail_mode: Mapped[str] = mapped_column(String, default="")  # sellout_on_confirm|price_jump


# ---- merchants ----
class Merchant(Base):
	__tablename__ = "merchants"
	id: Mapped[int] = _pk()
	name: Mapped[str] = mapped_column(String)
	tier: Mapped[str] = mapped_column(String)  # T1|T2|T3
	language: Mapped[str] = mapped_column(String, default="hi-IN")
	kyc_status: Mapped[str] = mapped_column(String, default="verified")
	city: Mapped[str] = mapped_column(String)
	category: Mapped[str] = mapped_column(String)
	tags: Mapped[list] = mapped_column(JSON, default=list)
	bank_status: Mapped[str] = mapped_column(String, default="ok")  # ok|mismatch
	bank_fixable: Mapped[bool] = mapped_column(Boolean, default=True)
	risk_flag: Mapped[str] = mapped_column(String, default="")


class Transaction(Base):
	__tablename__ = "transactions"
	id: Mapped[int] = _pk()
	user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
	merchant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
	customer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
	amount: Mapped[float] = mapped_column(Float)
	kind: Mapped[str] = mapped_column(String)  # sale|booking|recharge|bill|refund|khata|offer
	status: Mapped[str] = mapped_column(String, default="success")
	order_state: Mapped[str] = mapped_column(String, default="fulfilled")  # fulfilled|missing|failed
	ref: Mapped[str] = mapped_column(String, default="")
	meta: Mapped[dict] = mapped_column(JSON, default=dict)
	created_at: Mapped[datetime] = _ts()


class Settlement(Base):
	__tablename__ = "settlements"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
	amount: Mapped[float] = mapped_column(Float)
	status: Mapped[str] = mapped_column(String, default="settled")  # settled|processing|failed
	failure_reason: Mapped[str] = mapped_column(String, default="")
	eta_hours: Mapped[int] = mapped_column(Integer, default=0)
	created_at: Mapped[datetime] = _ts()


class Device(Base):
	__tablename__ = "devices"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
	kind: Mapped[str] = mapped_column(String, default="soundbox")
	status: Mapped[str] = mapped_column(String, default="online")
	last_seen: Mapped[datetime] = _ts()


class Customer(Base):
	__tablename__ = "customers"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
	name: Mapped[str] = mapped_column(String)
	visits: Mapped[int] = mapped_column(Integer, default=0)
	avg_spend: Mapped[float] = mapped_column(Float, default=0)
	last_visit: Mapped[datetime] = _ts()
	reliability: Mapped[float] = mapped_column(Float, default=0.7)


class KhataEntry(Base):
	__tablename__ = "khata_entries"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
	customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
	amount: Mapped[float] = mapped_column(Float)
	due_date: Mapped[datetime] = mapped_column(DateTime)
	status: Mapped[str] = mapped_column(String, default="open")  # open|paid|disputed
	reminders: Mapped[int] = mapped_column(Integer, default=0)
	pay_link: Mapped[str] = mapped_column(String, default="")
	paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---- bridge ----
class Cohort(Base):
	__tablename__ = "cohorts"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(Integer, index=True)
	key: Mapped[str] = mapped_column(String)
	description: Mapped[str] = mapped_column(String)
	size: Mapped[int] = mapped_column(Integer)
	computed_at: Mapped[datetime] = _ts()


class Offer(Base):
	__tablename__ = "offers"
	id: Mapped[int] = _pk()
	merchant_id: Mapped[int] = mapped_column(Integer, index=True)
	title: Mapped[str] = mapped_column(String)
	cohort_key: Mapped[str] = mapped_column(String)
	discount_pct: Mapped[float] = mapped_column(Float)
	min_bill: Mapped[float] = mapped_column(Float, default=0)
	valid_days: Mapped[int] = mapped_column(Integer, default=7)
	budget_cap: Mapped[float] = mapped_column(Float)
	spent: Mapped[float] = mapped_column(Float, default=0)
	expected_redemptions: Mapped[int] = mapped_column(Integer, default=0)
	assumption: Mapped[str] = mapped_column(String, default="")
	status: Mapped[str] = mapped_column(String, default="draft")  # draft|live|ended
	created_at: Mapped[datetime] = _ts()


class OfferDelivery(Base):
	__tablename__ = "offer_deliveries"
	__table_args__ = (UniqueConstraint("offer_id", "user_id"),)
	id: Mapped[int] = _pk()
	offer_id: Mapped[int] = mapped_column(Integer, index=True)
	user_id: Mapped[int] = mapped_column(Integer, index=True)
	status: Mapped[str] = mapped_column(String, default="delivered")  # delivered|redeemed
	reason: Mapped[str] = mapped_column(String, default="")
	delivered_at: Mapped[datetime] = _ts()
	redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
	bill_amount: Mapped[float] = mapped_column(Float, default=0)


class ExperimentAssignment(Base):
	__tablename__ = "experiment_assignments"
	__table_args__ = (UniqueConstraint("experiment", "subject_type", "subject_id"),)
	id: Mapped[int] = _pk()
	experiment: Mapped[str] = mapped_column(String, index=True)
	subject_type: Mapped[str] = mapped_column(String)
	subject_id: Mapped[int] = mapped_column(Integer)
	group: Mapped[str] = mapped_column("grp", String)  # treatment|holdout


# ---- Saathi state ----
class Case(Base):
	__tablename__ = "cases"
	id: Mapped[int] = _pk()
	actor_type: Mapped[str] = mapped_column(String)
	actor_id: Mapped[int] = mapped_column(Integer)
	workflow: Mapped[str] = mapped_column(String)
	status: Mapped[str] = mapped_column(String, default="open")  # open|resolved|escalated|waiting
	summary: Mapped[str] = mapped_column(Text, default="")
	state: Mapped[dict] = mapped_column(JSON, default=dict)
	created_at: Mapped[datetime] = _ts()
	resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentRun(Base):
	__tablename__ = "agent_runs"
	id: Mapped[int] = _pk()
	case_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
	agent: Mapped[str] = mapped_column(String)
	stage: Mapped[str] = mapped_column(String)  # detect|decide|act|approve|measure|escalate
	detail: Mapped[str] = mapped_column(Text, default="")
	created_at: Mapped[datetime] = _ts()


class Action(Base):
	__tablename__ = "actions"
	id: Mapped[int] = _pk()
	case_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
	tool: Mapped[str] = mapped_column(String)
	args: Mapped[dict] = mapped_column(JSON, default=dict)
	idempotency_key: Mapped[str] = mapped_column(String, unique=True)
	decision: Mapped[str] = mapped_column(String)
	reason: Mapped[str] = mapped_column(String, default="")
	status: Mapped[str] = mapped_column(String, default="pending")  # done|failed|blocked|pending
	result: Mapped[dict] = mapped_column(JSON, default=dict)
	attempts: Mapped[int] = mapped_column(Integer, default=0)
	created_at: Mapped[datetime] = _ts()


class Approval(Base):
	__tablename__ = "approvals"
	id: Mapped[int] = _pk()
	case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
	actor_type: Mapped[str] = mapped_column(String)
	actor_id: Mapped[int] = mapped_column(Integer)
	kind: Mapped[str] = mapped_column(String)
	title: Mapped[str] = mapped_column(String, default="")
	payload: Mapped[dict] = mapped_column(JSON, default=dict)
	payload_hash: Mapped[str] = mapped_column(String)
	status: Mapped[str] = mapped_column(String, default="pending")  # pending|approved|rejected|expired|used
	expires_at: Mapped[datetime] = mapped_column(DateTime)
	created_at: Mapped[datetime] = _ts()
	decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
	decided_via: Mapped[str] = mapped_column(String, default="")


class Escalation(Base):
	__tablename__ = "escalations"
	id: Mapped[int] = _pk()
	case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
	actor_type: Mapped[str] = mapped_column(String)
	actor_id: Mapped[int] = mapped_column(Integer)
	reason: Mapped[str] = mapped_column(String)
	packet: Mapped[dict] = mapped_column(JSON, default=dict)
	status: Mapped[str] = mapped_column(String, default="open")
	sla_minutes: Mapped[int] = mapped_column(Integer, default=60)
	reply: Mapped[str] = mapped_column(Text, default="")
	created_at: Mapped[datetime] = _ts()
	resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
	__tablename__ = "audit_log"
	id: Mapped[int] = _pk()
	ts: Mapped[datetime] = _ts()
	actor: Mapped[str] = mapped_column(String)  # who
	what: Mapped[str] = mapped_column(String)
	why: Mapped[str] = mapped_column(Text, default="")
	tool: Mapped[str] = mapped_column(String, default="")
	inputs: Mapped[dict] = mapped_column(JSON, default=dict)
	outputs: Mapped[dict] = mapped_column(JSON, default=dict)
	approval_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
	outcome: Mapped[str] = mapped_column(String, default="")
	original_text: Mapped[str] = mapped_column(Text, default="")
	translated_text: Mapped[str] = mapped_column(Text, default="")
	case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ImpactEvent(Base):
	__tablename__ = "impact_events"
	id: Mapped[int] = _pk()
	kind: Mapped[str] = mapped_column(String, index=True)
	actor_type: Mapped[str] = mapped_column(String)
	actor_id: Mapped[int] = mapped_column(Integer)
	amount: Mapped[float] = mapped_column(Float, default=0)
	group: Mapped[str] = mapped_column("grp", String, default="treatment")
	meta: Mapped[dict] = mapped_column(JSON, default=dict)
	ts: Mapped[datetime] = _ts()


class Notification(Base):
	__tablename__ = "notifications"
	id: Mapped[int] = _pk()
	actor_type: Mapped[str] = mapped_column(String)
	actor_id: Mapped[int] = mapped_column(Integer, index=True)
	channel: Mapped[str] = mapped_column(String, default="inbox")
	message: Mapped[str] = mapped_column(Text)
	meta: Mapped[dict] = mapped_column(JSON, default=dict)
	ts: Mapped[datetime] = _ts()


class MemoryItem(Base):
	"""SQLite fallback for the Memory interface (same contract as the Cognee adapter)."""
	__tablename__ = "memory_items"
	id: Mapped[int] = _pk()
	actor: Mapped[str] = mapped_column(String, index=True)  # "user:3" | "merchant:7" | "sop"
	kind: Mapped[str] = mapped_column(String)
	text: Mapped[str] = mapped_column(Text)
	meta: Mapped[dict] = mapped_column(JSON, default=dict)
	ts: Mapped[datetime] = _ts()


def make_engine(url: str | None = None):
	url = url or settings.DATABASE_URL
	kw: dict = {"connect_args": {"check_same_thread": False}}
	if url in ("sqlite://", "sqlite:///:memory:"):
		kw["poolclass"] = StaticPool
	elif url.startswith("sqlite:///"):
		(ROOT / "data").mkdir(exist_ok=True)
	eng = create_engine(url, **kw)

	@event.listens_for(eng, "connect")
	def _pragma(dbapi, _):  # the audit log is append-only at the database level
		cur = dbapi.cursor()
		cur.execute("PRAGMA journal_mode=WAL")
		cur.close()

	return eng


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

AUDIT_TRIGGERS = [
	"CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
	"BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
	"CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
	"BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
]


def init_db(eng=None, drop: bool = False) -> None:
	eng = eng or engine
	if drop:
		Base.metadata.drop_all(eng)
	Base.metadata.create_all(eng)
	with eng.begin() as conn:
		for stmt in AUDIT_TRIGGERS:
			conn.exec_driver_sql(stmt)


def get_session() -> Session:
	return SessionLocal()
