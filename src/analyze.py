"""Phase 2: review intelligence layer.

data/reviews.csv -> outputs/analysis.json
  1. define <=5 themes from a sample, classify every review into them (real counts)
  2. rank the top 3 themes
  3. pick 3 verbatim quotes (verified in code: quote in text)
  4. find 1 recurring fee/charge confusion (keyword pass + LLM pass) with 2 verbatim quotes
"""
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from src.llm import call_claude_json

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "reviews.csv"
OUT = ROOT / "outputs" / "analysis.json"

BATCH = 80          # reviews per classification call
WORKERS = 4         # parallel API calls
MAX_QUOTE_WORDS = 29  # "under 30 words"
MIN_FEE_REVIEWS = 15  # below this we say no fee confusion was found

# Words that hint a review is about money. Used only to narrow the LLM pass.
FEE_KEYWORDS = re.compile(
    r"\b(pric\w*|subscri\w*|charg\w*|refund\w*|bill\w*|gst|tax\w*|renew\w*|expensive|"
    r"pay\w*|paid|cost\w*|money|rupee\w*|rs\.?|inr|usd|dollar\w*|plus|pro|premium|"
    r"cancel\w*|deduct\w*|fee\w*|afford\w*|cheap\w*|free trial|purchase\w*|invoice\w*)\b|₹|\$",
    re.IGNORECASE)


def clip(text: str, n: int = 400) -> str:
    return text if len(text) <= n else text[:n] + "…"


def numbered(df: pd.DataFrame) -> str:
    """Render reviews for a prompt as `index | rating | text` lines."""
    return "\n".join(f"{i} | {r.rating}★ | {clip(r.text)}" for i, r in df.iterrows())


def run_batches(df: pd.DataFrame, fn, size: int = BATCH) -> dict:
    """Run fn(batch_df) -> {index: label} over df in parallel and merge the results."""
    batches = [df.iloc[i:i + size] for i in range(0, len(df), size)]
    out = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for n, res in enumerate(ex.map(fn, batches), 1):
            out.update(res)
            if n % 10 == 0 or n == len(batches):
                print(f"  classified batch {n}/{len(batches)}", flush=True)
    return out


# ---------- 1-2. themes ----------

def define_themes(df: pd.DataFrame) -> list:
    sample = df.sample(min(300, len(df)), random_state=7)
    prompt = f"""Below are {len(sample)} Play Store reviews of the ChatGPT mobile app (India).
Define AT MOST 5 themes that together describe what users talk about. Rules:
- Themes should be distinct, useful to a product team (e.g. a type of problem, a feature request, or a kind of praise).
- At most ONE theme may be generic praise.
- Not every review has to fit a theme.
Return JSON: {{"themes": [{{"id": "t1", "name": "...", "description": "one line"}}, ...]}}

Reviews:
{numbered(sample)}"""
    themes = call_claude_json(prompt, max_tokens=1500)["themes"][:5]
    for k, t in enumerate(themes, 1):
        t["id"] = f"t{k}"
    return themes


def classify_themes(df: pd.DataFrame, themes: list) -> dict:
    spec = "\n".join(f'{t["id"]}: {t["name"]} - {t["description"]}' for t in themes)
    ids = {t["id"] for t in themes} | {"none"}

    def fn(batch):
        prompt = f"""Assign each review to the single best-fitting theme, or "none" if no theme fits.
Themes:
{spec}

Return JSON: {{"labels": {{"<review index>": "<theme id or none>", ...}}}} covering EVERY index below.

Reviews:
{numbered(batch)}"""
        labels = call_claude_json(prompt, max_tokens=3000)["labels"]
        # Anything unlabeled or invalid counts as "none" so counts stay honest.
        return {i: (labels.get(str(i)) if labels.get(str(i)) in ids else "none") for i in batch.index}

    return run_batches(df, fn)


# ---------- 3. verbatim quotes ----------

def valid_quote(quote: str, text: str) -> bool:
    return bool(quote) and quote in text and len(quote.split()) <= MAX_QUOTE_WORDS


