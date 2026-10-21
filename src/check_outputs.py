"""Phase 7: check that the deliverables exist and agree with each other and with the review CSV.
Run: python -m src.check_outputs"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
O = ROOT / "outputs"
results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


files = {n: O / n for n in ("analysis.json", "weekly_pulse.md", "fee_explainer.md", "doc_entry.json",
                            "email_draft.txt", "sources.md")}
files["reviews.csv"] = ROOT / "data" / "reviews.csv"
for n, p in files.items():
    check(f"{n} exists", p.exists())
if not all(p.exists() for p in files.values()):
    sys.exit("Missing files; cannot continue.")

analysis = json.loads(files["analysis.json"].read_text())
pulse = files["weekly_pulse.md"].read_text()
expl = files["fee_explainer.md"].read_text()
doc = json.loads(files["doc_entry.json"].read_text())
email = files["email_draft.txt"].read_text()
reviews = pd.read_csv(files["reviews.csv"], dtype={"review_id": str})
texts = dict(zip(reviews.review_id, reviews.text))

# --- brief's final checklist ---
top = analysis["top_themes"]
check("3 top themes", len(top) == 3 and all(t["name"] in pulse for t in top))
quotes = analysis["quotes"]
check("3 verbatim quotes in the CSV", len(quotes) == 3 and all(q["quote"] in texts.get(q["review_id"], "") for q in quotes))
check("quotes under 30 words", all(len(q["quote"].split()) < 30 for q in quotes))
check("pulse quotes are those quotes", all(q["quote"] in pulse for q in quotes))
fee = analysis["fee_issue"]
check("1 fee issue", fee["found"] and bool(fee["fee_name"]), fee.get("fee_name", ""))
fee_quotes = fee.get("quotes", [])
check("2 fee quotes verbatim", len(fee_quotes) == 2 and all(q["quote"] in texts.get(q["review_id"], "") for q in fee_quotes))
words = len(pulse.split())
check("pulse <= 250 words", words <= 250, f"{words} words")
body, _, tail = expl.partition("Official sources:")
bullets = [l for l in body.splitlines() if l.startswith("- ")]
links = [l[2:].strip() for l in tail.splitlines() if l.startswith("- ")]
check("explainer <= 6 bullets", 0 < len(bullets) <= 6, f"{len(bullets)} bullets")
check(">= 2 official links", len(links) >= 2, f"{len(links)} links")
check("'Last checked: YYYY-MM-DD'", bool(re.search(r"Last checked: \d{4}-\d{2}-\d{2}\s*$", expl)))
check("every bullet cites a source", all(re.search(r"\[source \d+\]$", b) for b in bullets))

# --- cross-file consistency ---
check("doc_entry has the 6 required keys", list(doc) == ["date", "top_themes", "weekly_pulse", "identified_fee_issue",
                                                        "explanation_bullets", "source_links"])
check("doc_entry.weekly_pulse == weekly_pulse.md", doc["weekly_pulse"] == pulse)
check("doc_entry bullets == explainer bullets", doc["explanation_bullets"] == [re.sub(r"\s*\[source \d+\]$", "", b[2:]) for b in bullets])
check("doc_entry links == explainer links", doc["source_links"] == links)
check("doc_entry themes == analysis top 3", [t["name"] for t in doc["top_themes"]] == [t["name"] for t in top])
check("doc_entry fee == analysis fee", doc["identified_fee_issue"]["name"] == fee["fee_name"])
subject = f"Weekly Product Pulse + Customer Clarification — {doc['date']}"
check("email subject format", email.startswith(f"Subject: {subject}\n"))
check("email body = pulse + explainer", pulse in email and expl in email)
check("pulse title date == doc date", f"Weekly Product Pulse - {doc['date']}" in pulse)

# --- sources ---
urls = [u.strip() for u in (ROOT / "data" / "sources" / "urls.txt").read_text().splitlines() if u.strip()]
src_md = files["sources.md"].read_text()
check("sources.md lists every URL in urls.txt", all(u in src_md for u in urls), f"{len(urls)} URLs")
check("4 to 6 sources", 4 <= len(urls) <= 6)
check("every explainer link is a listed source", all(l in urls for l in links))

# --- no secrets in git ---
tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
check(".env is not tracked", ".env" not in tracked)
leak = [f for f in tracked if (ROOT / f).is_file() and re.search(r"sk-ant-[A-Za-z0-9_-]{20,}|connect\?token=[A-Za-z0-9=_-]{20,}",
                                                                  (ROOT / f).read_text(errors="ignore"))]
check("no API key or Zapier token in tracked files", not leak, ", ".join(leak))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
