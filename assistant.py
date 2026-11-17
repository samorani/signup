"""Ask Claude which free slot fits a request written in plain words.

Claude only suggests a slot: this module never books anything. The caller
shows the suggestion with a Book button, and the person decides.

The module knows nothing about the database or Flask. It gets the free slots
as a list of {"id": "YYYY-MM-DD HH:MM", "label": "Thursday, Nov 19, 2:30-3:00 pm"}
and returns a dict with the suggestion plus the exact messages exchanged with
Claude, so the page can show them while we debug.
"""

import json
import os

import anthropic

MODEL = "claude-opus-5-5"
MAX_REQUEST_CHARS = 300

SYSTEM_PROMPT = """\
You help a student choose a slot for a professor's office hours.

You get the current date and time, the list of free slots (each with an id), \
and the student's request in their own words. Choose the one free slot that \
best fits the request. If several fit equally well, choose the earliest. If \
none fits, or the request is not about choosing a time, choose none.

Read relative dates ("this week", "Thursday", "tomorrow") against the current \
date. A week runs Monday to Sunday. Every slot in the list is in the future \
and free, so you never need to check that.

Reply with:
- slot_id: the id of the chosen slot, copied exactly from the list, or "none".
- reason: one or two short sentences to the student, saying why this slot fits \
or why none does. Mention the day and time in words, not the id.

You only suggest. The student books the slot themselves."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "slot_id": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["slot_id", "reason"],
    "additionalProperties": False,
}

_client = None


def is_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def build_request(request_text, slots, now):
    """The parameters of the Messages API call, as a plain dict."""
    lines = "\n".join(f"{s['id']}  ({s['label']})" for s in slots) or "(no free slots)"
    user_message = (
        f"Current date and time: {now:%A, %Y-%m-%d, %H:%M}\n\n"
        f"Free slots:\n{lines}\n\n"
        f"Student's request:\n<request>\n{request_text}\n</request>"
    )
    return {
        "model": MODEL,
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
        # Picking from a short list is an easy task: low effort keeps it fast.
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        # If a safety classifier declines, the API retries on a fallback model.
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def call_claude(params):
    """Send the request and return the response as a plain dict. The only
    network call in this module; the tests replace it."""
    global _client
    if _client is None:
        # The key comes from ANTHROPIC_API_KEY (.env locally, Secret Manager on
        # Cloud Run). Fail fast: someone is waiting on the page.
        _client = anthropic.Anthropic(timeout=60.0, max_retries=1)
    return _client.beta.messages.create(**params).to_dict()


def suggest_slot(request_text, slots, now):
    """Ask Claude for the best slot. Returns a dict:

    slot_id   the chosen slot's id, or None if none fits
    reason    Claude's explanation, for the student
    error     a message for the student if something went wrong, else None
    sent      the request parameters (for the debug panel)
    received  the raw response, or the error, as a dict (for the debug panel)
    """
    result = {"slot_id": None, "reason": "", "error": None, "sent": None, "received": None}
    if not is_configured():
        result["error"] = "AI suggestions are not set up on this site (ANTHROPIC_API_KEY is missing)."
        return result

    params = build_request(request_text, slots, now)
    result["sent"] = params
    try:
        response = call_claude(params)
    except anthropic.APIConnectionError as e:
        result["received"] = {"error": type(e).__name__, "message": str(e)}
        result["error"] = "Could not reach Claude. Please try again."
        return result
    except anthropic.APIStatusError as e:
        result["received"] = {"error": type(e).__name__, "status": e.status_code, "body": e.body}
        result["error"] = "Claude could not answer right now. Please try again."
        return result
    result["received"] = response

    stop_reason = response.get("stop_reason")
    if stop_reason == "refusal":
        result["error"] = "Claude declined to answer that request."
        return result
    if stop_reason == "max_tokens":
        result["error"] = "Claude's answer was cut off. Please try a shorter request."
        return result

    text = next((b["text"] for b in response.get("content", []) if b.get("type") == "text"), "")
    try:
        answer = json.loads(text)
        slot_id, reason = str(answer["slot_id"]).strip(), str(answer["reason"]).strip()
    except (ValueError, KeyError, TypeError):
        result["error"] = "Claude's answer could not be read. Please try again."
        return result

    result["reason"] = reason
    # Never trust the id blindly: it must be one of the free slots we sent.
    if slot_id in {s["id"] for s in slots}:
        result["slot_id"] = slot_id
    elif slot_id.lower() != "none":
        result["reason"] = ""
        result["error"] = "Claude suggested a slot that is not free. Please try again."
    return result
