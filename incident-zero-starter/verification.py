def verify_recovery(before, after, diagnosis):
    """Never claim recovery without fresh post-action telemetry."""
    if after is None:
        return {
            "status": "inconclusive",
            "details": "Simulation did not change the system and no fresh telemetry was collected."
        }
    before_errors = _error_rate(before)
    after_errors = _error_rate(after)
    if before_errors is None or after_errors is None:
        return {
            "status": "inconclusive",
            "details": "Fresh telemetry did not contain comparable error-rate values."
        }
    improved = after_errors < before_errors
    return {
        "status": "recovered" if improved else "not_recovered",
        "details": (
            f"Comparable error rate changed from {before_errors:.4f} to "
            f"{after_errors:.4f}."
        ),
        "before_error_rate": before_errors,
        "after_error_rate": after_errors,
    }


def _error_rate(telemetry):
    values = [
        metric.get("value") for metric in telemetry.get("metrics", [])
        if metric.get("name") == "error_rate"
        and isinstance(metric.get("value"), (int, float))
    ]
    return sum(values) / len(values) if values else None
