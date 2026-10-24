from action_client import ActionClient

def propose_remediation(incident, diagnosis):
    # Proposal only. Check the official Action API allowlist before real integration.
    action = "no_action"
    rationale = "Evidence is insufficient for an automatic change; gather more evidence or escalate."
    if diagnosis["confidence"] == "medium" and "Payment dependency" in diagnosis["hypothesis"]:
        action = "investigate_payment_dependency"
        rationale = "Evidence points toward payment connectivity, but the root cause is not confirmed."
    return {
        "action": action,
        "parameters": {},
        "rationale": rationale,
        "risk": "An unsupported or premature change could worsen the incident.",
        "approval_status": "pending" if action != "no_action" else "not_applicable"
    }

def execute_if_approved(proposal, approved=False, simulate_action=False):
    if proposal["action"] == "no_action":
        return {"execution_status": "not_applicable", "execution_result": None}
    if not approved:
        return {"approval_status": "awaiting_approval", "execution_status": "not_executed", "execution_result": None}
    if not simulate_action:
        return {
            "approval_status": "approved",
            "execution_status": "blocked_not_configured",
            "execution_result": "No Action API configured. No change was made."
        }
    return {
        "approval_status": "approved",
        "execution_status": "simulated",
        "execution_result": ActionClient().simulate(proposal)
    }
