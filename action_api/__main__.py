import os

import uvicorn

if __name__ == "__main__":
    # Bound to localhost only: nothing outside this laptop can trigger actions.
    uvicorn.run("action_api.server:app", host="127.0.0.1", port=int(os.getenv("ACTION_API_PORT", "8090")))
