# Roadmap: voice experience and evaluation

Built one item at a time. Each item lists what it delivers and its acceptance test. An item is done when its
acceptance test passes in CI (`ruff`, `pytest`, the tier evals and the regression gate all green).

Status key: **done**, **in progress**, **planned**, **blocked** (with the blocker).

## Phase 1: measure conversations

### 1.1 Conversation metrics (done, uncommitted)
Code: `src/voiceshop/evaluation/conversation_metrics.py`, tests in `tests/test_conversation_metrics.py`.
Metrics computed from the run records: task completion, corrections, interruptions, latency, escalations.
Definitions (operational, from transcript signals):
- **Task completion**: a case passes all its checks (existing `completion_by_goal`, reported with the new metrics).
- **Corrections**: a caller turn that starts with a correction cue ("no,", "not", "wrong", "actually", "i meant",
  "i said", "sorry") and does not answer a yes/no confirmation. Rate = corrections per caller turn.
- **Interruptions**: not measurable on the text channel (turns never overlap). Reported as `measurable: false`
  until the audio channel exists (item 3.3).
- **Latency**: p50, p95 and max of agent-turn latency, plus the existing p95 budget gate.
- **Escalations**: cases that escalate (rate), and unexpected escalations (escalated, with no escalation check
  in the scenario).

Acceptance: unit tests on hand-built records for each metric; `summary.json` and `report.json` carry a
`conversation_metrics` block; no gate change in this item (report only).

## Phase 2: real conversations

### 2.1 Real conversation intake (blocked: needs a data source)
Source must be agreed first: QE recordings, a consented pilot, or anonymised support logs.
Acceptance: a JSON import of up to 50 conversations into the existing transcript shape, with personal
data redacted before storage; import refuses records without a consent or source field.

### 2.2 Failure classification (planned, after 2.1)
Each failed conversation gets one primary cause: `asr`, `routing`, `tool`, `reasoning` or
`conversation_design`, assigned to the first stage that went wrong. An `unclear` label is allowed.
Acceptance: a labelling file format; two-labeller agreement reported (Cohen's kappa); the breakdown appears in
the report, mapped onto the existing failure categories.

## Phase 3: the three gaps

### 3.1 Action confirmation policy (done, uncommitted)
Built: the refund agent reads back the amount and order, then waits. Only a yes records the approval; a no records
`refund.declined` and approves nothing; anything else asks again. Other routing is held while the read-back is
open. A repeat refund for an order already approved is answered as a duplicate without a new question. New
scenario `store_refund_declined_at_read_back` (in the full tier only, so the targeted tier stays at three) and checks
`refund_confirmed_first` and `refund_declined_leaves_ledger_unchanged`. Tests: `tests/test_refund_confirmation.py`.
Result: 72 of 72 cases pass in rules mode, regression gate PASS.

Original definition:
Refunds, and any money movement, are confirmed back to the caller before approval. Policy is data: which
actions need confirmation, and what must be read back (order number, amount, refund destination).
Acceptance: a refund is never approved without a yes after the read-back; a test shows a "no" or silence
leaves the ledger unchanged; the existing refund checks still pass.

### 3.2 Recovery flows (done, uncommitted)
Built: a repair ladder in the router. The first unclear turn gets the specialist's own answer (or "Sorry about that.
Could you tell me again?" when the caller says the agent got it wrong). The second lists what the agent can do.
The third hands over with reason code REPAIR_LIMIT. Any understood turn resets the count. Unclear means no team
matched, a correction cue ("that's not what I said"), or a specialist answering out_of_scope or no_match.
Scenarios: `store_recovery_three_misses_hands_over`, `store_recovery_resets_when_understood` (tag `recovery`).
Metrics: a `repairs` block in conversation metrics. Tests: `tests/test_repair_ladder.py`.
Result: 74 of 74 cases pass in rules mode, regression gate PASS.

Original definition:
A repair ladder: ask again, then rephrase with options, then offer a human. Each step has a limit.
Acceptance: a caller who is misunderstood three times is offered a handoff; the ladder is covered by
scenarios in the suite; recovery turns are counted in the conversation metrics.

### 3.3 Barge-in (planned, needs an audio channel)
Requires a channel interface with streaming recognition and endpointing. Caller speech stops agent playback
and is handled as the next turn. Built behind a flag.
Acceptance: an audio test case where the caller speaks over the agent; playback stops within a set time; the
interruption metric becomes measurable.

## Security and privacy (small items, do early)

### S.1 Stop reading card digits aloud (done, uncommitted)
Code: `src/voiceshop/privacy.py` (Luhn-checked detection and masking), applied to stored transcripts in
`RunRepository.append_turn`. Tests: `tests/test_card_privacy.py`. Agent replies only ever give the last four digits.
Payment references are not spoken back in full; use the last four digits only, or a token.
Acceptance: no spoken text contains a full card-like number (test over all scenarios).

### S.2 Sign-in for reads (done, uncommitted)
Built: `GET /api/runs*` (runs, turns, stream, audit) need a valid key (any role). The live stream uses a single-use
ticket from `POST /api/stream-tickets`, valid 60 seconds. The storefront stays open. The monitor and history pages
send the key from the browser tab's session storage. Local loopback exception unchanged. Tests:
`tests/test_read_access.py`.

Original definition:
Transcripts and the audit trail require a key. The storefront stays open.
Acceptance: unauthenticated requests to transcript and audit reads return 401; existing e2e checks updated.
