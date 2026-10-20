"""Phase 1: fetch recent Play Store reviews for the ChatGPT app (India) into data/reviews.csv."""
import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from google_play_scraper import Sort, app, reviews

APP_ID = "com.openai.chatgpt"
COUNTRY, LANG = "in", "en"
WEEKS = 12
OUT = Path(__file__).resolve().parent.parent / "data" / "reviews.csv"
BATCH = 200
MAX_BATCHES = 200  # safety cap against endless pagination


def looks_english(text: str) -> bool:
    """Heuristic: at least 60% of letters are ASCII (drops Hindi/other-script reviews)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(c.isascii() for c in letters) / len(letters) >= 0.6


def fetch_reviews(weeks: int = WEEKS) -> pd.DataFrame:
    """Page through newest reviews until we pass the cutoff, then keep only `weeks` weeks."""
    cutoff = datetime.now() - timedelta(weeks=weeks)
    rows, token = [], None
    for i in range(MAX_BATCHES):
        batch, token = reviews(APP_ID, lang=LANG, country=COUNTRY, sort=Sort.NEWEST,
                               count=BATCH, continuation_token=token)
        rows += batch
        oldest = min((r["at"] for r in batch), default=cutoff)
        print(f"  batch {i + 1}: total={len(rows)} oldest={oldest:%Y-%m-%d}")
        if not batch or token is None or oldest < cutoff:
            break
    df = pd.DataFrame({
        "review_id": [r["reviewId"] for r in rows],
        "date": [r["at"] for r in rows],
        "rating": [r["score"] for r in rows],
        "text": [r["content"] for r in rows],
        "thumbs_up": [r["thumbsUpCount"] for r in rows],
        "app_version": [r["appVersion"] for r in rows],
    })
    return clean(df, cutoff)


def clean(df: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    df = df[df["date"] >= cutoff]
    df["text"] = df["text"].fillna("").map(lambda t: re.sub(r"\s+", " ", t).strip())
    df = df[df["text"] != ""]
    df = df[df["text"].map(looks_english)]
    df = df.drop_duplicates("review_id").drop_duplicates("text")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def main(weeks: int = WEEKS):
    meta = app(APP_ID, lang=LANG, country=COUNTRY)
    print(f"Verified package {APP_ID}: {meta['title']} by {meta['developer']}")
    df = fetch_reviews(weeks)
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
    main(p.parse_args().weeks)
