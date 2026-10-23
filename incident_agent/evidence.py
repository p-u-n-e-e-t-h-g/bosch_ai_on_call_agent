"""Anti-hallucination check: every evidence quote must literally appear in the
output of the tool call it cites. Unverified evidence is dropped and lowers confidence."""
import re

from . import config

MIN_VERIFIED_EVIDENCE = 2
UNVERIFIED_CONFIDENCE_CAP = 0.5


def _norm(s: str) -> str:
    s = s.replace('\\"', '"').replace('"', "").replace("'", "")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify(diagnosis: dict, runner) -> dict:
    verified, rejected = [], []
    for ev in diagnosis.get("evidence", []):
        out = runner.output_of(ev.get("tool_call_id", ""))
        quote = ev.get("quote", "")
        if out is not None and quote and _norm(quote) in _norm(out):
            verified.append({**ev, "verified": True,
                             "source_tool": next(c["tool"] for c in runner.calls if c["id"] == ev["tool_call_id"])})
        else:
            reason = "cited tool call does not exist" if out is None else "quote not found in tool output"
            rejected.append({**ev, "verified": False, "rejection_reason": reason})

    stated = float(diagnosis.get("confidence", 0))
    confidence = stated
    notes = []
    if rejected:
        confidence -= 0.15 * len(rejected)
        notes.append(f"{len(rejected)} evidence item(s) could not be verified against tool output and were discarded")
    if diagnosis.get("status") == "diagnosed" and len(verified) < MIN_VERIFIED_EVIDENCE:
        confidence = min(confidence, UNVERIFIED_CONFIDENCE_CAP)
        notes.append(f"fewer than {MIN_VERIFIED_EVIDENCE} verified evidence items")
    confidence = max(0.0, min(1.0, confidence))

    status = diagnosis.get("status")
    if status == "diagnosed" and confidence < config.CONFIDENCE_THRESHOLD:
        status = "escalate"
        notes.append(f"confidence {confidence:.2f} is below threshold {config.CONFIDENCE_THRESHOLD}; escalating to a human")

    return {"status": status, "stated_confidence": stated, "confidence": round(confidence, 2),
            "verified_evidence": verified, "rejected_evidence": rejected, "notes": notes}
