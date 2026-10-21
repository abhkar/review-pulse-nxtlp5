"""Phase 1: fetch recent Play Store reviews for the ChatGPT app (India) into data/reviews.csv."""
import argparse
import heapq
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from google_play_scraper import Sort, app, reviews

APP_ID = "com.openai.chatgpt"
COUNTRY, LANG = "in", "en"
WEEKS = 12
OUT = Path(__file__).resolve().parent.parent / "data" / "reviews.csv"
BATCH = 200
MAX_BATCHES = 2500  # safety cap against endless pagination
PER_DAY = 150  # weighted sample size per day (app gets ~2,400 English reviews/day)


def looks_english(text: str) -> bool:
    """Heuristic: at least 60% of letters are ASCII (drops Hindi/other-script reviews)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(c.isascii() for c in letters) / len(letters) >= 0.6


def sample_weight(rating: int, text: str) -> float:
    """Sampling weight: favour 1-3 star reviews (where pain points live) and longer text."""
    return (3.0 if rating <= 3 else 1.0) * min(len(text.split()), 30) ** 0.5


def fetch_reviews(weeks: int = WEEKS, per_day: int = PER_DAY) -> pd.DataFrame:
    """Stream newest reviews back past the cutoff, keeping a weighted sample of
    `per_day` reviews per calendar day (weighted random sampling, Efraimidis-Spirakis:
    key = u ** (1/w), keep the largest keys). Keeps memory small for ~2,400 reviews/day."""
    cutoff = datetime.now() - timedelta(weeks=weeks)
    rng = random.Random(42)
    heaps = {}  # day -> min-heap of (key, review_id, row)
    seen, token, scanned = set(), None, 0
    for i in range(MAX_BATCHES):
        for attempt in range(5):  # retry transient network / rate-limit errors
            try:
                batch, token = reviews(APP_ID, lang=LANG, country=COUNTRY, sort=Sort.NEWEST,
                                       count=BATCH, continuation_token=token)
                break
            except Exception as e:
                wait = 5 * 2 ** attempt
                print(f"  error {type(e).__name__}: {e}; retry in {wait}s")
                time.sleep(wait)
        else:
            raise RuntimeError("Too many failures fetching reviews")
        scanned += len(batch)
        for r in batch:
            text = re.sub(r"\s+", " ", r["content"] or "").strip()
            if r["at"] < cutoff or not text or not looks_english(text) or r["reviewId"] in seen:
                continue
            seen.add(r["reviewId"])
            day = r["at"].strftime("%Y-%m-%d")
            key = rng.random() ** (1.0 / sample_weight(r["score"], text))
            row = {"review_id": r["reviewId"], "date": day, "rating": r["score"], "text": text,
                   "thumbs_up": r["thumbsUpCount"], "app_version": r["appVersion"]}
            h = heaps.setdefault(day, [])
            if len(h) < per_day:
                heapq.heappush(h, (key, r["reviewId"], row))
            else:
                heapq.heappushpop(h, (key, r["reviewId"], row))
        oldest = min((r["at"] for r in batch), default=cutoff)
        if i % 20 == 0:
            print(f"  batch {i + 1}: scanned={scanned} oldest={oldest:%Y-%m-%d}", flush=True)
        if not batch or token is None or oldest < cutoff:
            break
    else:
        print("WARNING: hit MAX_BATCHES before reaching the cutoff")
    df = pd.DataFrame([row for h in heaps.values() for _, _, row in h])
    print(f"Scanned {scanned} raw reviews; oldest reached {oldest:%Y-%m-%d}")
    return df.drop_duplicates("text").sort_values("date", ascending=False).reset_index(drop=True)


def clean(df: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    df = df[df["date"] >= cutoff]
    df["text"] = df["text"].fillna("").map(lambda t: re.sub(r"\s+", " ", t).strip())
    df = df[df["text"] != ""]
    df = df[df["text"].map(looks_english)]
    df = df.drop_duplicates("review_id").drop_duplicates("text")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def main(weeks: int = WEEKS, per_day: int = PER_DAY):
    meta = app(APP_ID, lang=LANG, country=COUNTRY)
    print(f"Verified package {APP_ID}: {meta['title']} by {meta['developer']}")
    df = fetch_reviews(weeks, per_day)
    OUT.parent.mkdir(exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"\nSaved {len(df)} rows -> {OUT}")
    print(f"Date range: {df['date'].min()} to {df['date'].max()}")
    print("Rating distribution:\n", df["rating"].value_counts().sort_index().to_string())
    print("\nSample rows:\n", df.sample(min(5, len(df)), random_state=1).to_string())
    if len(df) < 300:
        print(f"\nWARNING: only {len(df)} reviews (< 300 target).")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--weeks", type=int, default=WEEKS)
    p.add_argument("--per-day", type=int, default=PER_DAY)
    a = p.parse_args()
    main(a.weeks, a.per_day)
