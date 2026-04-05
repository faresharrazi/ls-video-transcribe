from __future__ import annotations

import argparse
from pathlib import Path

from .transcriber import DEFAULT_MODEL, transcribe_livestorm_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a Livestorm session recording and transcribe it into verbose JSON via Gladia."
    )
    parser.add_argument("session_id", help="Livestorm session ID.")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to the JSON output file. Defaults to <recording-file>.transcript.json",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_MODEL,
        help=f"Provider label to record in output metadata. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Keep the extracted MP3 next to the output JSON.",
    )
    parser.add_argument(
        "--keep-video",
        action="store_true",
        help="Keep the downloaded MP4 next to the output JSON.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output_path = transcribe_livestorm_session(
        session_id=args.session_id,
        output_path=Path(args.output).expanduser() if args.output else None,
        provider=args.provider,
        keep_audio=args.keep_audio,
        keep_video=args.keep_video,
    )
    print(output_path)


if __name__ == "__main__":
    main()
