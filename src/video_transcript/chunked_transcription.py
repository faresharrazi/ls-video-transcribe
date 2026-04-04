from __future__ import annotations

import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from openai import APIError, BadRequestError, OpenAI


MAX_CHUNK_SIZE_BYTES = 20 * 1024 * 1024
CHUNK_SIZE_SAFETY_RATIO = 0.95
MIN_CHUNK_DURATION_SECONDS = 1
MAX_CHUNK_DURATION_SECONDS = 8 * 60
DEFAULT_AUDIO_BITRATE = "32k"
DEFAULT_AUDIO_SAMPLE_RATE = 16000

ErrorMessageExtractor = Callable[[Exception], str | None]


def transcribe_audio_json(
    *,
    client: OpenAI,
    audio_path: Path,
    model: str,
    ffmpeg_executable: str,
    error_message_extractor: ErrorMessageExtractor,
    language: str | None = None,
) -> dict[str, Any]:
    audio_size_bytes = audio_path.stat().st_size
    duration_seconds = _probe_audio_duration_seconds(ffmpeg_executable, audio_path)

    if audio_size_bytes <= MAX_CHUNK_SIZE_BYTES and duration_seconds <= MAX_CHUNK_DURATION_SECONDS:
        response = _transcribe_chunk(
            client=client,
            chunk_path=audio_path,
            model=model,
            error_message_extractor=error_message_extractor,
            language=language,
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        payload["chunked"] = False
        payload["chunk_size_limit_bytes"] = MAX_CHUNK_SIZE_BYTES
        payload["chunk_duration_limit_seconds"] = MAX_CHUNK_DURATION_SECONDS
        return payload

    chunks = _split_audio_into_chunks(
        ffmpeg_executable=ffmpeg_executable,
        audio_path=audio_path,
        duration_seconds=duration_seconds,
    )

    chunk_payloads: list[dict[str, Any]] = []
    chunk_metadata: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        response = _transcribe_chunk(
            client=client,
            chunk_path=chunk["path"],
            model=model,
            error_message_extractor=error_message_extractor,
            language=language,
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        chunk_payloads.append(payload)
        chunk_metadata.append(
            {
                "index": index,
                "start_seconds": chunk["start_seconds"],
                "end_seconds": chunk["end_seconds"],
                "duration_seconds": chunk["duration_seconds"],
                "size_bytes": chunk["size_bytes"],
                "text": (payload.get("text") or "").strip(),
                "language": payload.get("language"),
                "usage": payload.get("usage"),
            }
        )

    language_value = next((payload.get("language") for payload in chunk_payloads if payload.get("language")), None)
    duration_value = sum(
        float(payload.get("duration", 0.0))
        for payload in chunk_payloads
        if isinstance(payload.get("duration"), (int, float))
    )

    return {
        "text": _join_chunk_text(chunk_payloads),
        "language": language_value,
        "duration": duration_value or duration_seconds,
        "usage": _merge_usage_payloads([payload.get("usage") for payload in chunk_payloads]),
        "chunked": True,
        "chunk_count": len(chunk_metadata),
        "chunk_size_limit_bytes": MAX_CHUNK_SIZE_BYTES,
        "chunk_duration_limit_seconds": MAX_CHUNK_DURATION_SECONDS,
        "chunks": chunk_metadata,
    }


def _transcribe_chunk(
    *,
    client: OpenAI,
    chunk_path: Path,
    model: str,
    error_message_extractor: ErrorMessageExtractor,
    language: str | None = None,
):
    request_kwargs: dict[str, Any] = {
        "file": chunk_path.open("rb"),
        "model": model,
        "response_format": "json",
    }
    if language:
        request_kwargs["language"] = language

    try:
        with request_kwargs["file"]:
            return client.audio.transcriptions.create(**request_kwargs)
    except BadRequestError as exc:
        message = error_message_extractor(exc)
        raise RuntimeError(message or "OpenAI rejected the transcription request.") from exc
    except APIError as exc:
        raise RuntimeError(f"OpenAI transcription failed: {exc}") from exc


def _probe_audio_duration_seconds(ffmpeg_executable: str, audio_path: Path) -> float:
    command = [ffmpeg_executable, "-i", str(audio_path)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError("Unable to determine extracted audio duration for chunking.")

    hours, minutes, seconds = match.groups()
    return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)


def _split_audio_into_chunks(
    *,
    ffmpeg_executable: str,
    audio_path: Path,
    duration_seconds: float,
) -> list[dict[str, Any]]:
    file_size_bytes = audio_path.stat().st_size
    size_based_chunk_duration = max(
        MIN_CHUNK_DURATION_SECONDS,
        math.floor(duration_seconds * (MAX_CHUNK_SIZE_BYTES / file_size_bytes) * CHUNK_SIZE_SAFETY_RATIO),
    )
    estimated_chunk_duration = min(float(MAX_CHUNK_DURATION_SECONDS), float(size_based_chunk_duration))

    chunks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="video-transcript-chunks-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        start_seconds = 0.0
        chunk_index = 0

        while start_seconds < duration_seconds:
            chunk_index += 1
            remaining_seconds = duration_seconds - start_seconds
            chunk_duration = min(float(estimated_chunk_duration), remaining_seconds)
            chunk_path = temp_dir_path / f"{audio_path.stem}.chunk-{chunk_index:03d}.mp3"

            chunk_duration = _write_chunk_with_size_cap(
                ffmpeg_executable=ffmpeg_executable,
                source_audio=audio_path,
                chunk_path=chunk_path,
                start_seconds=start_seconds,
                initial_duration_seconds=chunk_duration,
            )

            final_chunk_path = audio_path.parent / chunk_path.name
            chunk_path.replace(final_chunk_path)
            chunk_size_bytes = final_chunk_path.stat().st_size
            end_seconds = min(duration_seconds, start_seconds + chunk_duration)

            chunks.append(
                {
                    "path": final_chunk_path,
                    "start_seconds": round(start_seconds, 3),
                    "end_seconds": round(end_seconds, 3),
                    "duration_seconds": round(chunk_duration, 3),
                    "size_bytes": chunk_size_bytes,
                }
            )
            start_seconds = end_seconds

    return chunks


def _write_chunk_with_size_cap(
    *,
    ffmpeg_executable: str,
    source_audio: Path,
    chunk_path: Path,
    start_seconds: float,
    initial_duration_seconds: float,
) -> float:
    duration_seconds = max(float(MIN_CHUNK_DURATION_SECONDS), initial_duration_seconds)

    while True:
        _export_audio_chunk(
            ffmpeg_executable=ffmpeg_executable,
            source_audio=source_audio,
            chunk_path=chunk_path,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
        )
        if chunk_path.stat().st_size <= MAX_CHUNK_SIZE_BYTES:
            return duration_seconds
        if duration_seconds <= MIN_CHUNK_DURATION_SECONDS:
            raise RuntimeError("Unable to reduce audio chunk below the 20 MB upload cap.")
        duration_seconds = max(
            float(MIN_CHUNK_DURATION_SECONDS),
            math.floor(duration_seconds * CHUNK_SIZE_SAFETY_RATIO),
        )


def _export_audio_chunk(
    *,
    ffmpeg_executable: str,
    source_audio: Path,
    chunk_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    command = [
        ffmpeg_executable,
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(source_audio),
        "-ac",
        "1",
        "-ar",
        str(DEFAULT_AUDIO_SAMPLE_RATE),
        "-b:a",
        DEFAULT_AUDIO_BITRATE,
        str(chunk_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "Unknown ffmpeg error"
        raise RuntimeError(f"Audio chunking failed: {stderr}") from exc


def _join_chunk_text(chunk_payloads: list[dict[str, Any]]) -> str:
    parts = [(payload.get("text") or "").strip() for payload in chunk_payloads]
    return " ".join(part for part in parts if part).strip()


def _merge_usage_payloads(usages: list[object]) -> object:
    valid_usages = [usage for usage in usages if isinstance(usage, dict)]
    if not valid_usages:
        return None

    totals: dict[str, Any] = {}
    for usage in valid_usages:
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value

    return {
        "total": totals or None,
        "chunks": valid_usages,
    }
