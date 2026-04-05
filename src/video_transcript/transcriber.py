from __future__ import annotations

import http.client
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import imageio_ffmpeg
except ModuleNotFoundError:
    imageio_ffmpeg = None
from dotenv import load_dotenv

DEFAULT_MODEL = "gladia-v2-pre-recorded"
DEFAULT_AUDIO_BITRATE = "32k"
DEFAULT_AUDIO_SAMPLE_RATE = 16000
GLADIA_API_BASE = "https://api.gladia.io"
GLADIA_UPLOAD_PATH = "/v2/upload"
GLADIA_PRE_RECORDED_PATH = "/v2/pre-recorded"
GLADIA_POLL_INTERVAL_SECONDS = 3
GLADIA_POLL_TIMEOUT_SECONDS = 30 * 60
LIVESTORM_API_BASE = "https://api.livestorm.co/v1"
DEFAULT_GLADIA_OPTIONS: dict[str, Any] = {
    "diarization": True,
    "named_entity_recognition": True,
    "sentences": True,
}


def _resolve_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("GLADIA_KEY")
    if not api_key:
        raise RuntimeError("Missing Gladia API key. Set GLADIA_KEY in your environment or .env file.")
    return api_key


def _resolve_livestorm_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("LS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing Livestorm API key. Set LS_API_KEY in your environment or .env file.")
    return api_key


