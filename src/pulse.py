"""Phase 3: weekly product pulse (<=250 words) from outputs/analysis.json only.

The model writes the theme summaries, the observation and the actions. The quotes are
inserted by code straight from analysis.json, so they stay verbatim. The word limit is
enforced in code and every number in the model's text is checked against analysis.json.
"""
import json
import re
from datetime import date
from pathlib import Path

from src.llm import call_claude_json

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "outputs" / "analysis.json"
OUT = ROOT / "outputs" / "weekly_pulse.md"
MAX_WORDS = 250
MAX_TRIES = 3


def word_count(md: str) -> int:
    return len(md.split())


def allowed_numbers(a: dict) -> set:
    """Every number that appears in analysis.json (counts, shares, dates, ratings)."""
    return set(re.findall(r"\d+(?:\.\d+)?", json.dumps(a)))


def build_md(a: dict, parts: dict, today: str) -> str:
    lines = [f"# Weekly Product Pulse - {today}", "",
             f"_ChatGPT Android app (India), {a['meta']['date_from']} to {a['meta']['date_to']}, "
             f"{a['meta']['reviews_analyzed']:,} sampled reviews_", "", "## Top themes"]
    for t, s in zip(a["top_themes"], parts["theme_summaries"]):
        lines.append(f"{t['name']} ({t['review_count']} reviews, {t['share_pct']}%): {s}")
    lines += ["", "## What users said"]
    for q in a["quotes"]:
        lines.append(f"- \"{q['quote']}\" ({q['date']}, {q['rating']}★)")
    lines += ["", "## Key observation", parts["observation"], "", "## Actions"]
    lines += [f"{i}. {x}" for i, x in enumerate(parts["actions"], 1)]
    return "\n".join(lines) + "\n"


def generate(a: dict, today: str) -> str:
    facts = {"top_themes": a["top_themes"], "fee_issue": {k: a["fee_issue"].get(k) for k in
             ("fee_name", "description", "review_count", "share_of_all_reviews_pct")},
             "note": a["meta"]["note"]}
    allowed = allowed_numbers(a)
    tighter = ""
    for attempt in range(1, MAX_TRIES + 1):
        prompt = f"""Write parts of a weekly product pulse for the ChatGPT app team, using ONLY these facts:
{json.dumps(facts, ensure_ascii=False)}

Return JSON:
{{"theme_summaries": [3 strings, one per top theme in order, each max {16 - 3 * (attempt - 1)} words, plain statement of what users say],
  "observation": "max {45 - 8 * (attempt - 1)} words: what is going wrong or trending, mention the fee issue",
  "actions": [3 strings, each max {18 - 3 * (attempt - 1)} words, concrete product actions]}}

Rules: do not invent numbers or facts; use no numbers except ones present in the facts; neutral tone; no quotes.
{tighter}"""
        parts = call_claude_json(prompt, max_tokens=1200)
        md = build_md(a, parts, today)
        n = word_count(md)
        stray = [x for x in re.findall(r"\d+(?:\.\d+)?", " ".join(
            parts["theme_summaries"] + [parts["observation"]] + parts["actions"])) if x not in allowed]
        print(f"attempt {attempt}: {n} words, stray numbers: {stray}")
        if n <= MAX_WORDS and not stray:
            return md
        tighter = (f"Your previous draft was {n} words in total (limit {MAX_WORDS}) "
                   f"and used unsupported numbers {stray}. Be much shorter.")
    raise RuntimeError(f"Could not produce a valid pulse in {MAX_TRIES} tries")


def main():
    a = json.loads(ANALYSIS.read_text())
    md = generate(a, date.today().isoformat())
    OUT.write_text(md)
    print(md)
    print(f"Word count: {word_count(md)} (limit {MAX_WORDS}). Saved {OUT}")


if __name__ == "__main__":
    main()
