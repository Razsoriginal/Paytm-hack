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

Beyond those, Saathi also handles everyday jobs:

- **Upload a bill and pay** (C3): attach a photo or PDF, check what was read, approve once.
- **Manage a booking** (C4): reschedule, cancel or raise a concern, with the exact fee and
  refund shown before anything changes.
- **Spend insights and budgets** (C5): ask about your own spending; set budgets and goals.
- **Inventory for shops** (M1): add, adjust and check stock by chat, voice or invoice upload.

It is not a trip planner. It is a cross-service teammate that acts, handles what goes wrong,
and feeds merchants better offers.

## Run it

```bash
make install        # creates .venv and installs Python deps into it
make ui             # build the Svelte UI (needs Node 20+); `make dev` does this if missing
cp .env.example .env  # optional; with no keys everything runs in mock mode
make seed           # reproducible simulated world (SEED_RANDOM)
make dev            # http://localhost:8000
make test           # 63 tests
make eval           # runs every planted scenario, writes eval/report.md
make doctor         # KEY PRESENT/MISSING and live/mock per service (never prints values)
make dev-ui         # optional: Vite dev server on :5173, proxies /api to :8000
```

Every `make` target runs inside `.venv`, which is created on first use. To use it by hand:
`source .venv/bin/activate` (Windows: `.venv\Scripts\activate`, and run the commands from the
Makefile directly, since its paths assume macOS or Linux).

## Deploy

The UI is static and goes on any CDN. The API keeps SQLite, uploads and the TTS cache on
disk and runs a 60-second scan loop, so it needs a long-lived process with a volume —
serverless functions cannot host it.

**API on Render** (`render.yaml`, `Dockerfile`)

1. New > Blueprint, pick this repo. `plan: starter` is what allows the persistent disk at
   `/var/data`; the free plan cannot attach one.
2. Set `SARVAM_API_KEY` and `CORS_ORIGINS=https://<your-app>.vercel.app` in the dashboard.
3. An empty database seeds itself on first boot, so no `make seed` is needed.

On the free plan, drop the `disk:` block and set `DATA_DIR=/tmp/data` with
`DATABASE_URL=sqlite:////tmp/data/saathi.db`. Everything still works, but the simulated
world reseeds whenever the service restarts.

**UI on Vercel** (`vercel.json`)

1. Import the repo, no CLI needed.
2. Set **Root Directory** to `frontend` and **Framework Preset** to `Vite`. The repo root has
   no `package.json`, so the preset has nothing to build until the root directory is set.
   Vercel then fills in `npm run build` and `dist` by itself.
3. Set `VITE_API_BASE=https://<your-service>.onrender.com`. It is read at build time, so
   redeploy after changing it.
4. Leave `VITE_API_BASE` unset to keep the UI calling `/api` on its own origin, which is what
   `make dev` and the Docker image do.

Leaving the Root Directory at the repo root also works if you set Vite as the preset: the
root `vercel.json` runs the install and build inside `frontend/` and publishes `frontend/dist`.

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
| `backend/saathi/bills.py` | Upload rules, field checks, computed confidence, bill payment |
| `backend/saathi/changequote.py` | Fees, fare differences and refunds from partner policy tables |
| `backend/saathi/analytics.py` | Spend tools: summary, trend, compare, anomalies, forecast, budgets |
| `backend/saathi/inventory.py` | Stock tools: movements, undo, low stock, dead stock, margins |
| `backend/saathi/adapters/docai.py` | Sarvam Document AI job lifecycle, cache and mock reader |
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

- A bill is paid only from fields the user confirmed. Confidence per field is computed from
  the document text (1.0 printed and valid, 0.6 partial or failing a check, 0.3 not found),
  never taken from the model.
- A bill already paid is blocked; one far above the usual amount must be acknowledged; an
  unknown biller goes to a person.
- Text inside a document is data. An instruction hidden in a bill is ignored (tested).
- Booking fees and refunds come from partner policy tables, never from the model. A changed
  quote voids the approval, and a failed multi-item change is rolled back.
- Every number in a spend answer comes from an analytics tool (tested against SQL).
- Stock never goes negative; manual changes can be undone for 10 minutes.

## Uploaded files

- PDF, PNG, JPEG or ZIP only, checked by content, up to 10 MB and 10 PDF pages.
- Text chunks and EXIF are stripped before a file is stored, and the stored copy is deleted
  after 24 hours.
- Account numbers are masked to the last 4 digits in the UI and in the audit log.
- With a Sarvam key the file is sent to Sarvam Document AI to be read, and the review card
  says so. In mock mode nothing leaves the machine and only the seeded samples can be read.

## Privacy design (reference: India's DPDP Act, 2023)

- Spend insights have their own consent scope, on by default for the user's own view. Spend
  data is never used in cohorts or shown to shops.
- Two further consent scopes: `personalisation` and `offers`. `offers` is opt-in and explained in
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
8. **Consumer, user 1**: tap the paperclip, pick "Phone bill, number printed with spaces".
   Tick the highlighted number, confirm, approve. Then try "Electricity bill that is already
   paid" and "Broadband bill with an instruction hidden in the text".
9. **Trips**: book `Mumbai se Pune 2026-12-15 1 raat`, open Trips, pick a new date and
   Reschedule. Show Now / After / fee, approve.
10. **Chat**: `How much did I spend on food last month?`, then `plan a budget for me`. Open
    the Spend tab.
11. **Merchant 6**: `Add 20 packets of Parle-G at ₹8, selling ₹10`, press Undo. Ask
    `What is running low?`. Upload the supplier invoice with the paperclip and approve.
