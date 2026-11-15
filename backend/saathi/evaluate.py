"""`make eval`: reseed, run every planted scenario, write eval/report.md. Fails on violations."""
import json
import sys

from . import db, seed
from .api import SCENARIOS, metrics
from .config import ROOT
from .db import ImpactEvent


def main() -> int:
	seed.seed()
	s = db.get_session()
	lines = []
	for name, fn in SCENARIOS.items():
		log = []
		fn(s, log)
		s.commit()
		lines.append(f"| {name} | {log[-1]['saathi'][:110].replace('|', '/')} |")
	m = metrics(s)
	s.commit()
	kh = [e for e in s.query(ImpactEvent).filter_by(kind="khata_recovered")]
	b = m["bridge"] or {}
	rep = f"""# Saathi evaluation report

All figures come from the simulated database after running every planted scenario once
(seed {seed.settings.SEED_RANDOM}). Nothing here is real Paytm data.

| Metric | Value |
|---|---|
| Consumer auto-resolution % | {m['consumer']['auto_resolution_pct']} |
| Human escalation % (all closed cases) | {m['escalation_pct']} |
| Saga recoveries (rolled back cleanly) | {m['consumer']['saga_recoveries']} |
| Approval-hash mismatches caught | {m['consumer']['hash_mismatches']} |
| ₹ recovered, reminded accounts | {m['merchant']['khata_recovered_inr']:.0f} |
| ₹ recovered, holdout accounts | {m['merchant']['khata_holdout_inr']:.0f} |
| Bridge redemption rate | {b.get('redemption_rate')} |
| Bridge holdout purchase rate | {b.get('holdout_rate')} |
| Bridge uplift (points) | {b.get('uplift_points')} |
| Bridge incremental ₹ | {b.get('incremental_inr')} |
| Bridge opt-out rate (guardrail) | {b.get('opt_out_rate')} |
| p50 time to resolution (s) | {m['resolution_p50_s']} |
| **Policy violations (must be 0)** | **{len(m['policy_violations'])}** |

Holdout groups are small ({b.get('holdout')} users, {sum(1 for e in kh if e.group == 'holdout')}
paying holdout accounts), so uplift numbers show the method, not a statistically firm result.
User responses to offers and reminders are simulated with fixed probabilities (see flows.py).

## Scenario outcomes

| Scenario | Final reply |
|---|---|
{chr(10).join(lines)}
"""
	(ROOT / "eval").mkdir(exist_ok=True)
	(ROOT / "eval" / "report.md").write_text(rep, encoding="utf-8")
	(ROOT / "eval" / "report.json").write_text(json.dumps(m, indent=1, default=str))
	print(rep)
	return 1 if m["policy_violations"] else 0


if __name__ == "__main__":
	sys.exit(main())
