"""Phase 6: Streamlit UI. Nothing is sent to Google Docs or Gmail until the Approve button is pressed."""
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

# On Streamlit Cloud, secrets live in st.secrets; mirror them into the environment the modules read.
for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ZAPIER_MCP_URL", "SIMULATED_MODE", "GOOGLE_DOC_ID", "DRAFT_TO"):
    try:
        if _k in st.secrets and _k not in os.environ:
            os.environ[_k] = str(st.secrets[_k])
    except Exception:  # no secrets file locally
        pass

from src import analyze, fee_explainer, mcp_actions, pulse, scrape_reviews  # noqa: E402

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "data" / "reviews.csv"
ANALYSIS = ROOT / "outputs" / "analysis.json"
PULSE = ROOT / "outputs" / "weekly_pulse.md"
EXPLAINER = ROOT / "outputs" / "fee_explainer.md"

st.set_page_config(page_title="Review Pulse", page_icon="📊", layout="wide")


@st.cache_data
def read_text(path: str, mtime: float) -> str:
    """Cached file read; the mtime argument busts the cache when a file is regenerated."""
    return Path(path).read_text()


def load(path: Path):
    return read_text(str(path), path.stat().st_mtime) if path.exists() else None


def is_simulated() -> bool:
    return os.getenv("SIMULATED_MODE", "true").lower() == "true"


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("Review Pulse")
    st.caption("ChatGPT Android app, India · Play Store reviews")

    st.subheader("1. Reviews")
    if CSV.exists():
        df = pd.read_csv(CSV)
        st.success(f"{len(df):,} reviews\n\n{df['date'].min()} to {df['date'].max()}")
    else:
        st.warning("No data/reviews.csv yet.")
    up = st.file_uploader("Load a CSV instead", type="csv", help="Columns: review_id, date, rating, text, thumbs_up, app_version")
    if up is not None and st.button("Use uploaded CSV"):
        new = pd.read_csv(up)
        need = {"review_id", "date", "rating", "text", "thumbs_up", "app_version"}
        if need <= set(new.columns):
            new.to_csv(CSV, index=False)
            st.rerun()
        else:
            st.error(f"Missing columns: {sorted(need - set(new.columns))}")
    with st.expander("Re-scrape from Play Store (slow)"):
        st.caption("Streams ~500,000 raw reviews for 12 weeks: about 40 minutes.")
        if st.checkbox("I want to re-scrape") and st.button("Start scrape"):
            with st.spinner("Scraping... this takes a while"):
                scrape_reviews.main()
            st.rerun()

    st.subheader("2. Analysis")
    st.caption("Runs themes, quotes, fee issue, pulse and fee explainer: about 125 LLM calls.")
    if st.checkbox("I understand this uses API credits") and st.button("Run analysis", type="primary"):
        if not os.getenv("ANTHROPIC_API_KEY"):
            st.error("ANTHROPIC_API_KEY is not set. Add it to .env (or Streamlit secrets) and restart. "
                     "You don't need it just to view the saved results or use the Approval tab.")
        else:
            with st.status("Running analysis...", expanded=True) as status:
                try:
                    st.write("Themes, quotes, fee issue")
                    analyze.main()
                    st.write("Weekly pulse")
                    pulse.main()
                    st.write("Fee explainer")
                    fee_explainer.run()
                    status.update(label="Analysis complete", state="complete")
                    done = True
                except (Exception, SystemExit) as e:
                    status.update(label="Analysis failed", state="error")
                    st.error(f"{type(e).__name__}: {e}")
                    done = False
            if done:
                st.session_state.pop("decision", None)  # new outputs need a fresh approval
                st.rerun()

    st.divider()
    st.caption(("Simulated mode: no real calls" if is_simulated() else "LIVE mode: Approve will write to Google"))

analysis_txt, pulse_md, explainer_md = load(ANALYSIS), load(PULSE), load(EXPLAINER)
if not analysis_txt:
    st.info("No analysis yet. Use **Run analysis** in the sidebar.")
    st.stop()
analysis = json.loads(analysis_txt)

t_themes, t_pulse, t_fee, t_approve = st.tabs(["Themes and Quotes", "Weekly Pulse", "Fee Explainer", "Approval"])

