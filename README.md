# Review-to-Insight AI Workflow (ChatGPT Android app, India)

A Streamlit app that turns raw Google Play reviews of the **ChatGPT app (India)** into

1. an internal **weekly product pulse** (250 words or fewer), and
2. a customer-facing **fee explainer** grounded in official OpenAI pages (RAG),

and then, **only after a human clicks Approve**, appends both to a Google Doc and creates a Gmail **draft** through
Zapier MCP. Nothing is ever sent automatically, and the draft is never emailed.

```
reviews.csv ─► analyze.py ─► analysis.json ─► pulse.py ─► weekly_pulse.md ─┐
 (Play Store)   themes, quotes,                                            ├─► doc_entry.json + email_draft.txt
                fee issue        fee_explainer.py (RAG) ─► fee_explainer.md ┘            │
                                                                              Approval tab (Approve / Reject)
                                                                                         │
                                                                   Zapier MCP: Google Docs append + Gmail draft
```

## What was found

Data: 9,009 English reviews, 2026-07-29 to 2026-10-20 (a weighted sample, see Limitations).

| Top themes | Reviews | Share |
|---|---|---|
| Inaccurate or unreliable answers | 790 | 8.8% |
| Learning, study help and personal companionship (praise) | 789 | 8.8% |
| Usage limits and paywall frustration | 646 | 7.2% |

**Fee issue identified from the reviews: "Paywalled usage limits".** 190 reviews (2.1% of the sample) say free users hit
chat, image, upload or time caps and are pushed to buy Plus. 638 reviews matched money-related keywords. Related
categories the analysis also found: paid plans still limited or poor value (98), price too high (46), payment taken
but plan not activated (20), cancellation and unwanted recurring charges (15).
Reviews are the source of the issue, and the official pages in `data/sources/` are the source of the explainer's facts.

Every quote shown is a verbatim substring of a review in `data/reviews.csv`, checked in code.

## Where MCP approval happens

In the app's **Approval tab**. It shows the exact Google Doc JSON and the exact Gmail draft text, then two buttons:

- **Approve & Execute**: the only thing that can call Google Docs / Gmail. It calls
  `execute_approved_actions(approved=True)`, which raises `PermissionError` for anything else.
- **Reject**: sends nothing.

Both buttons lock after one click. A rerun cannot re-send, because the decision lives in `st.session_state` and an
already-logged payload is never sent twice. Every action is logged with a timestamp in `outputs/mcp_log.json`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env (it is git-ignored)
streamlit run app.py
```

`.env` variables:

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | A Console API key (starts `sk-ant-api03-`). |
| `ANTHROPIC_MODEL` | Model ID, e.g. `claude-sonnet-5-5`. |
| `SIMULATED_MODE` | `true` (default): Approve logs actions to `outputs/mcp_log.json` and sends nothing. `false`: real Zapier calls. |
| `ZAPIER_MCP_URL` | `https://mcp.zapier.com/api/v1/connect?token=...` (live mode only). |
| `GOOGLE_DOC_ID` | ID of the Google Doc to append to (live mode only). |
| `DRAFT_TO` | Optional recipient pre-filled on the Gmail draft. |

The committed outputs already exist, so the app opens without any API call. **Run analysis** in the sidebar
regenerates everything and uses about 125 LLM calls.

Command line, same code: `python -m src.scrape_reviews`, `python -m src.analyze`, `python -m src.pulse`,
`python -m src.fee_explainer`, `python -m src.mcp_actions` (builds the two payloads only), and
`python -m src.check_outputs` (consistency checks across all deliverables).

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub and create an app at https://share.streamlit.io pointing at `app.py`.
2. In the app's **Settings > Secrets**, add:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-api03-..."
   ANTHROPIC_MODEL = "claude-sonnet-5-5"
   SIMULATED_MODE = "true"
   # live mode only:
   # ZAPIER_MCP_URL = "https://mcp.zapier.com/api/v1/connect?token=..."
   # GOOGLE_DOC_ID = "..."
   ```
3. Leave `SIMULATED_MODE = "true"` for a public demo, so a visitor clicking Approve cannot write to your Google
   account. Anyone who can open a deployed app can press its buttons; use live mode only on a private deployment.

## Zapier MCP setup (live mode)

1. In Zapier, connect **Google Docs** and **Gmail** under App connections.
2. Go to https://mcp.zapier.com, create an MCP server, and in its **Apps** tab add Google Docs
   ("Append Text to Document") and Gmail ("Create Draft"). Do not add "Send Email".
3. **Connect client > See all > Other**, then on the **Connect** tab click **Generate token**. The token is shown
   once. The URL is `https://mcp.zapier.com/api/v1/connect?token=<token>`. (Clients on Zapier's supported list use
   OAuth instead, which a Python app cannot use.)
