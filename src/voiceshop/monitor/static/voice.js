/* Laptop speaker for the shopping conversation.
   Speaks each turn with the browser's built-in speech synthesis: the customer and the voice agent get
   different voices, and turns are spoken one after another in order. Browsers only allow speech after a
   user click, so the toggle must be pressed once (inside the page that is speaking).

   speakSegments() reports when each piece of a turn really starts being spoken (onStart), so the agent
   network blinks for the speaker that is actually talking. Its promise resolves when the turn has been
   spoken, which lets the transcript wait for the voice before showing the next turn. */
(() => {
  "use strict";

  const KEY = "voiceshop.speaker";
  const synth = window.speechSynthesis;
  const queue = [];
  let speaking = false;
  let generation = 0; // bumped by stop(); late callbacks from an old utterance are ignored
  let current = null; // the item being spoken right now
  let starts = 0;     // how many utterances have been started; a change means new speech was asked for
  let enabled = false;
  let voices = [];
  const buttons = new Set();

  function readStored() {
    try {
      return window.localStorage.getItem(KEY) === "on";
    } catch (err) {
      return false; // storage can be blocked; the toggle still works for this page
    }
  }

  function storeChoice(on) {
    try {
      window.localStorage.setItem(KEY, on ? "on" : "off");
    } catch (err) {
      /* ignore: per-browser convenience only */
    }
  }

  function loadVoices() {
    if (!synth) return;
    voices = synth.getVoices().filter((v) => /^en/i.test(v.lang));
  }

  // Pick two distinct English voices so the two speakers are easy to tell apart.
  function pickVoices() {
    if (!voices.length) return { agent: null, customer: null };
    const agent = voices.find((v) => /female|samantha|zira|aria|jenny|google us english/i.test(v.name)) || voices[0];
    const customer = voices.find((v) => v !== agent && /male|david|mark|guy|uk english/i.test(v.name))
      || voices.find((v) => v !== agent) || agent;
    return { agent, customer };
  }

  function paintButtons() {
    for (const b of buttons) {
      b.setAttribute("aria-pressed", String(enabled));
      b.textContent = enabled ? "Laptop speaker: On" : "Laptop speaker: Off";
      b.classList.toggle("on", enabled);
      b.title = enabled
        ? "Turn the laptop speaker off"
        : "Hear the conversation through this laptop's speakers";
    }
  }

  // Resolves the promise of the segment that was being spoken (completed = it really finished).
  function settle(item, completed) {
    if (item && item.settle) {
      const resolve = item.settle;
      item.settle = null;
      resolve(completed);
    }
  }

  // Cancel whatever the engine is still saying. Some browsers ignore a cancel that lands while an utterance
  // is starting, so cancel again for a short while, as long as the engine still reports speech. It stops
  // early if new speech is started on purpose, or if the page moves to another stop.
  function silenceEngine() {
    if (!synth) return;
    const g = generation;
    const startsSeen = starts;
    synth.cancel();
    let ticks = 0;
    const timer = setInterval(() => {
      ticks += 1;
      if (generation !== g || starts !== startsSeen || ticks > 20) {
        clearInterval(timer);
        return;
      }
      if (synth.speaking || synth.pending) synth.cancel();
      else if (ticks >= 3) clearInterval(timer); // quiet for a moment: done
    }, 100);
  }

  function next() {
    if (speaking || !queue.length || !enabled || !synth) return;
    const item = queue.shift();
    current = item;
    starts += 1;
    if (item.onStart) {
      try {
        item.onStart(item.who);
      } catch (err) {
        /* a listener must never stop the speech */
      }
    }
    const u = new SpeechSynthesisUtterance(item.text);
    u.lang = "en-US";
    u.rate = item.speaker === "agent" ? 1.0 : 1.06;
    u.pitch = item.speaker === "agent" ? 1.0 : 0.85;
    const v = pickVoices()[item.speaker === "agent" ? "agent" : "customer"];
    if (v) u.voice = v;
    speaking = true;
    const myGen = generation;
    let finished = false;
    let watchdog = null;
    const done = () => {
      if (finished || myGen !== generation) return;
      finished = true;
      clearTimeout(watchdog);
      speaking = false;
      current = null;
      settle(item, true);
      next();
    };
    u.onend = done;
    u.onerror = done;
    // Some browsers never fire 'end' (no audio device, muted tab). Estimate the length and move on,
    // so one silent utterance cannot stall the whole conversation.
    const estimateMs = Math.max(2500, (item.text.length / (13 * u.rate)) * 1000 + 800);
    watchdog = setTimeout(done, estimateMs);
    synth.speak(u);
  }

  const SpeakerVoice = {
    /** Attach a toggle button. It shows the current state and flips it on click. */
    attach(button) {
      if (!button) return;
      buttons.add(button);
      button.addEventListener("click", () => this.set(!enabled));
      paintButtons();
    },
    set(on) {
      enabled = Boolean(on) && Boolean(synth);
      storeChoice(enabled);
      if (!enabled) this.stop();
      paintButtons();
      if (enabled) next();
    },
    isOn() {
      return enabled;
    },
    /**
     * Speak the pieces of one turn in order. segments: [{ who, text }], speaker: "agent" | "customer".
     * onStart(who) is called the moment each piece starts. Resolves true when the last piece has been
     * spoken, or false when speech was stopped first (Stop, Reset, page refresh, speaker off).
     */
    speakSegments(segments, speaker, onStart) {
      if (!enabled || !synth || !segments.length) return Promise.resolve(false);
      return new Promise((resolve) => {
        segments.forEach((s, i) => {
          queue.push({
            speaker: speaker === "agent" ? "agent" : "customer",
            text: s.text,
            who: s.who,
            onStart,
            settle: i === segments.length - 1 ? resolve : null,
          });
        });
        next();
      });
    },
    stop() {
      generation += 1;
      const pending = queue.splice(0);
      const was = current;
      current = null;
      speaking = false;
      settle(was, false);
      for (const item of pending) settle(item, false);
      silenceEngine();
    },
    supported: Boolean(synth),
  };

  enabled = readStored() && Boolean(synth);
  if (synth) {
    loadVoices();
    synth.addEventListener?.("voiceschanged", loadVoices);
  }
  window.SpeakerVoice = SpeakerVoice;
  document.addEventListener("DOMContentLoaded", () => {
    for (const b of document.querySelectorAll("[data-speaker-toggle]")) SpeakerVoice.attach(b);
    if (!synth) {
      for (const b of document.querySelectorAll("[data-speaker-toggle]")) {
        b.disabled = true;
        b.textContent = "Laptop speaker: not supported";
      }
    }
  });
})();
