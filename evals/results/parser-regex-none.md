# Eval: parser — engine=regex, provider=none

Run on 2026-11-27.

| Metric | Value |
|---|---|
| Correct | 25/27 (93%) |
| Unsafe (wrong data written) | 0 |
| Unnecessary asks | 0 |
| LLM calls | 5 (0.2 per scenario) |
| LLM time | 0.0s (0.00s per scenario) |

| Scenario | Expected | Got | Result |
|---|---|---|---|
| p01_hdfc_upi_debit | debit 450.00 blinkit | debit 450.00 blinkit@ybl BLINKIT | ✅ |
| p02_icici_debit_then_credited | debit 1250.00 | debit 1250.00 - | ✅ |
| p03_sbi_amount_without_currency | debit 799.00 swiggy | not a transaction | ❌ |
| p04_credit_card_spend_with_limit | debit 2499.00 amazon | debit 2499.00 AMAZON | ✅ |
| p05_axis_multiline_upi | debit 350.00 | debit 350.00 - | ✅ |
| p06_salary_neft_credit | credit 50000.00 | credit 50000.00 TARKA LABS PVT LTD | ✅ |
| p07_credit_mentioning_transferred | credit 5000.00 rahul | credit 5000.00 RAHUL KUMAR | ✅ |
| p08_refund_credit | credit 299.00 | credit 299.00 AMAZON | ✅ |
| p09_atm_withdrawal | debit 2000.00 | debit 2000.00 ATM HDFC0001 from | ✅ |
| p10_balance_written_first | debit 500.00 zomato | debit 500.00 ZOMATO | ✅ |
| p11_otp | not a transaction | not a transaction | ✅ |
| p12_promo_cashback | not a transaction | not a transaction | ✅ |
| p13_kotak_p2p_sent | debit 120.00 rahul@okaxis | debit 120.00 rahul@okaxis | ✅ |
| p14_paid_no_date | debit 85.00 chai point | debit 85.00 Chai Point | ✅ |
| p15_account_to_account | debit 1500.00 | debit 1500.00 - | ✅ |
| p16_mandate_future_debit | not a transaction | not a transaction | ✅ |
| p17_bill_due_reminder | not a transaction | not a transaction | ✅ |
| p18_hdfc_p2p_multiline | debit 90.00 mohammed | debit 90.00 MOHAMMED MUSABBAB AL EIL | ✅ |
| p19_received_from_friend | credit 700.00 | credit 700.00 priya@okhdfc | ✅ |
| p20_greeting_not_sms | not a transaction | not a transaction | ✅ |
| p21_inr_suffix_amount | debit 1999.00 uber | debit 1999.00 UBER INDIA | ✅ |
| p22_rupee_symbol | debit 240.00 starbucks | debit 240.00 Starbucks | ✅ |
| p23_credit_card_name_before_debited | debit 500.00 | debit 500.00 AMAZON | ✅ |
| p24_otp_safety_footer | debit 500.00 swiggy | debit 500.00 SWIGGY | ✅ |
| p25_dear_customer_comma | credit 50000.00 | credit 50000.00 EMPLOYER LTD | ✅ |
| p26_available_limit_first | debit 999.00 flipkart | debit 999.00 FLIPKART | ✅ |
| p27_card_spend_no_verb | debit 1500.00 | not a transaction | ❌ |
