# Trader Brain EOD — 2026-11-25
Paper only. TRADER_BRAIN_PAPER_1000. **Uncertified; official daily return and equity unavailable.** Mac offline 478h. 07:00 missing; 09:15 research recovered, no new approvals; intraday events and close absent. No retrospective fills. Separate research and paper-aggressive directory excluded.

## Mover audit
|Ticker|Morning change %|External change %|Price|ET observation|Decision|
|---|---:|---:|---:|---|---|
|AMBA|-29.39|-19.62|35.75|16:00|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|DELL|-7.48|0.62|297.1|16:00|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|LI|6.38|11.17|11.9|16:00|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|WDAY|-5.12|1.36|181.66|15:56|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|URBN|-9.79|-10.14|76.9|15:50|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|NTNX|-14.82|-15.23|46.28|16:00|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|DE|-3.06|4.93|644.02|15:57|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|PHAR|-16.79|-23.41|10.13|16:00|REJECT_CURRENT_ENTRY + KEEP_ACTIVE_WATCH|
|GDRX|-10|-20.83|2.375|16:00|STRUCTURAL_REJECT|
|NRSN|144.9|58.18|1.74|16:00|STRUCTURAL_REJECT|
|SVRE|23.01|30.09|1.47|16:00|STRUCTURAL_REJECT|
|MEDS|17.57|11.15|1.645|16:00|STRUCTURAL_REJECT|
|APUS|14.72|18.18|2.73|16:00|STRUCTURAL_REJECT|

207/244 scanner counts not immutable. All 13 first detections, decision-time volume, executable spreads, VWAP/OR and rechecks unverified; no discovery or execution credit. DELL, WDAY and DE reversed; PHAR/GDRX continued down; LI high 11.90 below T1 12.05 and low 10.48 below stop 11.15, no chronology/trigger/fill. NRSN under-$5 research leader, structural reject: 2.6939 morning, 1.74 close; no approved trade. HPQ ranked omission +11.99% at 15:58, not full scanner miss. A083/A084 observed in research only. All RVOL samples unqualified.

## Capital and scoreboard
LI 50 x 11.60 = 580.00; 50 x (11.60-11.15) = 22.50 nominal risk, excluding execution reserve. Seed comparison 58% and 2.25%; no authorization. Options not priced. Current risk and cash unknown. All cumulative metrics null except original seed 1000.00. Historical November 5 cash not current. No recycling assumed. F auditability, scanner F auditability, analysis C+, review hindsight A; remaining grades ungradable. 

## Codex
- what_worked: ["Zero morning approvals when authority missing","A083 gap retention separated from capacity; all calibrated capacity null","A084 temporary watch queue and recheck times visible in report; no rechecks verified","Structural rejection of NRSN consistent with unresolved risk"]
- lost_money_or_opportunity: ["Actual P&L unknown","LI close above reference but neither target reached and low below stop; no sequence or fill credit","DELL and WDAY reversed bearish gaps; PHAR and GDRX declined further; no execution evidence"]
- scanner_misses_late: ["HPQ absent ranked 13, late observation +11.99%; not proven scanner miss","ADSK separately reported by another research identity, not imported; close -2.85%","207/244 scanner counts not immutable; first detections and full membership unknown"]
- scoring_defects: ["All RVOL baselines <10 sessions; no A084-qualified readings","Negative catalyst direction and market direction may diverge; late session sector rebound calls for prospective revalidation","NRSN adjusted prior close 1.10 vs reported official 1.075 discrepancy in morning archive; do not normalize silently","Capacity null; D+5 analogs small and not catalyst matched"]
- allocation_risk_defects: ["Current equity, settled cash, stress reserve, risk counters unavailable","LI illustrative notional 580.00 and nominal stop risk 22.50 exclude slippage; 58% and 2.25% of seed only","AMBA/DELL put verticals unpriced; no authorization"]
- exact_changes: {"mode":"SHADOW_ONLY","preserve_existing_risk_limits":true,"require_exchange_calendar":true,"skip_closed_sessions":true,"close_intraday_at_exchange_close":true,"require_swing_designation_before_entry":true,"watch_recheck_times_et":["09:45","10:00","10:30","11:00"],"sector_refresh_interval_minutes":5,"shadow_sector_recovery_trigger_percentage_points":2,"require_completed_5m_bar":true,"require_actual_rth_vwap_and_OR15":true,"require_fresh_atomic_quotes_and_depth":true,"require_direction_revalidation_after_sector_recovery":true,"do_not_auto_flip_direction":true,"record_recheck_or_outage":true,"retain_hard_vetoes":true,"require_canonical_authority":true,"execution_enabled":false}
- experiments: {"shadow_sessions":5,"negative_gap_cases":40,"sector_reversal_cases":20,"holiday_calendar_cases":10,"account_outage_cases":20,"accept":"100% scheduled close/recheck or explicit outage records; zero holiday trades, late liquidation certificates, hard-veto bypass or fabricated fills; >=20% reduction in false bearish chases with <=10% qualified recall loss versus frozen baseline; inconclusive until sample complete","reject":"any fabricated/late-as-early record, hard-veto bypass, retrospective fill, risk change or failed calendar test"}
- confirmation_rejection: {"shadow_sessions":5,"negative_gap_cases":40,"sector_reversal_cases":20,"holiday_calendar_cases":10,"account_outage_cases":20,"accept":"100% scheduled close/recheck or explicit outage records; zero holiday trades, late liquidation certificates, hard-veto bypass or fabricated fills; >=20% reduction in false bearish chases with <=10% qualified recall loss versus frozen baseline; inconclusive until sample complete","reject":"any fabricated/late-as-early record, hard-veto bypass, retrospective fill, risk change or failed calendar test"}
- unresolved_integrity: ["Mac offline 478h at check","No canonical 07:00 board or intraday event chain found","No 16:00 certificate; historic EOD TypeError unresolved","Separate research and paper-aggressive directory not canonical; excluded","No synchronized decision-time quotes or qualified RVOL"]

All 84 prior adjustments retained verbatim, A085 shadow only. Highest priority A072 and A069. Thanksgiving closed November 26; next session November 27 closes 13:00 ET. Full structured payload and registry adjacent in knowledge/daily-paper-handoffs. No account or execution changes.
