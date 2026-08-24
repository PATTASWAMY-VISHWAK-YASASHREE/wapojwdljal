"""ElevateBox voice agent — complete, self-contained, Render-ready.

Twilio flow:
  dial (Url=.../answer) -> raw TwiML: <Connect><Stream wss://absolute/>
  -> media WS: mulaw in -> Whisper STT -> OpenRouter brain -> Sarvam TTS
  -> mulaw out. Bidirectional <Connect> stream.
"""
import os
import json
import base64
import asyncio
import audioop
import urllib.request
import urllib.parse

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

try:
    import audioop as _audioop
except ImportError:
    import audioop_lts as _audioop

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")
BRAIN_MODEL = os.environ.get("OPENAI_MODEL", "openrouter/auto")

GREETING = ("Hi! This is Ananya from a web studio here in Hyderabad. We build "
            "online stores for local businesses. Do you have two minutes?")

SYSTEM_PROMPT = (
    "You are Ananya, calling a shop owner about building an e-commerce "
    "website. Reply in ONE short sentence. Ask about product count, budget, "
    "timeline one at a time. Mirror their language (English/Hindi/Telugu). "
    "If they ask price/timeline or show buying intent, say you will WhatsApp "
    "the details right now.")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------ helpers -------------------------------------
def _mulaw_to_pcm16(b: bytes) -> bytes:
    return _audioop.ulaw2lin(b, 2)


def _pcm16_to_mulaw(b: bytes) -> bytes:
    return _audioop.lin2ulaw(b, 2)


def _resample_to_8k(pcm: bytes, rate: int) -> bytes:
    if rate == 8000:
        return pcm
    return _audioop.ratecv(pcm, 2, 1, rate, 8000, None)[0]


def _auth_header() -> str:
    import base64 as b64
    return "Basic " + b64.b64encode(
        f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 30):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ------------------------------- STT -----------------------------------------
_whisper = None


def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("tiny.en", device="cpu",
                                compute_type="int8")
    return _whisper


def transcribe_pcm16(pcm16: bytes) -> str:
    audio = np.frombuffer(pcm16, dtype=np.int16).astype("float32") / 32768.0
    segs, _ = get_whisper().transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(s.text for s in segs).strip()


# ------------------------------- BRAIN ---------------------------------------
async def brain(history: list) -> str:
    def call():
        return _post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            {"model": BRAIN_MODEL,
             "messages": [{"role": "system",
                           "content": SYSTEM_PROMPT}] + history[-8:],
             "max_tokens": 120},
            {"Authorization": f"Bearer {OPENROUTER_KEY}"}, timeout=25)
    loop = asyncio.get_event_loop()
    try:
        resp = await loop.run_in_executor(None, call)
        return (resp["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        print("[brain] error:", e, flush=True)
        return "Sorry, could you say that again?"


# ------------------------------- TTS ------------------------------------------
async def tts_pcm16(text: str, lang: str) -> bytes | None:
    """Sarvam REST TTS; returns raw PCM16 at 8kHz, or None on failure."""
    if not SARVAM_KEY or not text or len(text) > 450:
        return None
    code_map = {"hi": "hi-IN", "te": "te-IN"}
    lc = code_map.get(lang.split("-")[0], "en-IN")

    def call():
        return _post_json(
            "https://api.sarvam.ai/text-to-speech",
            {"text": text, "target_language_code": lc,
             "speaker": "anushka", "model": "bulbul:v2"},
            {"api-subscription-key": SARVAM_KEY}, timeout=25)

    loop = asyncio.get_event_loop()
    try:
        resp = await loop.run_in_executor(None, call)
        wav = base64.b64decode("".join(resp["audios"]))
        import wave, io
        w = wave.open(io.BytesIO(wav), "rb")
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
        w.close()
        return _resample_to_8k(pcm, rate)
    except Exception as e:
        print("[tts] error:", e, flush=True)
        return None


# ------------------------------- ROUTES ---------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    params = {}
    if request.method == "POST":
        try:
            raw = (await request.body()).decode("utf-8", errors="replace")
            from urllib.parse import parse_qs
            params = {k: v[0] for k, v in parse_qs(raw).items()}
        except Exception:
            pass
    if not params:
        params = dict(request.query_params)
    call_sid = params.get("CallSid", "unknown")

    # derive absolute wss from Host header (zero-config on Render)
    host_hdr = request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto", "https")
    stream_url = f"wss://{host_hdr}/media/{call_sid}"

    twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
             '<Connect><Stream url="' + stream_url +
             '" track="inbound_track" /></Connect></Response>')
    return Response(twiml, media_type="application/xml")


class CallRequest(BaseModel):
    to: str


@app.post("/call")
async def trigger_call(body: CallRequest):
    to = body.to.strip()
    if not to.startswith("+"):
        to = "+91" + to.lstrip("0")
    data = urllib.parse.urlencode({
        "To": to, "From": TWILIO_NUMBER,
        "Url": f"{request_base()}/answer",
    }).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        data=data, method="POST", headers={"Authorization": _auth_header()})
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
    return {"status": result.get("status"), "call_uuid": result.get("sid")}


