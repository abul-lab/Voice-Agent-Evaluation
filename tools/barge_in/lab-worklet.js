// Barge-in audio processor. Runs on the browser's audio thread, so timing is counted in audio frames, not
// in main-thread callbacks.
//
// The agent's reply is played from a buffer in this processor. The caller's speech is read from a second buffer
// (it is not played aloud; it stands in for the microphone). A voice-activity check runs on the caller's samples in
// blocks of 128 frames. If the caller's speech lasts at least minSpeechFrames while the agent is talking, the
// agent's output is cut to silence at that block.
//
// All results are frame numbers on the audio clock. The main thread turns them into milliseconds.

class BargeInProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.cfg = null;
    this.agent = null;
    this.caller = null;
    this.finished = false;
    this.state = {
      agentStarted: false,
      agentEnded: false,
      cutFrame: null,
      inSpeech: false,
      speechStart: null,
      speechFrames: 0,
      silentFrames: 0,
      detectedFrame: null,
      segments: [],
      lastSpeechEnd: null,
    };
    this.port.onmessage = (e) => {
      if (e.data.type === "config") {
        this.cfg = e.data;
        this.agent = e.data.agent;
        this.caller = e.data.caller;
      }
    };
  }

  // Energy of one block of the caller's samples, starting at absolute frame `frame`.
  callerLevel(frame) {
    const from = frame - this.cfg.callerStartFrame;
    if (from < 0 || from >= this.caller.length) return 0;
    let sum = 0;
    const end = Math.min(this.caller.length, from + 128);
    for (let i = from; i < end; i++) sum += this.caller[i] * this.caller[i];
    return Math.sqrt(sum / (end - from));
  }

  process(inputs, outputs) {
    if (this.finished || !this.cfg) return !this.finished;
    const out = outputs[0][0];
    const blockStart = currentFrame;
    const blockEnd = blockStart + out.length;
    const s = this.state;

    // Play the agent's reply until it is cut or ends.
    const agentFrom = blockStart - this.cfg.startFrame;
    for (let i = 0; i < out.length; i++) {
      const idx = agentFrom + i;
      const playing = idx >= 0 && idx < this.agent.length && (s.cutFrame === null || blockStart + i < s.cutFrame);
      out[i] = playing ? this.agent[idx] : 0;
    }
    if (blockStart >= this.cfg.startFrame) s.agentStarted = true;
    if (s.agentStarted && !s.agentEnded && agentFrom >= this.agent.length) {
      s.agentEnded = true;
    }

    // Voice-activity check on the caller's samples, 128 frames at a time.
    const level = this.callerLevel(blockStart);
    const above = level > this.cfg.threshold;
    if (above) {
      s.silentFrames = 0;
      s.lastSpeechEnd = blockEnd;
      if (!s.inSpeech) {
        s.inSpeech = true;
        s.speechStart = blockStart;
        s.speechFrames = 0;
        if (s.detectedFrame === null && blockStart >= this.cfg.callerStartFrame) s.detectedFrame = blockStart;
      }
      s.speechFrames += out.length;
    } else {
      s.silentFrames += out.length;
      if (s.inSpeech && s.silentFrames >= this.cfg.hangFrames) {
        s.inSpeech = false;
        s.segments.push({ startFrame: s.speechStart, endFrame: s.lastSpeechEnd });
      }
    }

    // Decision: only caller speech that starts after the agent has begun, while the agent is still talking.
    const callerIsInterrupting = s.detectedFrame !== null && s.inSpeech && s.agentStarted && !s.agentEnded;
    if (s.cutFrame === null && callerIsInterrupting && s.speechFrames >= this.cfg.minSpeechFrames) {
      s.cutFrame = blockStart;
      this.post("stop");
    }

    // Finish half a second after a cut, or when the reply ends with no caller speech in progress.
    if (s.cutFrame !== null && blockStart >= s.cutFrame + 0.5 * sampleRate) {
      this.post("done");
      return true;
    }
    if (s.cutFrame === null && s.agentEnded && !s.inSpeech) {
      this.post("done");
      return true;
    }
    // No cut: finish 1.5 s after the caller's clip ends. Later agent speech cannot change the verdict.
    if (s.cutFrame === null && blockStart >= this.cfg.doneFrame) {
      this.post("done");
      return true;
    }
    if (blockStart > this.cfg.maxFrame) {
      this.post("timeout");
      return true;
    }
    return true;
  }

  post(reason) {
    if (this.finished) return;
    const s = this.state;
    this.port.postMessage({
      reason,
      agentStarted: s.agentStarted,
      agentEnded: s.agentEnded,
      cutFrame: s.cutFrame,
      detectedFrame: s.detectedFrame,
      segments: s.segments,
    });
    if (reason !== "stop") this.finished = true;
  }
}

registerProcessor("barge-in", BargeInProcessor);
