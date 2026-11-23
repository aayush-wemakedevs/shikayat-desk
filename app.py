import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

app = FastAPI(title="Shikayat Desk")
log = logging.getLogger("uvicorn.error")
page = Path(__file__).with_name("index.html")
key = os.environ.get("SARVAM_API_KEY")
if not key:
    raise RuntimeError("Set SARVAM_API_KEY before starting Shikayat Desk.")
client = SarvamAI(api_subscription_key=key, timeout=35)

KEYTERMS = ["bijli", "meter", "reading", "connection", "bill"]
SPEAKERS = {"hi-IN": "aparna_hi_customer", "en-IN": "aparna_en_edtech"}
# 25 seconds of webm/opus is well under 300 KB. The ceiling is generous but not open.
MAX_UPLOAD = 1_500_000
# Each complaint spends five paid Sarvam calls, so the public demo is metered.
RATE_WINDOW_SECONDS = 600
RATE_MAX_PER_IP = 6
DAILY_MAX_REQUESTS = 300
_rate_lock = threading.Lock()
_recent_by_ip: dict[str, deque] = {}
_today = {"date": "", "count": 0}
SUPPORT_PROMPT = (
    "You are a customer support agent at a fictional electricity provider. "
    "Help with wrong bills, power cuts, meter readings, and related electricity service issues. "
    "Reply in the same language and writing system as the customer's complaint, in two or three "
    "short sentences, with no markdown. Always give a concrete next step the customer can take. "
    "Do not claim to have opened a ticket, checked an account, or dispatched a technician."
)
TICKET_PROMPT = (
    'Return only one strict JSON object with "category", "urgency", and "summary". '
    'Use a short English category, "low", "medium", or "high" for urgency, and a '
    "one-line English summary of the electricity complaint. Use high for immediate safety risks. "
    "Do not include markdown or text outside the JSON object."
)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def enforce_limits(request: Request) -> None:
    now = time.monotonic()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    ip = client_ip(request)
    with _rate_lock:
        if _today["date"] != today:
            _today["date"], _today["count"] = today, 0
        if _today["count"] >= DAILY_MAX_REQUESTS:
            raise HTTPException(
                429,
                "This demo has spent its Sarvam quota for today. Try tomorrow, or run it yourself with your own key.",
            )
        hits = _recent_by_ip.setdefault(ip, deque())
        while hits and now - hits[0] > RATE_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_MAX_PER_IP:
            raise HTTPException(429, "Too many complaints from this address. Wait a few minutes and try again.")
        hits.append(now)
        _today["count"] += 1
        if len(_recent_by_ip) > 5000:
            for stale in [addr for addr, seen in _recent_by_ip.items() if not seen]:
                del _recent_by_ip[stale]


@contextmanager
def timed(timings, stage):
    started = perf_counter()
    try:
        yield
    except ApiError as error:
        body = error.body if isinstance(error.body, dict) else {}
        payload = body.get("error", body.get("detail", ""))
        message = payload.get("message", "") if isinstance(payload, dict) else payload
        detail = f"Sarvam {stage} returned HTTP {error.status_code}."
        if isinstance(message, str) and message:
            detail += " " + message.replace(key, "[redacted]")[:280]
        status = error.status_code if error.status_code in (402, 429) else 502
        raise HTTPException(status, detail) from None
    except httpx.TimeoutException:
        raise HTTPException(504, f"Sarvam {stage} timed out.") from None
    except httpx.RequestError:
        raise HTTPException(502, f"Could not reach Sarvam for {stage}.") from None
    finally:
        timings[stage] = round((perf_counter() - started) * 1000, 1)


def recognize(audio_file, mode, timings, stage):
    with timed(timings, stage):
        return client.speech_to_text.transcribe(
            file=audio_file,
            model="saaras:v4",
            mode=mode,
            keyterms=KEYTERMS,
        )


def complete(messages, timings, stage, max_tokens, **options):
    with timed(timings, stage):
        result = client.chat.completions(
            model="sarvam-105b-conversations",
            messages=messages,
            max_tokens=max_tokens,
            **options,
        )
    content = result.choices[0].message.content if result.choices else None
    if not isinstance(content, str):
        raise HTTPException(502, f"Sarvam {stage} returned an empty response.")
    return content


def parse_ticket(raw):
    try:
        ticket = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(ticket, dict) or ticket.keys() != {"category", "urgency", "summary"}:
        return raw
    if any(not isinstance(value, str) or not value.strip() for value in ticket.values()):
        return raw
    if ticket["urgency"] not in ("low", "medium", "high") or len(ticket["summary"].splitlines()) != 1:
        return raw
    return ticket


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(page, media_type="text/html")


@app.post("/api/complaint")
def complaint(request: Request, file: UploadFile):
    enforce_limits(request)
    recording = file.file.read(MAX_UPLOAD + 1)
    if not recording:
        raise HTTPException(400, "The recording was empty. Please record your complaint again.")
    if len(recording) > MAX_UPLOAD:
        raise HTTPException(413, "The recording is too large. Please record a shorter complaint.")
    # Sarvam accepts the WebM container but rejects MIME parameters from MediaRecorder.
    media_type = (file.content_type or "audio/webm").split(";", 1)[0].strip().lower()
    audio_file = (file.filename or "complaint.webm", recording, media_type)
    timings = {}
    native = recognize(audio_file, "transcribe", timings, "stt")
    transcript = native.transcript.strip() if native.transcript else ""
    language = native.language_code
    if not transcript or not language:
        raise HTTPException(422, "Sarvam could not detect clear speech. Please try recording again.")
    translated = recognize(audio_file, "translate", timings, "translate")

    reply = complete([
        {"role": "system", "content": SUPPORT_PROMPT},
        {"role": "user", "content": transcript},
    ], timings, "chat", 240).strip()
    if not reply:
        raise HTTPException(502, "Sarvam chat returned an empty reply.")

    speaker = SPEAKERS.get(language, "shubh_enhi_banking")
    with timed(timings, "tts"):
        speech = client.text_to_speech.convert(
            text=reply,
            language_code=language,
            model="bulbul:v4-flash",
            speaker=speaker,
        )
    if not speech.audios or not speech.audios[0]:
        raise HTTPException(502, "Sarvam TTS returned an empty audio response.")

    raw_ticket = complete([
        {"role": "system", "content": TICKET_PROMPT},
        {"role": "user", "content": translated.transcript},
    ], timings, "ticket", 160, response_format={"type": "json_object"})
    log.info("Sarvam complaint timings ms: %s", timings)

    return {
        "transcript": transcript,
        "language_code": language,
        "language_probability": native.language_probability,
        "english": translated.transcript,
        "reply": reply,
        "audio_base64": speech.audios[0],
        "ticket": parse_ticket(raw_ticket),
        "timings_ms": {stage: timings[stage] for stage in ("stt", "translate", "chat", "tts")},
    }
