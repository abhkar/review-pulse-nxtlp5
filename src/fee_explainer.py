"""Phase 4: fee explainer with RAG over official pages.

data/sources/urls.txt -> cached text in data/sources/*.txt -> chunks -> TF-IDF retrieval
-> Claude writes <=6 bullets, each tied to a chunk and an exact evidence phrase from it
-> outputs/fee_explainer.md

Page text can come from a fetch (python -m src.fee_explainer --fetch) or be saved by hand as
data/sources/NN_<name>.txt: first line "URL: <url>", then the page text.
"""
import argparse
import json
import re
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.llm import call_claude_json

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "sources"
URLS = SRC / "urls.txt"
OUT = ROOT / "outputs" / "fee_explainer.md"
ANALYSIS = ROOT / "outputs" / "analysis.json"
CHUNK_WORDS = 120
TOP_K = 8
MAX_BULLETS = 6
MIN_PAGE_CHARS = 300  # shorter than this is a block page / empty shell, not an article


def read_urls() -> list:
    if not URLS.exists():
        raise SystemExit("data/sources/urls.txt is missing. Provide 4-6 official URLs, one per line.")
    return [u.strip() for u in URLS.read_text().splitlines() if u.strip() and not u.startswith("#")]


def cache_path(i: int, url: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", url.split("//", 1)[-1].lower()).strip("-")[:60]
    return SRC / f"{i:02d}_{slug}.txt"


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def fetch_all(urls: list) -> None:
    """Fetch each page and cache its text. Report pages that are blocked or empty."""
    for i, url in enumerate(urls, 1):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as e:
            print(f"[{i}] FAILED {url}: {type(e).__name__}")
            continue
        text = clean_html(r.text) if r.ok else ""
        if not r.ok or r.headers.get("cf-mitigated") or len(text) < MIN_PAGE_CHARS:
            print(f"[{i}] BLOCKED/EMPTY {url} (HTTP {r.status_code}, {len(text)} chars)")
            continue
        cache_path(i, url).write_text(f"URL: {url}\n\n{text}\n")
        print(f"[{i}] cached {url} ({len(text)} chars)")


def load_sources(urls: list) -> list:
    """Return [{n, url, text}] for every url with cached text, matched by the URL line."""
    by_url = {}
    for f in sorted(SRC.glob("*.txt")):
        if f.name == "urls.txt":
            continue
        first, _, body = f.read_text().partition("\n")
        if first.startswith("URL:") and len(body.strip()) >= MIN_PAGE_CHARS:
            by_url[first[4:].strip()] = body.strip()
    out = [{"n": i, "url": u, "text": by_url[u]} for i, u in enumerate(urls, 1) if u in by_url]
    missing = [u for u in urls if u not in by_url]
    if missing:
        print("No cached text for:\n  " + "\n  ".join(missing))
    return out


def chunk(sources: list) -> list:
    """Split each page into ~CHUNK_WORDS-word chunks on paragraph boundaries."""
    chunks = []
    for s in sources:
        cur, words, k = [], 0, 1
        for para in [p.strip() for p in s["text"].split("\n") if p.strip()]:
            cur.append(para)
            words += len(para.split())
            if words >= CHUNK_WORDS:
                chunks.append({"id": f"S{s['n']}-C{k}", "src": s["n"], "text": " ".join(cur)})
                cur, words, k = [], 0, k + 1
        if cur:
            chunks.append({"id": f"S{s['n']}-C{k}", "src": s["n"], "text": " ".join(cur)})
    return chunks


def retrieve(chunks: list, query: str, k: int = TOP_K) -> list:
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    m = vec.fit_transform([c["text"] for c in chunks] + [query])
    sims = cosine_similarity(m[-1], m[:-1]).ravel()
    return [chunks[i] for i in sims.argsort()[::-1][:k] if sims[i] > 0]


def explain(fee: dict, sources: list, today: str) -> str:
    chunks = chunk(sources)
    query = f"{fee['fee_name']} {fee['description']} free plan limits upgrade Plus price billing"
    top = retrieve(chunks, query)
    by_id = {c["id"]: c for c in top}
    print(f"{len(chunks)} chunks; retrieved {len(top)}: {[c['id'] for c in top]}")
    prompt = f"""Customer question: why do free users hit limits and get asked to upgrade, and what do the official pages say?
Fee issue from user reviews: "{fee['fee_name']}" - {fee['description']}

Write AT MOST {MAX_BULLETS} bullets using ONLY the source chunks below. Neutral, facts-only tone, no promises,
no advice, no speculation. If the chunks do not state something, do not say it.
The customer is in India: if a chunk states prices in INR (₹), include one bullet with them, exactly as stated
(including whether GST is included), and leave out USD prices unless no INR price is given. Each bullet needs:
- "chunk_id": the chunk it comes from
- "evidence": an exact phrase (copied character for character) from that chunk that supports the bullet
- "text": the bullet, max 30 words

Return JSON: {{"bullets": [{{"chunk_id": "...", "evidence": "...", "text": "..."}}]}}

Chunks:
""" + "\n\n".join(f"[{c['id']}] {c['text']}" for c in top)
    bullets = []
    for _ in range(3):
        for b in call_claude_json(prompt, max_tokens=2000)["bullets"]:
            c = by_id.get(b.get("chunk_id"))
            if c and b.get("evidence", "") in c["text"] and b["evidence"].strip():
                bullets.append(b)
        if bullets:
            break
    if not bullets:
        raise RuntimeError("No bullet could be tied to a source chunk with verbatim evidence.")
    bullets = bullets[:MAX_BULLETS]

    # Links: every source a bullet cites (at least 2 when 2+ are cited), so each bullet is traceable.
    url_of = {s["n"]: s["url"] for s in sources}
    cites = {}
    for b in bullets:
        n = by_id[b["chunk_id"]]["src"]
        cites[n] = cites.get(n, 0) + 1
    links = [url_of[n] for n in sorted(cites, key=lambda n: (-cites[n], n))]

    lines = ["# What the official sources say about free-plan limits and upgrades", ""]
    lines += [f"- {b['text']} [source {by_id[b['chunk_id']]['src']}]" for b in bullets]
    lines += ["", "Official sources:"] + [f"- {u}" for u in links] + ["", f"Last checked: {today}"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="fetch the URLs and cache their text")
    args = ap.parse_args()
    urls = read_urls()
    if args.fetch:
        fetch_all(urls)
    sources = load_sources(urls)
    if len(sources) < 2:
        raise SystemExit(f"Only {len(sources)} source page(s) have cached text; need at least 2. "
                         "Run with --fetch, or save page text into data/sources/ (see module docstring).")
    fee = json.loads(ANALYSIS.read_text())["fee_issue"]
    md = explain(fee, sources, date.today().isoformat())
    OUT.write_text(md)
    print(md)


if __name__ == "__main__":
    main()
