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

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:
    psycopg = None
    dict_row = None

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
    <title>Video Transcript Test UI</title>
    <style>
      :root {
        --text: #1f1b17;
        --muted: #6b6257;
        --accent: #0b6e4f;
        --danger: #a4372f;
        --border: rgba(31, 27, 23, 0.12);
        --panel: rgba(255, 252, 246, 0.92);
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
        width: min(960px, calc(100vw - 32px));
        margin: 32px auto;
      }
      .card {
        padding: 28px;
        background: var(--panel);
        backdrop-filter: blur(10px);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: var(--shadow);
      }
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
      label {
        display: block;
        margin-bottom: 8px;
        font-size: 0.95rem;
      }
      input[type="text"] {
        width: 100%;
        padding: 14px 16px;
        border: 1px solid var(--border);
        border-radius: 14px;
        background: #fffdfa;
        font: inherit;
        color: var(--text);
      }
      input[type="text"]:focus {
        outline: 2px solid rgba(11, 110, 79, 0.2);
        border-color: rgba(11, 110, 79, 0.38);
      }
      .actions {
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
        box-shadow: 0 12px 24px rgba(11, 110, 79, 0.18);
      }
      button:disabled {
        opacity: 0.65;
        cursor: wait;
      }
      .hint {
        margin-top: 14px;
        color: var(--muted);
        line-height: 1.5;
        font-size: 0.92rem;
      }
      .results {
        margin-top: 20px;
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
      .panel {
        padding: 18px;
        border-radius: 20px;
        background: rgba(255, 253, 248, 0.82);
        border: 1px solid var(--border);
      }
      .panel h2 {
        margin: 0 0 12px;
        font-size: 1rem;
      }
      .empty {
        padding: 32px 16px;
        text-align: center;
        color: var(--muted);
        line-height: 1.7;
      }
      pre {
        margin: 0;
        min-height: 420px;
        overflow: auto;
        white-space: pre;
        font-family: "SFMono-Regular", Menlo, monospace;
        font-size: 0.9rem;
      }
      @media (max-width: 640px) {
        .card { padding: 20px; }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="card">
        <div class="eyebrow">Online Test UI</div>
        <h1>Session to Raw JSON</h1>
        <p class="lead">Paste a Livestorm session ID and this page will run the built-in transcription flow, then show the raw JSON response.</p>
        <form id="transcript-form">
          <label for="session-id">Session ID</label>
          <input id="session-id" name="session_id" type="text" placeholder="fdbd0600-9a46-4755-b25b-21b51d4e29cb" autocomplete="off" required>
          <div class="actions">
            <button id="submit-btn" type="submit">Fetch JSON</button>
          </div>
        </form>
        <p class="hint">This page is for built-in testing only. Protected API routes can still require <code>API_AUTH_KEY</code>.</p>
        <section class="results">
          <div id="status" class="status"></div>
          <div id="empty-state" class="empty">Raw JSON output will appear here once a session is processed.</div>
          <div id="result-content" style="display:none;">
            <div class="panel">
              <h2>JSON Output</h2>
              <pre id="json-output"></pre>
            </div>
          </div>
        </section>
      </section>
    </main>
    <script>
      const form = document.getElementById("transcript-form");
      const button = document.getElementById("submit-btn");
      const statusEl = document.getElementById("status");
      const emptyEl = document.getElementById("empty-state");
      const resultContentEl = document.getElementById("result-content");
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
        button.textContent = isBusy ? "Working..." : "Fetch JSON";
      }

      function resetResult() {
        emptyEl.style.display = "block";
        resultContentEl.style.display = "none";
        jsonOutputEl.textContent = "";
      }

      function renderResult(data) {
        jsonOutputEl.textContent = JSON.stringify(data, null, 2);
        emptyEl.style.display = "none";
        resultContentEl.style.display = "block";
      }

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const sessionId = document.getElementById("session-id").value.trim();

        if (!sessionId) {
          resetResult();
          setStatus("error", "Please enter a Livestorm session ID.");
          return;
        }

        setBusy(true);
        clearStatus();
        resetResult();
        setStatus("info", "Fetching the recording and waiting for the final JSON result. Large sessions can take a while.");

        try {
          const response = await fetch("/ui/transcribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, timestamped: true })
          });

          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Unknown server error");
          }

          clearStatus();
          renderResult(payload.transcript);
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
    output_dir = _storage_root() / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{session_id}.{suffix}.transcript.json"


def _storage_root() -> Path:
    configured = os.getenv("TRANSCRIPT_STORAGE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd()


def _database_url() -> str | None:
    load_dotenv()
    configured = os.getenv("DATABASE_URL", "").strip()
    return configured or None


def _postgres_enabled() -> bool:
    return _database_url() is not None


def _connect_db():
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg is required when DATABASE_URL is configured. Install dependencies again.")
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row)


