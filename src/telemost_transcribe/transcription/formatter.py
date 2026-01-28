"""Output formatting for transcription results."""

import json
from datetime import datetime
from typing import Any


def format_output(
    url: str,
    transcript: str,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: int,
) -> str:
    """
    Format transcription result as JSON.

    Args:
        url: Meeting URL
        transcript: Transcribed text
        started_at: Recording start time
        ended_at: Recording end time
        duration_seconds: Duration in seconds

    Returns:
        JSON string with meeting data.
    """
    data: dict[str, Any] = {
        "url": url,
        "duration_seconds": duration_seconds,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "transcript": transcript,
    }

    return json.dumps(data, ensure_ascii=False, indent=2)
