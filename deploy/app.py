"""Deployable ASGI app: Twilio webhooks + media stream relay.

Public endpoints:
  POST /answer        -> raw TwiML XML (Start Stream + Pause)
  WS   /media/stream  -> inbound mulaw audio (Whisper STT) + agent replies
  POST /call          -> trigger outbound call via Twilio REST
  GET  /health        -> liveness probe
"""
import os, json, asyncio
from pathlib import Path as _Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi import Request as FastAPIRequest
from fastapi.responses import Response, PlainTextResponse
from pydantic import BaseModel, Field
import urllib.request
import urllib.parse

# env loader (deploy platforms inject env vars directly; .env is local-only)
_here = _Path(__file__).resolve().parent
_envf = _here.parent / ".env"
if _envf.exists():
    for line in _envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

queues: dict[str, asyncio.Queue] = {}
media_sockets: dict[str, WebSocket] = {}
connected_events: dict[str, asyncio.Event] = {}
stream_state: dict[str, dict] = {}


def q(call_sid: str) -> asyncio.Queue:
    return queues.setdefault(call_sid, asyncio.Queue())


def _auth():
    import base64
    return "Basic " + base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()


@app.get("/health")
async def health():
    return {"status": "ok", "active_calls": list(queues.keys())}


@app.post("/answer")
async def answer(request: FastAPIRequest):
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        from urllib.parse import parse_qs
        params = {k: v[0] for k, v in parse_qs(raw).items()}
    except Exception:
        params = dict(request.query_params)
    call_sid = params.get("CallSid", "unknown")
    host = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
             f'<Connect><Stream url="{host}/media/{call_sid}" /></Connect>'
             '</Response>')
    connected_events.setdefault(call_sid, asyncio.Event())
    return Response(twiml, media_type="application/xml")


@app.websocket("/media/{call_sid}")
async def media(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    media_sockets[call_sid] = websocket
    connected_events.setdefault(call_sid, asyncio.Event()).set()
    try:
        while True:
            msg = await websocket.receive_text()
            event = json.loads(msg).get("event")
            if event == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        media_sockets.pop(call_sid, None)


class CallRequest(BaseModel):
    to: str


@app.post("/call")
async def call(body: CallRequest):
    if not (TWILIO_SID and TWILIO_TOKEN):
        return PlainTextResponse("Twilio not configured", status_code=500)
    to = body.to.strip()
    if not to.startswith("+"):
        to = "+91" + to.lstrip("0")
    host = PUBLIC_BASE_URL.replace("https://", "").replace("http://", "").rstrip("/")
    data = urllib.parse.urlencode({
        "To": to,
        "From": os.environ.get("TWILIO_NUMBER", ""),
        "Url": f"https://{host}/answer",
    }).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        data=data, method="POST", headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    return {"status": result.get("status"), "call_uuid": result.get("sid")}
