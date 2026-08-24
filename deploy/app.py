"""ElevateBox voice agent on Render — full loop via cloud APIs.

Twilio WS mulaw in -> PCM -> faster-whisper STT -> OpenRouter brain ->
Sarvam REST TTS (WAV) -> resample 8k -> ulaw -> Twilio WS out.
"""
import os, json, asyncio, audioop, base64
import urllib.request
import urllib.parse
import numpy as np
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi import Request as FastAPIRequest
from fastapi.responses import Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")

GREET = ("Hi! This is Ananya from a web studio here in Hyderabad. We build "
         "online stores for local businesses. Do you have two minutes?")

BRAIN_MODEL = os.environ.get("OPENAI_MODEL", "openrouter/auto")
BRAIN_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are Ananya, calling a local business owner about building them an "
    "e-commerce website. Rules: ONE short sentence replies. Ask about budget, "
    "products count, timeline one at a time. Mirror their language "
    "(English/Hindi/Telugu). If they ask price/timeline or show buying intent, "
    "say you will WhatsApp the details right now.")

def _http_json(url, payload, headers, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ------------------------------- BRAIN --------------------------------------
BRAIN_TOOLS = [{
    "type": "function",
    "function": {"name": "send_whatsapp_now",
                 "description": "Send details on WhatsApp NOW (high intent)",
                 "parameters": {"type": "object", "properties": {}}}
}]

async def brain_reply(transcript_text: str, history):
    """Blocking call run in thread; returns (reply_text, whatsapp_flag)."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history[-8:]
    def _call():
        return _http_json(BRAIN_URL,
            {"model": BRAIN_MODEL, "messages": messages,
             "tools": BRAIN_TOOLS, "max_tokens": 120},
            {"Authorization": f"Bearer {OPENROUTER_KEY}"}, timeout=25)
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, _call)
    choice = resp["choices"][0]["message"]
    text = choice.get("content") or ""
    wants_wa = any(t.get("function", {}).get("name") == "send_whatsapp_now"
                   for t in choice.get("tool_calls", []) or [])
    return text.strip(), wants_wa

# -------------------------------- STT ---------------------------------------
_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    return _whisper

def transcribe_pcm16(pcm16_bytes: bytes) -> str:
    audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype("float32") / 32768.0
    segs, _ = get_whisper().transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(s.text for s in segs).strip()

# -------------------------------- TTS ---------------------------------------
async def sarvam_tts_pcm16(text: str, lang: str) -> bytes | None:
    """Sarvam REST TTS -> WAV bytes; extract raw PCM after 44-byte header."""
    if not SARVAM_KEY or len(text) > 450:
        return None
    voice = "anushka"
    code_map = {"hi": "hi-IN", "te": "te-IN"}
    lc = code_map.get(lang.split("-")[0], "en-IN")
    def _call():
        return _http_json(
            "https://api.sarvam.ai/text-to-speech",
            {"text": text, "target_language_code": lc, "speaker": voice,
             "model": "bulbul:v2"},
            {"api-subscription-key": SARVAM_KEY}, timeout=25)
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _call)
        wav = base64.b64decode("".join(resp["audios"]))
        return wav[44:] if len(wav) > 44 else None   # strip WAV header
    except Exception as e:
        print("[tts] error:", e, flush=True)
        return None


# ------------------------------ TWILIO REST ---------------------------------
def twilio_dial(to: str):
    host = os.environ.get("PUBLIC_BASE_URL", "").replace(
        "https://", "").replace("http://", "").rstrip("/")
    data = urllib.parse.urlencode({
        "To": to if to.startswith("+") else "+91" + to.lstrip("0"),
        "From": TWILIO_NUMBER,
        "Url": f"https://{host}/answer",
    }).encode()
    import base64
    auth = "Basic " + base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        data=data, method="POST", headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

# --------------------------------- ROUTES ------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    params = {}
    if request.method == "POST":
        raw = (await request.body()).decode("utf-8", errors="replace")
        from urllib.parse import parse_qs
        params = {k: v[0] for k, v in parse_qs(raw).items()}
    if not params:
        params = dict(request.query_params)
    call_sid = params.get("CallSid", "unknown")
    wss_host = os.environ.get("PUBLIC_BASE_URL", "").replace(
        "https://", "wss://").replace("http://", "ws://")
    twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
             '<Connect><Stream url="' + wss_host + '/media/' + call_sid +
             '" /></Connect></Response>')
    return Response(twiml, media_type="application/xml")

class CallRequest(BaseModel):
    to: str

@app.post("/call")
async def call(body: CallRequest):
    try:
        result = twilio_dial(body.to)
        return {"status": result.get("status"), "call_uuid": result.get("sid")}
    except Exception as e:
        return {"error": str(e)[:300]}

# --------------------------- MEDIA WS + AGENT LOOP ---------------------------
media_sockets: dict[str, WebSocket] = {}
queues: dict[str, asyncio.Queue] = {}
connected_events: dict[str, asyncio.Event] = {}

def q(call_sid): return queues.setdefault(call_sid, asyncio.Queue())
def cev(call_sid): return connected_events.setdefault(call_sid, asyncio.Event())

_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    return _whisper

import numpy as np
def stt(pcm16_bytes):
    audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype("float32") / 32768.0
    segs, _ = get_whisper().transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(s.text for s in segs).strip()

async def brain(history):
    def _call():
        return _http_json(BRAIN_URL,
            {"model": BRAIN_MODEL,
             "messages": [{"role": "system", "content": SYSTEM_PROMPT}] +
                         history[-8:],
             "max_tokens": 120},
            {"Authorization": f"Bearer {OPENROUTER_KEY}"}, timeout=25)
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, _call)
    msg = resp["choices"][0]["message"]
    return (msg.get("content") or "").strip()

SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")
async def tts_mulaw(text, lang="en"):
    if not SARVAM_KEY or not text:
        return b""
    code_map = {"hi": "hi-IN", "te": "te-IN"}
    lc = code_map.get(lang.split("-")[0], "en-IN")
    def _call():
        return _http_json("https://api.sarvam.ai/text-to-speech",
            {"text": text[:450], "target_language_code": lc,
             "speaker": "anushka", "model": "bulbul:v2"},
            {"api-subscription-key": SARVAM_KEY}, timeout=25)
    loop = asyncio.get_event_loop()
    try:
        resp = await loop.run_in_executor(None, _call)
        wav = base64.b64decode("".join(resp["audios"]))
        import wave, io
        w = wave.open(io.BytesIO(wav), "rb")
        pcm = w.readframes(w.getnframes()); rate = w.getframerate(); w.close()
        if rate != 8000:
            pcm = audioop.ratecv(pcm, 2, 1, rate, 8000, None)[0]
        return audioop.lin2ulaw(pcm, 2)
    except Exception as e:
        print("[tts] error:", e, flush=True)
        return b""

@app.websocket("/media/{call_sid}")
async def media(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    media_sockets[call_sid] = websocket
    cev(call_sid).set()
    print(f"[media] stream connected for {call_sid}", flush=True)

    import base64 as b64
    pcm_buf = bytearray()
    history = []
    FLUSH = 32000          # ~2s of 8k16bit
    speaking = False

    async def play_ulaw(ulaw_bytes):
        nonlocal speaking
        speaking = True
        for off in range(0, len(ulaw_bytes), 160):
            frame = b64.b64encode(ulaw_bytes[off:off+160]).decode()
            await media_sockets[call_sid].send_text(json.dumps({
                "event": "media", "streamSid":
                (await get_stream_sid(call_sid)),
                "media": {"payload": frame}}))
        speaking = False

    stream_sids = {}

    async def get_stream_sid(cs):
        return stream_sids.get(cs, "")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            ev = msg.get("event")

            if ev == "start":
                stream_sids[call_sid] = msg["start"]["streamSid"]
                print(f"[media] start, streamSid={stream_sids[call_sid]}",
                      flush=True)
                # greet immediately after connect
                greeting = await tts_mulaw(GREET, "en")
                await play_ulaw(greeting)

            elif ev == "media":
                payload = b64.b64decode(msg["media"]["payload"])
                pcm_buf.extend(mulaw_to_pcm16(payload))
                if len(pcm_buf) >= FLUSH and not speaking:
                    chunk, pcm_buf = bytes(pcm_buf), bytearray()
                    text = await asyncio.get_event_loop().run_in_executor(
                        None, stt, chunk)
                    text = text.strip()
                    print(f"[agent] caller: {text!r}", flush=True)
                    if len(text) < 2:
                        continue
                    history.append({"role": "user", "content": text})
                    reply = await brain(history)
                    history.append({"role": "assistant", "content": reply})
                    print(f"[agent] Ananya: {reply}", flush=True)
                    audio = await tts_mulaw(reply, "en")
                    if audio:
                        await play_ulaw(audio)

            elif ev == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        media_sockets.pop(call_sid, None)
        print(f"[media] disconnected {call_sid}", flush=True)