def _ffmpeg_executable() -> str:
    if imageio_ffmpeg is not None:
        return imageio_ffmpeg.get_ffmpeg_exe()

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    raise RuntimeError(
        "FFmpeg is required but not available. Install the Python package "
        "`imageio-ffmpeg` with `pip install -r requirements.txt` or install "
        "a system `ffmpeg` binary and make sure it is on your PATH."
    )


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg_path = _ffmpeg_executable()
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(DEFAULT_AUDIO_SAMPLE_RATE),
        "-b:a",
        DEFAULT_AUDIO_BITRATE,
        str(audio_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "Unknown ffmpeg error"
        raise RuntimeError(f"Audio extraction failed: {stderr}") from exc


def _json_request(
    *,
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: bytes | None = None
    headers = {
        "accept": "application/json",
        "x-gladia-key": api_key,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gladia API request failed with HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gladia API request failed: {exc.reason}") from exc


def _upload_audio_file(audio_path: Path, api_key: str) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    boundary = f"gladia-{uuid.uuid4().hex}"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="{audio_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = len(preamble) + audio_path.stat().st_size + len(epilogue)

    connection = http.client.HTTPSConnection("api.gladia.io", timeout=120)
    try:
        connection.putrequest("POST", GLADIA_UPLOAD_PATH)
        connection.putheader("x-gladia-key", api_key)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Accept", "application/json")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        connection.send(preamble)
        with audio_path.open("rb") as audio_handle:
            while True:
                chunk = audio_handle.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        connection.send(epilogue)

        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"Gladia upload failed with HTTP {response.status}: {body}")
        return json.loads(body) if body else {}
    except OSError as exc:
        raise RuntimeError(f"Gladia upload failed: {exc}") from exc
    finally:
        connection.close()


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
            continue
        merged[key] = value
    return merged


def _build_gladia_request(audio_url: str, gladia_options: dict[str, Any] | None = None) -> dict[str, Any]:
    request_payload: dict[str, Any] = _deep_merge({}, DEFAULT_GLADIA_OPTIONS)
    if gladia_options:
        request_payload = _deep_merge(request_payload, gladia_options)
    request_payload["diarization"] = True
    request_payload["named_entity_recognition"] = True
    request_payload["sentences"] = True
    request_payload.pop("audio_to_llm", None)
    request_payload.pop("audio_to_llm_config", None)
    request_payload["audio_url"] = audio_url
    return request_payload


def _start_gladia_transcription(audio_url: str, api_key: str, gladia_options: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _build_gladia_request(audio_url, gladia_options)
    return _json_request(
        method="POST",
        url=f"{GLADIA_API_BASE}{GLADIA_PRE_RECORDED_PATH}",
        api_key=api_key,
        payload=payload,
    )


def _poll_gladia_transcription(job_id: str, api_key: str) -> dict[str, Any]:
    deadline = time.monotonic() + GLADIA_POLL_TIMEOUT_SECONDS
    last_payload: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        payload = _json_request(
            method="GET",
            url=f"{GLADIA_API_BASE}{GLADIA_PRE_RECORDED_PATH}/{job_id}",
            api_key=api_key,
        )
        last_payload = payload
        status = str(payload.get("status") or "").strip().lower()
        if status == "done":
            return payload
        if status == "error":
            error_code = payload.get("error_code")
            raise RuntimeError(f"Gladia transcription failed with status=error and error_code={error_code}.")
        time.sleep(GLADIA_POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"Timed out while waiting for Gladia transcription job {job_id}. "
        f"Last status: {last_payload.get('status') if last_payload else 'unknown'}."
    )


def _extract_text_segments(result_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    result = result_payload.get("result")
    if not isinstance(result, dict):
        return "", [], []

    transcription = result.get("transcription")
    if not isinstance(transcription, dict):
        return "", [], []

    full_transcript = str(transcription.get("full_transcript") or "").strip()
    utterances = transcription.get("utterances")
    words = transcription.get("words")

    segments: list[dict[str, Any]] = []
    if isinstance(utterances, list):
        for index, utterance in enumerate(utterances, start=1):
            if not isinstance(utterance, dict):
                continue
            segments.append(
                {
                    "id": utterance.get("id", index),
                    "start": utterance.get("start"),
                    "end": utterance.get("end"),
                    "speaker": utterance.get("speaker"),
                    "confidence": utterance.get("confidence"),
                    "text": str(utterance.get("text") or "").strip(),
                }
            )

    normalized_words: list[dict[str, Any]] = []
    if isinstance(words, list):
        for word in words:
            if not isinstance(word, dict):
                continue
            normalized_words.append(
                {
                    "word": word.get("word"),
                    "start": word.get("start"),
                    "end": word.get("end"),
                    "speaker": word.get("speaker"),
                    "confidence": word.get("confidence"),
                }
            )

    return full_transcript, segments, normalized_words


def _extract_duration_seconds(gladia_payload: dict[str, Any]) -> float | None:
    file_data = gladia_payload.get("file")
    if isinstance(file_data, dict):
        for key in ("audio_duration", "duration"):
            value = file_data.get(key)
            if isinstance(value, (int, float)):
                return float(value)

    result = gladia_payload.get("result")
    if isinstance(result, dict):
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            for key in ("audio_duration", "duration"):
                value = metadata.get(key)
                if isinstance(value, (int, float)):
                    return float(value)

    return None


def _extract_language(gladia_payload: dict[str, Any]) -> str | None:
    result = gladia_payload.get("result")
    if not isinstance(result, dict):
        return None

    transcription = result.get("transcription")
    if isinstance(transcription, dict):
        languages = transcription.get("languages")
        if isinstance(languages, list) and languages:
            first_language = languages[0]
            if isinstance(first_language, str) and first_language.strip():
                return first_language.strip()

    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        language = metadata.get("language")
        if isinstance(language, str) and language.strip():
            return language.strip()

    return None


def _normalize_transcription(
    gladia_payload: dict[str, Any],
    *,
    source_video: Path,
    extracted_audio: Path | None,
    requested_model: str,
    actual_model: str,
    session_id: str | None = None,
    recording: dict[str, Any] | None = None,
    upload_payload: dict[str, Any] | None = None,
    gladia_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text, segments, words = _extract_text_segments(gladia_payload)
    normalized = dict(gladia_payload)
    normalized["provider"] = "gladia"
    normalized["source_video"] = str(source_video.resolve())
    normalized["extracted_audio"] = str(extracted_audio.resolve()) if extracted_audio else None
    normalized["model"] = actual_model
    normalized["requested_model"] = requested_model
    normalized["timestamped"] = True
    normalized["created_at"] = datetime.now(timezone.utc).isoformat()
    normalized["text"] = text
    normalized["language"] = _extract_language(gladia_payload)
    normalized["duration_seconds"] = _extract_duration_seconds(gladia_payload)
    normalized["segments"] = segments
    normalized["words"] = words
    if session_id:
        normalized["session_id"] = session_id
    if recording:
        normalized["recording"] = {
            "id": recording.get("id"),
            "event_id": recording.get("attributes", {}).get("event_id"),
            "session_id": recording.get("attributes", {}).get("session_id"),
            "file_type": recording.get("attributes", {}).get("file_type"),
            "mime_type": recording.get("attributes", {}).get("mime_type"),
            "file_size": recording.get("attributes", {}).get("file_size"),
            "file_name": recording.get("attributes", {}).get("file_name"),
            "url_generated_at": recording.get("attributes", {}).get("url_generated_at"),
            "url_expires_in": recording.get("attributes", {}).get("url_expires_in"),
        }
    if upload_payload is not None:
        normalized["upload"] = upload_payload
    if gladia_request is not None:
        normalized["request_payload"] = gladia_request
    return normalized


def _fetch_livestorm_recordings(session_id: str) -> dict[str, Any]:
    api_key = _resolve_livestorm_api_key()
    request = urllib.request.Request(
        url=f"{LIVESTORM_API_BASE}/sessions/{session_id}/recordings",
        headers={
            "Authorization": api_key,
            "accept": "application/vnd.api+json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Livestorm API request failed with HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Livestorm API request failed: {exc.reason}") from exc


def _select_recording(payload: dict[str, Any]) -> dict[str, Any]:
    recordings = payload.get("data", [])
    for recording in recordings:
        attributes = recording.get("attributes", {})
        if attributes.get("file_type") == "video" and attributes.get("mime_type") == "mp4":
            return recording
    raise RuntimeError("No MP4 video recording found in the Livestorm session response.")


def _download_recording(recording: dict[str, Any], destination: Path) -> None:
    url = recording.get("attributes", {}).get("url")
    if not url:
        raise RuntimeError("Livestorm recording is missing a download URL.")

    request = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(request) as response, destination.open("wb") as output_handle:
            shutil.copyfileobj(response, output_handle)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Recording download failed with HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Recording download failed: {exc.reason}") from exc


def transcribe_video(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    provider: str | None = None,
    keep_audio: bool = False,
    gladia_options: dict[str, Any] | None = None,
) -> Path:
    source_video = Path(input_path).expanduser().resolve()
    if not source_video.exists():
        raise FileNotFoundError(f"Input file not found: {source_video}")

    if output_path is None:
        output_file = source_video.with_suffix(".transcript.json")
    else:
        output_file = Path(output_path).expanduser().resolve()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    api_key = _resolve_api_key()
    requested_model = provider or DEFAULT_MODEL
    actual_model = DEFAULT_MODEL

    with tempfile.TemporaryDirectory(prefix="video-transcript-") as temp_dir:
        temp_audio = Path(temp_dir) / f"{source_video.stem}.mp3"
        _extract_audio(source_video, temp_audio)
        upload_payload = _upload_audio_file(temp_audio, api_key)
        audio_url = upload_payload.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url.strip():
            raise RuntimeError("Gladia upload succeeded but no audio_url was returned.")

        gladia_request = _build_gladia_request(audio_url, gladia_options)
        started_job = _start_gladia_transcription(audio_url, api_key, gladia_options)
        job_id = started_job.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise RuntimeError("Gladia transcription request succeeded but no job id was returned.")
        gladia_result = _poll_gladia_transcription(job_id, api_key)

        extracted_audio: Path | None = None
        if keep_audio:
            extracted_audio = output_file.with_suffix(".audio.mp3")
            shutil.copy2(temp_audio, extracted_audio)

        normalized = _normalize_transcription(
            gladia_payload=gladia_result,
            source_video=source_video,
            extracted_audio=extracted_audio,
            requested_model=requested_model,
            actual_model=actual_model,
            upload_payload=upload_payload,
            gladia_request=gladia_request,
        )
        normalized["job"] = started_job
        output_file.write_text(json.dumps(normalized, indent=2, ensure_ascii=True) + "\n")

    return output_file


def transcribe_livestorm_session(
    session_id: str,
    output_path: str | Path | None = None,
    *,
    provider: str | None = None,
    keep_audio: bool = False,
    keep_video: bool = False,
    gladia_options: dict[str, Any] | None = None,
) -> Path:
    payload = _fetch_livestorm_recordings(session_id)
    recording = _select_recording(payload)
    file_name = recording.get("attributes", {}).get("file_name") or f"{session_id}.mp4"
    file_stem = Path(file_name).stem or session_id

    if output_path is None:
        output_file = Path.cwd() / f"{file_stem}.transcript.json"
    else:
        output_file = Path(output_path).expanduser().resolve()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="video-transcript-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        downloaded_video = temp_dir_path / file_name
        _download_recording(recording, downloaded_video)

        final_video_path: Path | None = None
        if keep_video:
            final_video_path = output_file.with_suffix(".mp4")
            shutil.copy2(downloaded_video, final_video_path)

        transcript_path = transcribe_video(
            input_path=downloaded_video,
            output_path=output_file,
            provider=provider,
            keep_audio=keep_audio,
            gladia_options=gladia_options,
        )

        transcript_payload = json.loads(transcript_path.read_text())
        transcript_payload["session_id"] = session_id
        transcript_payload["source_video"] = (
            str(final_video_path.resolve()) if final_video_path else transcript_payload["source_video"]
        )
        transcript_payload["recording"] = {
            "id": recording.get("id"),
            "event_id": recording.get("attributes", {}).get("event_id"),
            "session_id": recording.get("attributes", {}).get("session_id"),
            "file_type": recording.get("attributes", {}).get("file_type"),
            "mime_type": recording.get("attributes", {}).get("mime_type"),
            "file_size": recording.get("attributes", {}).get("file_size"),
            "file_name": recording.get("attributes", {}).get("file_name"),
            "url_generated_at": recording.get("attributes", {}).get("url_generated_at"),
            "url_expires_in": recording.get("attributes", {}).get("url_expires_in"),
        }
        transcript_path.write_text(json.dumps(transcript_payload, indent=2, ensure_ascii=True) + "\n")

    return output_file


def transcribe_livestorm_session_data(
    session_id: str,
    output_path: str | Path | None = None,
    *,
    provider: str | None = None,
    keep_audio: bool = False,
    keep_video: bool = False,
    gladia_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transcript_path = transcribe_livestorm_session(
        session_id=session_id,
        output_path=output_path,
        provider=provider,
        keep_audio=keep_audio,
        keep_video=keep_video,
        gladia_options=gladia_options,
    )
    transcript_payload = json.loads(transcript_path.read_text())
    transcript_payload["output_path"] = str(transcript_path.resolve())
    return transcript_payload
