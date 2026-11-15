# Barge-in: specification, method and pilot results

Status: **proof of concept.** The question is whether a barge-in decision can be built and measured in the browser, and what it costs. It is not a production release. The decision logic in `src/voiceshop/speech/barge_in.py` is unchanged and still uses simulated timing. The browser measurement below is separate: it measures a real audio decision on the browser's own clock. The feature stays off by default (`VOICESHOP_BARGE_IN`).

## 1. What barge-in is

The caller speaks while the agent is still talking. The agent should stop quickly, listen, and answer what the caller said, without losing track of the conversation.

## 2. Why it matters for customers

- A caller who cannot interrupt waits through a long reply, or repeats themselves, and gets annoyed.
- A caller who interrupts and is ignored may think the line is dead.
- An agent that stops for a sound it should ignore ("mm-hmm", a cough) cuts its own reply for no reason.

## 3. What we measure

| Measure | Definition | Why |
|---|---|---|
| **Miss** | The caller really interrupts and the agent keeps talking. | The critical error. Target: none. |
| **False stop** | The caller only backchannels ("mm-hmm") and the agent stops. | Cuts replies for no reason. |
| **Decision latency** | From the caller's true speech onset to the agent's audio being cut. | Long stops mean the caller talks into the reply. Budget: 300 ms. |
| **Detection delay** | From the true onset to the first frame where the check sees speech. | Part of the latency. |

## 4. How it is measured (as built)

- **The browser does the measuring.** `tools/barge_in/run.mjs` opens Chromium, which loads `tools/barge_in/lab.html`. The page plays the agent's reply and the caller's clip as audio buffers on one audio clock.
- **The decision runs on the audio thread.** `tools/barge_in/lab-worklet.js` plays the agent's reply, reads the caller's samples, runs an energy voice-activity check in blocks of 128 frames, and cuts the agent's output when caller speech has lasted the minimum time. Timing is counted in frames, not in wall-clock milliseconds, because the wall clock in a browser is not reliable enough for this (see section 7).
- **Ground truth is exact.** The true onset is the first block of the caller's own clip above the threshold. The caller's start time on the clock is known because the page schedules it.
- **Test set.** Two caller phrases that interrupt ("Wait, I want the Ridgeline tent instead." and "Actually, can you check my order first?") and two backchannels ("Mm-hmm." and "Yeah, right."). Speech is generated locally with the Windows speech synthesiser (`tools/barge_in/make_clips.ps1`), so no keys or licensed audio are used. The backchannels are spoken faster, as people say them.
- **Valid cases only.** A case counts only if the agent had started before the caller's speech began. Otherwise it is recorded as invalid.

### What this does not measure

- **The microphone and speaker hardware, and the operating system's audio path.** A real phone call adds delay on each of these.
- **Echo.** The agent's voice coming back through a speaker into the microphone is the real risk for a laptop demo. It is not in this test.
- **Telephone networks.** Real latency will be higher than the numbers here.
- **Real speech.** The clips are synthetic. Real callers vary more in pace, volume and accent.

Each report must state these limits.

## 5. Feasibility criteria (proof of concept)

A setting "works" when all three hold on the cases run:

| Criterion | Why |
|---|---|
| Every valid real interruption stops the agent. | The core behaviour. |
| No backchannel stops the agent. | The agent must not cut itself off for a sound it should ignore. |
| 95th-percentile decision latency is at or below 300 ms. | The stop budget. |

The scorer also shows the 95% interval for each rate. With a few cases that interval is wide, and the report says so. Sample sizes for a production claim (73 or more cases) are a later decision, not a requirement here.

## 6. Pilot run

Run on this machine: 12 cases (2 interruptions and 2 backchannels, each at 3 minimum-speech settings), valid on all 12.

| Min speech | Interruptions stopped | Backchannels stopped | Decision latency p50 / p95 | Verdict |
|---|---|---|---|---|
| 150 ms | 2 of 2 | 2 of 2 | 164 / 178 ms | does not work (backchannels stopped the agent) |
| 300 ms | 2 of 2 | 1 of 2 | 674 / 975 ms | does not work (a backchannel stopped the agent; latency over budget) |
| 600 ms | 2 of 2 | 0 of 2 | 1550 / 1693 ms | does not work (latency over budget) |

**Answer to the proof-of-concept question:** yes, the mechanism can be built and measured in the browser. The decision can be made on the audio clock, and the stop works. But no setting in the pilot met all three criteria, so the trade-off is real and it is the result to report.

What the pilot shows:

- **The trade-off is real, and it is measured.** A short minimum speech time stops quickly but also stops for backchannels. A long one ignores backchannels but cuts in late. No setting in the pilot met both the 300 ms budget and avoided false stops.
- **Pauses in the caller's speech make the rule slower.** The rule needs unbroken speech for the minimum time. A caller who pauses between words resets the count, so one of the two 300 ms interruption cases took about a second (the other took 340 ms). Any real rule must allow for this, which is one reason a word-level signal (speech recognition, or a trained model) may be needed to meet the budget.
- **The pilot is too small to pass anything.** Two interruption phrases per setting cannot support a 5% bound. The gate correctly fails.

## 7. Things found while building it (kept, because they matter for the report)

- **A wall-clock measurement lied.** An early version counted time in browser callbacks. Those callbacks run late and in bursts, so measured latencies drifted by up to 1.7 seconds, and the lab could not tell caller speech from its own voice. The fix was to count time in audio frames on the audio thread.
- **A microphone file was not a reliable clock.** The first version fed the page a WAV file as the microphone (Chromium's fake device). Checking it with two known beeps showed that the audio arrived out of step with the file's timeline, so the timing of the caller's speech could not be trusted. The current version plays the caller as a buffer on the page's own clock instead, which gives exact ground truth but no microphone in the loop.
- **A backchannel clip can be too long.** The first "Mm-hmm." took 0.7 seconds of continuous speech, which is longer than a real backchannel, so the 600 ms setting stopped for it. Regenerating it at a faster speaking rate gave 0.5 seconds. The test set is only as good as its clips, so each clip's speech length is checked.

## 8. Order of work from here

1. **Widen the pilot if the story needs it.** More phrases and voices would narrow the intervals. A production claim would need far more cases; that is a separate decision.
2. **Add echo and real-audio paths** (a virtual audio cable, or a recorded phone call with consent) before any claim about a real line.
3. **Decide the rule** from the results. If no setting meets both the budget and the false-stop target, the feature stays off, and the report says why.
4. **Wire the decision into the monitor's voice playback** only after the gate passes.

## 9. What would count as success

A report that says, for each setting, how many real interruptions were stopped, how many backchannels were stopped in error, how fast the agent stopped, and which limits apply. A feature that stays off because the numbers are not good enough is a correct outcome.
