"""Command-line interface for Telemost transcription."""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

from .config import Config, ConfigError
from .browser.telemost import TelemostSession, NoParticipantsError
from .transcription.groq_transcriber import GroqTranscriber
from .transcription.formatter import format_output

console = Console()


@click.command()
@click.argument("url")
@click.option(
    "--output", "-o",
    type=click.Path(),
    help="Write JSON output to file (default: stdout)",
)
@click.option(
    "--language", "-l",
    default="ru",
    help="Audio language code (default: ru)",
)
@click.option(
    "--keep-audio",
    is_flag=True,
    help="Keep the recorded audio file after transcription",
)
@click.option(
    "--headed",
    is_flag=True,
    help="Show browser window (for debugging)",
)
@click.option(
    "--ffmpeg",
    type=click.Path(exists=True),
    help="Path to ffmpeg binary (auto-detected by default)",
)
@click.option(
    "--name",
    default="Transcriber Bot",
    help="Display name in meeting (default: Transcriber Bot)",
)
@click.option(
    "--fake-video",
    type=click.Path(exists=True),
    help="Custom video file for fake camera (Y4M or MJPEG format)",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode (save screenshots to /tmp)",
)
def main(
    url: str,
    output: str | None,
    language: str,
    keep_audio: bool,
    headed: bool,
    ffmpeg: str | None,
    name: str,
    fake_video: str | None,
    debug: bool,
):
    """
    Record and transcribe a Yandex Telemost meeting.

    URL is the Telemost meeting link (e.g., https://telemost.yandex.ru/j/12345678).

    The tool will join the meeting, record audio, and transcribe it using Groq's
    Whisper API. Output is JSON containing the transcript and metadata.

    \b
    Examples:
        # Output to console
        telemost-transcribe "https://telemost.yandex.ru/j/12345678"

        # Save to file
        telemost-transcribe "https://telemost.yandex.ru/j/12345" -o meeting.json

        # Debug with visible browser
        telemost-transcribe "https://telemost.yandex.ru/j/12345" --headed
    """
    try:
        config = Config.load(ffmpeg_override=ffmpeg)
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)

    # Run async main
    try:
        result = asyncio.run(
            _run_recording(
                url=url,
                config=config,
                language=language,
                headless=not headed,
                display_name=name,
                keep_audio=keep_audio,
                fake_video_path=fake_video,
                debug=debug,
            )
        )

        # Output result
        if output:
            Path(output).write_text(result, encoding="utf-8")
            console.print(f"[green]Output written to {output}[/green]")
        else:
            # Print to stdout
            print(result)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(130)
    except NoParticipantsError:
        console.print("[yellow]No one joined the meeting[/yellow]")
        sys.exit(2)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_recording(
    url: str,
    config: Config,
    language: str,
    headless: bool,
    display_name: str,
    keep_audio: bool,
    fake_video_path: str | None = None,
    debug: bool = False,
) -> str:
    """Run the recording and transcription pipeline."""
    audio_path = None

    try:
        # Join meeting and record
        async with TelemostSession(
            meeting_url=url,
            display_name=display_name,
            headless=headless,
            fake_video_path=fake_video_path,
            debug=debug,
            alone_wait_seconds=config.alone_wait_seconds,
            empty_meeting_timeout=config.empty_meeting_timeout,
        ) as session:
            recording = await session.join_and_record()
            audio_path = recording.audio_path

        # Transcribe
        transcriber = GroqTranscriber(
            api_key=config.groq_api_key,
            ffmpeg_path=config.ffmpeg_path,
            silence_duration=config.silence_duration,
            silence_threshold=config.silence_threshold,
        )
        transcript = await transcriber.transcribe(audio_path, language=language)

        # Format output
        return format_output(
            url=url,
            transcript=transcript.text,
            started_at=recording.started_at,
            ended_at=recording.ended_at,
            duration_seconds=recording.duration_seconds,
        )

    finally:
        # Clean up audio file unless --keep-audio
        if audio_path and audio_path.exists():
            if keep_audio:
                console.print(f"[dim]Audio saved to: {audio_path}[/dim]")
            else:
                audio_path.unlink()


if __name__ == "__main__":
    main()
