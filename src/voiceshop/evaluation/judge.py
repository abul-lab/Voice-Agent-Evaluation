"""LLM-as-judge: a rubric-based grader for the agent's replies, behind a backend interface.

The judge sees one customer line, one agent reply and the facts the shop publishes. It returns scores for
accuracy, helpfulness and tone, and says whether the reply states anything that is not in those facts.

Backends:
  ollama  local model (default for plumbing and integration checks; no key, no cost)
  gemini  Gemini API (needs a key; per-call cost; use only when the director approves a judged run)

A judge is only trusted after it has been calibrated: ``calibrate`` measures how often its scores agree with
human labels in ``tests/judge/calibration.json``.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from voiceshop.net import ServiceError, post_json

RUBRIC = """You are grading one reply from a shopping voice assistant.

Customer said: {heard}
Assistant replied: {reply}

Facts the shop publishes (the only facts the assistant may state):
{facts}

Score each from 1 (poor) to 5 (excellent):
- accuracy: every fact in the reply is true according to the facts above.
- helpfulness: the reply moves the customer's request forward.
- tone: polite, clear and brief, suitable for a phone call.
Also say whether the reply states any fact that is not in the list above (invented_facts).

Answer only with JSON in this form:
{{"accuracy": <1-5>, "helpfulness": <1-5>, "tone": <1-5>, "invented_facts": <true or false>, "reason": "<one short sentence>"}}"""

SCORE_KEYS = ("accuracy", "helpfulness", "tone")


class JudgeError(RuntimeError):
    """The judge backend failed, or returned something that is not a valid score."""


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: float


@dataclass(frozen=True)
class JudgeScore:
    accuracy: int
    helpfulness: int
    tone: int
    invented_facts: bool
    reason: str


class JudgeBackend(Protocol):
    name: str
    model: str

    def complete(self, prompt: str) -> Completion: ...


def parse_score(text: str) -> JudgeScore:
    """Read the judge's JSON answer. Anything outside the rubric is an error, never a guess."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge did not return JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise JudgeError("judge answer is not an object")
    values: dict[str, int] = {}
    for key in SCORE_KEYS:
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
            raise JudgeError(f"{key} must be an integer from 1 to 5")
        values[key] = value
    invented = raw.get("invented_facts")
    if not isinstance(invented, bool):
        raise JudgeError("invented_facts must be true or false")
    reason = str(raw.get("reason", ""))[:300]
    return JudgeScore(values["accuracy"], values["helpfulness"], values["tone"], invented, reason)


def judge_one(backend: JudgeBackend, heard: str, reply: str, facts: str) -> tuple[JudgeScore, Completion]:
    done = backend.complete(RUBRIC.format(heard=heard, reply=reply, facts=facts))
    return parse_score(done.text), done