def pick_quotes(df: pd.DataFrame, what: str, n: int = 1, exclude: set = frozenset()) -> list:
    """Ask Claude for n verbatim quotes from df about `what`; verify each in code.
    Retries with feedback, then falls back to a short whole review (still verbatim)."""
    cand = df[~df.review_id.isin(exclude)]
    cand = cand[cand.text.str.split().str.len().between(6, 60)]
    cand = cand.sort_values(["thumbs_up"], ascending=False).head(40)
    by_id = {r.review_id: r for r in cand.itertuples()}
    good, feedback = [], ""
    for _ in range(3):
        prompt = f"""From these reviews, pick {n - len(good)} distinct, representative quotes about: {what}.
Each quote MUST be copied exactly, character for character, from ONE review's text (a contiguous substring, max {MAX_QUOTE_WORDS} words). Do not fix spelling or punctuation.
{feedback}
Return JSON: {{"quotes": [{{"review_id": "...", "quote": "..."}}]}}

Reviews:
""" + "\n".join(f"{r.review_id} | {r.rating}★ | {clip(r.text)}" for r in cand.itertuples())
        bad = []
        for q in call_claude_json(prompt, max_tokens=1500)["quotes"]:
            r = by_id.get(q.get("review_id"))
            used = {g["review_id"] for g in good}
            if r and q["review_id"] not in used and valid_quote(q.get("quote", ""), r.text):
                good.append({"quote": q["quote"], "review_id": r.review_id, "date": r.date,
                             "rating": int(r.rating)})
            else:
                bad.append(q.get("quote", ""))
        if len(good) >= n:
            return good[:n]
        feedback = f"Previous attempt had quotes that were not exact substrings or too long: {bad}. Try again."
    # Fallback: whole short reviews are trivially verbatim.
    for r in cand.itertuples():
        if len(good) >= n:
            break
        if r.review_id not in {g["review_id"] for g in good} and len(r.text.split()) <= MAX_QUOTE_WORDS:
            good.append({"quote": r.text, "review_id": r.review_id, "date": r.date,
                         "rating": int(r.rating)})
    return good[:n]


# ---------- 4. fee issue ----------

def find_fee_issue(df: pd.DataFrame, themes: list, theme_of: dict) -> dict:
    hits = df[df.text.str.contains(FEE_KEYWORDS)]
    print(f"Fee keyword pass: {len(hits)} of {len(df)} reviews match")
    sample = hits.sample(min(200, len(hits)), random_state=3)
    prompt = f"""These Play Store reviews of the ChatGPT app (India) contain money-related words.
Find recurring confusion or pain points about a FEE or CHARGE (e.g. subscription price, taxes, auto-renewal,
currency/billing, refunds, paywalled limits). Derive them from the reviews only; do not assume.
Return AT MOST 5 categories, most common first. If nothing recurs, return an empty list.
JSON: {{"categories": [{{"id": "f1", "name": "short fee name", "description": "one line"}}]}}

Reviews:
{numbered(sample)}"""
    cats = call_claude_json(prompt, max_tokens=1200)["categories"][:5]
    for k, c in enumerate(cats, 1):
        c["id"] = f"f{k}"
    if not cats:
        return {"found": False, "keyword_matches": len(hits)}

    spec = "\n".join(f'{c["id"]}: {c["name"]} - {c["description"]}' for c in cats)
    ids = {c["id"] for c in cats} | {"none"}

    def fn(batch):
        p = f"""Does each review complain about or express confusion about a fee/charge in one of these categories?
{spec}
Choose one category id, or "none" if the review is not about such a fee issue.
Return JSON: {{"labels": {{"<review index>": "<category id or none>"}}}} covering EVERY index.

Reviews:
{numbered(batch)}"""
        labels = call_claude_json(p, max_tokens=3000)["labels"]
        return {i: (labels.get(str(i)) if labels.get(str(i)) in ids else "none") for i in batch.index}

    labels = run_batches(hits, fn)
    counts = pd.Series(labels).value_counts()
    for c in cats:
        c["review_count"] = int(counts.get(c["id"], 0))
    cats.sort(key=lambda c: -c["review_count"])
    top = cats[0]
    if top["review_count"] < MIN_FEE_REVIEWS:
        return {"found": False, "keyword_matches": len(hits), "categories": cats}
    members = df.loc[[i for i, l in labels.items() if l == top["id"]]]
    quotes = pick_quotes(members, f'the fee issue "{top["name"]}": {top["description"]}', n=2)
    return {"found": True, "fee_name": top["name"], "description": top["description"],
            "review_count": top["review_count"],
            "share_of_all_reviews_pct": round(100 * top["review_count"] / len(df), 1),
            "keyword_matches": len(hits), "other_categories": cats[1:], "quotes": quotes}


