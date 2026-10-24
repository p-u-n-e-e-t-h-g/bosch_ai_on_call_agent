"""Optional Gemini diagnosis adapter."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(RuntimeError):
    """Raised when Gemini cannot return a usable diagnosis."""


def load_local_env(path=None):
    """Load simple KEY=VALUE entries without overwriting process variables."""
    env_path = Path(path or Path(__file__).parent / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value


def _extract_text(payload):
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part["text"] for part in parts if "text" in part)
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError("Gemini returned no usable text content") from exc
    if not text.strip():
        raise GeminiError("Gemini returned empty text content")
    return text.strip()


def diagnose_with_gemini(incident, telemetry, baseline):
    """Ask Gemini to improve a diagnosis while preserving the local baseline."""
    load_local_env()
    api_key = os.getenv("GEMINI_API")
    if not api_key:
        raise GeminiError("GEMINI_API is not configured")
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
    endpoint = os.getenv("GEMINI_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    url = f"{endpoint}/{model}:generateContent"
    prompt = {
        "incident": incident,
        "telemetry": telemetry,
        "baseline_diagnosis": baseline,
        "requirements": [
            "Return JSON only.",
            "Do not claim a confirmed root cause unless multiple evidence items support it.",
            "Use only the supplied telemetry.",
            "Return keys: hypothesis, confidence, evidence, alternative_hypotheses, "
            "counter_evidence, next_steps.",
        ],
    }
    body = json.dumps({
        "contents": [{"parts": [{"text": json.dumps(prompt)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20"))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise GeminiError(f"Gemini request failed: {exc}") from exc

    try:
        result = json.loads(_extract_text(payload))
    except json.JSONDecodeError as exc:
        raise GeminiError("Gemini returned text that was not valid JSON") from exc
    if not isinstance(result, dict):
        raise GeminiError("Gemini diagnosis must be a JSON object")
    required = {"hypothesis", "confidence", "evidence", "alternative_hypotheses",
                "counter_evidence", "next_steps"}
    missing = required - result.keys()
    if missing:
        raise GeminiError(f"Gemini diagnosis is missing keys: {sorted(missing)}")
    result["provider"] = "gemini"
    result["baseline_diagnosis"] = baseline
    return result
