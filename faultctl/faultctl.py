"""Practice-incident injector, used by HUMANS to create incidents for testing.

The agent never imports or calls this. It plays the role of the organisers'
faultctl: it sets demo feature flags (the same file the flag UI edits) or
breaks containers.

  python faultctl/faultctl.py list
  python faultctl/faultctl.py inject payment-failure
  python faultctl/faultctl.py reset
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DEMO_DIR = Path(os.getenv("OTEL_DEMO_DIR", Path(__file__).resolve().parents[2] / "opentelemetry-demo"))
FLAG_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"

FLAG_INCIDENTS = {
    "payment-failure": ("paymentFailure", "50%"),
    "payment-unreachable": ("paymentUnreachable", "on"),
    "cart-failure": ("cartFailure", "25%"),
    "product-catalog-failure": ("productCatalogFailure", "on"),
    "recommendation-cache-leak": ("recommendationCacheFailure", "on"),
    "ad-high-cpu": ("adHighCpu", "on"),
    "ad-manual-gc": ("adManualGc", "on"),
    "ad-failure": ("adFailure", "on"),
    "homepage-flood": ("loadGeneratorFloodHomepage", "on"),
    "image-slow-load": ("imageSlowLoad", "5sec"),
    "email-memory-leak": ("emailMemoryLeak", "100x"),
}
INFRA_INCIDENTS = {
    "stop-cart": [["docker", "stop", "cart"]],
    "stop-valkey": [["docker", "stop", "valkey-cart"]],
    "stop-payment": [["docker", "stop", "payment"]],
    "starve-recommendation": [["docker", "update", "--cpus", "0.05", "recommendation"]],
    "starve-product-catalog": [["docker", "update", "--cpus", "0.03", "product-catalog"]],
}
INFRA_RESET = [
    ["docker", "start", "cart", "valkey-cart", "payment"],
    ["docker", "update", "--cpus", "0", "recommendation"],
    ["docker", "update", "--cpus", "0", "product-catalog"],
]


def set_flag(name, variant):
    data = json.loads(FLAG_FILE.read_text())
    data["flags"][name]["defaultVariant"] = variant
    FLAG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        print("flag incidents: ", ", ".join(FLAG_INCIDENTS))
        print("infra incidents:", ", ".join(INFRA_INCIDENTS))
    elif cmd == "inject":
        name = sys.argv[2]
        if name in FLAG_INCIDENTS:
            set_flag(*FLAG_INCIDENTS[name])
        else:
            for c in INFRA_INCIDENTS[name]:
                subprocess.run(c, check=True)
        print(f"injected {name}")
    elif cmd == "reset":
        data = json.loads(FLAG_FILE.read_text())
        for f in data["flags"].values():
            f["defaultVariant"] = "off"
        FLAG_FILE.write_text(json.dumps(data, indent=2) + "\n")
        for c in INFRA_RESET:
            subprocess.run(c, capture_output=True)
        print("reset: all flags off, containers started, CPU limits removed")


if __name__ == "__main__":
    main()
