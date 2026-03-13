"""Gemini Vision API — generate outfit roasts from images."""

import logging
import random

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a funny fashion roast comedian.
You just saw a surveillance camera photo of someone and you MUST
roast their outfit in the funniest way possible.

RULES:
  - Be really really funny as if you are the worlds best standup comedian.
  - Comment on clothing, shoes, accessories, colour combos, fit,
    and overall style. Use vivid, creative insults.
  - Write EXACTLY 2-3 sentences. This prints on a receipt — be concise.
"""
 
ROAST_PROMPT = (
    "Roast this persons outfit. Be creative, funny and specific."
)

FALLBACK_ROASTS = [
    "Even AI refused to process this fit. That's the roast.",
    "My neural network crashed trying to classify this look. Error 404: Style Not Found.",
    "I was going to roast your outfit but my training data doesn't cover whatever this is.",
]


class GeminiRoaster:
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        log.info("Gemini roaster ready (model=%s)", model)

    def roast_outfit(self, image_bytes: bytes) -> str:
        """Send an image to Gemini and return the roast text."""
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(text=ROAST_PROMPT),
                            types.Part.from_bytes(
                                data=image_bytes,
                                mime_type="image/jpeg",
                            ),
                        ],
                    ),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=1024,
                    temperature=1.0,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=0,   # skip reasoning, just roast
                    ),
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
                roast = response.text.strip()
                log.info("Roast generated (%d chars)", len(roast))
                return roast

            log.warning("Empty Gemini response")
        except Exception as exc:
            log.error("Gemini API error: %s", exc, exc_info=True)

        return random.choice(FALLBACK_ROASTS)