# ---------- main ----------

def main():
    df = pd.read_csv(CSV, dtype={"review_id": str}).dropna(subset=["text"]).reset_index(drop=True)
    print(f"Loaded {len(df)} reviews, {df.date.min()} to {df.date.max()}")

    print("Defining themes...")
    themes = define_themes(df)
    print("Classifying reviews into themes...")
    theme_of = classify_themes(df, themes)
    counts = pd.Series(theme_of).value_counts()
    for t in themes:
        t["review_count"] = int(counts.get(t["id"], 0))
        t["share_pct"] = round(100 * t["review_count"] / len(df), 1)
    themes.sort(key=lambda t: -t["review_count"])
    top3 = themes[:3]

    print("Picking verbatim quotes...")
    quotes, used = [], set()
    for t in top3:
        members = df.loc[[i for i, l in theme_of.items() if l == t["id"]]]
        q = pick_quotes(members, f'{t["name"]}: {t["description"]}', n=1, exclude=used)
        for x in q:
            x["theme"] = t["name"]
            used.add(x["review_id"])
        quotes += q

    print("Finding fee issue...")
    fee = find_fee_issue(df, themes, theme_of)
    if not fee["found"]:
        billing = next((t for t in themes if re.search(r"bill|pric|pay|subscri|fee|cost", t["name"] + t["description"], re.I)), None)
        fee["closest_billing_theme"] = billing["name"] if billing else None

    # Final safety net: every quote must be a verbatim substring of its CSV row.
    text_of = dict(zip(df.review_id, df.text))
    for q in quotes + fee.get("quotes", []):
        assert valid_quote(q["quote"], text_of[q["review_id"]]), f"unverified quote: {q}"

    result = {
        "meta": {"reviews_analyzed": len(df), "date_from": df.date.min(), "date_to": df.date.max(),
                 "note": "Counts are from a weighted sample (1-3 star and longer reviews over-sampled), "
                         "not the full review stream."},
        "themes": themes, "top_themes": top3, "quotes": quotes, "fee_issue": fee,
        "unthemed_reviews": int(counts.get("none", 0)),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print_summary(result)


def print_summary(r: dict):
    print(f"\n=== THEMES ({r['meta']['reviews_analyzed']} reviews, {r['meta']['date_from']} to {r['meta']['date_to']}) ===")
    for t in r["themes"]:
        print(f"- {t['name']}: {t['review_count']} ({t['share_pct']}%) - {t['description']}")
    print(f"(no theme: {r['unthemed_reviews']})")
    print("\n=== TOP 3 QUOTES (verified verbatim) ===")
    for q in r["quotes"]:
        print(f"[{q['theme']}] \"{q['quote']}\" ({q['date']}, {q['rating']}★)")
    f = r["fee_issue"]
    print("\n=== FEE ISSUE ===")
    if f["found"]:
        print(f"{f['fee_name']}: {f['review_count']} reviews ({f['share_of_all_reviews_pct']}% of sample); "
              f"{f['keyword_matches']} reviews matched money keywords")
        print(f["description"])
        for q in f["quotes"]:
            print(f"  \"{q['quote']}\" ({q['date']}, {q['rating']}★)")
    else:
        print(f"No recurring fee confusion found. Closest billing theme: {f.get('closest_billing_theme')}")
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()
