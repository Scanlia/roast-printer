"""Speech-to-text transcription using Google Gemini's audio capabilities."""

import logging
import tempfile
from pathlib import Path

from google import genai
from google.genai import types

log = logging.getLogger(__name__)


class SpeechTranscriber:
    """Transcribe audio chunks using Gemini's multimodal audio input."""

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        log.info("Speech transcriber ready (model=%s)", model)

    def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
        """Transcribe an audio chunk and return the text.

        Returns empty string if no speech detected or on error.
        """
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text="Transcribe this audio exactly. Return ONLY the "
                                     "spoken words, nothing else. If there is no speech "
                                     "or it's unclear, return exactly: [silence]"
                            ),
                            types.Part.from_bytes(
                                data=audio_bytes,
                                mime_type=mime_type,
                            ),
                        ],
                    ),
                ],
                config=types.GenerateContentConfig(
                    max_output_tokens=2048,
                    temperature=0.1,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )

            if response.text:
                text = response.text.strip()
                if text == "[silence]" or not text:
                    log.debug("No speech detected in audio chunk")
                    return ""
                log.info("Transcribed %d chars: %.80s...", len(text), text)
                return text

            log.debug("Empty transcription response")
        except Exception as exc:
            log.error("Transcription error: %s", exc, exc_info=True)

        return ""