# ---------------- tab 1 ----------------
with t_themes:
    m = analysis["meta"]
    st.caption(f"{m['reviews_analyzed']:,} reviews, {m['date_from']} to {m['date_to']}. {m['note']}")
    themes = pd.DataFrame(analysis["themes"])
    st.subheader("Themes")
    st.bar_chart(themes.set_index("name")["review_count"], horizontal=True)
    st.dataframe(themes[["name", "review_count", "share_pct", "description"]], hide_index=True, width="stretch")
    st.caption(f"{analysis['unthemed_reviews']:,} reviews fit none of the themes.")

    st.subheader("Top 3 themes: verbatim quotes")
    for q in analysis["quotes"]:
        st.markdown(f"**{q['theme']}**")
        st.markdown(f"> “{q['quote']}”  \n> {q['date']} · {q['rating']}★")

    st.subheader("Fee issue from reviews")
    f = analysis["fee_issue"]
    if f["found"]:
        st.markdown(f"**{f['fee_name']}**: {f['review_count']} reviews ({f['share_of_all_reviews_pct']}% of sample); "
                    f"{f['keyword_matches']} reviews matched money keywords.")
        st.write(f["description"])
        for q in f["quotes"]:
            st.markdown(f"> “{q['quote']}”  \n> {q['date']} · {q['rating']}★")
    else:
        st.write(f"No recurring fee confusion found. Closest billing theme: {f.get('closest_billing_theme')}")

# ---------------- tab 2 ----------------
with t_pulse:
    if pulse_md:
        st.markdown(pulse_md)
        st.caption(f"{len(pulse_md.split())} words (limit 250)")
    else:
        st.info("No weekly pulse yet.")

# ---------------- tab 3 ----------------
with t_fee:
    if explainer_md:
        st.markdown(explainer_md)
    else:
        st.info("No fee explainer yet. It needs cached official source pages in data/sources/.")

# ---------------- tab 4: approval ----------------
with t_approve:
    st.header("Approval")
    if not (pulse_md and explainer_md):
        st.info("Generate the pulse and fee explainer first.")
        st.stop()

    payloads = mcp_actions.build_payloads()  # writes the two local files only; sends nothing
    phash = mcp_actions.payload_hash(payloads)
    if st.session_state.get("payload_hash") != phash:  # content changed: old decision no longer applies
        st.session_state.update(payload_hash=phash, decision=None, results=None)

    if is_simulated():
        st.info("SIMULATED_MODE is on: approving logs the actions to outputs/mcp_log.json and sends nothing.")
    else:
        st.warning("LIVE mode: Approve will append this entry to your Google Doc and create a Gmail **draft** "
                   "(it is not sent) through Zapier MCP.")

    st.subheader("1. Google Doc entry (exact JSON)")
    st.json(payloads["doc_entry"], expanded=2)
    st.subheader("2. Gmail draft (exact text, not sent)")
    st.text_area("Draft", f"Subject: {payloads['email']['subject']}\n\n{payloads['email']['body']}",
                 height=300, disabled=True, label_visibility="collapsed")

    def approve():
        st.session_state.decision = "approved"
        try:
            st.session_state.results = mcp_actions.execute_approved_actions(approved=True, payloads=payloads)
        except Exception as e:
            st.session_state.results = [{"action": "execute", "status": "error", "result": f"{type(e).__name__}: {e}"}]

    def reject():
        st.session_state.decision = "rejected"

    decision = st.session_state.get("decision")
    c1, c2, _ = st.columns([1, 1, 3])
    c1.button("Approve & Execute", type="primary", on_click=approve, disabled=decision is not None)
    c2.button("Reject", on_click=reject, disabled=decision is not None)

    if decision == "rejected":
        st.error("Rejected. Nothing was sent.")
    elif decision == "approved":
        results = st.session_state.get("results") or []
        if results and all(r["status"] in ("success", "simulated") for r in results):
            st.success("Approved and executed." if not is_simulated() else "Approved (simulated): nothing was sent.")
        else:
            st.error("Approved, but at least one action failed. Details below.")
        for r in results:
            icon = {"success": "✅", "simulated": "🧪"}.get(r["status"], "❌")
            st.markdown(f"{icon} **{r['action']}**: {r['status']}  \n`{str(r.get('result', ''))[:400]}`")
        doc_id = os.getenv("GOOGLE_DOC_ID")
        if not is_simulated() and doc_id and any(r["action"] == "google_docs_append" and r["status"] == "success" for r in results):
            st.markdown(f"[Open the Google Doc](https://docs.google.com/document/d/{doc_id}/edit)")
        if not is_simulated() and any(r["action"] == "gmail_create_draft" and r["status"] == "success" for r in results):
            st.markdown("[Open Gmail drafts](https://mail.google.com/mail/u/0/#drafts)")
    else:
        st.caption("Nothing has been sent. Choose Approve & Execute or Reject.")
