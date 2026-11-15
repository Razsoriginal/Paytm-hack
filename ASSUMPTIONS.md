# Assumptions and known limitations

## Scope
- **Workflows built:** C1 trip bundle, C2 failed payment and refund, B1 preference-to-offer
  bridge, W1 money not received, W2 collect dues, C3 bill upload and pay, C4 manage a booking,
  C5 spend insights and budgets, M1 inventory.
- **Not built yet:** W3 (restock and fund it), M2 supplier reorder and payment, M3 GST
  invoices and settlement reconciliation, X1 proactive nudges, C6 disruption handling, C7
  group split, C8 autopay suggestions, M4 daily voice brief. Low-stock results already carry
  a suggested reorder quantity; nothing orders or pays a supplier.
- **Frontend:** Svelte 5, Vite, Tailwind CSS 4 and Lucide icons, in plain JavaScript. Fonts
  are bundled so the demo works offline: Plus Jakarta Sans for text, Bricolage Grotesque for
  headings and amounts, Noto Sans Devanagari for Hindi. The built UI (`frontend/dist`) is not
  committed; `make dev` builds it when missing.
- **Python:** developed on 3.13. Nothing here needs more than 3.11.
- **Seed size:** 120 consumers. A smaller set cannot reach `COHORT_MIN_SIZE`, and a 20% holdout
  would be one or two people. Users 1-20 are the demo personas.
- The live timeline uses server-sent events from `/api/runs/stream`.

## Services
- **Sarvam:** `POST /v1/chat/completions`, `/speech-to-text`, `/text-to-speech`, `/translate`.
  Chat is sent with both `Authorization: Bearer` and `api-subscription-key`; voice and
  translation use `api-subscription-key`. The default speech model is `saaras:v3`; Sarvam also
  offers `saaras:v4`.
- **The live Sarvam path is not yet verified end to end.** Tests and the evaluation run in
  mock mode. With a key set, the app tries Sarvam first and drops to the scripted mock per
  request if a call fails or returns output that fails the schema. `make doctor` shows which
  mode each service is in.
- **Mock LLM** is keyword rules for Hinglish and English. In mock mode replies are English and
  speech uses the browser's own voice input and speech synthesis.
- **Cognee:** without a key, memory runs on SQLite behind the same interface. The Cognee
  adapter is written but not yet tested against a live instance.
- **Paytm PG:** `PAYTM_ENV=mock` by default, so payment links are mock. The staging path needs
  the `paytmchecksum` package and is not yet tested. `PAYTM_MID` and `PAYTM_MERCHANT_KEY` are
  read as well as `PAYTM_PG_*`.

## Bills, bookings, spend and stock
- **All AI in these features is Sarvam only.** There is no other provider behind them; the
  offline fallback is the mock.
- **Document AI** uses `POST /doc-ai/v1/job/extract` and `/digitise` with `language` and
  `output_format` (`md`), polls `/doc-ai/v1/job/{id}/status`, then reads `/results` and
  `/download-url`. The job lifecycle is tested against a fake transport. **The live path is
  not yet verified against Sarvam.**
- **The mock reader only understands the seeded samples**: PDFs written by the seeder, and
  PNGs that carry their text in a metadata chunk. A real photo in mock mode is reported as
  unreadable. The seeded "photos" are grey placeholder images, not pictures of bills.
- **Spend answers are template sentences** filled from tool results, in mock and live mode,
  so no figure can be invented. The model's part is choosing the tool.
- **Category rules are deterministic** (payment type, then shop type). The model is not
  consulted for ambiguous payees yet; those fall into "other" until the user corrects them.
- **A proposed budget** is the 3-month average less a 10% savings target, rounded down to
  ₹100. Proposing one saves it straight away; any line can be changed in chat.
- **Forecasts are straight-line** from spend so far this month.
- **Booking changes:** fares do not vary by date in the fixtures, so a reschedule normally
  costs only the change fee. A trip departs at 09:00 on its travel date for fee tiers.
- **A reschedule moves every item** of the booking to the new date. Changing passengers or
  swapping one item is supported by the quote tools but has no button yet.
- **Concerns:** a duplicate charge inside the auto limit is refunded on the spot; anything
  else becomes a ticket with the evidence attached and goes to the support desk.
- **Inventory:** new products from an invoice are priced at cost plus 20% until changed.
  T1 shops can ask about stock but not change it. Reorder quantity covers 14 days of sales
  at the last 14 days' pace, raised 10-20% in festival months from a fixed calendar.
- **Stock uploads always need review and one approval**, whatever the confidence.

## Simulation
- Trips are outbound leg + hotel × nights + return leg. `nights` defaults to 1.
- Origin defaults to the user's home city; destination and date are the required fields.
- Offer and reminder responses are **simulated with fixed probabilities** (offers: 30% redeem,
  12% holdout purchase, 3% opt-out; Khata: 0.7 × reliability vs 0.25 × reliability). These are
  simulation parameters, not findings. Holdout groups are small, so uplift shows the method.
- Compensating refunds in a saga run automatically at any amount, but only against the exact
  charge Saathi just made (the policy engine checks the original transaction).
- A refund above the auto limit that is not a rollback goes to a human rather than to a tap.
- T1 merchants send Khata reminders without a tap (L2); T2 and T3 approve the plan once (L3).
- `.env` is git-ignored. Never commit keys.
