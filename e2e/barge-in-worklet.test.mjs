// Tests for the barge-in decision on the audio thread (tools/barge_in/lab-worklet.js).
//
// The worklet file is loaded unchanged into a sandbox that stands in for the audio APIs. Synthetic signals stand in
// for the caller and the agent, and the test checks what the decision reports. No browser is needed.
// Run: node --test barge-in-worklet.test.mjs   (from the e2e folder)
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, "..", "tools", "barge_in", "lab-worklet.js"), "utf8");

const RATE = 48000;
const BLOCK = 128;

// Loads the worklet into a sandbox and returns its processor, plus the sandbox (for currentFrame).
function loadProcessor() {
  let Processor = null;
  class FakeBase {
    constructor() {
      this.sent = [];
      this.port = { postMessage: (m) => this.sent.push(m), onmessage: null };
    }
  }
  const sandbox = {
    AudioWorkletProcessor: FakeBase,
    registerProcessor: (_name, cls) => {
      Processor = cls;
    },
    currentFrame: 0,
    sampleRate: RATE,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { processor: new Processor(), sandbox };
}

// A tone of `ms` milliseconds at the given amplitude, or silence when amplitude is 0.
function tone(ms, amplitude = 0.2) {
  const n = Math.round((ms / 1000) * RATE);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = amplitude * Math.sin((2 * Math.PI * 440 * i) / RATE);
  return out;
}

// Runs the processor block by block for `totalMs`, with the agent playing from frame 0.
function run({ caller, minSpeechMs, totalMs = 4000, callerStartFrame = RATE }) {
  const { processor, sandbox } = loadProcessor();
  const agent = new Float32Array(Math.round((totalMs / 1000) * RATE)).fill(0.1);
  const hangFrames = Math.round((0.2 * RATE) / BLOCK) * BLOCK;
  processor.port.onmessage({
    data: {
      type: "config",
      agent,
      caller,
      startFrame: 0,
      callerStartFrame,
      minSpeechFrames: Math.round(((minSpeechMs / 1000) * RATE) / BLOCK) * BLOCK,
      hangFrames,
      threshold: 0.02,
      maxFrame: Math.round((totalMs / 1000) * RATE) + RATE,
      doneFrame: callerStartFrame + caller.length + Math.round(1.5 * RATE),
    },
  });
  const endFrame = Math.round((totalMs / 1000) * RATE);
  for (let frame = 0; frame < endFrame + RATE; frame += BLOCK) {
    sandbox.currentFrame = frame;
    const out = new Float32Array(BLOCK);
    const keepGoing = processor.process([[]], [[out]]);
    if (!keepGoing) break;
  }
  return processor.sent;
}

test("a real interruption cuts the agent once, after the minimum unbroken speech", () => {
  const sent = run({ caller: tone(1000), minSpeechMs: 300 });
  const stops = sent.filter((m) => m.reason === "stop");
  assert.equal(stops.length, 1, "exactly one cut");
  const cut = stops[0].cutFrame;
  const callerStart = RATE;
  assert.ok(cut >= callerStart + (0.3 * RATE) - BLOCK, "cut not before the minimum speech time");
  assert.ok(cut <= callerStart + (0.3 * RATE) + 2 * BLOCK, "cut close to the minimum speech time");
  assert.equal(stops[0].detectedFrame, callerStart, "speech is detected on the block where it starts");
});

test("a short blip shorter than the minimum does not cut the agent", () => {
  const sent = run({ caller: tone(100), minSpeechMs: 300 });
  assert.equal(sent.filter((m) => m.reason === "stop").length, 0);
});

test("silence never cuts the agent", () => {
  const sent = run({ caller: new Float32Array(RATE), minSpeechMs: 150 });
  assert.equal(sent.filter((m) => m.reason === "stop").length, 0);
});

test("with no cut, the run reports done after the caller's clip plus a margin", () => {
  const sent = run({ caller: new Float32Array(RATE), minSpeechMs: 150 });
  const done = sent.find((m) => m.reason === "done");
  assert.ok(done, "a done message was posted");
  assert.equal(done.cutFrame, null);
});

test("the minimum speech setting changes when the cut happens", () => {
  const fast = run({ caller: tone(1000), minSpeechMs: 150 }).find((m) => m.reason === "stop").cutFrame;
  const slow = run({ caller: tone(1000), minSpeechMs: 600 }).find((m) => m.reason === "stop").cutFrame;
  assert.ok(slow - fast >= 0.4 * RATE - BLOCK, "a longer minimum cuts later by about the difference");
});
