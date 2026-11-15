# Assumptions and known limitations

## Follow-up scope and conflicts
- The requested `saathi_master_prompt.md` is not present in this checkout; the master
  requirements cannot yet be checked against the follow-up. The follow-up takes precedence
  where it conflicts with those requirements.
- SQLite remains the local memory fallback; it is not an AI provider. New document and
  language features must use Sarvam or mock mode, without requiring new environment variables.
- The v2 backend is work in progress. Bill uploads, spend and inventory do not yet have the
  complete requested UI and evaluation coverage. Live Document AI is unverified.
- Booking cancellation compensation and the remaining v2 safety and end-to-end tests still
  require work; the existing passing tests do not establish v2 completion.

## Scope
- **Workflows built:** C1 trip bundle, C2 failed payment and refund, B1 preference-to-offer
  bridge, W1 money not received, W2 collect dues.
- **Not built yet:** W3 (restock and fund it). The loan tools and their policy rules exist;
  the workflow does not.
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
