"""Audit log, agent-run timeline, approvals, escalations, impact ledger and the action executor."""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from . import policy, tools
from .config import settings
from .db import (
	Action, AgentRun, Approval, AuditLog, Case, Escalation, ImpactEvent, Merchant, Notification,
	now,
)
from .policy import Decision, ProposedAction, payload_hash
from .tools import ToolError

MAX_ATTEMPTS = 3


# ---------------- audit + timeline + impact ----------------
def _mask(v):
	"""Consumer and account numbers never reach the audit log in full."""
	if isinstance(v, dict):
		return {k: ("•" * max(len(str(x)) - 4, 0) + str(x)[-4:]) if k == "consumer_number" and x
			else _mask(x) for k, x in v.items()}
	if isinstance(v, list):
		return [_mask(x) for x in v]
	return v


def audit(s: Session, actor: str, what: str, why: str = "", tool: str = "", inputs: dict | None = None,
		outputs: dict | None = None, approval_id: int | None = None, outcome: str = "",
		case_id: int | None = None, original_text: str = "", translated_text: str = "") -> None:
	s.add(AuditLog(actor=actor, what=what, why=why, tool=tool, inputs=_mask(inputs or {}),
		outputs=_mask(outputs or {}), approval_id=approval_id, outcome=outcome, case_id=case_id,
		original_text=original_text, translated_text=translated_text))


def run_log(s: Session, case_id: int | None, agent: str, stage: str, detail: str) -> None:
	"""One step on the streaming timeline: detect → decide → act → approve → measure."""
	s.add(AgentRun(case_id=case_id, agent=agent, stage=stage, detail=detail))


def impact(s: Session, kind: str, actor_type: str, actor_id: int, amount: float = 0,
		group: str = "treatment", **meta) -> None:
	s.add(ImpactEvent(kind=kind, actor_type=actor_type, actor_id=actor_id, amount=amount,
		group=group, meta=meta))


def open_case(s: Session, actor_type: str, actor_id: int, workflow: str, summary: str = "") -> Case:
	c = Case(actor_type=actor_type, actor_id=actor_id, workflow=workflow, summary=summary)
	s.add(c)
	s.flush()
	return c


def close_case(s: Session, case: Case, status: str = "resolved") -> None:
	case.status = status
	if status in ("resolved", "escalated"):
		case.resolved_at = now()
	impact(s, f"case_{status}", case.actor_type, case.actor_id, workflow=case.workflow,
		case_id=case.id, seconds=(now() - case.created_at).total_seconds())


def inbox(s: Session, actor_type: str, actor_id: int, message: str, **meta) -> None:
	s.add(Notification(actor_type=actor_type, actor_id=actor_id, message=message, meta=meta))


# ---------------- approvals ----------------
YES = {"haan", "han", "ha", "haa", "yes", "ok", "okay", "theek", "thik", "approve", "ho", "houdu",
	"kar", "karo", "kardo", "हाँ", "हां", "ठीक", "होय"}
NO = {"nahi", "nahin", "na", "no", "mat", "cancel", "reject", "nako", "beda", "नहीं", "नको"}


def parse_voice_decision(text: str) -> bool | None:
	"""'haan' / 'nahi' style answers. None when the answer is not clearly either."""
	words = {w.strip(".,!?।").lower() for w in text.split()}
	yes, no = bool(words & YES), bool(words & NO)
	if yes == no:
		return None
	return yes


def create_approval(s: Session, actor_type: str, actor_id: int, kind: str, title: str,
		payload: dict, case_id: int | None = None) -> Approval:
	a = Approval(actor_type=actor_type, actor_id=actor_id, kind=kind, title=title, payload=payload,
		payload_hash=payload_hash(payload), case_id=case_id,
		expires_at=now() + timedelta(minutes=settings.PLAN_APPROVAL_TTL_MIN))
	s.add(a)
	s.flush()
	audit(s, f"{actor_type}:{actor_id}", "approval_requested", title, inputs=payload,
		approval_id=a.id, outcome="pending", case_id=case_id)
	run_log(s, case_id, "Approval", "approve", f"Waiting for approval: {title}")
	return a


def decide_approval(s: Session, approval_id: int, approve: bool, via: str = "button") -> Approval:
	a = s.get(Approval, approval_id)
	if not a:
		raise ValueError("approval_not_found")
	if a.status != "pending":
		return a  # already decided: a double tap changes nothing
	if a.expires_at < now():
		a.status = "expired"
	else:
		a.status = "approved" if approve else "rejected"
	a.decided_at, a.decided_via = now(), via
	audit(s, f"{a.actor_type}:{a.actor_id}", f"approval_{a.status}", a.title, approval_id=a.id,
		outcome=a.status, case_id=a.case_id, inputs={"via": via})
	impact(s, f"approval_{a.status}", a.actor_type, a.actor_id, approval_kind=a.kind)
	return a


# ---------------- escalation ----------------
def build_packet(s: Session, case: Case, issue: str, why: str, recommended: str,
		suggested_reply: str, amount: float = 0, confidence: float = 1.0) -> dict:
	tier = ""
	if case.actor_type == "merchant" and (m := s.get(Merchant, case.actor_id)):
		tier = m.tier
	tried = [{"tool": a.tool, "decision": a.decision, "status": a.status,
		"result": a.result or {"reason": a.reason}}
		for a in s.query(Action).filter_by(case_id=case.id).order_by(Action.id).all()]
	return {"case_id": case.id, "actor_type": case.actor_type, "actor_id": case.actor_id,
		"tier": tier, "workflow": case.workflow, "issue_summary": issue, "tried": tried,
		"recommended_action": recommended, "why_escalated": why, "amount": amount,
		"confidence": confidence, "suggested_reply": suggested_reply}


