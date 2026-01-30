"""Groq API transcription using Whisper Large V3."""

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from groq import Groq
from rich.console import Console

console = Console()

# Language-specific prompts for better transcription quality
TRANSCRIPTION_PROMPTS = {
    "ru": "Это запись рабочей видеоконференции. Транскрибируй только то, что реально было сказано.",
    "en": "This is a work video conference recording. Transcribe only what was actually said.",
    "default": "Transcribe the audio accurately.",
}


@dataclass
class TranscriptResult:
    """Result of transcription."""
    text: str
    language: str
    duration_seconds: float


class GroqTranscriber:
    """Transcribes audio using Groq's Whisper API."""

    MODEL = "whisper-large-v3-turbo"
    MAX_FILE_SIZE = 24 * 1024 * 1024  # 24 MB (safe margin under 25MB limit)
    TARGET_SEGMENT_SIZE = 20 * 1024 * 1024  # Target 20 MB per segment

    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 2  # seconds

    def __init__(
        self,
        api_key: str,
        ffmpeg_path: str,
        silence_duration: float = 1.0,
        silence_threshold: str = "-40dB",
    ):
        self.client = Groq(api_key=api_key)
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = self._get_ffprobe_path(ffmpeg_path)
        self.silence_duration = silence_duration
        self.silence_threshold = silence_threshold

    @staticmethod
    def _get_ffprobe_path(ffmpeg_path: str) -> str:
        """Get ffprobe path from ffmpeg path."""
        ffmpeg_dir = Path(ffmpeg_path).parent
        ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        return str(ffmpeg_dir / ffprobe_name)

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
        mp3_path = await self._convert_to_mp3(audio_path)

        try:
            file_size = mp3_path.stat().st_size

            if file_size > self.MAX_FILE_SIZE:
                console.print(
                    f"[yellow]Audio file is {file_size / 1024 / 1024:.1f} MB, "
                    f"splitting into segments...[/yellow]"
                )
                return await self._transcribe_segmented(mp3_path, language)
            else:
                return await self._transcribe_single(mp3_path, language)

        finally:
            # Clean up temp MP3 file
            if mp3_path != audio_path and mp3_path.exists():
                mp3_path.unlink()

    async def _transcribe_single(
        self,
        mp3_path: Path,
        language: str,
    ) -> TranscriptResult:
        """Transcribe a single audio file with retry logic."""
        console.print(f"[dim]Sending to Groq API ({self.MODEL})...[/dim]")

        prompt = TRANSCRIPTION_PROMPTS.get(language, TRANSCRIPTION_PROMPTS["default"])
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                with open(mp3_path, "rb") as audio_file:
                    transcription = self.client.audio.transcriptions.create(
                        file=(mp3_path.name, audio_file.read()),
                        model=self.MODEL,
                        language=language,
                        response_format="verbose_json",
                        prompt=prompt,
                    )

                text = transcription.text
                detected_language = getattr(transcription, "language", language)
                duration = getattr(transcription, "duration", 0.0)

                console.print(f"[green]Transcription complete: {len(text)} characters[/green]")

                return TranscriptResult(
                    text=text.strip(),
                    language=detected_language,
                    duration_seconds=duration,
                )

            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAY * (2 ** attempt)  # Exponential backoff
                    console.print(f"[yellow]Groq API error, retrying in {delay}s: {e}[/yellow]")
                    await asyncio.sleep(delay)
                else:
                    console.print(f"[red]Groq API failed after {self.MAX_RETRIES} attempts[/red]")

        raise RuntimeError(f"Transcription failed: {last_error}")

    async def _transcribe_segmented(
        self,
        mp3_path: Path,
        language: str,
    ) -> TranscriptResult:
        """Split audio by silence and transcribe each segment."""
        # Get audio duration
        duration = await self._get_audio_duration(mp3_path)
        file_size = mp3_path.stat().st_size

        # Calculate approximate segment duration based on file size
        bytes_per_second = file_size / duration if duration > 0 else 16000
        target_segment_duration = self.TARGET_SEGMENT_SIZE / bytes_per_second

        # Detect silence points
        silence_points = await self._detect_silence(mp3_path)
        console.print(f"[dim]Found {len(silence_points)} silence points[/dim]")

        # Create segments
        segments = await self._create_segments(
            mp3_path, silence_points, duration, target_segment_duration
        )

        if not segments:
            # Fallback: split by fixed duration
            console.print("[yellow]No suitable silence points, splitting by duration[/yellow]")
            segments = await self._split_by_duration(mp3_path, target_segment_duration, duration)

        console.print(f"[dim]Processing {len(segments)} segments...[/dim]")

        # Transcribe each segment
        all_texts = []
        total_duration = 0.0
        detected_language = language

        try:
            for i, segment_path in enumerate(segments, 1):
                segment_size = segment_path.stat().st_size / 1024 / 1024
                console.print(
                    f"[dim]Segment {i}/{len(segments)} ({segment_size:.1f} MB)...[/dim]"
                )

                result = await self._transcribe_single(segment_path, language)
                if result.text:
                    all_texts.append(result.text)
                total_duration += result.duration_seconds
                detected_language = result.language

        finally:
            # Clean up segment files
            for segment_path in segments:
                if segment_path.exists():
                    segment_path.unlink()

        combined_text = " ".join(all_texts)
        console.print(
            f"[green]Combined transcription: {len(combined_text)} characters "
            f"from {len(segments)} segments[/green]"
        )

        return TranscriptResult(
            text=combined_text.strip(),
            language=detected_language,
            duration_seconds=total_duration,
        )

    async def _get_audio_duration(self, audio_path: Path) -> float:
        """Get audio duration in seconds using ffprobe."""
        cmd = [
            self.ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]

        try:
            stdout, _, returncode = await self._run_subprocess(cmd, timeout=30)
            if returncode == 0:
                return float(stdout.strip())
        except (asyncio.TimeoutError, ValueError):
            pass

        return 0.0

    async def _run_subprocess(
        self,
        cmd: list[str],
        timeout: int,
    ) -> tuple[str, str, int]:
        """Run subprocess asynchronously."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

    async def _detect_silence(self, audio_path: Path) -> list[float]:
        """Detect silence points in audio file."""
        cmd = [
            self.ffmpeg_path,
            "-i", str(audio_path),
            "-af", f"silencedetect=noise={self.silence_threshold}:d=0.5",
            "-f", "null",
            "-",
        ]

        try:
            _, stderr, _ = await self._run_subprocess(cmd, timeout=300)

            # Parse silence_end timestamps
            silence_points = []
            for match in re.finditer(r"silence_end: ([\d.]+)", stderr):
                silence_points.append(float(match.group(1)))

            return silence_points

        except asyncio.TimeoutError:
            return []

    async def _create_segments(
        self,
        audio_path: Path,
        silence_points: list[float],
        total_duration: float,
        target_duration: float,
    ) -> list[Path]:
        """Create audio segments at silence boundaries."""
        if not silence_points:
            return []

        segments = []
        segment_start = 0.0

        for silence_point in silence_points:
            segment_length = silence_point - segment_start

            # Split if segment is long enough
            if segment_length >= target_duration * 0.8:
                segment_path = await self._extract_segment(
                    audio_path, segment_start, silence_point, len(segments)
                )
                if segment_path and segment_path.stat().st_size < self.MAX_FILE_SIZE:
                    segments.append(segment_path)
                    segment_start = silence_point

        # Handle remaining audio
        if segment_start < total_duration - 1:
            segment_path = await self._extract_segment(
                audio_path, segment_start, total_duration, len(segments)
            )
            if segment_path:
                # If last segment is too large, we need to split it further
                if segment_path.stat().st_size > self.MAX_FILE_SIZE:
                    segment_path.unlink()
                    # Recursively split the remaining part
                    remaining_segments = await self._split_by_duration(
                        audio_path, target_duration,
                        total_duration, segment_start
                    )
                    segments.extend(remaining_segments)
                else:
                    segments.append(segment_path)

        return segments

    async def _split_by_duration(
        self,
        audio_path: Path,
        target_duration: float,
        total_duration: float,
        start_offset: float = 0.0,
    ) -> list[Path]:
        """Split audio by fixed duration intervals."""
        segments = []
        current_start = start_offset

        while current_start < total_duration:
            end_time = min(current_start + target_duration, total_duration)
            segment_path = await self._extract_segment(
                audio_path, current_start, end_time, len(segments)
            )
            if segment_path:
                segments.append(segment_path)
            current_start = end_time

        return segments

    async def _extract_segment(
        self,
        audio_path: Path,
        start_time: float,
        end_time: float,
        index: int,
    ) -> Path | None:
        """Extract a segment from audio file."""
        output_path = Path(
            tempfile.mktemp(suffix=f"_seg{index:03d}.mp3", prefix="telemost_")
        )

        duration = end_time - start_time
        if duration < 0.5:
            return None

        cmd = [
            self.ffmpeg_path,
            "-i", str(audio_path),
            "-ss", str(start_time),
            "-t", str(duration),
            "-acodec", "copy",
            "-y",
            str(output_path),
        ]

        try:
            _, _, returncode = await self._run_subprocess(cmd, timeout=60)
            if returncode == 0 and output_path.exists():
                return output_path
        except asyncio.TimeoutError:
            pass

        # Cleanup on failure
        if output_path.exists():
            output_path.unlink()
        return None

    async def _convert_to_mp3(self, input_path: Path) -> Path:
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
            _, stderr, returncode = await self._run_subprocess(cmd, timeout=300)

            if returncode != 0:
                raise RuntimeError(f"FFmpeg conversion failed: {stderr}")

            console.print(f"[dim]Converted to MP3: {output_path.stat().st_size / 1024:.1f} KB[/dim]")
            return output_path

        except asyncio.TimeoutError:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError("FFmpeg conversion timed out")
