"""Audio listener — receives audio chunks from the Android tablet and processes them.

The tablet streams 30-second WAV chunks to the /api/audio endpoint.
Each chunk is transcribed, added to the conversation buffer, and
evaluated for roast-worthiness.
"""

import logging
import threading
import time
from datetime import datetime

from speech_transcriber import SpeechTranscriber
from conversation_roaster import ConversationRoaster

log = logging.getLogger(__name__)


class AudioListener:
    """Processes incoming audio chunks and triggers conversation roasts."""

    def __init__(
        self,
        transcriber: SpeechTranscriber,
        roaster: ConversationRoaster,
        printer_client,
        paper_width_dots: int = 520,
        on_roast_callback=None,
    ):
        self.transcriber = transcriber
        self.roaster = roaster
        self.printer = printer_client
        self.paper_width_dots = paper_width_dots
        self.on_roast_callback = on_roast_callback
        self._enabled = True
        self._lock = threading.Lock()
        self._stats = {
            "chunks_received": 0,
            "chunks_transcribed": 0,
            "roasts_generated": 0,
            "last_chunk_time": None,
            "last_transcript": "",
        }
        log.info("Audio listener ready")

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value
        log.info("Audio listener %s", "enabled" if value else "disabled")

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def process_audio_chunk(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> dict:
        """Process a single audio chunk. Called from the web endpoint.

        Returns a dict with status info.
        """
        if not self.enabled:
            return {"ok": False, "reason": "audio_disabled"}

        with self._lock:
            self._stats["chunks_received"] += 1
            self._stats["last_chunk_time"] = datetime.now().strftime("%H:%M:%S")

        log.info("🎙️ Audio chunk received (%d bytes, %s)", len(audio_bytes), mime_type)

        # Transcribe in a background thread to not block the HTTP response
        thread = threading.Thread(
            target=self._process_async,
            args=(audio_bytes, mime_type),
            daemon=True,
        )
        thread.start()

        return {"ok": True, "reason": "processing"}

    def _process_async(self, audio_bytes: bytes, mime_type: str) -> None:
        """Background processing of an audio chunk."""
        try:
            # 1. Transcribe
            text = self.transcriber.transcribe(audio_bytes, mime_type)

            if not text:
                log.debug("No speech in audio chunk")
                return

            with self._lock:
                self._stats["chunks_transcribed"] += 1
                self._stats["last_transcript"] = text[:200]

            # 2. Add to conversation buffer
            self.roaster.add_transcript(text)
            log.info("Transcript added (%d chars). Buffer: %d lines",
                     len(text), len(self.roaster._transcript))

            # 3. Evaluate for roast
            roast = self.roaster.evaluate_and_roast()

            if roast:
                with self._lock:
                    self._stats["roasts_generated"] += 1

                log.info("🎤🔥 Conversation roast: %s", roast)
                self._print_conversation_roast(roast)

                if self.on_roast_callback:
                    self.on_roast_callback(roast)

        except Exception as exc:
            log.error("Audio processing error: %s", exc, exc_info=True)

    def _print_conversation_roast(self, roast_text: str) -> None:
        """Build and send a text-only conversation roast receipt."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build a payload compatible with the existing printer client
        payload = {
            "image_b64": "",  # no image for conversation roasts
            "roast_text": roast_text,
            "timestamp": ts,
            "receipt_title": "OVERHEARD",
            "receipt_footer": "EAVESDROP-O-MATIC 3000",
        }

        if self.printer:
            ok = self.printer.print_receipt(payload)
            if ok:
                log.info("✅ Conversation roast receipt sent")
            else:
                log.error("Failed to print conversation roast")
        else:
            log.warning("No printer configured for conversation roasts")
