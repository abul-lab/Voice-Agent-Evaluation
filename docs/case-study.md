# Case study: a release gate for a voice shopping agent

## The problem

A voice assistant that routes callers to specialist agents can fail in ways a normal demo never shows: it routes to the wrong specialist, it gives an order status before checking who is calling, it approves a refund without a clear yes, or it reads a card number aloud. Those failures are rare in a happy-path demo and costly in production. The question this project answers is: **how do we decide, before each release, whether the assistant is safe and good enough to ship?**

## The approach

1. **Scripted callers, fixed seeds.** Each of the 74 test cases is a caller with a goal and a seed, so the same case always produces the same conversation. A failure can be replayed exactly.
2. **Evidence, not claims.** Every check reads something independent of the agent: the transcript, the audit log of every tool call, or the store data. If an agent says "your refund is done" but the ledger shows nothing, the check fails. There are 26 named checks, grouped into routing, task completion, policy, identity, integrity, guardrails, resilience, conduct and barge-in.
3. **Red team as part of the suite.** 31 of the 74 cases are attacks: prompt injection, data extraction, impersonation, social engineering, policy bypass and off-topic abuse. The gate allows zero successful attacks.
4. **A gate that can say no.** A version passes only if at least 90% of cases pass, no case errors, no attack succeeds, the 95th-percentile reply time is within 250 ms, and no case that passed in the saved baseline now fails.
5. **Recorded baseline.** The baseline is a file in the repository, written and reviewed in the same change as the behaviour it describes. Changing it is a visible decision.

## Design decisions

- **Rules-based routing, not a model.** The front desk uses explicit cues and a rule order. It is predictable and every route can be tested. A model is used only where it adds something the rules cannot, and that path is optional and bounded (a Gemini routing option waits at most 15 seconds, with two attempts).
- **Least-privilege tools.** Each agent has named scopes (for example, Shopping can read the catalogue and write to the checkout; Customer Care has no tools at all). Every call goes through one gateway that checks scopes and writes the audit trail.
- **Identity before details.** Order status and refunds need an order number and a matching email. The check `verification_gate` tests that no order detail appears before this.
- **Read-back before money moves.** A refund is read back to the caller (amount, order). Only an explicit yes approves it. A no, or silence, leaves the ledger unchanged. Two checks test this: `refund_confirmed_first` and `refund_declined_leaves_ledger_unchanged`.
- **Repair before handover.** A misunderstood caller gets a retry, then a list of what the agent can do, then a handover with a reason code after three misses. Any understood turn resets the count.
- **Card numbers are never read back.** Stored transcripts mask Luhn-valid card numbers, and agents give only the last four digits. The test checks this across all scenarios.

## What went wrong along the way

These are real commits from the history, not hypotheticals.

- **Gemini routing could wait forever.** An unbounded wait on the model was replaced with a 15-second limit and two attempts, and the retries are logged (`3526515`).
- **An unavailable router passed the gate.** A test was added so that an unavailable router is an error for the case and fails the release gate (`5295389`).
- **Judge retries hid rate-limit failures.** The judge now retries rate limits and reports failures correctly (`646cca6`).
- **Transcripts were readable without a key.** Reads of transcripts and the audit trail now need a key, and live streaming uses a single-use ticket because a browser's event stream cannot send headers (`7a40fc8`).
- **Card digits were spoken and stored in full.** Masking was added to stored transcripts and to agent replies (`328e209`).
- **The container's data paths were wrong.** Compose now sets them, and the TLS proxy example was tested through the proxy (`7e1dd49`).

## What is not solved

- **The judge is not trusted yet.** Its calibration labels are one author's interim labels. Judge scores are shown as information, not used as a gate, until a second rater labels the set and agreement is accepted.
- **Speech is tested in one case only.** Real audio runs through one front-desk case with Gemini. It is not in CI because it needs a key and an external service.
- **Barge-in is an evaluation test type, not live audio playback.** The 12-case barge-in test type runs recorded caller clips through the real agents and scores stop decisions on the audio clock (see [barge-in-measurement.md](barge-in-measurement.md)). Live microphone capture, echo cancellation, and browser playback cut are not built yet.
- **The storefront is open by design.** Cart and order actions in the demonstration shop do not need a key. This must change before any public deployment.

## Results

The full suite on a clean run: 74 of 74 cases pass, 0 of 31 attacks succeed, the 95th-percentile reply time is about 50 to 65 ms across recent runs (budget 250 ms), and the regression gate passes against the baseline. A sample report is in [docs/examples/sample-report.md](examples/sample-report.md).

## What I would do next

1. Get a second rater to label the judge calibration set, then decide whether the judge can gate releases.
2. Collect real conversations under consent, redacted and imported as transcripts, so the suite reflects real callers.
3. Classify each failed conversation by its first broken stage (speech, routing, tool, reasoning, or conversation design), so fixes go to the right place.
4. Add authentication to the storefront before any public exposure.
