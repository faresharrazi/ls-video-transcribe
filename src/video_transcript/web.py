from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import uvicorn

from .transcriber import transcribe_livestorm_session_data

logger = logging.getLogger(__name__)
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


APP_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Video Transcript</title>
    <style>
      :root {
        --bg: #f4efe4;
        --panel: rgba(255, 250, 241, 0.88);
        --panel-strong: #fffaf1;
        --text: #1f1b17;
        --muted: #6b6257;
        --accent: #0b6e4f;
        --danger: #a4372f;
        --border: rgba(31, 27, 23, 0.12);
        --shadow: 0 18px 50px rgba(60, 40, 20, 0.12);
      }

      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: Georgia, "Times New Roman", serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(209, 122, 34, 0.24), transparent 28%),
          radial-gradient(circle at top right, rgba(11, 110, 79, 0.2), transparent 26%),
          linear-gradient(135deg, #efe7d5 0%, #f9f6ef 46%, #ece4d2 100%);
      }
      .shell {
        width: min(1260px, calc(100vw - 32px));
        margin: 32px auto;
        display: grid;
        grid-template-columns: minmax(320px, 460px) minmax(0, 1fr);
        gap: 24px;
      }
      .card {
        background: var(--panel);
        backdrop-filter: blur(10px);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: var(--shadow);
      }
      .sidebar { padding: 28px; }
      .eyebrow {
        display: inline-block;
        margin-bottom: 12px;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(11, 110, 79, 0.08);
        color: var(--accent);
        font-size: 12px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      h1 {
        margin: 0 0 12px;
        font-size: clamp(2rem, 4vw, 3rem);
        line-height: 0.95;
      }
      .lead {
        margin: 0 0 24px;
        color: var(--muted);
        line-height: 1.6;
      }
      .field {
        margin-bottom: 14px;
      }
      label {
        display: block;
        font-size: 0.95rem;
        margin-bottom: 8px;
      }
      input[type="text"], input[type="password"], textarea {
        width: 100%;
        padding: 14px 16px;
        border: 1px solid var(--border);
        border-radius: 14px;
        background: #fffdfa;
        font: inherit;
        color: var(--text);
      }
      textarea {
        min-height: 110px;
        resize: vertical;
      }
      input[type="text"]:focus, input[type="password"]:focus, textarea:focus {
        outline: 2px solid rgba(11, 110, 79, 0.2);
        border-color: rgba(11, 110, 79, 0.38);
      }
      .toggle {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 14px 16px;
        border-radius: 16px;
        background: rgba(255, 253, 248, 0.8);
        border: 1px solid var(--border);
      }
      .toggle + .toggle { margin-top: 10px; }
      .toggle input[type="checkbox"] {
        width: 18px;
        height: 18px;
        accent-color: var(--accent);
        margin: 2px 0 0;
      }
      .toggle-copy {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .toggle-copy strong { font-size: 0.96rem; }
      .toggle-copy span {
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.4;
      }
      .actions {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-top: 18px;
      }
      button {
        border: 0;
        border-radius: 999px;
        padding: 14px 20px;
        background: linear-gradient(135deg, var(--accent), #0f8a64);
        color: white;
        font: inherit;
        cursor: pointer;
        transition: transform 120ms ease, box-shadow 120ms ease;
        box-shadow: 0 12px 24px rgba(11, 110, 79, 0.18);
      }
      button:hover { transform: translateY(-1px); }
      button:disabled {
        opacity: 0.65;
        cursor: wait;
        transform: none;
      }
      .hint {
        margin-top: 14px;
        font-size: 0.92rem;
        color: var(--muted);
        line-height: 1.5;
      }
      .results {
        padding: 24px;
        display: flex;
        flex-direction: column;
        min-height: 70vh;
      }
      .status {
        display: none;
        margin-bottom: 18px;
        padding: 14px 16px;
        border-radius: 16px;
        line-height: 1.5;
      }
      .status.visible { display: block; }
      .status.info {
        background: rgba(11, 110, 79, 0.08);
        color: var(--accent);
      }
      .status.error {
        background: rgba(164, 55, 47, 0.08);
        color: var(--danger);
      }
      .metrics {
        display: none;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 12px;
        margin-bottom: 18px;
      }
      .metrics.visible { display: grid; }
      .metric {
        padding: 14px;
        border-radius: 18px;
        background: var(--panel-strong);
        border: 1px solid var(--border);
      }
      .metric span {
        display: block;
        color: var(--muted);
        font-size: 0.82rem;
        margin-bottom: 6px;
      }
      .metric strong {
        font-size: 1rem;
        word-break: break-word;
      }
      .panel {
        padding: 18px;
        border-radius: 20px;
        background: rgba(255, 253, 248, 0.82);
        border: 1px solid var(--border);
      }
      .panel + .panel { margin-top: 16px; }
      .panel h2 {
        margin: 0 0 12px;
        font-size: 1rem;
      }
      .transcript {
        white-space: pre-wrap;
        line-height: 1.7;
      }
      pre {
        margin: 0;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: "SFMono-Regular", Menlo, monospace;
        font-size: 0.9rem;
      }
      .empty {
        margin: auto 0;
        text-align: center;
        color: var(--muted);
        line-height: 1.7;
      }
      @media (max-width: 980px) {
        .shell { grid-template-columns: 1fr; }
        .results { min-height: auto; }
        .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      }
      @media (max-width: 640px) {
        .metrics { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="card sidebar">
        <div class="eyebrow">Livestorm to Transcript</div>
        <h1>Gladia Session to JSON</h1>
        <p class="lead">Paste a Livestorm session ID and the app will fetch the recording, extract the full MP3, send it to Gladia, and return the resulting JSON plus a readable transcript preview.</p>
        <form id="transcript-form">
          <div class="field">
            <label for="session-id">Session ID</label>
            <input id="session-id" name="session_id" type="text" placeholder="fdbd0600-9a46-4755-b25b-21b51d4e29cb" autocomplete="off" required>
          </div>
          <div class="field">
            <label for="api-key">API Key</label>
            <input id="api-key" name="api_key" type="password" placeholder="Optional unless server auth is enabled" autocomplete="off">
          </div>
          <div class="actions">
            <button id="submit-btn" type="submit">Generate Transcript</button>
          </div>
        </form>
        <p class="hint">Every request now includes the same Gladia profile: diarization, named entity recognition, and sentences.</p>
      </section>
      <section class="card results">
        <div id="status" class="status"></div>
        <div id="metrics" class="metrics">
          <div class="metric"><span>Session</span><strong id="metric-session">-</strong></div>
          <div class="metric"><span>Language</span><strong id="metric-language">-</strong></div>
          <div class="metric"><span>Duration</span><strong id="metric-duration">-</strong></div>
          <div class="metric"><span>Segments</span><strong id="metric-segments">-</strong></div>
        </div>
        <div id="empty-state" class="empty">Transcript output will appear here once a session is processed.</div>
        <div id="result-content" style="display:none;">
          <div class="panel">
            <h2>Transcript</h2>
            <div id="transcript-text" class="transcript"></div>
          </div>
          <div class="panel">
            <h2>JSON Output</h2>
            <pre id="json-output"></pre>
          </div>
        </div>
      </section>
    </main>
    <script>
      const form = document.getElementById("transcript-form");
      const button = document.getElementById("submit-btn");
      const statusEl = document.getElementById("status");
      const metricsEl = document.getElementById("metrics");
      const emptyEl = document.getElementById("empty-state");
      const resultContentEl = document.getElementById("result-content");
      const transcriptTextEl = document.getElementById("transcript-text");
      const jsonOutputEl = document.getElementById("json-output");

      function setStatus(kind, message) {
        statusEl.textContent = message;
        statusEl.className = "status visible " + kind;
      }

      function clearStatus() {
        statusEl.textContent = "";
        statusEl.className = "status";
      }

      function setBusy(isBusy) {
        button.disabled = isBusy;
        button.textContent = isBusy ? "Working..." : "Generate Transcript";
      }

      function transcriptText(data) {
        if (data.text) {
          return data.text;
        }
        if (data.result && data.result.transcription && data.result.transcription.full_transcript) {
          return data.result.transcription.full_transcript;
        }
        return "(No transcript text returned)";
      }

      function renderTranscript(data) {
        document.getElementById("metric-session").textContent = data.session_id || "-";
        document.getElementById("metric-language").textContent = data.language || "unknown";
        document.getElementById("metric-duration").textContent = data.duration_seconds != null ? `${data.duration_seconds}s` : "-";
        document.getElementById("metric-segments").textContent = Array.isArray(data.segments) ? String(data.segments.length) : "n/a";
        transcriptTextEl.textContent = transcriptText(data);
        jsonOutputEl.textContent = JSON.stringify(data, null, 2);
        metricsEl.classList.add("visible");
        emptyEl.style.display = "none";
        resultContentEl.style.display = "block";
      }

      function resetResult() {
        metricsEl.classList.remove("visible");
        emptyEl.style.display = "block";
        resultContentEl.style.display = "none";
        transcriptTextEl.textContent = "";
        jsonOutputEl.textContent = "";
      }

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const sessionId = document.getElementById("session-id").value.trim();
        const apiKey = document.getElementById("api-key").value.trim();

        if (!sessionId) {
          resetResult();
          setStatus("error", "Please enter a Livestorm session ID.");
          return;
        }

        setBusy(true);
        clearStatus();
        resetResult();
        setStatus("info", "Fetching recording, extracting the full MP3, uploading it to Gladia, and waiting for the final JSON result. Large recordings can take a while.");

        try {
          const headers = { "Content-Type": "application/json" };
          if (apiKey) {
            headers["X-API-Key"] = apiKey;
          }

          const response = await fetch("/api/transcribe", {
            method: "POST",
            headers,
            body: JSON.stringify({ session_id: sessionId, timestamped: true })
          });

          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Unknown server error");
          }

          clearStatus();
          renderTranscript(payload.transcript);
        } catch (error) {
          resetResult();
          setStatus("error", error.message || "Something went wrong while generating the transcript.");
        } finally {
          setBusy(false);
        }
      });
    </script>
  </body>
</html>
"""


class TranscriptRequest(BaseModel):
    session_id: str
    timestamped: bool = True
    async_mode: bool = False
    gladia_options: dict[str, Any] | None = None


class TranscriptJobRequest(BaseModel):
    session_id: str
    timestamped: bool = True
    gladia_options: dict[str, Any] | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configured_api_key() -> str | None:
    load_dotenv()
    return os.getenv("API_AUTH_KEY") or os.getenv("TRANSCRIPT_API_KEY")


def _parse_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _build_output_path(session_id: str, timestamped: bool) -> Path:
    del timestamped
    suffix = "verbose"
    output_dir = Path.cwd() / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{session_id}.{suffix}.transcript.json"


def _build_jobs_dir() -> Path:
    jobs_dir = Path.cwd() / "outputs" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    return jobs_dir


def _build_job_path(job_id: str) -> Path:
    return _build_jobs_dir() / f"{job_id}.json"


def _serialize_transcription_exception(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, ValueError):
        return 400, str(exc)
    if isinstance(exc, FileNotFoundError):
        return 404, str(exc)
    if isinstance(exc, RuntimeError):
        message = str(exc)
        status_code = 502
        if "Missing Gladia API key" in message or "Missing Livestorm API key" in message:
            status_code = 500
        elif "No MP4 video recording found" in message:
            status_code = 404
        return status_code, message
    return 500, "Unexpected server error while generating the transcript."


def _sanitize_gladia_options(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _sanitize_gladia_options(item)
            if cleaned is None:
                continue
            if cleaned == {}:
                continue
            if cleaned == []:
                continue
            sanitized[key] = cleaned
        return sanitized
    if isinstance(value, list):
        sanitized_list = []
        for item in value:
            cleaned = _sanitize_gladia_options(item)
            if cleaned is None:
                continue
            if cleaned == {}:
                continue
            if cleaned == []:
                continue
            sanitized_list.append(cleaned)
        return sanitized_list
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _parse_gladia_options_query(raw_value: str | None) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid gladia_options JSON: {exc.msg}.") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="gladia_options must be a JSON object.")
    sanitized = _sanitize_gladia_options(parsed)
    return sanitized if isinstance(sanitized, dict) and sanitized else None


def _perform_transcription(
    session_id: str,
    timestamped: bool,
    gladia_options: dict[str, Any] | None = None,
) -> dict[str, object]:
    session_id = session_id.strip()
    if not session_id:
        raise ValueError("Session ID is required.")

    output_path = _build_output_path(session_id, timestamped)
    sanitized_gladia_options = _sanitize_gladia_options(gladia_options)
    if not isinstance(sanitized_gladia_options, dict):
        sanitized_gladia_options = None
    transcript = transcribe_livestorm_session_data(
        session_id=session_id,
        output_path=output_path,
        gladia_options=sanitized_gladia_options,
    )

    return {"transcript": transcript}


def _transcribe_request(
    session_id: str,
    timestamped: bool,
    gladia_options: dict[str, Any] | None = None,
) -> dict[str, object]:
    try:
        return _perform_transcription(
            session_id=session_id,
            timestamped=timestamped,
            gladia_options=gladia_options,
        )
    except Exception as exc:
        status_code, detail = _serialize_transcription_exception(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


class TranscriptJobManager:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._queued_job_ids: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._recover_jobs_locked()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="transcript-job-worker",
                daemon=True,
            )
            self._thread.start()

    def enqueue(
        self,
        session_id: str,
        timestamped: bool,
        gladia_options: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise HTTPException(status_code=400, detail="Session ID is required.")

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "session_id": normalized_session_id,
            "timestamped": timestamped,
            "gladia_options": gladia_options,
            "status": JOB_STATUS_QUEUED,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "result": None,
            "error": None,
        }
        self._write_job(job)
        self._enqueue_job_id(job_id)
        return job

    def get(self, job_id: str) -> dict[str, object]:
        job = self._read_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Transcript job not found.")
        return job

    def _enqueue_job_id(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued_job_ids:
                return
            self._queued_job_ids.add(job_id)
            self._queue.put(job_id)

    def _recover_jobs_locked(self) -> None:
        for job_file in _build_jobs_dir().glob("*.json"):
            try:
                job = json.loads(job_file.read_text())
            except (OSError, json.JSONDecodeError):
                logger.exception("Unable to read queued transcript job from %s", job_file)
                continue

            if job.get("status") not in {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}:
                continue

            job["status"] = JOB_STATUS_QUEUED
            job["updated_at"] = _utc_now_iso()
            self._write_job(job)
            job_id = str(job.get("job_id") or "").strip()
            if job_id and job_id not in self._queued_job_ids:
                self._queued_job_ids.add(job_id)
                self._queue.put(job_id)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                self._queued_job_ids.discard(job_id)

            job = self._read_job(job_id)
            if job is None:
                self._queue.task_done()
                continue

            job["status"] = JOB_STATUS_RUNNING
            job["updated_at"] = _utc_now_iso()
            self._write_job(job)

            try:
                result = _perform_transcription(
                    session_id=str(job["session_id"]),
                    timestamped=bool(job["timestamped"]),
                    gladia_options=job.get("gladia_options"),
                )
            except Exception as exc:
                status_code, detail = _serialize_transcription_exception(exc)
                logger.exception("Transcript job %s failed", job_id, exc_info=exc)
                job["status"] = JOB_STATUS_FAILED
                job["updated_at"] = _utc_now_iso()
                job["error"] = {
                    "message": detail,
                    "status_code": status_code,
                }
                job["result"] = None
            else:
                job["status"] = JOB_STATUS_COMPLETED
                job["updated_at"] = _utc_now_iso()
                job["result"] = result
                job["error"] = None

            self._write_job(job)
            self._queue.task_done()

    def _read_job(self, job_id: str) -> dict[str, object] | None:
        job_path = _build_job_path(job_id)
        if not job_path.exists():
            return None
        try:
            return json.loads(job_path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.exception("Unable to read transcript job state from %s", job_path)
            raise HTTPException(status_code=500, detail="Unable to read transcript job state.")

    def _write_job(self, job: dict[str, object]) -> None:
        job_path = _build_job_path(str(job["job_id"]))
        temp_path = job_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(job, indent=2, ensure_ascii=True) + "\n")
        temp_path.replace(job_path)


job_manager = TranscriptJobManager()


def _validate_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    configured_key = _configured_api_key()
    if not configured_key:
        return

    provided_key = None
    if x_api_key:
        provided_key = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        provided_key = authorization[7:].strip()

    if provided_key != configured_key:
        raise HTTPException(
            status_code=401,
            detail="Provide a valid API key via X-API-Key or Authorization: Bearer <key>.",
        )


def create_app() -> FastAPI:
    load_dotenv()
    app = FastAPI(title="Video Transcript API", version="0.1.0")

    cors_origins = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if cors_origins:
        origins = [origin.strip() for origin in cors_origins.split(",") if origin.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins if origins != ["*"] else ["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(_: Request, exc: HTTPException):
        return fastapi_json_response({"error": "Unauthorized." if exc.status_code == 401 else exc.detail}, exc.status_code)

    @app.exception_handler(Exception)
    async def _generic_exception_handler(_: Request, exc: Exception):
        logger.exception("Unexpected server error while generating transcript", exc_info=exc)
        return fastapi_json_response({"error": f"Unexpected server error while generating the transcript."}, 500)

    @app.on_event("startup")
    async def _startup() -> None:
        job_manager.start()

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(APP_HTML)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/transcribe", dependencies=[Depends(_validate_api_key)])
    async def get_transcript(
        session_id: str = Query(...),
        verbose: str = Query("true"),
        async_mode: str = Query("false"),
        gladia_options: str | None = Query(default=None),
    ) -> dict[str, object]:
        parsed_gladia_options = _parse_gladia_options_query(gladia_options)
        if _parse_bool(async_mode, default=False):
            job = job_manager.enqueue(
                session_id=session_id,
                timestamped=_parse_bool(verbose, default=True),
                gladia_options=parsed_gladia_options,
            )
            return fastapi_json_response(job, 202)
        return await run_in_threadpool(
            _transcribe_request,
            session_id=session_id,
            timestamped=_parse_bool(verbose, default=True),
            gladia_options=parsed_gladia_options,
        )

    @app.post("/api/transcribe", dependencies=[Depends(_validate_api_key)])
    async def post_transcript(payload: TranscriptRequest) -> dict[str, object]:
        if payload.async_mode:
            return fastapi_json_response(
                job_manager.enqueue(
                    session_id=payload.session_id,
                    timestamped=payload.timestamped,
                    gladia_options=payload.gladia_options,
                ),
                202,
            )
        return await run_in_threadpool(
            _transcribe_request,
            session_id=payload.session_id,
            timestamped=payload.timestamped,
            gladia_options=payload.gladia_options,
        )

    @app.post("/api/transcribe/jobs", dependencies=[Depends(_validate_api_key)])
    async def create_transcript_job(payload: TranscriptJobRequest):
        return fastapi_json_response(
            job_manager.enqueue(
                session_id=payload.session_id,
                timestamped=payload.timestamped,
                gladia_options=payload.gladia_options,
            ),
            202,
        )

    @app.get("/api/transcribe/jobs/{job_id}", dependencies=[Depends(_validate_api_key)])
    async def get_transcript_job(job_id: str) -> dict[str, object]:
        return job_manager.get(job_id)

    return app


def fastapi_json_response(payload: dict[str, object], status_code: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(content=payload, status_code=status_code)


app = create_app()


def run_server(host: str | None = None, port: int | None = None) -> None:
    resolved_host = host or os.getenv("HOST", "0.0.0.0")
    resolved_port = port or int(os.getenv("PORT", "8000"))
    uvicorn.run("video_transcript.web:app", host=resolved_host, port=resolved_port, reload=False)
