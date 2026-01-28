"""Groq API transcription using Whisper Large V3."""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from groq import Groq
from rich.console import Console

console = Console()


@dataclass
class TranscriptResult:
    """Result of transcription."""
    text: str
    language: str
    duration_seconds: float


class GroqTranscriber:
    """Transcribes audio using Groq's Whisper API."""

    MODEL = "whisper-large-v3-turbo"
    MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

    def __init__(
        self,
        api_key: str,
        ffmpeg_path: str,
        silence_duration: float = 1.0,
        silence_threshold: str = "-40dB",
    ):
        self.client = Groq(api_key=api_key)
        self.ffmpeg_path = ffmpeg_path
        self.silence_duration = silence_duration
        self.silence_threshold = silence_threshold

    async def transcribe(
        self,
        audio_path: Path,
        language: str = "ru",
    ) -> TranscriptResult:
        """
        Transcribe audio file.

        Args:
            audio_path: Path to audio file (WebM, MP3, WAV, etc.)
            language: Language code (e.g., "ru", "en")

        Returns:
            TranscriptResult with text and metadata.
        """
        # Convert to MP3 for Groq (better compatibility)
        mp3_path = self._convert_to_mp3(audio_path)

        try:
            # Check file size
            file_size = mp3_path.stat().st_size
            if file_size > self.MAX_FILE_SIZE:
                console.print(
                    f"[yellow]Warning: Audio file is {file_size / 1024 / 1024:.1f} MB, "
                    f"may exceed Groq limits[/yellow]"
                )

            console.print(f"[dim]Sending to Groq API ({self.MODEL})...[/dim]")

            with open(mp3_path, "rb") as audio_file:
                transcription = self.client.audio.transcriptions.create(
                    file=(mp3_path.name, audio_file.read()),
                    model=self.MODEL,
                    language=language,
                    response_format="verbose_json",
                    prompt="Это запись рабочей видеоконференции. Транскрибируй только то, что реально было сказано.",
                )

            # Extract text
            text = transcription.text
            detected_language = getattr(transcription, "language", language)
            duration = getattr(transcription, "duration", 0.0)

            console.print(f"[green]Transcription complete: {len(text)} characters[/green]")

            return TranscriptResult(
                text=text.strip(),
                language=detected_language,
                duration_seconds=duration,
            )

        finally:
            # Clean up temp MP3 file
            if mp3_path != audio_path and mp3_path.exists():
                mp3_path.unlink()

    def _convert_to_mp3(self, input_path: Path) -> Path:
        """Convert audio to MP3 format."""
        if input_path.suffix.lower() == ".mp3":
            return input_path

        console.print(f"[dim]Converting {input_path.suffix} to MP3...[/dim]")

        output_path = Path(tempfile.mktemp(suffix=".mp3", prefix="telemost_"))

        cmd = [
            self.ffmpeg_path,
            "-i", str(input_path),
            "-vn",  # No video
            "-af", f"silenceremove=stop_periods=-1:stop_duration={self.silence_duration}:stop_threshold={self.silence_threshold}",  # Remove silence
            "-acodec", "libmp3lame",
            "-ab", "128k",  # Bitrate
            "-ar", "16000",  # Sample rate (Whisper prefers 16kHz)
            "-ac", "1",  # Mono
            "-y",  # Overwrite
            str(output_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="replace")
                raise RuntimeError(f"FFmpeg conversion failed: {error}")

            console.print(f"[dim]Converted to MP3: {output_path.stat().st_size / 1024:.1f} KB[/dim]")
            return output_path

        except subprocess.TimeoutExpired:
            raise RuntimeError("FFmpeg conversion timed out")
