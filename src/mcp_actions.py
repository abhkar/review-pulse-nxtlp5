"""Phase 5: approval-gated MCP actions (Google Doc append + Gmail draft) via Zapier MCP.

build_payloads()          writes outputs/doc_entry.json and outputs/email_draft.txt. Sends nothing.
execute_approved_actions  only runs when called with approved=True (the UI's Approve button).
                          SIMULATED_MODE=true logs the actions to outputs/mcp_log.json instead of
                          calling Zapier. Every action is logged with a timestamp and result.
"""
import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
LOG = OUTPUTS / "mcp_log.json"


# ---------- 1. build payloads (no side effects besides writing local files) ----------

def _bullets_and_links(explainer_md: str):
    """Split fee_explainer.md into its bullets and its official source links."""
    head, _, tail = explainer_md.partition("Official sources:")
    bullets = [re.sub(r"\s*\[source \d+\]$", "", l[2:].strip()) for l in head.splitlines() if l.startswith("- ")]
    links = [l[2:].strip() for l in tail.splitlines() if l.startswith("- ")]
    return bullets, links


def build_payloads() -> dict:
    analysis = json.loads((OUTPUTS / "analysis.json").read_text())
    pulse = (OUTPUTS / "weekly_pulse.md").read_text()
    explainer = (OUTPUTS / "fee_explainer.md").read_text()
    m = re.search(r"Weekly Product Pulse - (\d{4}-\d{2}-\d{2})", pulse)
    day = m.group(1) if m else date.today().isoformat()
    bullets, links = _bullets_and_links(explainer)
    fee = analysis["fee_issue"]

    doc_entry = {
        "date": day,
        "top_themes": [{"name": t["name"], "review_count": t["review_count"], "share_pct": t["share_pct"]}
                       for t in analysis["top_themes"]],
        "weekly_pulse": pulse,
        "identified_fee_issue": {"name": fee.get("fee_name"), "description": fee.get("description"),
                                 "review_count": fee.get("review_count")},
        "explanation_bullets": bullets,
        "source_links": links,
    }
    subject = f"Weekly Product Pulse + Customer Clarification — {day}"
    body = (f"{pulse}\n---\nCustomer support snippet (reusable):\n\n{explainer}")
    email = {"subject": subject, "body": body}

    (OUTPUTS / "doc_entry.json").write_text(json.dumps(doc_entry, indent=2, ensure_ascii=False))
    (OUTPUTS / "email_draft.txt").write_text(f"Subject: {subject}\n\n{body}")
    return {"doc_entry": doc_entry, "email": email}


# ---------- 2. logging ----------

def _read_log() -> list:
    try:
        return json.loads(LOG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _log(entry: dict) -> dict:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    log = _read_log()
    log.append(entry)
    OUTPUTS.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False))
    return entry


def payload_hash(payloads: dict) -> str:
    return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()[:16]


# ---------- 3. live Zapier MCP call ----------
# This Zapier server exposes generic tools (execute_zapier_write_action, ...), not one tool per action.
# We call it with the app's selected_api and action key, found via inspect_zapier_actions:
#   Google Docs: GoogleDocsV2CLIAPI / append   (params: file, text, newline)
#   Gmail:       GoogleMailV2CLIAPI / draft_v2 (params: subject, body, to, body_type)

DOCS_API, DOCS_ACTION = "GoogleDocsV2CLIAPI", "append"
GMAIL_API, GMAIL_ACTION = "GoogleMailV2CLIAPI", "draft_v2"


def _is_error(result) -> bool:
    return bool(getattr(result, "isError", getattr(result, "is_error", False)))


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content).strip()


async def _run_live(payloads: dict) -> list:
    url = os.getenv("ZAPIER_MCP_URL")
    if not url:
        raise RuntimeError("ZAPIER_MCP_URL is not set. Set it, or use SIMULATED_MODE=true.")
    doc_name = os.getenv("GOOGLE_DOC_ID")
    if not doc_name:
        raise RuntimeError("GOOGLE_DOC_ID is not set: the Google Doc (name or ID as Zapier lists it) to append to.")
    from mcp import ClientSession
    try:                                   # mcp >= 2
        from mcp.client.streamable_http import streamable_http_client as http_client
    except ImportError:                    # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client as http_client

    doc, email = payloads["doc_entry"], payloads["email"]
    doc_text = (f"Weekly Product Pulse {doc['date']}\n\n{doc['weekly_pulse']}\n\n"
                f"Fee issue: {doc['identified_fee_issue']['name']}\n"
                + "\n".join(f"- {b}" for b in doc["explanation_bullets"])
                + "\nSources:\n" + "\n".join(doc["source_links"]))
    email_params = {"subject": email["subject"], "body": email["body"], "body_type": "plain"}
    if os.getenv("DRAFT_TO"):
        email_params["to"] = os.getenv("DRAFT_TO")
    plan = [
        ("google_docs_append", DOCS_API, DOCS_ACTION, {"file": doc_name, "text": doc_text, "newline": "true"}),
        ("gmail_create_draft", GMAIL_API, GMAIL_ACTION, email_params),
    ]
    results = []
    async with http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            for action, api, key, params in plan:
                try:
                    res = await session.call_tool("execute_zapier_write_action",
                                                  {"selected_api": api, "action": key, "params": params})
                    results.append({"action": action, "tool": f"{api}/{key}",
                                    "status": "error" if _is_error(res) else "success", "result": _text(res)})
                except Exception as e:  # keep going so the second action is still attempted
                    results.append({"action": action, "tool": f"{api}/{key}", "status": "error",
                                    "result": f"{type(e).__name__}: {e}"})
    return results


# ---------- 4. approval-gated entry point ----------

def execute_approved_actions(approved: bool = False, payloads: dict | None = None) -> list:
    """Run the two actions. Raises unless `approved` is exactly True (set by the UI's Approve button).
    A payload that already succeeded is not sent again (protects against Streamlit reruns)."""
    if approved is not True:
        raise PermissionError("Actions require explicit approval; nothing was sent.")
    payloads = payloads or build_payloads()
    h = payload_hash(payloads)
    simulated = os.getenv("SIMULATED_MODE", "true").lower() == "true"
    mode = "simulated" if simulated else "live"

    done = [e for e in _read_log() if e.get("payload_hash") == h and e.get("mode") == mode
            and e.get("status") in ("success", "simulated")]
    if done:
        return done

    if simulated:
        results = [{"action": "google_docs_append", "status": "simulated", "result": "not sent (SIMULATED_MODE)",
                    "payload": payloads["doc_entry"]},
                   {"action": "gmail_create_draft", "status": "simulated", "result": "not sent (SIMULATED_MODE)",
                    "payload": payloads["email"]}]
    else:
        try:
            results = asyncio.run(_run_live(payloads))
        except BaseException as e:  # connection / protocol failures arrive as exception groups
            inner = e.exceptions[0] if hasattr(e, "exceptions") and e.exceptions else e
            err = f"{type(inner).__name__}: {inner}"
            return [_log({"mode": mode, "payload_hash": h, "action": a, "status": "error", "result": err})
                    for a in ("google_docs_append", "gmail_create_draft")]
    return [_log({"mode": mode, "payload_hash": h, **r}) for r in results]


if __name__ == "__main__":
    p = build_payloads()
    print("Built outputs/doc_entry.json and outputs/email_draft.txt. Nothing was sent.")
    print("Subject:", p["email"]["subject"])
