# Video Transcript

Small Python app with:

- a CLI that takes a Livestorm `session_id`, fetches the recording metadata from the Livestorm API, downloads the `.mp4`, extracts lightweight mono audio, sends that audio to OpenAI speech-to-text, and writes a timestamped JSON transcript
- a local web UI where you paste a session ID and get the transcript plus raw JSON output
- a FastAPI-based HTTP API that other apps can call with a session ID and a `verbose` flag

## Why this is optimized

- It transcribes audio instead of video.
- It downmixes to mono.
- It resamples to `16 kHz`.
- It compresses the extracted audio to a low bitrate MP3 before upload.
- It downloads the recording only after resolving the MP4 URL from Livestorm.
- It defaults to `whisper-1` because timestamped `verbose_json` output currently requires a timestamp-capable model.
- If you request a model that does not support timestamped `verbose_json`, the app automatically falls back to `whisper-1`.
- For non-timestamped transcription, extracted audio is automatically split when it exceeds `20 MB` or when it runs longer than `8 minutes`, each chunk is transcribed with `gpt-4o-mini-transcribe`, and the text is concatenated before being returned to the UI or API.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Your `.env` should contain:

```bash
OPENAI_KEY=...
LS_API_KEY=...
API_AUTH_KEY=your-shared-secret
```

`API_AUTH_KEY` is optional locally, but recommended for online deployment. You can also use `TRANSCRIPT_API_KEY` if you prefer that env var name.

## Usage

```bash
.venv/bin/python main.py SESSION_ID
```

This writes:

```text
<recording-file>.transcript.json
```

Custom output path:

```bash
.venv/bin/python main.py SESSION_ID --output out/transcript.json
```

Include per-word timestamps:

```bash
.venv/bin/python main.py SESSION_ID --word-timestamps
```

Keep the extracted MP3:

```bash
.venv/bin/python main.py SESSION_ID --keep-audio
```

Keep the downloaded MP4 too:

```bash
.venv/bin/python main.py SESSION_ID --keep-video
```

Pick a different model or provide a language hint:

```bash
.venv/bin/python main.py SESSION_ID --model whisper-1 --language en
```

Optional, if you want the `video-transcript` shell command instead of `python main.py`:

```bash
.venv/bin/pip install setuptools wheel
.venv/bin/pip install -e . --no-build-isolation
```

## Web UI

Start the local server:

```bash
.venv/bin/python web.py
```

Then open:

```text
http://127.0.0.1:8000
```

The UI:

- accepts a Livestorm session ID
- shows the transcript text directly in the page
- shows the full JSON response
- saves output files under `outputs/`
- displays friendly errors for missing keys, invalid sessions, missing MP4 recordings, download issues, and transcription failures

## API

Run the API server locally:

```bash
.venv/bin/python api.py
```

By default it binds to `0.0.0.0:8000`. You can override:

```bash
HOST=0.0.0.0 PORT=8080 .venv/bin/python api.py
```

Protect the public API with a shared key:

```bash
API_AUTH_KEY=your-shared-secret HOST=0.0.0.0 PORT=8080 .venv/bin/python api.py
```

Health check:

```bash
curl "http://127.0.0.1:8000/health"
```

Recommended endpoint for other apps:

```bash
curl "http://127.0.0.1:8000/api/transcribe?session_id=SESSION_ID&verbose=true" \
  -H "X-API-Key: your-shared-secret"
```

Notes:

- `GET` request bodies are not reliable on the public internet, so the API uses query params for `GET`
- `verbose=true` means timestamped output via `whisper-1` with `segments`
- `verbose=false` means plain JSON via `gpt-4o-mini-transcribe` without `segments`
- if `API_AUTH_KEY` or `TRANSCRIPT_API_KEY` is set, requests must send either `X-API-Key: ...` or `Authorization: Bearer ...`

You can also call it with `POST` and a JSON body:

```bash
curl -X POST "http://127.0.0.1:8000/api/transcribe" \
  -H "X-API-Key: your-shared-secret" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"SESSION_ID","timestamped":true}'
```

Safer production pattern for long transcriptions:

```bash
curl -X POST "http://127.0.0.1:8000/api/transcribe/jobs" \
  -H "X-API-Key: your-shared-secret" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"SESSION_ID","timestamped":true}'
```

That returns `202 Accepted` with a `job_id`. Then poll:

```bash
curl "http://127.0.0.1:8000/api/transcribe/jobs/JOB_ID" \
  -H "X-API-Key: your-shared-secret"
```

Job states:

- `queued`
- `running`
- `completed`
- `failed`

You can also opt into async mode on the existing endpoint by sending `"async_mode": true` in `POST /api/transcribe` or `async_mode=true` on the `GET` query string.

Successful response shape:

```json
{
  "transcript": {
    "session_id": "SESSION_ID",
    "timestamped": true,
    "model": "whisper-1",
    "requested_model": "whisper-1",
    "text": "Transcript text here"
  }
}
```

Error response shape:

```json
{
  "error": "Session ID is required."
}
```

## Render Deployment

This repo now includes [render.yaml](/Users/fares/Code/video-transcript/render.yaml), so Render can create the service with the right build and start commands automatically.

Recommended deployment steps:

1. Push this repo to GitHub.
2. In Render, create a new Blueprint or Web Service from the repo.
3. Use a paid plan such as `Starter` for production use.
4. Set these environment variables in Render:
   `OPENAI_KEY`, `LS_API_KEY`, `API_AUTH_KEY`
5. Optionally set `CORS_ALLOW_ORIGINS` to a comma-separated list of allowed frontend origins, or `*` if you intentionally want public browser access.

Render start command:

```bash
uvicorn --app-dir src video_transcript.web:app --host 0.0.0.0 --port $PORT
```

Health check path:

```text
/health
```

Once deployed, your other app can call:

```bash
curl "https://your-service.onrender.com/api/transcribe?session_id=SESSION_ID&verbose=true" \
  -H "X-API-Key: your-shared-secret"
```

## Output shape

The JSON contains:

- `session_id`
- `source_video`
- `extracted_audio`
- `recording`
- `model`
- `created_at`
- `language`
- `duration_seconds`
- `text`
- `segments`
- `words`
- `usage`

Segment entries look like:

```json
{
  "id": 0,
  "start": 0.0,
  "end": 2.64,
  "text": "Hello and welcome."
}
```
