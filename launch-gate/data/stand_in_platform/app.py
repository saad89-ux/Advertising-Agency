"""SYNTHETIC stand-in ad platform (dry run). Run: uvicorn app:app --port 8100
POST /v1/ads  (header Idempotency-Key recommended). Chaos via POST /admin/config.
Without an Idempotency-Key every call creates a NEW ad (duplicates are possible on purpose)."""
import json, random, threading, time, uuid
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel
RULES = json.loads((Path(__file__).parent / "rejection_rules.json").read_text())["rules"]
app = FastAPI(title="Stand-in platform (SYNTHETIC)")
LOCK = threading.Lock()
STATE = {"ads": [], "keys": {}, "cfg": {"fail_before_commit_rate": 0.0, "fail_after_commit_rate": 0.0, "timeout_rate": 0.0, "timeout_seconds": 15, "seed": 1}}
R = random.Random(1)
class Ad(BaseModel):
    ad_id: str; client: str; platform: str; text: str; headline: str = ""; link: str; creative: str = ""; content_hash: str
@app.post("/admin/config")
def config(cfg: dict):
    STATE["cfg"].update(cfg); R.seed(STATE["cfg"].get("seed", 1)); return STATE["cfg"]
@app.post("/admin/reset")
def reset():
    with LOCK: STATE["ads"].clear(); STATE["keys"].clear()
    return {"ok": True}
@app.get("/admin/stats")
def stats():
    hashes = {}
    for a in STATE["ads"]: hashes[a["content_hash"]] = hashes.get(a["content_hash"], 0) + 1
    return {"ads": len(STATE["ads"]), "duplicates": sum(c - 1 for c in hashes.values() if c > 1),
            "accepted": sum(a["status"] == "accepted" for a in STATE["ads"]), "rejected": sum(a["status"] == "rejected" for a in STATE["ads"])}
@app.get("/v1/ads")
def list_ads(): return STATE["ads"]
def decide(ad):
    blob = (ad.text + " " + ad.headline).lower()
    for r in RULES:
        if "pattern" in r and r["pattern"] in blob: return "rejected", r["reason"]
        if "link_contains" in r and r["link_contains"] in ad.link.lower(): return "rejected", r["reason"]
    return "accepted", None
@app.post("/v1/ads", status_code=201)
def create(ad: Ad, response: Response, idempotency_key: str | None = Header(default=None)):
    cfg = STATE["cfg"]
    if R.random() < cfg["timeout_rate"]: time.sleep(cfg["timeout_seconds"])
    with LOCK:
        if idempotency_key and idempotency_key in STATE["keys"]:
            response.headers["Idempotent-Replay"] = "true"; return STATE["keys"][idempotency_key]
        if R.random() < cfg["fail_before_commit_rate"]: raise HTTPException(503, "temporarily unavailable")
        status, reason = decide(ad)
        rec = {"platform_ad_id": str(uuid.uuid4()), "status": status, "reason": reason, **ad.model_dump()}
        STATE["ads"].append(rec)
        if idempotency_key: STATE["keys"][idempotency_key] = rec
        if R.random() < cfg["fail_after_commit_rate"]: raise HTTPException(503, "failed after saving")
        return rec