4. Create a Google Doc (this project used "Review Pulse Log") and find its ID, the long string in its URL
   (`docs.google.com/document/d/<ID>/edit`).
5. Put the URL and the Doc ID in `.env` or the Streamlit secrets and set `SIMULATED_MODE=false`.
6. Treat the token like a password. Anyone holding it can run the server's tools. Rotate it by regenerating it.

The code uses Zapier's generic tool `execute_zapier_write_action` with `GoogleDocsV2CLIAPI`/`append` and
`GoogleMailV2CLIAPI`/`draft_v2`, found by inspecting the live server. It speaks Streamable HTTP (Zapier does not
support SSE) and works with `mcp` 1.x and 2.x.

**Verification status.** Against the real Zapier server I verified the connection, the tool list, that both apps
are enabled and connected, and the parameters of both actions. The write path itself was tested against a local
stand-in server. **The real Docs append and Gmail draft have not been executed yet**; run them once through the
Approval tab with `SIMULATED_MODE=false` and check the Doc and your Drafts folder.

## Project layout

```
app.py                  Streamlit UI (Themes and Quotes, Weekly Pulse, Fee Explainer, Approval)
src/llm.py              Claude helper: JSON output, fence stripping, retry
src/scrape_reviews.py   Play Store scraper (12 weeks, weighted sample per day)
src/analyze.py          themes, verbatim quotes, fee issue  -> outputs/analysis.json
src/pulse.py            weekly pulse, word limit enforced in code
src/fee_explainer.py    RAG: cached official pages -> TF-IDF chunks -> <= 6 sourced bullets
src/mcp_actions.py      payloads, approval gate, simulated mode, Zapier MCP client, action log
src/check_outputs.py    consistency checks
data/reviews.csv        9,009 sampled reviews          data/sources/   cached official page text
outputs/                analysis.json, weekly_pulse.md, fee_explainer.md, doc_entry.json,
                        email_draft.txt, sources.md (mcp_log.json appears after an approval)
```

## How the guarantees are enforced

- **Verbatim quotes:** the model proposes a quote and the code checks `quote in review_text` and the word limit,
  re-asking with feedback, with a last fallback to a whole short review. A final assertion runs before saving.
- **Pulse length and facts:** the code counts words and regenerates with tighter limits up to 3 times. Quotes are
  inserted by code, and every number in the model's text must appear in `analysis.json`.
- **Explainer grounding:** each bullet must name a retrieved chunk and give an exact evidence phrase from it. The code
  verifies the phrase and drops bullets that fail. The prompt forbids claims the chunks do not make.
- **No auto-send:** the gate above, plus the Gmail action being a draft only.

## Limitations

- **Sampled data.** The app gets roughly 6,600 raw reviews a day (555,400 scanned over 12 weeks), so the CSV is a weighted sample (up to 150 per day,
  seeded, favouring 1-3 star and longer reviews). Theme counts and shares are shares of this sample, not of all reviews,
  and 5-star reviews are under-represented (56% here against about 75% in a scrape of the newest 9 days).
- **LLM classification is approximate.** Themes are defined and applied by a model with no human check, and reruns give
  slightly different names and counts. 63% of reviews fit none of the five themes, because many are short praise.
- **"Fee confusion" is mostly paywall frustration.** The largest fee category is about usage caps and upselling more
  than a misunderstanding of a price or tax. In this sample, "GST"/"tax" appears in 1 review, "refund" in 8 and "renew"/auto-pay in 2.
- **Official pages were pasted, not fetched.** OpenAI's help pages block scripted requests (Cloudflare). The text was
  copied by hand and trimmed to the article body. `python -m src.fee_explainer --fetch` works only where the pages
  are reachable. Pages and limits change over time, hence the "Last checked" date. The pricing source is the in-app
  `https://chatgpt.com/#pricing` view, which is a fragment URL and may change.
- **Three links, not two.** The explainer lists every source its bullets cite (3 in the current run), so each bullet is
  traceable. The assignment asks for 2.
- **English heuristic.** The language filter drops mostly non-Latin-script reviews but keeps romanised Hindi.
- **Weekly pulse covers 12 weeks.** The title format is weekly, and the header states the real date range.
- **Secrets.** API keys and the Zapier token belong only in `.env` or Streamlit secrets. Rotate any that were shared
  outside those places.
