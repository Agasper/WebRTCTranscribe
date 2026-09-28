"""Telemost browser automation using Playwright."""

import asyncio
import base64
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Frame, Page
from rich.console import Console

from ..config import get_js_interceptor_path


console = Console()

# Timing constants (seconds)
PAGE_LOAD_WAIT = 3
AUDIO_TRACKS_WAIT = 5
AFTER_CONTINUE_WAIT = 3
AFTER_JOIN_WAIT = 2
STATUS_CHECK_INTERVAL = 5
EVALUATE_TIMEOUT = 30  # a single JS call into the page must not hang the bot

# Since Telemost 3.0 the pre-join screen and the call UI live in an iframe
# (https://telemost.yandex.ru/private-join/<id>?...), not in the top page.
CALL_FRAME_PATH = "/private-join/"

# Supported meeting URL hosts
ALLOWED_HOSTS = {"telemost.yandex.ru", "telemost.yandex.com"}


class NoParticipantsError(Exception):
    """Raised when no one joins the meeting within timeout."""
    pass


class WaitingRoomTimeoutError(Exception):
    """Raised when stuck in waiting room and not admitted."""
    pass


class JoinError(Exception):
    """Raised when the bot could not get into the call (page layout changed, etc.)."""
    pass


@dataclass
class RecordingResult:
    """Result of a recording session."""
    audio_path: Path
    started_at: datetime
    ended_at: datetime
    duration_seconds: int