def open_escalation(s: Session, packet: dict) -> Escalation:
	e = Escalation(case_id=packet.get("case_id"), actor_type=packet.get("actor_type", "consumer"),
		actor_id=packet.get("actor_id", 0), reason=packet.get("why_escalated", ""), packet=packet,
		sla_minutes=30 if packet.get("amount", 0) > settings.ESCALATE_ABOVE_INR else 60)
	s.add(e)
	s.flush()
	return e


def escalate(s: Session, case: Case, issue: str, why: str, recommended: str,
		suggested_reply: str, amount: float = 0, confidence: float = 1.0) -> Escalation:
	packet = build_packet(s, case, issue, why, recommended, suggested_reply, amount, confidence)
	res = execute(s, ProposedAction(tool="escalate_to_human", args={"packet": packet},
		actor_type=case.actor_type, actor_id=case.actor_id, case_id=case.id, agent="Escalation",
		idempotency_key=f"case{case.id}:escalate", why=why))
	run_log(s, case.id, "Escalation", "escalate", f"Escalated to a human: {why}")
	close_case(s, case, "escalated")
	s.commit()
	return s.get(Escalation, res.result["escalation_id"])


def resolve_escalation(s: Session, escalation_id: int, reply: str) -> Escalation:
	e = s.get(Escalation, escalation_id)
	e.status, e.reply, e.resolved_at = "resolved", reply, now()
	inbox(s, e.actor_type, e.actor_id, f"Support team: {reply}", escalation_id=e.id)
	audit(s, "ops", "escalation_resolved", reply, case_id=e.case_id, outcome="resolved")
	return e


# ---------------- action executor ----------------
@dataclass
class ActionResult:
	outcome: str  # done | failed | needs_approval | blocked | escalate
	decision: Decision
	result: dict = field(default_factory=dict)
	error: str = ""
	replayed: bool = False
	action_id: int | None = None

	@property
	def ok(self) -> bool:
		return self.outcome == "done"


def execute(s: Session, p: ProposedAction) -> ActionResult:
	"""Policy check, then run the tool at most once per idempotency key, with retries."""
	row = s.query(Action).filter_by(idempotency_key=p.idempotency_key).first()
	if row and row.status == "done":
		d = Decision(outcome="execute", level="", reason="idempotent_replay")
		return ActionResult("done", d, row.result, replayed=True, action_id=row.id)

	d = policy.decide(s, p)
	if not row:
		row = Action(case_id=p.case_id, tool=p.tool, args=_mask(p.args),
			idempotency_key=p.idempotency_key,
			decision=d.outcome)
		s.add(row)
	row.decision, row.reason = d.outcome, d.reason
	who = f"{p.agent or 'agent'} for {p.actor_type}:{p.actor_id}"

	if d.outcome != "execute":
		row.status = "blocked" if d.outcome == "blocked" else "pending"
		audit(s, who, f"{p.tool}:{d.outcome}", p.why, p.tool, p.args, {"reason": d.reason},
			p.approval_id, d.outcome, p.case_id)
		run_log(s, p.case_id, "Compliance", "decide", f"{p.tool} → {d.outcome} ({d.reason})")
		s.commit()
		return ActionResult(d.outcome, d, error=d.reason, action_id=row.id)

	error = ""
	for attempt in range(1, MAX_ATTEMPTS + 1):
		row.attempts = attempt
		try:
			result = tools.call(s, p.tool, p.args)
			row.status, row.result = "done", result
			audit(s, who, f"{p.tool}:done", p.why, p.tool, p.args, result, p.approval_id, "success",
				p.case_id)
			run_log(s, p.case_id, p.agent or "Executor", "act", f"{p.tool} ✓ [{d.level}: {d.reason}]")
			s.commit()
			return ActionResult("done", d, result, action_id=row.id)
		except ToolError as e:
			error = e.code
			if not e.retryable:
				break
	row.status, row.result = "failed", {"error": error}
	audit(s, who, f"{p.tool}:failed", p.why, p.tool, p.args, {"error": error}, p.approval_id,
		"failed", p.case_id)
	run_log(s, p.case_id, p.agent or "Executor", "act", f"{p.tool} ✗ {error}")
	s.commit()
	return ActionResult("failed", d, error=error, action_id=row.id)


@dataclass
class Step:
	name: str
	action: Callable[[dict], ProposedAction]  # receives results of earlier steps by name
	compensate: Callable[[dict], ProposedAction | None] | None = None


@dataclass
class SagaResult:
	ok: bool
	results: dict
	failed_step: str = ""
	error: str = ""
	outcome: str = ""
	compensated: list[str] = field(default_factory=list)
	compensation_failed: list[str] = field(default_factory=list)


def run_saga(s: Session, steps: list[Step]) -> SagaResult:
	"""Run steps in order. If one fails, undo the completed ones in reverse so nothing is left
	half-done. A compensation that itself fails is reported, never swallowed."""
	results: dict = {}
	done: list[Step] = []
	for st in steps:
		res = execute(s, st.action(results))
		if not res.ok:
			out = SagaResult(False, results, st.name, res.error, res.outcome)
			for prev in reversed(done):
				comp = prev.compensate(results) if prev.compensate else None
				if comp is None:
					continue
				comp.compensation = True
				cres = execute(s, comp)
				(out.compensated if cres.ok else out.compensation_failed).append(prev.name)
			return out
		results[st.name] = res.result
		done.append(st)
	return SagaResult(True, results)
