# Saathi evaluation report

All figures come from the simulated database after running every planted scenario once
(seed 42). Nothing here is real Paytm data.

| Metric | Value |
|---|---|
| Consumer auto-resolution % | 57.1 |
| Human escalation % (all closed cases) | 27.8 |
| Saga recoveries (rolled back cleanly) | 1 |
| Approval-hash mismatches caught | 1 |
| ₹ recovered, reminded accounts | 10314 |
| ₹ recovered, holdout accounts | 0 |
| Bridge redemption rate | 0.333 |
| Bridge holdout purchase rate | 0.0 |
| Bridge uplift (points) | 33.3 |
| Bridge incremental ₹ | 1852 |
| Bridge opt-out rate (guardrail) | 0.0 |
| p50 time to resolution (s) | 0.01 |
| **Policy violations (must be 0)** | **0** |

Holdout groups are small (5 users, 0
paying holdout accounts), so uplift numbers show the method, not a statistically firm result.
User responses to offers and reminders are simulated with fixed probabilities (see flows.py).

## Scenario outcomes

| Scenario | Final reply |
|---|---|
| C1 happy path | Booked. 3 items confirmed for ₹1792. Your trip is in Bookings. |
| C1 sold out mid-checkout | Booked. 3 items confirmed for ₹2634. Your trip is in Bookings. |
| C1 price change after approval | Booked. 3 items confirmed for ₹2244. Your trip is in Bookings. |
| C1 needs one question | Booked. 3 items confirmed for ₹1794. Your trip is in Bookings. |
| C2 debited, not booked | ₹1850 was debited but the order did not go through. I have refunded it to your wallet. New balance ₹9850. |
| C2 recharge failed | Your ₹299 recharge had failed after the debit. I retried it and it is now done. You were not charged again. |
| C2 refund past SLA | Your ₹1240 refund was past its 5-day window. I pushed it through and it is now in your wallet. |
| C2 refund stuck (escalates) | I have passed this to our support team with all the details. They will get back to you shortly. |
| C2 refund inside SLA | Your ₹1240 refund is in progress and inside the 5-day window. Expected in about 3 day(s). I will notify you. |
| C2 large amount (escalates) | This one needs a specialist. I have shared the full details with our support team, so you will not have to rep |
| C2 dispute (escalates) | This one needs a specialist. I have shared the full details with our support team, so you will not have to rep |
| B1 offer bridge | {"offer_id": 1, "title": "15% off on bills above \u20b9313", "cohort_size_now": 20, "matched": 20, "delivered" |
| W1 bank mismatch, fixable | Done. Bank details updated and ₹4860 is credited. |
| W1 bank mismatch, escalates | The bank needs a document check for this. I have sent everything to the support team, so you will not need to  |
| W1 settlement in progress | Your ₹2140 is on the way. It should reach your bank in about 6 hours. I will tell you when it is credited. |
| W1 soundbox offline | Your payments are settled. Your Soundbox is offline, which is why you did not hear the alerts. I have raised a |
| W1 suspected fraud (escalates) | Your settlement needs a manual review. I have sent the details to the support team and they will call you. |
| W2 collect dues | {"case_id": 19, "reminded": 17, "reminded_paid": 7, "recovered_inr": 10314.0, "holdout": 2, "holdout_paid": 0, |
