"""Telemost browser automation using Playwright."""

import asyncio
import base64
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console

from ..config import get_js_interceptor_path


console = Console()


class NoParticipantsError(Exception):
    """Raised when no one joins the meeting within timeout."""
    pass


class WaitingRoomTimeoutError(Exception):
    """Raised when stuck in waiting room and not admitted."""
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
    ):
        self.meeting_url = meeting_url
        self.display_name = display_name
        self.headless = headless
        self.on_status = on_status or (lambda x: None)
        self.debug = debug
        self.fake_video_path = fake_video_path
        self.alone_wait_seconds = alone_wait_seconds
        self.empty_meeting_timeout = empty_meeting_timeout
        self.waiting_room_timeout = waiting_room_timeout

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

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

        # Inject RTC interceptor script before any page loads
        js_interceptor = get_js_interceptor_path().read_text()
        await self._page.add_init_script(js_interceptor)

        self._log("Browser initialized")

    async def _cleanup(self):
        """Clean up browser resources."""
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
        await asyncio.sleep(3)
        await self._screenshot("01_loaded")

        # Handle "Continue in browser" prompt
        await self._click_continue_in_browser()
        await self._screenshot("02_after_continue")

        # Handle pre-join flow (name input, media settings)
        await self._handle_prejoin()
        await self._screenshot("03_after_prejoin")

        # Try to join the meeting
        await self._click_join()
        await self._screenshot("04_after_join_click")

        # Wait for connection and start recording
        await self._wait_for_connection()
        await self._screenshot("05_connected")

        # Mute microphone after joining
        await self._mute_microphone()

        # Wait a bit for audio tracks to be established
        self._log("Waiting for audio tracks...")
        await asyncio.sleep(5)

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

    async def _mute_microphone(self):
        """Mute the microphone after joining the meeting."""
        self._log("Muting microphone and camera...")

        # Mute microphone
        # The button title is "Выключить микрофон" when mic is ON
        try:
            mic_button = await self._page.query_selector('button[title="Выключить микрофон"]')
            if not mic_button:
                mic_button = await self._page.query_selector('.MicrophoneButton_XysKF button')

            if mic_button and await mic_button.is_visible():
                title = await mic_button.get_attribute('title') or ""
                if "Выключить" in title:
                    await mic_button.click()
                    self._log("Microphone muted")
                else:
                    self._log("Microphone already muted")
            else:
                self._log("Microphone button not found")
        except Exception as e:
            self._log(f"Failed to mute microphone: {e}")

        # Mute camera
        # The button title is "Выключить камеру" when camera is ON
        try:
            cam_button = await self._page.query_selector('button[title="Выключить камеру"]')
            if not cam_button:
                cam_button = await self._page.query_selector('.CameraButton_kttfg button')

            if cam_button and await cam_button.is_visible():
                title = await cam_button.get_attribute('title') or ""
                if "Выключить" in title:
                    await cam_button.click()
                    self._log("Camera muted")
                else:
                    self._log("Camera already muted")
        except Exception as e:
            self._log(f"Failed to mute camera: {e}")

    async def _click_continue_in_browser(self):
        """Click 'Continue in browser' button if present."""
        self._log("Looking for 'Continue in browser' button...")

        selectors = [
            'button:has-text("Продолжить в браузере")',
            'button:has-text("Continue in browser")',
            'a:has-text("Продолжить в браузере")',
        ]

        for selector in selectors:
            try:
                button = await self._page.wait_for_selector(selector, timeout=5000)
                if button and await button.is_visible():
                    await button.click()
                    self._log("Clicked 'Continue in browser'")
                    await asyncio.sleep(3)  # Wait for next page to load
                    return
            except Exception:
                continue

        self._log("No 'Continue in browser' button found")

    async def _handle_prejoin(self):
        """Handle pre-join page (name input, etc.)."""
        self._log("Looking for name input...")

        # Try various name input selectors
        name_selectors = [
            'input[placeholder*="имя" i]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
            'input[name="displayName"]',
            'input[type="text"]',
        ]

        for selector in name_selectors:
            try:
                inputs = await self._page.query_selector_all(selector)
                for inp in inputs:
                    if await inp.is_visible():
                        await inp.fill(self.display_name)
                        self._log(f"Entered name: {self.display_name}")
                        return
            except Exception:
                continue

        self._log("No name input found (may not be required)")

    async def _click_join(self):
        """Click the join meeting button."""
        self._log("Looking for join button...")

        # Try various join button selectors
        join_selectors = [
            'button:has-text("Войти")',
            'button:has-text("Присоединиться")',
            'button:has-text("Join")',
            'button:has-text("Подключиться")',
            '[data-testid*="join"]',
            'button[type="submit"]',
        ]

        for selector in join_selectors:
            try:
                buttons = await self._page.query_selector_all(selector)
                for button in buttons:
                    if await button.is_visible():
                        text = await button.text_content()
                        self._log(f"Found button: '{text}' - clicking...")
                        await button.click()
                        await asyncio.sleep(2)
                        return
            except Exception as e:
                if self.debug:
                    self._log(f"Selector {selector} failed: {e}")
                continue

        # Log all visible buttons for debugging
        if self.debug:
            await self._log_all_buttons()

        self._log("No join button found - may already be in call or page structure changed")

    async def _log_all_buttons(self):
        """Log all visible buttons for debugging."""
        try:
            buttons = await self._page.query_selector_all("button")
            self._log(f"Found {len(buttons)} buttons on page:")
            for i, btn in enumerate(buttons[:10]):  # Limit to first 10
                try:
                    text = await btn.text_content()
                    visible = await btn.is_visible()
                    self._log(f"  Button {i}: '{text.strip()[:50]}' visible={visible}")
                except Exception:
                    pass
        except Exception as e:
            self._log(f"Error logging buttons: {e}")

    async def _wait_for_connection(self):
        """Wait for WebRTC connection to establish."""
        self._log("Waiting for WebRTC connection...")

        waiting_room_time = 0
        has_connection_no_audio = False

        for i in range(self.waiting_room_timeout):
            try:
                status = await self._page.evaluate("window.__rtcGetStatus ? window.__rtcGetStatus() : {}")
                peer_conns = status.get("peerConnections", 0)
                tracks = status.get("tracksConnected", 0)

                if peer_conns > 0:
                    if tracks > 0:
                        self._log(f"Connected! Peer connections: {peer_conns}, Audio tracks: {tracks}")
                        return
                    else:
                        # Have connections but no audio - likely in waiting room
                        has_connection_no_audio = True
                        waiting_room_time += 1
                        remaining = self.waiting_room_timeout - waiting_room_time

                        if i % 10 == 0:
                            self._log(f"In waiting room... ({remaining}s until timeout)")

                if i % 10 == 0 and i > 0 and not has_connection_no_audio:
                    self._log(f"Still waiting for connection... ({i}s)")

            except Exception as e:
                if self.debug:
                    self._log(f"Status check error: {e}")

            await asyncio.sleep(1)

        if has_connection_no_audio:
            raise WaitingRoomTimeoutError("Timed out waiting in the waiting room - not admitted to meeting")

    async def _start_recording(self):
        """Start audio recording."""
        # Try to capture audio from page elements as fallback
        await self._page.evaluate("""
            if (window.__rtcCapturePageAudio) {
                window.__rtcCapturePageAudio();
            }
        """)

        # Resume AudioContext (requires user gesture, but we fake it)
        await self._page.evaluate("""
            if (window.__rtcInterceptor && window.__rtcInterceptor.audioContext) {
                window.__rtcInterceptor.audioContext.resume();
            }
        """)

        result = await self._page.evaluate("window.__rtcStartRecording()")
        if result:
            self._log("Recording started")
        else:
            self._log("Warning: Recording may not have started properly")

        # Log current status
        status = await self._page.evaluate("window.__rtcGetStatus ? window.__rtcGetStatus() : {}")
        self._log(f"Status: peers={status.get('peerConnections', 0)}, tracks={status.get('tracksConnected', 0)}, ctx={status.get('audioContextState', 'unknown')}")

    async def _wait_for_end(self):
        """Wait for the meeting to end."""
        self._log("Waiting for meeting to end (Ctrl+C to stop manually)...")

        check_interval = 5  # seconds
        alone_count = 0
        total_alone_time = 0
        had_participants = False
        max_alone = max(1, self.alone_wait_seconds // check_interval)  # Convert seconds to check count

        while True:
            await asyncio.sleep(check_interval)

            # Check if meeting ended (page changed)
            if await self._check_meeting_ended():
                self._log("Meeting ended (detected end screen)")
                break

            # Check participant count
            participant_count = await self._get_participant_count()

            # Check audio status
            try:
                status = await self._page.evaluate("window.__rtcGetStatus ? window.__rtcGetStatus() : {}")
                chunks = status.get("chunksRecorded", 0)
                tracks = status.get("tracksConnected", 0)

                if participant_count == 1:
                    alone_count += 1
                    total_alone_time += check_interval

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
                    self._log(f"Recording: {chunks} chunks | {tracks} audio tracks")

            except Exception as e:
                error_msg = str(e)
                if "Target page, context or browser has been closed" in error_msg:
                    raise RuntimeError("Browser was closed unexpectedly")
                self._log(f"Status check error: {e}")

    async def _check_meeting_ended(self) -> bool:
        """Check if the meeting has ended."""
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

        for selector in end_selectors:
            try:
                element = await self._page.query_selector(selector)
                if element and await element.is_visible():
                    return True
            except Exception:
                pass

        # Check if we're no longer on a meeting page (URL changed)
        current_url = self._page.url
        if "/j/" not in current_url and "telemost" in current_url:
            return True

        return False

    async def _get_participant_count(self) -> int:
        """Get current participant count from DOM."""
        try:
            # Method 1: Badge in "Участники" button
            badge = await self._page.query_selector('button[title="Участники"] .badge_iL7ZW')
            if badge:
                text = await badge.text_content()
                if text and text.strip().isdigit():
                    return int(text.strip())

            # Method 2: Count participant items in grid
            items = await self._page.query_selector_all('.item_NZ2DW')
            if items:
                return len(items)

            # Method 3: Alternative badge class
            badge = await self._page.query_selector('[class*="badge_"]')
            if badge:
                text = await badge.text_content()
                if text and text.strip().isdigit():
                    return int(text.strip())

        except Exception:
            pass

        return -1  # Unknown

    async def _get_recording(self) -> Path:
        """Stop recording and save audio to temp file."""
        self._log("Retrieving recording...")

        result = await self._page.evaluate("window.__rtcStopRecording()")

        if not result or not result.get("data"):
            # Try to get more info
            status = await self._page.evaluate("window.__rtcGetStatus ? window.__rtcGetStatus() : {}")
            self._log(f"Final status: {status}")
            raise RuntimeError("No audio data recorded. The meeting audio may not have been captured.")

        # Decode base64 and save to temp file
        audio_data = base64.b64decode(result["data"])
        size_mb = len(audio_data) / (1024 * 1024)
        self._log(f"Retrieved {size_mb:.2f} MB of audio")

        # Save to temp file
        temp_file = tempfile.NamedTemporaryFile(
            suffix=".webm",
            delete=False,
            prefix="telemost_"
        )
        temp_file.write(audio_data)
        temp_file.close()

        return Path(temp_file.name)
