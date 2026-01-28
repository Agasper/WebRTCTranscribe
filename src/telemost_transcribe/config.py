"""Configuration and environment settings."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuration error."""
    pass


@dataclass
class Config:
    """Application configuration."""
    groq_api_key: str
    ffmpeg_path: str
    # Silence removal settings
    silence_duration: float  # seconds
    silence_threshold: str  # dB (e.g., "-40dB")
    # Meeting settings
    alone_wait_seconds: int  # seconds to wait after last participant leaves
    empty_meeting_timeout: int  # seconds to wait if no one joins
    waiting_room_timeout: int  # seconds to wait in waiting room before giving up

    @classmethod
    def load(cls, ffmpeg_override: str | None = None) -> "Config":
        """Load configuration from environment."""
        load_dotenv()

        groq_api_key = os.getenv("GROQ_API_KEY")
        if not groq_api_key:
            raise ConfigError(
                "GROQ_API_KEY not set. Get your key at https://console.groq.com/ "
                "and set it via: export GROQ_API_KEY='gsk_...'"
            )

        ffmpeg_path = find_ffmpeg(ffmpeg_override)

        # Silence removal settings
        silence_duration = float(os.getenv("SILENCE_DURATION", "1"))
        silence_threshold = os.getenv("SILENCE_THRESHOLD", "-40dB")

        # Meeting settings
        alone_wait_seconds = int(os.getenv("ALONE_WAIT_SECONDS", "15"))
        empty_meeting_timeout = int(os.getenv("EMPTY_MEETING_TIMEOUT", "600"))  # 10 minutes
        waiting_room_timeout = int(os.getenv("WAITING_ROOM_TIMEOUT", "300"))  # 5 minutes

        return cls(
            groq_api_key=groq_api_key,
            ffmpeg_path=ffmpeg_path,
            silence_duration=silence_duration,
            silence_threshold=silence_threshold,
            alone_wait_seconds=alone_wait_seconds,
            empty_meeting_timeout=empty_meeting_timeout,
            waiting_room_timeout=waiting_room_timeout,
        )


def find_ffmpeg(override_path: str | None = None) -> str:
    """
    Find FFmpeg binary.

    Search order:
    1. CLI argument (override_path)
    2. FFMPEG_PATH environment variable
    3. System PATH (via shutil.which)
    4. Common installation locations

    Raises ConfigError if not found.
    """
    candidates = []

    # 1. CLI override
    if override_path:
        candidates.append(override_path)

    # 2. Environment variable
    env_path = os.getenv("FFMPEG_PATH")
    if env_path:
        candidates.append(env_path)

    # 3. System PATH
    which_path = shutil.which("ffmpeg")
    if which_path:
        candidates.append(which_path)

    # 4. Common locations
    common_paths = [
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/opt/local/bin/ffmpeg",
        # Windows paths
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    candidates.extend(common_paths)

    # Check each candidate
    for path in candidates:
        if path and _is_valid_ffmpeg(path):
            return path

    raise ConfigError(
        "FFmpeg not found. Install it via:\n"
        "  macOS: brew install ffmpeg\n"
        "  Ubuntu: sudo apt install ffmpeg\n"
        "  Windows: Download from https://ffmpeg.org/download.html\n\n"
        "Or specify path via:\n"
        "  export FFMPEG_PATH=/path/to/ffmpeg\n"
        "  --ffmpeg /path/to/ffmpeg"
    )


def _is_valid_ffmpeg(path: str) -> bool:
    """Check if path is a valid FFmpeg binary."""
    if not Path(path).exists():
        return False

    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def get_js_interceptor_path() -> Path:
    """Get path to the JavaScript RTC interceptor."""
    return Path(__file__).parent / "browser" / "js" / "rtc_interceptor.js"