class TelemostSession:
    """
    Manages a Telemost meeting session.

    Usage:
        async with TelemostSession(url, headless=False) as session:
            result = await session.join_and_record()
    """

    def __init__(
        self,
        meeting_url: str,
        display_name: str = "Transcriber Bot",
        headless: bool = True,
        on_status: Callable[[str], None] | None = None,
        debug: bool = False,
        fake_video_path: str | None = None,
        alone_wait_seconds: int = 15,
        empty_meeting_timeout: int = 600,
        waiting_room_timeout: int = 300,
        join_step_timeout: int = 60,
        max_call_duration: int = 6 * 3600,
        lost_call_timeout: int = 300,
    ):
        # Validate URL
        parsed = urlparse(meeting_url)
        if parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(
                f"Invalid meeting URL host: {parsed.hostname}. "
                f"Expected one of: {', '.join(ALLOWED_HOSTS)}"
            )
        self.meeting_url = meeting_url
        self.display_name = display_name
        self.headless = headless
        self.on_status = on_status or (lambda x: None)
        self.debug = debug
        self.fake_video_path = fake_video_path
        self.alone_wait_seconds = alone_wait_seconds
        self.empty_meeting_timeout = empty_meeting_timeout
        self.waiting_room_timeout = waiting_room_timeout
        self.join_step_timeout = join_step_timeout
        self.max_call_duration = max_call_duration
        self.lost_call_timeout = lost_call_timeout

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # Frame with the call UI and the RTC interceptor we record from
        self._call_frame: Frame | None = None
        # Audio is written here chunk by chunk while recording
        self._audio_path: Path | None = None
        self._audio_file = None
        self._chunks_saved = 0

    async def __aenter__(self) -> "TelemostSession":
        await self._setup_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._cleanup()

    async def _setup_browser(self):
        """Initialize browser with required permissions."""
        self._playwright = await async_playwright().start()

        # Browser args for fake media devices
        browser_args = [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--autoplay-policy=no-user-gesture-required",
        ]

        # Use custom video file if provided
        if self.fake_video_path:
            browser_args.append(f"--use-file-for-fake-video-capture={self.fake_video_path}")
            self._log(f"Using custom video: {self.fake_video_path}")

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=browser_args,
        )

        self._context = await self._browser.new_context(
            permissions=["microphone", "camera"],
            ignore_https_errors=True,
            locale="ru-RU",
            viewport={"width": 1280, "height": 720},
        )

        self._page = await self._context.new_page()

        # Recorded chunks come straight to disk (available in every frame)
        await self._page.expose_function("__rtcSaveChunk", self._save_chunk)

        # Inject RTC interceptor script before any page loads
        js_interceptor = get_js_interceptor_path().read_text()
        await self._page.add_init_script(js_interceptor)

        self._log("Browser initialized")

    def _save_chunk(self, data: str):
        """Append a recorded chunk (base64) to the audio file."""
        if self._audio_file is None:
            self._audio_file = tempfile.NamedTemporaryFile(
                suffix=".webm",
                delete=False,
                prefix="telemost_",
            )
            self._audio_path = Path(self._audio_file.name)
        self._audio_file.write(base64.b64decode(data))
        self._audio_file.flush()
        self._chunks_saved += 1

    async def _cleanup(self):
        """Clean up browser resources."""
        if self._audio_file:
            self._audio_file.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _log(self, message: str):
        """Log status message."""
        console.print(f"[dim][Telemost][/dim] {message}")
        self.on_status(message)

    async def _screenshot(self, name: str):
        """Save debug screenshot."""
        if self.debug:
            path = f"/tmp/telemost_debug_{name}.png"
            await self._page.screenshot(path=path)
            self._log(f"Screenshot saved: {path}")

    async def join_and_record(self, wait_for_end: bool = True) -> RecordingResult:
        """
        Join the meeting and record audio.

        Args:
            wait_for_end: If True, wait for meeting to end. Otherwise, record until Ctrl+C.

        Returns:
            RecordingResult with audio file path and timing info.
        """
        # Navigate to meeting
        self._log(f"Navigating to {self.meeting_url}")
        await self._page.goto(self.meeting_url, wait_until="domcontentloaded")

        # Wait for page to fully load
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await self._screenshot("01_loaded")

        # Close the "Big update in Telemost" onboarding popup, it covers the page
        await self._dismiss_onboarding()

        # Handle "Continue in browser" prompt
        await self._click_continue_in_browser()
        await self._screenshot("02_after_continue")

        # Find the pre-join screen (raises JoinError if it never shows up)
        await self._wait_for_prejoin()

        # Handle pre-join flow (name input, media settings)
        await self._handle_prejoin()
        await self._screenshot("03_after_prejoin")

        # Mute microphone and camera BEFORE joining
        await self._mute_prejoin()
        await self._screenshot("04_after_mute")

        # Try to join the meeting
        await self._click_join()
        await self._screenshot("05_after_join_click")

        # Wait for connection and start recording
        await self._wait_for_connection()
        await self._screenshot("06_connected")

        # Wait a bit for audio tracks to be established
        self._log("Waiting for audio tracks...")
        await asyncio.sleep(AUDIO_TRACKS_WAIT)

        started_at = datetime.now(timezone.utc)
        await self._start_recording()

        # Wait for call to end
        if wait_for_end:
            await self._wait_for_end()
        else:
            self._log("Recording... Press Ctrl+C to stop")
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

        ended_at = datetime.now(timezone.utc)

        # Get recorded audio
        audio_path = await self._get_recording()

        duration = int((ended_at - started_at).total_seconds())
        self._log(f"Recording complete: {duration} seconds")

        return RecordingResult(
            audio_path=audio_path,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
        )

    def _frame(self) -> Frame:
        """Frame with the call UI: the Telemost 3.0 iframe, or the top page for the old layout."""
        if self._call_frame and not self._call_frame.is_detached():
            return self._call_frame
        for frame in self._page.frames:
            if frame is not self._page.main_frame and CALL_FRAME_PATH in frame.url:
                return frame
        return self._page.main_frame

    async def _eval(self, expression: str, timeout: float = EVALUATE_TIMEOUT):
        """Evaluate JS in the call frame with a timeout, so a frozen page cannot hang the bot."""
        return await asyncio.wait_for(self._frame().evaluate(expression), timeout)

    async def _rtc_status(self) -> dict:
        return await self._eval("window.__rtcGetStatus ? window.__rtcGetStatus() : {}")

    async def _find_visible(self, frame: Frame, selectors: list[str]):
        """Return the first visible element matching any selector in the frame, or None."""
        for selector in selectors:
            try:
                for element in await frame.query_selector_all(selector):
                    if await element.is_visible():
                        return element
            except Exception:
                continue
        return None

    async def _dismiss_onboarding(self):
        """Close the 'Big update in Telemost' popup shown on first visit."""
        button = await self._find_visible(self._page.main_frame, [
            '[data-testid="telemost-3-onboarding-confirm"]',
            '[data-testid="telemost-3-onboarding-close"]',
        ])
        if button:
            try:
                await button.click(timeout=5000)
                self._log("Closed onboarding popup")
                await asyncio.sleep(1)
            except Exception as e:
                self._log(f"Failed to close onboarding popup: {e}")

    async def _mute_prejoin(self):
        """Mute microphone and camera on the pre-join screen BEFORE joining."""
        self._log("Muting microphone (pre-join)...")
        frame = self._frame()

        # Telemost 3.0 has separate buttons for "turn off" and "turn on"
        mic_button = await self._find_visible(frame, [
            '[data-testid="turn-off-mic-button"]',
            'button[title="Выключить микрофон"]',
        ])
        if mic_button:
            await mic_button.click()
            self._log("Microphone muted")
        elif await self._find_visible(frame, ['[data-testid="turn-on-mic-button"]', 'button[title="Включить микрофон"]']):
            self._log("Microphone already muted")
        else:
            self._log("Microphone button not found")

        # Only mute camera if no custom video is provided
        if not self.fake_video_path:
            await asyncio.sleep(0.3)
            cam_button = await self._find_visible(frame, [
                '[data-testid="turn-off-camera-button"]',
                'button[title="Выключить камеру"]',
            ])
            if cam_button:
                await cam_button.click()
                self._log("Camera muted")
            else:
                self._log("Camera already muted or button not found")
        else:
            self._log("Camera kept on (custom video provided)")

    async def _click_continue_in_browser(self):
        """Click 'Continue in browser' button if present."""
        self._log("Looking for 'Continue in browser' button...")

        selector = (
            '[data-testid="meeting-continue-in-browser-continue"], '
            'button:has-text("Продолжить в браузере"), '
            'button:has-text("Continue in browser"), '
            'a:has-text("Продолжить в браузере")'
        )
        try:
            button = self._page.locator(selector).first
            await button.wait_for(state="visible", timeout=10000)
            await button.click(timeout=10000)
            self._log("Clicked 'Continue in browser'")
            await asyncio.sleep(AFTER_CONTINUE_WAIT)
        except Exception:
            self._log("No 'Continue in browser' button found")

    JOIN_SELECTORS = [
        '[data-testid="enter-conference-button"]',
        'button:has-text("Подключиться")',
        'button:has-text("Присоединиться")',
        'button:has-text("Join")',
    ]

    async def _wait_for_prejoin(self):
        """Wait for the pre-join screen with the join button, remember its frame."""
        self._log("Waiting for pre-join screen...")
        deadline = time.monotonic() + self.join_step_timeout
        while time.monotonic() < deadline:
            frame = self._frame()
            if await self._find_visible(frame, self.JOIN_SELECTORS):
                self._call_frame = frame
                self._log(f"Pre-join screen found ({'iframe' if frame is not self._page.main_frame else 'main page'})")
                return
            await asyncio.sleep(1)

        await self._screenshot("prejoin_not_found")
        if self.debug:
            await self._log_all_buttons()
        raise JoinError(
            f"Pre-join screen not found within {self.join_step_timeout}s - "
            "Telemost page layout may have changed"
        )

    async def _handle_prejoin(self):
        """Handle pre-join page (name input, etc.)."""
        self._log("Looking for name input...")

        name_input = await self._find_visible(self._frame(), [
            '[data-testid="orb-textinput-input"]',
            'input[placeholder*="имя" i]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
            'input[name="displayName"]',
            'input[type="text"]',
        ])
        if name_input:
            await name_input.fill(self.display_name)
            self._log(f"Entered name: {self.display_name}")
        else:
            self._log("No name input found (may not be required)")

    async def _click_join(self):
        """Click the join meeting button."""
        self._log("Looking for join button...")

        button = await self._find_visible(self._frame(), self.JOIN_SELECTORS)
        if not button:
            raise JoinError("Join button disappeared before it could be clicked")

        text = (await button.text_content() or "").strip()
        self._log(f"Found button: '{text}' - clicking...")
        await button.click()
        await asyncio.sleep(AFTER_JOIN_WAIT)

    async def _log_all_buttons(self):
        """Log all visible buttons for debugging."""
        for frame in self._page.frames:
            try:
                buttons = await frame.query_selector_all("button")
                self._log(f"Frame {frame.url[:80]}: {len(buttons)} buttons")
                for i, btn in enumerate(buttons[:20]):
                    try:
                        if not await btn.is_visible():
                            continue
                        text = (await btn.text_content() or "").strip()[:50]
                        testid = await btn.get_attribute("data-testid")
                        title = await btn.get_attribute("title") or await btn.get_attribute("aria-label")
                        self._log(f"  Button {i}: '{text}' testid={testid} title={title}")
                    except Exception:
                        pass
            except Exception as e:
                self._log(f"Error logging buttons: {e}")

    async def _in_call_ui_visible(self) -> bool:
        """Whether the in-call toolbar is shown (we are in the call, not on pre-join)."""
        return await self._find_visible(self._frame(), [
            '[data-testid="participants-button"]',
            '[data-testid="end-call-alt-button"]',
        ]) is not None

    async def _wait_for_connection(self):
        """Wait until we are in the call; fail after waiting_room_timeout."""
        self._log("Waiting for WebRTC connection...")

        deadline = time.monotonic() + self.waiting_room_timeout
        i = 0
        while time.monotonic() < deadline:
            try:
                status = await self._rtc_status()
                peer_conns = status.get("peerConnections", 0)
                tracks = status.get("tracksConnected", 0)

                if tracks > 0 or await self._in_call_ui_visible():
                    self._log(f"Connected! Peer connections: {peer_conns}, Audio tracks: {tracks}")
                    return

                if i % 10 == 0:
                    remaining = int(deadline - time.monotonic())
                    self._log(f"Not in call yet (waiting room?) - {remaining}s until timeout")
            except Exception as e:
                if self.debug:
                    self._log(f"Status check error: {e}")

            i += 1
            await asyncio.sleep(1)

        await self._screenshot("not_connected")
        if await self._find_visible(self._frame(), self.JOIN_SELECTORS):
            raise JoinError("Still on pre-join screen after clicking join")
        raise WaitingRoomTimeoutError("Timed out waiting in the waiting room - not admitted to meeting")

    async def _start_recording(self):
        """Start audio recording."""
        # Try to capture audio from page elements as fallback
        await self._eval("""
            if (window.__rtcCapturePageAudio) {
                window.__rtcCapturePageAudio();
            }
        """)

        # Resume AudioContext (requires user gesture, but we fake it)
        await self._eval("""
            if (window.__rtcInterceptor && window.__rtcInterceptor.audioContext) {
                window.__rtcInterceptor.audioContext.resume();
            }
        """)

        result = await self._eval("window.__rtcStartRecording()")
        if result:
            self._log("Recording started")
        else:
            self._log("Warning: Recording may not have started properly")

        # Log current status
        status = await self._rtc_status()
        self._log(f"Status: peers={status.get('peerConnections', 0)}, tracks={status.get('tracksConnected', 0)}, ctx={status.get('audioContextState', 'unknown')}")

    async def _wait_for_end(self):
        """Wait for the meeting to end (at most max_call_duration)."""
        self._log("Waiting for meeting to end (Ctrl+C to stop manually)...")

        alone_count = 0
        total_alone_time = 0
        had_participants = False
        max_alone = max(1, self.alone_wait_seconds // STATUS_CHECK_INTERVAL)
        deadline = time.monotonic() + self.max_call_duration
        unknown_since: float | None = None

        while True:
            await asyncio.sleep(STATUS_CHECK_INTERVAL)

            if time.monotonic() >= deadline:
                self._log(f"Max call duration ({self.max_call_duration}s) reached - stopping recording")
                break

            # Check if meeting ended (page changed)
            if await self._check_meeting_ended():
                self._log("Meeting ended (detected end screen)")
                break

            # Check participant count
            participant_count = await self._get_participant_count()

            # Call UI is gone for too long: we are no longer in the call
            if participant_count < 0:
                unknown_since = unknown_since or time.monotonic()
                if time.monotonic() - unknown_since >= self.lost_call_timeout:
                    self._log(f"Call UI not found for {self.lost_call_timeout}s - stopping recording")
                    break
            else:
                unknown_since = None

            # Check audio status
            try:
                status = await self._rtc_status()
                chunks = self._chunks_saved
                tracks = status.get("tracksConnected", 0)

                if participant_count == 1:
                    alone_count += 1
                    total_alone_time += STATUS_CHECK_INTERVAL

                    if had_participants:
                        # Someone was here but left
                        self._log(f"Recording: {chunks} chunks | Alone in meeting ({alone_count}/{max_alone})")
                        if alone_count >= max_alone:
                            self._log("All participants left - ending recording")
                            break
                    else:
                        # No one has joined yet
                        remaining = self.empty_meeting_timeout - total_alone_time
                        self._log(f"Recording: {chunks} chunks | Waiting for participants ({remaining}s remaining)")
                        if total_alone_time >= self.empty_meeting_timeout:
                            self._log("No one joined the meeting - timeout reached")
                            raise NoParticipantsError("No one joined the meeting within timeout")
                elif participant_count > 1:
                    alone_count = 0
                    had_participants = True
                    self._log(f"Recording: {chunks} chunks | {participant_count} participants, {tracks} audio tracks")
                else:
                    # Unknown participant count, fall back to audio track detection
                    self._log(f"Recording: {chunks} chunks | {tracks} audio tracks | participant count unknown")

            except Exception as e:
                error_msg = str(e)
                if "Target page, context or browser has been closed" in error_msg:
                    raise RuntimeError("Browser was closed unexpectedly")
                self._log(f"Status check error: {e}")

    async def _check_meeting_ended(self) -> bool:
        """Check if the meeting has ended."""
        # The call iframe was removed from the page
        if self._call_frame and self._call_frame.is_detached():
            return True

        # Check for end-of-meeting indicators
        end_selectors = [
            'text="Конференция завершена"',
            'text="Встреча завершена"',
            'text="Meeting ended"',
            'text="Вы покинули встречу"',
            'text="Вы вышли из встречи"',
            'button:has-text("Вернуться")',
            'button:has-text("Перейти на главную")',
        ]
        for frame in self._page.frames:
            if await self._find_visible(frame, end_selectors):
                return True

        # Check if we're no longer on a meeting page (URL changed)
        current_url = self._page.url
        if "/j/" not in current_url and "telemost" in current_url:
            return True

        return False

    async def _get_participant_count(self) -> int:
        """Get current participant count from DOM."""
        try:
            frame = self._frame()

            # Method 1: counter on the "Участники" button (Telemost 3.0)
            button = await self._find_visible(frame, [
                '[data-testid="participants-button"]',
                'button[title="Участники"]',
            ])
            if button:
                text = (await button.text_content() or "").strip()
                if text.isdigit():
                    return int(text)

            # Method 2: Count participant items in grid (old layout)
            items = await frame.query_selector_all('.item_NZ2DW')
            if items:
                return len(items)

        except Exception:
            pass

        return -1  # Unknown

    async def _get_recording(self) -> Path:
        """Stop recording and return the audio file written so far."""
        self._log("Retrieving recording...")

        # Flushes the last chunk; the call frame may already be gone, then we keep what we have
        try:
            await self._eval("window.__rtcStopRecording ? window.__rtcStopRecording() : null", timeout=30)
        except Exception as e:
            self._log(f"Could not stop recorder in page ({e}), using chunks saved so far")

        if self._audio_file:
            self._audio_file.close()
            self._audio_file = None

        if not self._audio_path or self._audio_path.stat().st_size == 0:
            raise RuntimeError("No audio data recorded. The meeting audio may not have been captured.")

        size_mb = self._audio_path.stat().st_size / (1024 * 1024)
        self._log(f"Retrieved {size_mb:.2f} MB of audio ({self._chunks_saved} chunks)")

        return self._audio_path
