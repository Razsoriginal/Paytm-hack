# Saathi: a two-sided AI teammate for Paytm (working prototype)

> **Simulated data.** Paytm core systems, partner inventory, wallets, settlements and all
> money in this repo are fake. Nothing here has real Paytm access.

Saathi is one agent engine with two faces and a link between them. It receives or detects a
need, decides, acts through tools, escalates to a human only when needed, and measures the ₹
outcome.

- **Consumer Saathi**: plan and book a trip with one approval and one payment, fix a failed
  payment or late refund.
- **Merchant Saathi**: find money that did not arrive, collect Khata dues.
- **The bridge**: consented user preferences become a cohort count, the merchant approves an
  offer, matching users get it with a reason, and uplift is measured against a holdout.

It is not a trip planner. It is a cross-service teammate that acts, handles what goes wrong,
and feeds merchants better offers.

## Run it

```bash
make install        # creates .venv and installs Python deps into it
make ui             # build the Svelte UI (needs Node 20+); `make dev` does this if missing
cp .env.example .env  # optional; with no keys everything runs in mock mode
make seed           # reproducible simulated world (SEED_RANDOM)
make dev            # http://localhost:8000
make test           # 23 tests
make eval           # runs every planted scenario, writes eval/report.md
make doctor         # KEY PRESENT/MISSING and live/mock per service (never prints values)
make dev-ui         # optional: Vite dev server on :5173, proxies /api to :8000
```

Every `make` target runs inside `.venv`, which is created on first use. To use it by hand:
`source .venv/bin/activate` (Windows: `.venv\Scripts\activate`, and run the commands from the
Makefile directly, since its paths assume macOS or Linux).

## Architecture

```
message (text/voice) · scheduled scans · scenario runner
   │
Manager ── Sarvam proposes an Intent (schema-validated) ── loads memory
   ├─ Concierge (C1 trip)      ├─ Diagnoser/Fixer (W1)
   ├─ Care (C2)                ├─ Collector (W2)
   └─ Personalisation/Seller (B1 bridge)
   │            every step is a ProposedAction
Policy engine → execute | needs_approval | blocked | escalate
   │
Action executor (idempotency keys, retries, saga rollback) → typed mock tools
   │
Audit log (append-only) · impact ledger (₹ vs holdout) · memory
```

**Sarvam proposes, policy decides, the executor acts, memory remembers.** The model can only
return an `Intent` object; it has no way to call a tool. Prices and availability come from the
deterministic planner, never from the model.

| Path | What |
|---|---|
| `backend/saathi/policy.py` | Autonomy ladder L0-L4, limits, consent, cohort and approval-hash checks |
| `backend/saathi/services.py` | Executor, saga, approvals, escalation packet, audit, impact |
| `backend/saathi/tools.py` | Typed mock tools |
| `backend/saathi/agents/` | Manager routing and the five workflows |
| `backend/saathi/privacy.py` | Preference store, consent, cohorts, deletion |
| `backend/saathi/adapters/` | Sarvam LLM/voice/translate, memory, Paytm PG, each with a mock |
| `frontend/` | Svelte 5 + Vite + Tailwind CSS 4 UI with Lucide icons: Consumer, Merchant, Support desk, Impact, Scenarios |

## Safety rules that are tested

- An approval is bound to a hash of the exact plan. A price change after approval voids it.
- A booking is a saga: hold → pay once → confirm. A failure cancels and refunds everything.
- Double submit never double charges (idempotency keys).
- Daily spend cap and wallet checks run before any debit.
- Success is only reported after a tool confirms the final state.
- Invalid model output is rejected, not executed.
- The audit log cannot be updated or deleted (database triggers).

## Privacy design (reference: India's DPDP Act, 2023)

- Two consent scopes: `personalisation` and `offers`. `offers` is opt-in and explained in
  plain language. Turning it off takes effect on the next read.
- Each preference records value, source (`stated` or `inferred`), confidence and time.
  Stated beats inferred. Inferred items are shown as guesses the user can correct.
- "What Saathi knows about me": view, add, delete one, delete all. Deletion clears memory and
  recomputes cohorts.
- Only six categories are allowed (budget, travel mode, cuisine, pace, price sensitivity,
  time slot). Health, religion, caste and politics are refused.
- Merchants see cohort counts at or above `COHORT_MIN_SIZE`, never a person.
- 20% of matched users are held out so uplift is measured, and opt-out rate is a guardrail.

## What is simulated

Everything except the Sarvam calls (live only when a key is set and reachable). See
`ASSUMPTIONS.md` for the full list, including what was not built and what was not tested live.

## 3-minute demo

1. **Consumer, user 1**: type `Mumbai se Goa kal 2 raat`. Show the itemised plan and "why".
   Approve. The hotel sells out mid-checkout: Saathi cancels, refunds, offers a replacement.
   Approve again. Open Bookings.
2. **Consumer, user 2**: `mere paise kat gaye`. Saathi finds the debit with no booking and
   refunds it. Switch to user 7: same message, large amount, goes to a human.
3. **Ops**: open the escalation. Show the packet: what was tried, why it escalated, the
   suggested reply. Resolve it.
4. **Merchant 1**: `naye customer ke liye offer`. Show the cohort count (no names) and the
   drafted offer. Approve.
5. **Consumer, user 1**: Inbox shows the offer with "Why am I seeing this?". Redeem, approve.
6. **Scenarios**: run "B1 offer bridge" and "W2 collect dues" to simulate responses.
7. **Impact**: bridge funnel, uplift vs holdout, opt-out rate, ₹ recovered, policy violations 0.