def _serialize_json_field(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True)


def _deserialize_json_field(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return value


def _normalize_job_record(row: dict[str, Any]) -> dict[str, object]:
    normalized = dict(row)
    for key in ("gladia_options", "result", "error"):
        normalized[key] = _deserialize_json_field(normalized.get(key))
    return normalized


def _build_jobs_dir() -> Path:
    jobs_dir = _storage_root() / "outputs" / "jobs"
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
            self._initialize_storage_locked()
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
            storage_root = _storage_root()
            raise HTTPException(
                status_code=404,
                detail=(
                    "Transcript job not found. "
                    f"Current job storage directory: {storage_root}. "
                    "If this service restarted or uses non-persistent storage, async jobs can disappear."
                ),
            )
        return job

    def _enqueue_job_id(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued_job_ids:
                return
            self._queued_job_ids.add(job_id)
            self._queue.put(job_id)

    def _initialize_storage_locked(self) -> None:
        if not _postgres_enabled():
            return
        with _connect_db() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamped BOOLEAN NOT NULL,
                    gladia_options TEXT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result TEXT NULL,
                    error TEXT NULL
                )
                """
            )

    def _recover_jobs_locked(self) -> None:
        if _postgres_enabled():
            self._recover_db_jobs_locked()
            return
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

    def _recover_db_jobs_locked(self) -> None:
        with _connect_db() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcript_jobs
                SET status = %s, updated_at = %s
                WHERE status IN (%s, %s)
                RETURNING job_id
                """,
                (JOB_STATUS_QUEUED, _utc_now_iso(), JOB_STATUS_QUEUED, JOB_STATUS_RUNNING),
            )
            rows = cursor.fetchall()

        for row in rows:
            job_id = str(row.get("job_id") or "").strip()
            if job_id and job_id not in self._queued_job_ids:
                self._queued_job_ids.add(job_id)
                self._queue.put(job_id)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                self._queued_job_ids.discard(job_id)

            job = self._claim_job(job_id)
            if job is None:
                self._queue.task_done()
                continue

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

    def _claim_job(self, job_id: str) -> dict[str, object] | None:
        if _postgres_enabled():
            with _connect_db() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE transcript_jobs
                    SET status = %s, updated_at = %s
                    WHERE job_id = %s AND status = %s
                    RETURNING job_id, session_id, timestamped, gladia_options, status, created_at, updated_at, result, error
                    """,
                    (JOB_STATUS_RUNNING, _utc_now_iso(), job_id, JOB_STATUS_QUEUED),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return _normalize_job_record(row)

        job = self._read_job(job_id)
        if job is None:
            return None
        if job.get("status") != JOB_STATUS_QUEUED:
            return None
        job["status"] = JOB_STATUS_RUNNING
        job["updated_at"] = _utc_now_iso()
        self._write_job(job)
        return job

    def _read_job(self, job_id: str) -> dict[str, object] | None:
        if _postgres_enabled():
            with _connect_db() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT job_id, session_id, timestamped, gladia_options, status, created_at, updated_at, result, error
                    FROM transcript_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                row = cursor.fetchone()
            return _normalize_job_record(row) if row is not None else None

        job_path = _build_job_path(job_id)
        if not job_path.exists():
            return None
        try:
            return json.loads(job_path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.exception("Unable to read transcript job state from %s", job_path)
            raise HTTPException(status_code=500, detail="Unable to read transcript job state.")

    def _write_job(self, job: dict[str, object]) -> None:
        if _postgres_enabled():
            with _connect_db() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO transcript_jobs (
                        job_id,
                        session_id,
                        timestamped,
                        gladia_options,
                        status,
                        created_at,
                        updated_at,
                        result,
                        error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO UPDATE SET
                        session_id = EXCLUDED.session_id,
                        timestamped = EXCLUDED.timestamped,
                        gladia_options = EXCLUDED.gladia_options,
                        status = EXCLUDED.status,
                        created_at = EXCLUDED.created_at,
                        updated_at = EXCLUDED.updated_at,
                        result = EXCLUDED.result,
                        error = EXCLUDED.error
                    """,
                    (
                        str(job["job_id"]),
                        str(job["session_id"]),
                        bool(job["timestamped"]),
                        _serialize_json_field(job.get("gladia_options")),
                        str(job["status"]),
                        str(job["created_at"]),
                        str(job["updated_at"]),
                        _serialize_json_field(job.get("result")),
                        _serialize_json_field(job.get("error")),
                    ),
                )
            return

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

    @app.post("/ui/transcribe")
    async def ui_transcript(payload: TranscriptRequest) -> dict[str, object]:
        return await run_in_threadpool(
            _transcribe_request,
            session_id=payload.session_id,
            timestamped=payload.timestamped,
            gladia_options=payload.gladia_options,
        )

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
