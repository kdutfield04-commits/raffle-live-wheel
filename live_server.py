import os
import json
import sqlite3
import secrets
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.getenv("LIVE_DB_PATH", "live_draws.sqlite3")
API_KEY = os.getenv("LIVE_DRAW_API_KEY", "").strip()
app = FastAPI(title="Raffle Live Wheel API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"]
)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS draws (raffle_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS live_state (id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL, updated_at TEXT NOT NULL)")

@app.on_event("startup")
def startup():
    init_db()

def require_key(authorization: str | None):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="Server API key has not been configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorised")

class Winner(BaseModel):
    place: int = Field(ge=1, le=10)
    ticket_number: int = Field(ge=1)
    username: str = Field(max_length=100)

class DrawStart(BaseModel):
    raffle_id: int = Field(ge=1)
    prize: str = Field(min_length=1, max_length=200)
    ticket_limit: int = Field(ge=2, le=500)
    winner_count: int = Field(ge=1, le=10)
    start_at: datetime
    winners: list[Winner]

class DrawReady(BaseModel):
    raffle_id: int = Field(ge=1)
    prize: str = Field(min_length=1, max_length=200)
    ticket_limit: int = Field(ge=2, le=500)

class DrawComplete(BaseModel):
    raffle_id: int = Field(ge=1)

def save_live_state(payload: dict[str, Any]):
    with db() as c:
        c.execute(
            "INSERT INTO live_state(id,payload,updated_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (json.dumps(payload), datetime.now(timezone.utc).isoformat())
        )

@app.get("/")
def root():
    return {"service": "Raffle Live Wheel API", "ok": True}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/raffle/ready")
def raffle_ready(data: DrawReady, authorization: str | None = Header(default=None)):
    """Set the public wheel to the newly-created raffle before tickets are sold."""
    require_key(authorization)
    payload = {
        "status": "READY",
        "raffle_id": data.raffle_id,
        "prize": data.prize,
        "ticket_limit": data.ticket_limit,
        "winner_count": 0,
        "start_at": None,
        "winners": [],
    }
    save_live_state(payload)
    return {"ok": True, "raffle_id": data.raffle_id, "status": "READY"}

@app.get("/api/live")
def get_live():
    """Return whichever raffle should currently be shown on the public wheel."""
    with db() as c:
        row = c.execute("SELECT payload,updated_at FROM live_state WHERE id=1").fetchone()
    if not row:
        return {
            "status": "WAITING",
            "raffle_id": None,
            "prize": "",
            "ticket_limit": 30,
            "winner_count": 0,
            "start_at": None,
            "winners": [],
        }
    payload = json.loads(row["payload"])
    payload["updated_at"] = row["updated_at"]
    return payload

@app.post("/api/draw/start")
def start_draw(data: DrawStart, authorization: str | None = Header(default=None)):
    require_key(authorization)
    if len(data.winners) != data.winner_count:
        raise HTTPException(status_code=400, detail="Winner count does not match winners list")
    if len({w.ticket_number for w in data.winners}) != len(data.winners):
        raise HTTPException(status_code=400, detail="Winning tickets must be unique")
    if any(w.ticket_number > data.ticket_limit for w in data.winners):
        raise HTTPException(status_code=400, detail="Winning ticket is outside the ticket range")
    payload = {
        "raffle_id": data.raffle_id, "prize": data.prize,
        "ticket_limit": data.ticket_limit, "winner_count": data.winner_count,
        "start_at": data.start_at.astimezone(timezone.utc).isoformat(),
        "winners": [w.model_dump() for w in data.winners],
    }
    with db() as c:
        c.execute("INSERT INTO draws(raffle_id,payload,status,updated_at) VALUES(?,?,?,?) ON CONFLICT(raffle_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at",
                  (data.raffle_id, json.dumps(payload), "SPINNING", datetime.now(timezone.utc).isoformat()))
    payload["status"] = "SPINNING"
    save_live_state(payload)
    return {"ok": True, "raffle_id": data.raffle_id}

@app.post("/api/draw/complete")
def complete_draw(data: DrawComplete, authorization: str | None = Header(default=None)):
    require_key(authorization)
    with db() as c:
        row = c.execute("SELECT payload FROM draws WHERE raffle_id=?", (data.raffle_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draw not found")
        c.execute("UPDATE draws SET status='COMPLETED', updated_at=? WHERE raffle_id=?",
                  (datetime.now(timezone.utc).isoformat(), data.raffle_id))
    # Once the reveal is finished, clear the public display ready for the next raffle.
    save_live_state({
        "status": "WAITING",
        "raffle_id": None,
        "last_completed_raffle_id": data.raffle_id,
        "prize": "",
        "ticket_limit": 30,
        "winner_count": 0,
        "start_at": None,
        "winners": [],
    })
    return {"ok": True, "raffle_id": data.raffle_id, "status": "WAITING"}

@app.get("/api/draw/{raffle_id}")
def get_draw(raffle_id: int):
    with db() as c:
        row = c.execute("SELECT payload,status,updated_at FROM draws WHERE raffle_id=?", (raffle_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No live draw found")
    payload = json.loads(row["payload"])
    payload["status"] = row["status"]
    payload["updated_at"] = row["updated_at"]
    return payload
