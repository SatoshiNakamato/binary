"""Local server for the degen-fly-inspired Flybrain control room.

The existing roam process remains the source of neural telemetry. This server
only serves the new UI and proxies read-only state from the local brain service.
It does not create credentials or expose an execution interface.
"""
import os
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).parent
UI_PORT = int(os.environ.get("FLY_UI_PORT", "4661"))
BRAIN_URL = os.environ.get("FLY_BRAIN_URL", "http://127.0.0.1:4660").rstrip("/")

app = FastAPI(title="Degenfly Flybrain UI")


@app.get("/")
def index():
    return FileResponse(str(ROOT / "web" / "degenfly.html"))


@app.get("/state")
def state():
    try:
        r = requests.get(f"{BRAIN_URL}/state", timeout=2)
        r.raise_for_status()
        return JSONResponse(r.json())
    except Exception as exc:
        return JSONResponse(
            {"updated": 0, "steps": 0, "clicks": 0, "hops": 0,
             "hz": {}, "neural": None, "error": str(exc)[:160]},
            status_code=200,
        )


if __name__ == "__main__":
    print(f"Degenfly UI: http://127.0.0.1:{UI_PORT}")
    print(f"Brain source: {BRAIN_URL}")
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")
