// Barge-in lab: one case, run in a real browser. The decision runs on the audio thread (lab-worklet.js), so the
// timing is counted in audio frames on one clock.
//
// The agent's reply and the caller's speech are both loaded here. The caller's speech is read by the audio
// processor as the caller's samples; it is not played aloud. The caller's true onset is found from the clip itself,
// so the detection delay and the decision latency are exact on that clock.
//
// What this measures:
//   - Whether the agent stops for a real interruption, and whether it stops for a backchannel.
//   - Decision latency: from the caller's true onset to the frame where the agent's audio is cut.
//   - Detection delay: from the true onset to the frame where the voice-activity check first sees speech.
//
// What this does NOT measure (stated in the report): the microphone and speaker hardware, the operating system's
// audio path, echo from the speaker into the microphone, and telephone networks. A caller on a real line arrives
// through all of those, so real latency will be higher than these numbers.

(async () => {
  const q = new URLSearchParams(location.search);
  const cfg = {
    caseId: q.get("case") || "unnamed",
    expect: q.get("expect") || "stop",
    agent: q.get("agent") || "/clips/agent_reply.wav",
    caller: q.get("caller") || "/clips/interrupt_tent.wav",
    minSpeechMs: Number(q.get("minSpeechMs") || 600),
    leadSec: Number(q.get("leadSec") || 3),
    threshold: Number(q.get("threshold") || 0.02),
    hangMs: 200,
  };
  const BLOCK = 128; // the audio processor works in blocks of 128 frames; the grids below are aligned to it

  function finish(payload) {
    window.__BARGE_RESULT__ = payload;
  }

  try {
    const ctx = new AudioContext();
    const rate = ctx.sampleRate;
    const load = async (url) => {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`could not load ${url}: ${res.status}`);
      return ctx.decodeAudioData(await res.arrayBuffer());
    };
    const [agentBuf, callerBuf] = await Promise.all([load(cfg.agent), load(cfg.caller)]);
    const agent = agentBuf.getChannelData(0).slice();
    const caller = callerBuf.getChannelData(0).slice();
    if (callerBuf.sampleRate !== rate || agentBuf.sampleRate !== rate) {
      // decodeAudioData resamples to the context rate, so this should not happen; check it rather than assume it.
      throw new Error("decoded clips do not match the context sample rate");
    }

    // Ground truth on the caller's own clip: the first block of 128 whose level passes the threshold.
    let trueOffsetBlocks = null;
    for (let b = 0; (b + 1) * BLOCK <= caller.length; b++) {
      let sum = 0;
      for (let i = b * BLOCK; i < (b + 1) * BLOCK; i++) sum += caller[i] * caller[i];
      if (Math.sqrt(sum / BLOCK) > cfg.threshold) {
        trueOffsetBlocks = b;
        break;
      }
    }

    await ctx.audioWorklet.addModule("/lab-worklet.js");
    await ctx.resume();

    // Start in the future, on block boundaries, so every grid lines up.
    const startFrame = Math.ceil(((ctx.currentTime + 0.5) * rate) / BLOCK) * BLOCK;
    const callerStartFrame = startFrame + Math.round((cfg.leadSec * rate) / BLOCK) * BLOCK;
    const maxFrame = callerStartFrame + Math.round((caller.length / rate + agent.length / rate + 5) * rate);
    const minSpeechFrames = Math.round((cfg.minSpeechMs / 1000) * rate / BLOCK) * BLOCK;
    const hangFrames = Math.round((cfg.hangMs / 1000) * rate / BLOCK) * BLOCK;

    const node = new AudioWorkletNode(ctx, "barge-in", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
    node.connect(ctx.destination);

    const trueOnsetFrame = trueOffsetBlocks === null ? null : callerStartFrame + trueOffsetBlocks * BLOCK;
    let cutFrame = null;
    let detectedFrame = null;
    let agentStarted = false;
    let agentEnded = false;
    let segments = [];

    node.port.onmessage = (e) => {
      const m = e.data;
      cutFrame = m.cutFrame;
      detectedFrame = m.detectedFrame;
      agentStarted = m.agentStarted;
      agentEnded = m.agentEnded;
      segments = m.segments;
      if (m.reason === "done" || m.reason === "timeout") {
        const ms = (f) => (f === null ? null : Math.round(((f - startFrame) / rate) * 1000));
        const rel = (f) => (f === null || trueOnsetFrame === null ? null : Math.round(((f - trueOnsetFrame) / rate) * 1000));
        const interrupted = cutFrame !== null;
        finish({
          caseId: cfg.caseId,
          expect: cfg.expect,
          minSpeechMs: cfg.minSpeechMs,
          valid: trueOnsetFrame !== null && agentStarted && trueOnsetFrame > startFrame,
          interrupted,
          trueOnsetMs: ms(trueOnsetFrame),
          detectedOnsetMs: ms(detectedFrame),
          stopMs: ms(cutFrame),
          decisionLatencyMs: interrupted ? rel(cutFrame) : null,
          detectionDelayMs: rel(detectedFrame),
          heardSec: interrupted ? Number(((cutFrame - startFrame) / rate).toFixed(3)) : null,
          agentEnded,
          segments: segments.map((s) => ({
            startMs: ms(s.startFrame),
            endMs: ms(s.endFrame),
          })),
          error: trueOnsetFrame === null ? "the caller clip has no speech above the threshold" : null,
        });
      }
    };

    node.port.postMessage({
      type: "config",
      agent,
      caller,
      startFrame,
      callerStartFrame,
      minSpeechFrames,
      hangFrames,
      threshold: cfg.threshold,
      maxFrame,
      doneFrame: callerStartFrame + Math.round((caller.length / rate + 1.5) * rate),
    });
  } catch (err) {
    finish({ caseId: cfg.caseId, expect: cfg.expect, valid: false, interrupted: false, error: err.message });
  }
})();