@dataclass
class OllamaBackend:
    model: str
    url: str = "http://localhost:11434"
    timeout_s: float = 120.0
    name: str = field(default="ollama", init=False)

    def complete(self, prompt: str) -> Completion:
        body = {"model": self.model, "prompt": prompt, "stream": False, "format": "json",
                "options": {"temperature": 0}}
        started = time.perf_counter()
        data = _post_json(f"{self.url.rstrip('/')}/api/generate", body, {}, self.timeout_s)
        return Completion(
            text=str(data.get("response", "")),
            prompt_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


@dataclass
class GeminiBackend:
    api_key: str
    model: str = "gemini-3.5-flash"
    timeout_s: float = 120.0
    name: str = field(default="gemini", init=False)

    def complete(self, prompt: str) -> Completion:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        started = time.perf_counter()
        data = _post_json(url, body, {"x-goog-api-key": self.api_key}, self.timeout_s)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise JudgeError("gemini returned no text") from None
        usage = data.get("usageMetadata", {})
        return Completion(
            text=text,
            prompt_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    try:
        return post_json(url, body, headers, timeout_s=timeout_s)
    except ServiceError as exc:
        raise JudgeError(f"judge backend: {exc}") from None


def make_backend(name: str, model: str | None) -> JudgeBackend:
    if name == "ollama":
        return OllamaBackend(model=model or "qwen2.5:7b")
    if name == "gemini":
        key = os.environ.get("VOICESHOP_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise JudgeError("the gemini judge needs GEMINI_API_KEY in the environment")
        return GeminiBackend(api_key=key, model=model or "gemini-3.5-flash")
    raise JudgeError(f"unknown judge backend {name!r}")


def judge_report(report: dict[str, Any], backend: JudgeBackend, facts: str) -> dict[str, Any]:
    """Judge every agent reply in a finished suite report."""
    rows: list[dict[str, Any]] = []
    for run in report["runs"]:
        conv = run.get("conversation") or []
        heard = ""
        for t in conv:
            if t["speaker"] == "customer":
                heard = t["text_heard"]
                continue
            try:
                score, done = judge_one(backend, heard, t["text_spoken"], facts)
                rows.append({"scenario_id": run["scenario_id"], "seq": t["seq"], "agent": t.get("agent"),
                             "score": score.__dict__, "latency_ms": done.latency_ms,
                             "tokens": (done.prompt_tokens or 0) + (done.output_tokens or 0)})
            except JudgeError as exc:
                rows.append({"scenario_id": run["scenario_id"], "seq": t["seq"], "agent": t.get("agent"),
                             "error": str(exc)})
    scored = [r for r in rows if "score" in r]
    return {
        "judge": {"backend": backend.name, "model": backend.model},
        "summary": _summarise(rows, scored),
        "turns": rows,
    }


def _summarise(rows: list[dict[str, Any]], scored: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(key: str) -> float | None:
        vals = [r["score"][key] for r in scored]
        return round(sum(vals) / len(vals), 2) if vals else None

    per_agent: dict[str, dict[str, Any]] = {}
    for r in scored:
        bucket = per_agent.setdefault(r["agent"] or "router", {"turns": 0, "accuracy": 0, "helpfulness": 0, "tone": 0})
        bucket["turns"] += 1
        for k in SCORE_KEYS:
            bucket[k] += r["score"][k]
    for bucket in per_agent.values():
        for k in SCORE_KEYS:
            bucket[k] = round(bucket[k] / bucket["turns"], 2)
    return {
        "turns_judged": len(rows),
        "failures": len(rows) - len(scored),
        "accuracy": mean("accuracy"),
        "helpfulness": mean("helpfulness"),
        "tone": mean("tone"),
        "invented_facts": sum(1 for r in scored if r["score"]["invented_facts"]),
        "per_agent": per_agent,
        "total_latency_ms": round(sum(r.get("latency_ms", 0) for r in rows), 1),
        "total_tokens": sum(r.get("tokens", 0) for r in rows),
    }


def calibrate(backend: JudgeBackend, items: list[dict[str, Any]], facts: str) -> dict[str, Any]:
    """Agreement with human labels. A judge score agrees when it is within one point on each rubric score
    and the invented-facts answer matches."""
    details = []
    agree = 0
    for item in items:
        human = item["human"]
        try:
            score, _ = judge_one(backend, item["heard"], item["reply"], facts)
        except JudgeError as exc:
            details.append({"heard": item["heard"], "error": str(exc)})
            continue
        close = all(abs(getattr(score, k) - human[k]) <= 1 for k in SCORE_KEYS)
        same_fact_call = score.invented_facts == human["invented_facts"]
        ok = close and same_fact_call
        agree += int(ok)
        details.append({"heard": item["heard"], "judge": score.__dict__, "human": human, "agree": ok})
    scored = [d for d in details if "judge" in d]
    return {
        "judge": {"backend": backend.name, "model": backend.model},
        "items": len(items),
        "agreement": round(agree / len(items), 4) if items else None,
        "failures": len(items) - len(scored),
        "details": details,
    }


def assert_complete(result: dict[str, Any]) -> None:
    """A result with failed calls is not a result. Refuse to write it unless the caller accepts partial output."""
    failed = result.get("failures", 0)
    if failed:
        total = result.get("items") or result.get("summary", {}).get("turns_judged", 0) + failed
        raise JudgeError(f"{failed} of {total} judge calls failed; nothing was written. "
                         "Fix the cause (for example a rate limit) and run again, or pass --allow-partial.")


def build_facts(catalog: Any, store: Any) -> str:
    """The facts the shop publishes. The judge checks replies against these, and nothing else."""
    from voiceshop.catalog.multiagent import CARE_INFO
    from voiceshop.catalog.shop import SAVED_ADDRESS, TEST_CARD

    lines = [
        f"Shop: {catalog.store_name}.",
        f"Refund window: {store.window_days} days after delivery (policy {store.policy['version']}). "
        "Final sale items cannot be refunded. An order must be delivered before it can be refunded.",
        f"Customer care: open {CARE_INFO['hours']}; phone {CARE_INFO['phone']}; email {CARE_INFO['email']}.",
    ]
    for p in catalog.products.values():
        stock = "in stock" if p.stock > 0 else "out of stock"
        sleeps = f", sleeps {p.capacity}" if p.category == "tent" else ""
        lines.append(f"Product: {p.name}, ${p.price}, {stock}{sleeps}.")
    for o in store.orders.values():
        extra = []
        if o.tracking:
            extra.append(f"tracking {o.tracking}")
        if o.eta:
            extra.append(f"due {o.eta:%B} {o.eta.day}")
        if o.delivered_on:
            extra.append(f"delivered {o.delivered_on:%B} {o.delivered_on.day}")
        lines.append(f"Order {o.id}: status {o.status}, total ${o.total}" + (", " + ", ".join(extra) if extra else "") + ".")
    lines.append(f"Saved delivery address: {SAVED_ADDRESS}. Test payment card: {TEST_CARD}.")
    return "\n".join(lines)


DRIFT_TOLERANCE = 0.05  # agreement may fall by at most five points before it counts as drift


def drift(reference: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Reasons the current calibration differs from the reference. An empty list means no drift.
    A different model is always reported: a judge that changed silently is not the judge that was accepted."""
    reasons: list[str] = []
    ref_model = reference.get("judge", {})
    cur_model = current.get("judge", {})
    if ref_model.get("model") != cur_model.get("model") or ref_model.get("backend") != cur_model.get("backend"):
        reasons.append(f"judge changed from {ref_model.get('backend')}:{ref_model.get('model')} "
                       f"to {cur_model.get('backend')}:{cur_model.get('model')}")
    if current.get("items") != reference.get("items"):
        reasons.append(f"the calibration set changed ({reference.get('items')} to {current.get('items')} items)")
    ref_a, cur_a = reference.get("agreement"), current.get("agreement")
    if ref_a is not None and cur_a is not None and cur_a < ref_a - DRIFT_TOLERANCE:
        reasons.append(f"agreement fell from {ref_a:.1%} to {cur_a:.1%}")
    if current.get("failures"):
        reasons.append(f"{current['failures']} judge calls failed, so this result is incomplete")
    return reasons
