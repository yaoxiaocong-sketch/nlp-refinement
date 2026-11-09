# M7 Finding Severity and Proposal Score

V4 preserves `origin_severity` and `effective_gate`, and adds
`evidence_confidence`, `impact_scope`, and `claim_status`. Severity describes
consequence if true; it is not evidence strength. P0 is blocking with E2/E3
evidence, P1 is never recordable only, P2 is never blocking, and P3 is always
recordable. E1 requires an inferred claim, E2 a direct static or observed
runtime claim matching its evidence plane, and E3 a corroborated hybrid claim.

A proposal must bind to an open finding, its invariant, and existing evidence;
it must state its review-only authority, verification and regression plans,
risks, alternatives, and unverified claims. Its named assessor must differ
from the proposer, cite linked evidence, and retain `unverified` runtime
effectiveness. `assessor_id` is an auditable declaration, not proof that the
assessor was an independent person or Agent.

The scorecard totals 100: coverage 20, causal 20, scope 15, verification 15,
regression 10, evidence 10, alternatives 5, clarity 5. `recommend` requires
70 overall plus causal 12, verification 10, and evidence 7. A recommendation
is for human inspection only; it cannot close or lower the finding.
