# Video Transcript

Small Python app with:

- a CLI that takes a Livestorm `session_id`, fetches the recording metadata from the Livestorm API, downloads the `.mp4`, extracts a mono MP3, uploads the full audio to Gladia, and writes the resulting verbose JSON transcript
- a local web UI where you paste a session ID and get the transcript plus raw JSON output
- a FastAPI-based HTTP API that other apps can call with a session ID and a `verbose` flag

## Why this is optimized

- It transcribes audio instead of video.
- It downmixes to mono.
- It resamples to `16 kHz`.
- It compresses the extracted audio to a low bitrate MP3 before upload.
- It downloads the recording only after resolving the MP4 URL from Livestorm.
- It extracts audio locally so Gladia receives the full MP3 rather than the source video file.
- It keeps the API session-based so other apps can continue calling the same endpoints.
- It always requests the same Gladia profile: diarization, named entity recognition, and sentences.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Your `.env` should contain:

```bash
GLADIA_KEY=...
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

Keep the extracted MP3:

```bash
.venv/bin/python main.py SESSION_ID --keep-audio
```

Keep the downloaded MP4 too:

```bash
.venv/bin/python main.py SESSION_ID --keep-video
```

Record a different provider label:

```bash
.venv/bin/python main.py SESSION_ID --provider gladia-v2-pre-recorded
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
- `verbose` is preserved for compatibility and is effectively always on with the Gladia integration
- the backend always includes diarization, named entity recognition, and sentences in the Gladia request
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
    "provider": "gladia",
    "model": "gladia-v2-pre-recorded",
    "requested_model": "gladia-v2-pre-recorded",
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
   `GLADIA_KEY`, `LS_API_KEY`, `API_AUTH_KEY`
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
- `provider`
- `model`
- `requested_model`
- `created_at`
- `language`
- `duration_seconds`
- `text`
- `segments`
- `words`

Segment entries look like:

```json
{
  "id": 0,
  "start": 0.0,
  "end": 2.64,
  "text": "Hello and welcome."
}
```
