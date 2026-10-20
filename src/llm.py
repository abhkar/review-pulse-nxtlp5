"""Shared Claude helper: call the model and return parsed JSON (or raw text)."""
import json
import os
import re
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

_client = None


def _get_client() -> anthropic.Anthropic:
    """Create the Anthropic client lazily so imports work without a key."""
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example).")
        _client = anthropic.Anthropic(api_key=key)
    return _client


def _model() -> str:
    model = os.getenv("ANTHROPIC_MODEL")
    if not model:
        raise RuntimeError("ANTHROPIC_MODEL is not set (see .env.example).")
    return model


def call_claude(prompt: str, system: str = "", max_tokens: int = 4096,
                temperature: float = 0.2, retries: int = 3) -> str:
    """Return the model's text reply. Backs off on rate limits / transient errors."""
    client = _get_client()
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=_model(),
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.InternalServerError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"[llm] {type(e).__name__}; retrying in {wait}s")
            time.sleep(wait)


def _parse_json(text: str):
    """Parse JSON from a reply: strip code fences, ignore prose before/after."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise json.JSONDecodeError("no JSON found", text, 0)
    # raw_decode stops at the end of the first JSON value, so trailing prose is fine.
    obj, _ = json.JSONDecoder().raw_decode(text[min(starts):])
    return obj


def call_claude_json(prompt: str, system: str = "", max_tokens: int = 4096,
                     temperature: float = 0.2):
    """Call Claude and parse a JSON reply. Retries once on a parse failure."""
    system = (system + "\n" if system else "") + \
        "Respond with valid JSON only. No commentary."
    raw = call_claude(prompt, system, max_tokens, temperature)
    try:
        return json.loads(_parse_json(raw))
    except json.JSONDecodeError as e:
        # One retry: show the model its bad output and ask for a fix.
        fix = (f"{prompt}\n\nYour previous reply was not valid JSON ({e}). "
               f"Previous reply:\n{raw}\n\nReturn ONLY the corrected JSON.")
        raw2 = call_claude(fix, system, max_tokens, 0.0)
        return json.loads(_parse_json(raw2))  # raises if still bad
