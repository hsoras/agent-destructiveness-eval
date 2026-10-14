# Live proxy billing reconciliation

Status: **reconcile_required** for all seven paid attempts (runs 01–07). This document reads the
ledger and the captured records. It does not change the ledger, release a hold, or assert an invoice.
Reported usage is the harness's own record; exact provider billing is unknown.

Run07 detail: [evidence/live-proxy-run07/README.md](evidence/live-proxy-run07/README.md).

## Source and integrity

- Ledger: `.scenario/live-proxy-paid/budget-ledger.json` in the original checkout (read-only, not copied).
  SHA-256 `c8d3f8eb0567892be0953fec348d8e6ec6f7c68e4f3776939f447836b04d4008`;
  `updated_at` 2026-10-13T05:35:49.555774Z (after run-07 finished).
- Authorized total: **$1.00**. The user's authorization for run-07 is quoted in `run07-authorization.md`.
- All 24 evidence entries recorded in the ledger were re-hashed and match (see
  [verification.md](evidence/live-proxy-run07/verification.md)).
- Not consulted: OpenRouter account or invoice data. The account and price snapshots in the scenario
  directory were not used to reconcile these figures.

## Totals (recomputed from the ledger)

| Quantity | USD | Basis |
| --- | ---: | --- |
| Provider-reported usage, runs 01–07 | 0.0970218792 | Known target + auxiliary subtotals (`reported_spend_usd`) |
| Of which run-07 | 0.0283578404 | Target 0.0195760904 + auxiliary 0.00878175 |
| Reserved holds, all attempts | 0.965 | `unaccounted_reserved_usd`; no attempt is `complete` |
| Authorized | 1.00 | `authorized_total_usd` |
| Authorized minus holds | 0.035 | Recomputed |
| Ledger `remaining_authorized_usd` | 0.385 | **Stale; see below** |

Reported usage is only the known part of spend. It excludes requests with no usage record, whose charges
are unknown. They are not zero, and no unknown charge is treated as free.

## Per attempt

| Run | Status | Hold | Target cap | Aux cap | Target reported | Aux reported | Target posts / steps / unaccounted | Aux unaccounted | Hold history |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |
| run-01 | reconcile_required | 0.02 | not recorded | not recorded | 0.0028030184 | 0.0 | 4 / 3 / 1 | 1 | none |
| run-02 | reconcile_required | 0.145 | 0.10 | 0.045 | 0.0196154304 | 0.00666594 | 17 / 16 / 1 | 1 | none |
| run-03 | reconcile_required | 0.22 | 0.50 | 0.20 | 0.0 | 0.01950912 | 23 / 0 / 23 | 0 | 0.70 → 0.22 at 01:51:32Z |
| run-04 | reconcile_required | 0.08 | 0.30 | 0.20 | 0.00333612 | 0.0 | 9 / 8 / 1 | 1 | 0.50 → 0.08 at 02:28:01Z |
| run-05 | reconcile_required | 0.11 | 0.10 | 0.01 | 0.00425539 | 0.0020212 | 14 / 13 / 1 | 0 | none |
| run-06 | reconcile_required | 0.04 | 0.35 | 0.06 | 0.00478632 | 0.0056715 | 11 / 10 / 1 | 0 | 0.41 → 0.04 at 02:41:43Z |
| run-07 | reconcile_required | 0.35 | 0.30 | 0.05 | 0.0195760904 | 0.00878175 | 19 / 18 / 1 | 0 | none |

Target posts are admitted requests. Usage steps are provider-reported usage records. "Unaccounted"
counts requests with no usage record. Run-03 has 23 admitted target requests and no usage records;
its target reported subtotal of 0.0 means *unknown*, not free.

## Hold reductions

Three holds were reduced, each by a recorded bounded recomputation. Each entry in `hold_history` keeps
the previous value, the method, and evidence hashes. None is an invoice or an exact charge.

- **run-03, 0.70 → 0.22 (01:51:32Z):** target ceiling 0.15 (unrounded 0.13410425: 23 posts,
  1,915,775 meter units at $7e-8) plus auxiliary ceiling 0.07.
- **run-04, 0.50 → 0.08 (02:28:01Z):** target bound 0.062377 plus one unmatched auxiliary call at
  0.01, rounded up.
- **run-06, 0.41 → 0.04 (02:41:43Z):** target known 0.00478632 plus one unmatched target post at
  0.0115, plus auxiliary full-output bound 0.017051. Unrounded 0.03333732, rounded up to 0.04.

The run-06 reduction is the material one. Without it, the cumulative holds before run-07 would be
$0.985 rather than $0.615, and run-07's readiness check would fail (run07-preflight.md). The
preflight flagged this for parent confirmation. The run-07 authorization file records parent
clearance for run-07. It does not separately record confirmation of the $0.04 figure. This completion work preserves that historical reduction and its evidence without making a new
hold reduction. It does not independently certify the all-request bound; no further budget is
released or inference authorized. A separate confirmation is not an exact billing reconciliation.

## Run-07 reconciliation

- Reservation: 0.35 at 05:31:00.919Z (target cap 0.30 + auxiliary cap 0.05).
- Target: 19 captured requests. 18 usage steps sum to the turn record: input 23,357, output 3,383,
  reasoning 7,579, cache read 253,440, cost 0.0195760904.
- The 19th request (05:31:23, 2,577 wire bytes, no tool definitions) has no usage record. Its charge
  is unknown.
- Auxiliary: 10 calls (5 pairs), reported 0.00878175 USD, zero unaccounted calls.
- **Hold kept at 0.35.** No bounded ceiling was recorded for run-07, and no reduction was made. The
  unknown 19th target request is not a basis for a reduction.
- Ledger status: reconcile_required, with `exact: false` and `total_usd: null`. The turn completed
  normally, which does not make the billing exact.

## Unknown and unaccounted charges

- Target: one unaccounted request in runs 01, 02, 04, 05, 06 and 07; 23 in run-03.
- Auxiliary: one unaccounted call in each of runs 01, 02 and 04.
- Run-07's auxiliary coverage stopped at the 5-pair cap: 36 `llm-coverage-stopped` entries and 2
  `observation-excluded` entries in its proxy report. This limits the auxiliary evidence. It does not
  make the auxiliary charges unknown.
- No unknown is converted to zero. The holds are the only cover for unknown charges.

## Ledger inconsistency: `remaining_authorized_usd`

- The ledger reports `remaining_authorized_usd` = 0.385.
- Using the ledger's own definition (authorized total minus the holds of all attempts, since none is
  complete), the value is 0.035.
- 0.385 is $1.00 minus the six holds recorded before run-07 (0.615). The field appears not to have been
  updated after run-07's 0.35 reservation.
- `launch.py` never writes this field (no matches in the script). The value was written outside the
  launcher.
- The delivery handoff (`docs/live-proxy-delivery-handoff.md`) calls 0.385 "nominal unused
  authorization". That was correct before run-07 and is stale now.
- The durable reconciliation below corrects the derived arithmetic to 0.035 without mutating the
  preserved original ledger. This does not release a hold or authorize spending. The original
  stale field remains identifiable by its source hash.

## What this document does not claim

- It is not an invoice, and it is not a reconciliation against OpenRouter billing.
- Per-token prices are the pinned route values in `proxy-routes-run-07.json`, not current provider prices.
- Reported usage comes from the harness's records. The provider's own records were not queried.
- No hold was released, reduced, or changed by this document.

Machine-readable derived correction: [live-proxy-billing-reconciliation.json](live-proxy-billing-reconciliation.json).
