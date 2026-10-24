class ActionClient:
    """Safe placeholder: no network calls and no service changes."""

    def execute(self, action, parameters):
        raise NotImplementedError(
            "Read the official event Action API documentation and implement its exact schema."
        )

    def simulate(self, proposal):
        return {
            "mode": "simulation_only",
            "message": "Simulated approved proposal. No service was changed.",
            "action": proposal["action"]
        }
