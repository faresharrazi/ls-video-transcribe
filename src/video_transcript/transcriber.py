from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from dotenv import load_dotenv
from openai import APIError, BadRequestError, OpenAI


DEFAULT_MODEL = "gpt-4o-mini-transcribe"
TIMESTAMPED_MODEL = "whisper-1"
NON_TIMESTAMPED_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_AUDIO_BITRATE = "32k"
DEFAULT_AUDIO_SAMPLE_RATE = 16000
LIVESTORM_API_BASE = "https://api.livestorm.co/v1"


def _resolve_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing OpenAI API key. Set OPENAI_KEY or OPENAI_API_KEY in your environment or .env file."
        )
    return api_key


def _resolve_livestorm_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("LS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing Livestorm API key. Set LS_API_KEY in your environment or .env file.")
    return api_key


def _ffmpeg_executable() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


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


def _resolve_transcription_request(
    requested_model: str | None,
    timestamped: bool,
    include_word_timestamps: bool,
) -> dict[str, Any]:
    if timestamped:
        timestamp_granularities = ["segment"]
        if include_word_timestamps:
            timestamp_granularities.append("word")
        return {
            "requested_model": requested_model or TIMESTAMPED_MODEL,
            "actual_model": TIMESTAMPED_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": timestamp_granularities,
            "timestamped": True,
        }

    return {
        "requested_model": requested_model or NON_TIMESTAMPED_MODEL,
        "actual_model": NON_TIMESTAMPED_MODEL,
        "response_format": "json",
        "timestamp_granularities": None,
        "timestamped": False,
    }


def _normalize_transcription(
    response: Any,
    source_video: Path,
    extracted_audio: Path | None,
    requested_model: str,
    actual_model: str,
    timestamped: bool,
    session_id: str | None = None,
    recording: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    segments = payload.get("segments", []) or []
    words = payload.get("words", []) or []

    result = {
        "source_video": str(source_video.resolve()),
        "extracted_audio": str(extracted_audio.resolve()) if extracted_audio else None,
        "model": actual_model,
        "requested_model": requested_model,
        "timestamped": timestamped,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "language": payload.get("language"),
        "duration_seconds": payload.get("duration"),
        "text": payload.get("text", ""),
        "usage": payload.get("usage"),
    }
    if timestamped:
        result["segments"] = [
            {
                "id": segment.get("id"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "text": (segment.get("text") or "").strip(),
            }
            for segment in segments
        ]
        result["words"] = [
            {
                "word": word.get("word"),
                "start": word.get("start"),
                "end": word.get("end"),
            }
            for word in words
        ]
    if session_id:
        result["session_id"] = session_id
    if recording:
        result["recording"] = {
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
    return result


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
    model: str | None = None,
    timestamped: bool = True,
    include_word_timestamps: bool = False,
    keep_audio: bool = False,
    language: str | None = None,
) -> Path:
    source_video = Path(input_path).expanduser().resolve()
    if not source_video.exists():
        raise FileNotFoundError(f"Input file not found: {source_video}")

    if output_path is None:
        output_file = source_video.with_suffix(".transcript.json")
    else:
        output_file = Path(output_path).expanduser().resolve()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    client = OpenAI(api_key=_resolve_api_key())
    transcription_request = _resolve_transcription_request(model, timestamped, include_word_timestamps)

    with tempfile.TemporaryDirectory(prefix="video-transcript-") as temp_dir:
        temp_audio = Path(temp_dir) / f"{source_video.stem}.mp3"
        _extract_audio(source_video, temp_audio)

        request_kwargs: dict[str, Any] = {
            "model": transcription_request["actual_model"],
            "response_format": transcription_request["response_format"],
        }
        if transcription_request["timestamp_granularities"] is not None:
            request_kwargs["timestamp_granularities"] = transcription_request["timestamp_granularities"]
        if language:
            request_kwargs["language"] = language

        with temp_audio.open("rb") as audio_handle:
            request_kwargs["file"] = audio_handle
            try:
                response = client.audio.transcriptions.create(**request_kwargs)
            except BadRequestError as exc:
                message = None
                if getattr(exc, "body", None):
                    message = exc.body.get("error", {}).get("message")
                raise RuntimeError(message or "OpenAI rejected the transcription request.") from exc
            except APIError as exc:
                raise RuntimeError(f"OpenAI transcription failed: {exc}") from exc

        extracted_audio: Path | None = None
        if keep_audio:
            extracted_audio = output_file.with_suffix(".audio.mp3")
            shutil.copy2(temp_audio, extracted_audio)

        normalized = _normalize_transcription(
            response=response,
            source_video=source_video,
            extracted_audio=extracted_audio,
            requested_model=transcription_request["requested_model"],
            actual_model=transcription_request["actual_model"],
            timestamped=transcription_request["timestamped"],
        )
        output_file.write_text(json.dumps(normalized, indent=2, ensure_ascii=True) + "\n")

    return output_file


def transcribe_livestorm_session(
    session_id: str,
    output_path: str | Path | None = None,
    *,
    model: str | None = None,
    timestamped: bool = True,
    include_word_timestamps: bool = False,
    keep_audio: bool = False,
    keep_video: bool = False,
    language: str | None = None,
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
            model=model,
            timestamped=timestamped,
            include_word_timestamps=include_word_timestamps,
            keep_audio=keep_audio,
            language=language,
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
    model: str | None = None,
    timestamped: bool = True,
    include_word_timestamps: bool = False,
    keep_audio: bool = False,
    keep_video: bool = False,
    language: str | None = None,
) -> dict[str, Any]:
    transcript_path = transcribe_livestorm_session(
        session_id=session_id,
        output_path=output_path,
        model=model,
        timestamped=timestamped,
        include_word_timestamps=include_word_timestamps,
        keep_audio=keep_audio,
        keep_video=keep_video,
        language=language,
    )
    transcript_payload = json.loads(transcript_path.read_text())
    transcript_payload["output_path"] = str(transcript_path.resolve())
    return transcript_payload
