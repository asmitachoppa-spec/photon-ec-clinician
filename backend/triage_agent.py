"""AI-drafted urgency annotation for the clinician inbox.

Design intent, deliberately narrow -- same spirit as catalog_agent.py in the
sibling patient app: this NEVER makes or re-makes a clinical judgment. The
deterministic eligibility engine (photon-ec-patient's eligibility.py) has
already decided, per case, which EC options are time/weight eligible, which
one it would recommend, and whether the case needs to be escalated -- all of
that ships over in `eligibility_summary` before this module ever runs. This
agent's only job is to translate that already-computed verdict into a short,
skimmable priority note for a clinician scanning a list of several pending
cases at once, so the ones where the clock matters more (EC has a hard
effectiveness window that keeps closing) don't get buried under ones that
don't. It is not told to, and must not, decide eligibility, suggest a
treatment, or add any interaction/allergy claim that isn't already sitting in
the data it's given.

If this can't run (no API key, network unreachable, malformed output), the
caller shows the inbox with no annotation at all -- exactly what the inbox
already looked like before this existed. Nothing about triage is required
for the review workflow to function.

Same raw-urllib-to-the-Messages-API pattern as this app's own assistant.py,
kept consistent rather than pulling in the Anthropic SDK for one more small,
single-turn call. Unlike catalog_agent.py this doesn't need a tool-use loop
-- everything it's allowed to reason about is already in the one payload it's
handed, so this is a single pinned tool call (workflow style), not a
multi-turn agent.
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("triage_agent")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

TRIAGE_TOOL = {
    "name": "triage_note",
    "description": (
        "Record a one-line urgency label and note for a clinician skimming "
        "an inbox of pending cases. Base this ONLY on the deterministic "
        "eligibility summary you're given -- never introduce a new clinical "
        "claim, interaction, or recommendation of your own."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "urgency": {
                "type": "string",
                "enum": ["urgent", "time_sensitive", "routine"],
                "description": (
                    "urgent: the deterministic engine's own escalate flag is "
                    "true (no option is eligible), or every remaining "
                    "eligible option already carries an efficacy caveat. "
                    "time_sensitive: at least one option is still cleanly "
                    "eligible but a shorter-window option (e.g. "
                    "levonorgestrel's 72-hour label) is about to close. "
                    "routine: a clean, uncomplicated eligible option with "
                    "no caveats and no time pressure yet."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "One short sentence (under ~20 words) a clinician can "
                    "read in under 3 seconds -- restate the deterministic "
                    "engine's own reasons/efficacy_note in plain language, "
                    "don't add anything it didn't already say."
                ),
            },
        },
        "required": ["urgency", "note"],
    },
}

SYSTEM_PROMPT = """You annotate a clinician's inbox of pending emergency contraception (EC) handoff cases with a short urgency note, so the ones where time matters most don't get buried in a list.

A DETERMINISTIC, non-AI eligibility engine has already decided, for this exact case, which options are time/weight eligible, which one (if any) it would recommend, and whether the case needs escalation because nothing is eligible. You are never asked to re-decide any of that, and you have no prescribing or clinical authority here.

Ground rules:
- Use only the eligibility_summary, hours_since_intercourse, weight_lb, and interacting_meds_found you are given. Never invent a clinical fact, interaction, or threshold that isn't already present in that data.
- If eligibility_summary.escalate is true, urgency must be "urgent".
- Otherwise, base urgency on how much runway is left and whether the remaining eligible options carry efficacy caveats -- not on your own read of the patient's situation.
- Keep the note to one short, plain-language sentence restating what the deterministic data already says. Do not add a new recommendation, and do not say anything reassuring or alarming that isn't grounded in the supplied reasons/efficacy_note text.
- Always call the triage_note tool. Never respond in plain prose.
"""


def _post(payload: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Anthropic API: {e.reason}") from e


def triage_case(case: dict) -> Optional[dict]:
    """case needs: hours_since_intercourse, weight_lb,
    interacting_meds_found, eligibility_summary (the dict shape
    eligibility.result_to_dict() produces). Returns {"urgency", "note"} on
    success, or None if the model is unavailable/unreachable or didn't
    return a usable annotation -- the inbox just renders with no badge for
    that row in that case, same as before this existed."""

    summary = case.get("eligibility_summary")
    if not summary:
        # Nothing deterministic to ground a note in -- don't guess.
        return None

    context = {
        "hours_since_intercourse": case.get("hours_since_intercourse"),
        "weight_lb": case.get("weight_lb"),
        "interacting_meds_found": case.get("interacting_meds_found"),
        "eligibility_summary": summary,
    }
    user_content = (
        "Here is the deterministic eligibility engine's output for one "
        "pending case. Annotate it.\n\n" + json.dumps(context, indent=2, default=str)
    )

    try:
        message = _post({
            "model": MODEL,
            "max_tokens": 256,
            "system": SYSTEM_PROMPT,
            "tools": [TRIAGE_TOOL],
            "tool_choice": {"type": "tool", "name": "triage_note"},
            "messages": [{"role": "user", "content": user_content}],
        })
    except RuntimeError as e:
        logger.info("triage_agent unavailable: %s", e)
        return None

    for block in message.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "triage_note":
            result = block.get("input") or {}
            if result.get("urgency") and result.get("note"):
                return {"urgency": result["urgency"], "note": result["note"]}
            return None

    logger.info("triage_agent did not return a structured note (unexpected)")
    return None
