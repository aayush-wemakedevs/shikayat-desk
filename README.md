# Shikayat Desk

A voice-in, voice-out support desk built on the [Sarvam AI](https://dashboard.sarvam.ai) APIs, for a
fictional electricity provider. A customer records a complaint in their own language and gets a
spoken answer back in that same language. A panel on the right carries the English translation for a
human agent, a parsed ticket, and the time each API call took.

It was built for [Festive Hacks](https://www.wemakedevs.org/hackathons/festivehacks) on 25 November
2026, as the demo behind a walkthrough of the Sarvam APIs.

## What it does, in five calls

One `POST /api/complaint` with a recording makes five Sarvam calls in order:

1. `speech_to_text.transcribe` with `mode="transcribe"`, no `language_code`, so the model detects the
   language and returns it with a probability.
2. the same audio again with `mode="translate"`, for the agent's English view.
3. `chat.completions` on `sarvam-105b-conversations`, answering in the caller's language.
4. `text_to_speech.convert` on `bulbul:v4-flash`, with the speaker chosen from the detected language.
5. `chat.completions` again, in JSON mode, for the ticket category, urgency and summary.

The response carries the transcript, the detected language, the English version, the reply, the audio
as base64, the ticket, and the measured duration of the first four calls.

## Running it

```bash
pip install -r requirements.txt
export SARVAM_API_KEY=your_key_here
uvicorn app:app --host 127.0.0.1 --port 8787
```

Then open http://localhost:8787 and allow the microphone. Recording stops itself at 25 seconds,
because the synchronous speech-to-text endpoint rejects audio longer than 30.

Keep the key in your environment or a `.env` you do not commit. It is read once at startup and never
reaches the browser.

## Two things worth knowing

Chrome's `MediaRecorder` labels its output `audio/webm;codecs=opus`, and Sarvam matches the media type
exactly, so the codec parameter has to be trimmed off before the upload is sent. Every TTS speaker
belongs to one model, so a speaker from another model returns a 400 rather than falling back.

## Limits

The public deployment is metered, because every complaint spends five paid API calls: six per address
per ten minutes, three hundred a day in total, and uploads capped at 1.5 MB. Change
`RATE_MAX_PER_IP`, `DAILY_MAX_REQUESTS` and `MAX_UPLOAD` in `app.py` if you are running it on your
own key.
