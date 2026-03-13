"""Conversation roaster — analyses transcribed conversation and decides when to roast.

Maintains a rolling transcript window. After each new chunk of speech,
asks Gemini whether the conversation warrants a printed roast/commentary.
If Gemini says yes, it generates the receipt text.
"""

import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

# How many transcript lines to keep in the rolling window
MAX_TRANSCRIPT_LINES = 40
# Minimum seconds between conversation roasts
CONVO_COOLDOWN_SECONDS = 90


JUDGE_SYSTEM_PROMPT = """\
You are a comedy writer who eavesdrops on conversations.
You will receive a transcript of a recent conversation.

Your job:
1. Decide if something FUNNY, EMBARRASSING, ABSURD, or ROAST-WORTHY
   was just said in the most recent lines.
2. If YES: write a short, hilarious receipt-style commentary (2-3 sentences max).
   Be specific — reference what was actually said. Think of yourself as a
   heckler in the room who can't resist commenting.
3. If NO: respond with exactly: [SKIP]

RULES:
- Only roast when it's genuinely funny or interesting. Don't force it.
- Be witty, and comedic. Use names if mentioned.
- Keep it to 2-3 sentences — this prints on a receipt.
- Reference specific things people said.
- Don't roast every single chunk — be selective and wait for gold.
"""

JUDGE_PROMPT_TEMPLATE = """\
Here's what was just overheard. The most recent speech is at the bottom.

--- CONVERSATION TRANSCRIPT ---
{transcript}
--- END TRANSCRIPT ---

Should this get a printed roast? If yes, write the roast. If not, say [SKIP].
"""

FALLBACK_ROASTS = [
    "I tried to roast your conversation but even AI found it too boring to process.",
    "The things I've heard... I need therapy now. And so do you.",
    "My speech recognition crashed trying to make sense of whatever that was.",
]


@dataclass
class ConversationRoaster:
    """Judges conversation transcripts and generates roasts when warranted."""

    api_key: str
    model: str = "gemini-2.0-flash"
    cooldown_seconds: int = CONVO_COOLDOWN_SECONDS
    _transcript: deque = field(default_factory=lambda: deque(maxlen=MAX_TRANSCRIPT_LINES))
    _last_roast_time: float = 0.0
    _client: genai.Client = field(init=False, repr=False)
    _chunks_since_roast: int = 0

    def __post_init__(self):
        self._client = genai.Client(api_key=self.api_key)
        log.info("Conversation roaster ready (model=%s, cooldown=%ds)",
                 self.model, self.cooldown_seconds)

    def add_transcript(self, text: str) -> None:
        """Add a new chunk of transcribed speech to the rolling window."""
        if not text.strip():
            return
        # Split into individual lines and add each
        for line in text.strip().splitlines():
            line = line.strip()
            if line:
                self._transcript.append(line)
        self._chunks_since_roast += 1

    def get_transcript(self) -> str:
        """Return the current transcript as a single string."""
        return "\n".join(self._transcript)

    def clear_transcript(self) -> None:
        """Clear the transcript buffer."""
        self._transcript.clear()
        self._chunks_since_roast = 0

    def should_evaluate(self) -> bool:
        """Check if we should evaluate the transcript for a roast."""
        if not self._transcript:
            return False
        # Need at least 2 chunks of speech since last roast
        if self._chunks_since_roast < 2:
            return False
        # Respect cooldown
        if time.time() - self._last_roast_time < self.cooldown_seconds:
            return False
        return True

    def evaluate_and_roast(self) -> str | None:
        """Evaluate the current transcript. Returns roast text or None if [SKIP].

        Sends the transcript to Gemini to judge whether it's roast-worthy.
        """
        if not self.should_evaluate():
            return None

        transcript = self.get_transcript()
        if len(transcript) < 20:
            return None

        prompt = JUDGE_PROMPT_TEMPLATE.format(transcript=transcript)

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=prompt)],
                    ),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=JUDGE_SYSTEM_PROMPT,
                    max_output_tokens=512,
                    temperature=1.0,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    safety_settings=[
                        types.SafetySetting(
                            category="HARM_CATEGORY_HARASSMENT",
                            threshold="BLOCK_ONLY_HIGH",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_HATE_SPEECH",
                            threshold="BLOCK_MEDIUM_AND_ABOVE",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                            threshold="BLOCK_MEDIUM_AND_ABOVE",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_DANGEROUS_CONTENT",
                            threshold="BLOCK_MEDIUM_AND_ABOVE",
                        ),
                    ],
                ),
            )

            if response.text:
                result = response.text.strip()
                if "[SKIP]" in result:
                    log.debug("Gemini skipped this transcript — not funny enough")
                    return None

                log.info("🎤 Conversation roast generated (%d chars)", len(result))
                self._last_roast_time = time.time()
                self._chunks_since_roast = 0
                return result

            log.debug("Empty response from conversation judge")
        except Exception as exc:
            log.error("Conversation roast error: %s", exc, exc_info=True)

        return None
