# Telemost Transcribe

CLI tool for recording and transcribing Yandex Telemost video conferences.

## Installation

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install package
pip install -e .

# Install Playwright browser
playwright install chromium
```

## Configuration

Get a Groq API key:
1. Go to https://console.groq.com/
2. Sign up (free)
3. API Keys → Create new key

```bash
export GROQ_API_KEY="gsk_..."
```

## Usage

```bash
# Output to console
python -m telemost_transcribe "https://telemost.yandex.ru/j/12345678"

# Save to file
python -m telemost_transcribe "https://telemost.yandex.ru/j/12345" -o meeting.json

# Debug with visible browser
python -m telemost_transcribe "https://telemost.yandex.ru/j/12345" --headed --debug

# Custom bot name
python -m telemost_transcribe "https://telemost.yandex.ru/j/12345" --name "My Bot"

# Custom avatar video
python -m telemost_transcribe "https://telemost.yandex.ru/j/12345" --fake-video avatar.y4m
```

## CLI Arguments

| Argument | Short | Default | Description |
|----------|-------|---------|-------------|
| `URL` | | (required) | Telemost meeting link |
| `--output` | `-o` | stdout | Write JSON output to file |
| `--language` | `-l` | `ru` | Audio language code |
| `--name` | | `Transcriber Bot` | Display name in meeting |
| `--fake-video` | | | Custom video file for camera (Y4M/MJPEG) |
| `--ffmpeg` | | auto-detect | Path to FFmpeg binary |
| `--headed` | | | Show browser window |
| `--debug` | | | Save debug screenshots to /tmp |
| `--keep-audio` | | | Keep recorded audio file after transcription |

## Output Format

```json
{
  "url": "https://telemost.yandex.ru/j/12345678",
  "duration_seconds": 1847,
  "started_at": "2026-01-28T10:00:00Z",
  "ended_at": "2026-01-28T10:30:47Z",
  "transcript": "Transcription text..."
}
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error |
| 2 | No one joined the meeting |
| 3 | Not admitted from waiting room |
| 130 | Interrupted by user (Ctrl+C) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | (required) | Groq API key |
| `FFMPEG_PATH` | auto-detect | Path to FFmpeg binary |
| `SILENCE_DURATION` | `1` | Min silence duration (seconds) to remove |
| `SILENCE_THRESHOLD` | `-40dB` | Audio level threshold for silence |
| `ALONE_WAIT_SECONDS` | `15` | Seconds to wait after last participant leaves |
| `EMPTY_MEETING_TIMEOUT` | `600` | Seconds to wait if no one joins (10 min) |
| `WAITING_ROOM_TIMEOUT` | `300` | Seconds to wait in waiting room (5 min) |

## Requirements

- Python 3.10+
- FFmpeg (`brew install ffmpeg` on macOS)
- Groq API key (free tier available)
