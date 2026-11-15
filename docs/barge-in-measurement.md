# Barge-in: method, results and limits

Status: **proof of concept, built as a test type.** The Barge-in test type runs caller interruptions through the real agents. It is an evaluation of the agents' handling of barge-in, not a finished feature. The product's voice playback is not connected to the decision, and the feature stays off by default (`VOICESHOP_BARGE_IN`).

## 1. What barge-in is

The caller speaks while the agent is still talking. The agent should stop quickly, listen, and answer what the caller said, without losing track of the conversation.

## 2. What the test checks

| Check | What it means | Expected |
|---|---|---|
| `barge_in_timing_valid` | The caller spoke while the reply was still being spoken. | Always, for a valid case. |
| `barge_in_decision` | The agent stops for a real interruption and does not stop for a backchannel ("mm-hmm", "yeah, right"). | Stop for an interruption; continue for a backchannel. |
| `barge_in_answered` | After a cut, the agent gives a new answer to what was said. | Always, when the agent stopped. |

The transcript keeps both what the agent said and what the caller heard before the cut, so a reader can see exactly which words were lost.

## 3. How it is measured

- **The agent produces the reply.** The Front Desk and specialist code run as they do in the other test types. The reply is not scripted.
- **The caller's clip is real audio.** It is generated with the Windows speech synthesiser (`tools/barge_in/make_clips.ps1`), so no keys or licensed audio are used. The manifest records each clip's kind, expected result and text.
- **The timing rule runs on the caller's audio.** `src/voiceshop/speech/barge_in_timeline.py` uses blocks of 128 frames, an energy threshold, a 200 ms hang time, and a minimum unbroken speech time. The same rule runs in the browser lab for the cross-check in section 6.
- **The agent's speaking rate is a model.** Each word takes 380 ms, so the moment the caller interrupts is known exactly, and the words heard before the cut are the words that finished before it. The rate is not measured from real speech.
- **The conversation continues from what was heard.** After a cut, the caller's next line is the interruption, and the agent answers it. A backchannel the rule ignores changes nothing in the conversation.
- **Scenarios.** Twelve cases: two interruption clips and two backchannel clips (one per voice), each at three minimum-speech settings (150, 300 and 600 ms). The caller interrupts the first reply after its fourth word.

## 4. Results

### Through the agents (the Barge-in test type)

One run from the monitor UI, Instant pace, 12 cases:

| Setting | Result |
|---|---|
| 150 ms | Interruptions handled; backchannels stopped the agent (2 of 2 in both voices). |
| 300 ms | Interruptions handled; backchannels stopped the agent (2 of 2 in both voices). |
| 600 ms | All 12 handled as expected. |

8 of 12 cases pass. The 4 failures are the false stops. The checks that depend on the agent's answer (`barge_in_answered`, `barge_in_timing_valid`) pass in every case.

This is a small sample: one interruption phrase and one backchannel phrase per voice. It is enough to show the test works end to end and that the trade-off appears through the real agents. It is not enough to make a claim about callers in general.

### Browser cross-check (72 cases, same clips, audio-thread decision)

The earlier browser lab ran the same kinds of clip with more phrases (six per kind, two voices). It is kept as evidence for the timing method:

| Min speech | Interruptions stopped | Backchannels wrongly stopped | Decision latency (median / 95th percentile) |
|---|---|---|---|
| 150 ms | 12 of 12 | 11 of 12 | 162 / 196 ms |
| 300 ms | 12 of 12 | 4 of 12 | 367 / 1217 ms |
| 600 ms | 10 of 12 | 0 of 12 | 1662 / 1756 ms |

No setting met all three criteria: stop every real interruption, ignore every backchannel, and keep the 95th-percentile latency at or below 300 ms. A short minimum speech time cuts quickly but stops for acknowledgements; a long one ignores acknowledgements but misses real interruptions when the caller pauses mid-phrase.

### Python timing against the browser

The Python rule and the browser rule were compared on the same clips. At 150 ms they agree to within about 18 ms on every case. At 300 and 600 ms they can differ by up to about 100 ms, because a pause near the 200 ms hang time can be read differently after the clip is resampled to the browser's rate. The Python rule is the one the monitor runs. The test that compares them records this spread rather than hiding it.

## 5. What this does not measure

- **The microphone, the speaker, the operating system's audio path, and telephone networks.** A real call adds delay on each.
- **Echo.** The agent's voice coming back through a speaker into a microphone is a real risk for a laptop demo. It is not in this test.
- **Real speech.** The clips are synthetic. Real callers vary more in pace, volume and accent, and their pauses differ.
- **The agent's real speaking rate.** It is modelled at 380 ms per word.
- **Speech recognition.** The agents receive the text of the caller's line. Recognition errors are the subject of the separate speech round-trip test, which is not built yet.

Every report must state these limits. The Barge-in report includes them.

## 6. Earlier findings (kept because they shaped the method)

- **A wall-clock measurement misled.** An early browser version counted time in callbacks. Those callbacks run late and in bursts, so measured latencies drifted by up to 1.7 seconds. The fix was to count time in audio frames.
- **A microphone file was not a reliable clock.** Chromium's fake microphone did not keep to the file's timeline when checked with two known beeps. The lab plays the caller's clip on the page's own clock instead.
- **A backchannel clip can be too long.** The first "Mm-hmm." had 0.7 seconds of continuous speech, which made the 600 ms setting stop for it. Regenerating it at a faster rate gave 0.5 seconds. Each clip's speech length should be checked.

## 7. Order of work from here

1. **Widen the case set** with more phrases and voices, and more interruption points in the reply. This narrows the intervals.
2. **Add echo and a real-audio path** (a virtual audio cable, or consented recordings of real calls) before any claim about a real line.
3. **Decide the rule from the results.** If no setting meets all the criteria, the feature stays off and the report says why.
4. **Connect the decision to the product's voice playback** only after that.

## 8. What would count as success

A report that says, for each setting, how many real interruptions were handled, how many backchannels were stopped in error, what the agent said and what was heard, and which limits apply. A result that says the trade-off is still open is a correct result.
