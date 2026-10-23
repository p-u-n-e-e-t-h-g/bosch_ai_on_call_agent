"""Endpoints and thresholds. Override any value with an environment variable or .env."""
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:8080/jaeger/ui")
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash")
# Tried in order when the main model is overloaded (503) or out of quota (429).
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]

MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "14"))
# Below this confidence the agent escalates to a human instead of proposing a fix.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))

# Alert thresholds used by `watch`.
ALERT_ERROR_RATE = float(os.getenv("ALERT_ERROR_RATE", "0.05"))
ALERT_MIN_RPS = float(os.getenv("ALERT_MIN_RPS", "0.05"))
ALERT_LATENCY_P95_MS = float(os.getenv("ALERT_LATENCY_P95_MS", "1500"))

# Services the agent must never inspect: flagd holds the feature-flag settings,
# and reading them to diagnose is against the rules.
BLOCKED_SERVICES = {"flagd", "flagd-ui"}


def opensearch_url() -> str:
    """OpenSearch is published on a random host port, so look it up from Docker."""
    if os.getenv("OPENSEARCH_URL"):
        return os.environ["OPENSEARCH_URL"]
    out = subprocess.run(["docker", "port", "opensearch", "9200"],
                         capture_output=True, text=True, timeout=10).stdout
    port = out.strip().splitlines()[0].rsplit(":", 1)[1]
    return f"http://localhost:{port}"
