# Eval: reconcile — engine=pipeline, provider=none — BEFORE Phase 0 (commit fe2b550)

Run on 2026-11-27.

| Metric | Value |
|---|---|
| Correct | 3/20 (15%) |
| Unsafe (wrong data written) | 0 |
| Unnecessary asks | 0 |
| LLM calls | 17 (0.8 per scenario) |
| LLM time | 0.0s (0.00s per scenario) |

| Scenario | Expected | Got | Result |
|---|---|---|---|
| r01_remembered_merchant | auto_log Food | auto_log Food | ✅ |
| r02_matches_manual_expense | auto_link | error | ❌ |
| r03_p2p_to_a_person | ask_user | error | ❌ |
| r04_swiggy_food | auto_log Food | error | ❌ |
| r05_possible_double_charge | ask_user | error | ❌ |
| r06_salary_credit | income | income | ✅ |
| r07_otp | rejected | error | ❌ |
| r08_uber_transport | auto_log Transport | error | ❌ |
| r09_netflix | auto_log Entertainment | error | ❌ |
| r10_pharmacy_health | auto_log Health | error | ❌ |
| r11_electricity_bill | auto_log Bills | error | ❌ |
| r12_amazon_shopping | auto_log Shopping | error | ❌ |
| r13_near_amount_manual_expense | auto_link | error | ❌ |
| r14_manual_expense_too_far_apart | auto_log Food | error | ❌ |
| r15_refund | income | income | ✅ |
| r16_balance_first | auto_log Food | error | ❌ |
| r17_future_mandate | rejected | error | ❌ |
| r18_prompt_injection_in_sms | ask_user | error | ❌ |
| r19_atm_cash | ask_user | error | ❌ |
| r20_caption_free_unknown_shop | ask_user | error | ❌ |
