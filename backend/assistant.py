"""The AI assistant's reasoning layer -- same pattern as photon-clinic-
assistant's assistant.py, adapted for an EC handoff instead of a refill.

Design intent, unchanged from that project:
  - The model NEVER decides eligibility -- that's eligibility.py's job,
    already done before this ever runs. This module only drafts a
    recommendation about the SPECIFIC treatment the clinician clicked,
    for the clinician to review, edit, or reject.
  - It only reasons over data actually pulled from Photon (the real
    interaction/allergy screen) plus the patient's reported intake -- it
    is explicitly told not to invent a clinical fact that wasn't supplied.
  - Output is constrained to a strict tool-call schema.
  - `requires_human_review` is always true by design -- there is no code
    path where this output goes anywhere except in front of a clinician,
    who still has to click "Approve & Send" (and, separately, actually
    complete the real Photon Elements prescribe workflow) for anything
    to happen for real.
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("assistant")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

DRAFT_TOOL = {
    "name": "draft_ec_recommendation",
    "description": "Draft a recommendation for a clinician reviewing a specific emergency contraception option for a patient. This is a draft only -- it is never sent without explicit clinician approval, and the clinician still has to complete the actual prescribing step themselves.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": ["approve_as_selected", "approve_with_caution", "do_not_approve", "escalate_to_clinician"],
                "description": "approve_with_caution when eligible but a MODERATE interaction or efficacy caveat applies; do_not_approve or escalate_to_clinician if a MAJOR alert or allergy conflict was found.",
            },
            "rationale": {
                "type": "string",
                "description": "Short clinical rationale a clinician can read in a few seconds, referencing the specific Photon screen results and intake data provided.",
            },
            "safety_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Plain-language flags surfaced from the interaction/allergy screen and the deterministic eligibility engine -- never omit ones that were supplied.",
            },
            "patient_counseling_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything worth telling the patient about this specific option (how it's taken/placed, what to expect), grounded only in the supplied treatment name.",
            },
        },
        "required": ["recommendation", "rationale", "safety_flags"],
    },
}

SYSTEM_PROMPT = """You are a clinical assistant supporting a prescriber reviewing an emergency contraception (EC) case that a patient-facing intake app has already routed to them.

A DETERMINISTIC, non-AI eligibility engine has already computed which EC options are time/weight eligible for this patient -- you are never asked to re-decide that, and you do not have prescribing authority. Your only job is to draft a short recommendation about the ONE specific treatment the clinician just selected (by clicking a button for Ella, ParaGard, Mirena, or Liletta), for that clinician to review, edit, or reject. Your output is never sent to a pharmacy or patient without an explicit human approval step, and even after approval, only the clinician's own authenticated action in Photon can actually complete the prescription.

Ground rules:
- Use only the patient intake data, the deterministic eligibility summary, and Photon's real interaction/allergy screen results you are given. Do not invent a diagnosis, lab value, or clinical history that wasn't provided.
- If the Photon screen returned any MAJOR severity alert, or the selected treatment conflicts with a listed allergy, your recommendation must be "do_not_approve" or "escalate_to_clinician" -- never "approve_as_selected".
- If the screen returned MODERATE alerts (e.g. a CYP3A4 inducer interaction), lean toward "approve_with_caution" and say plainly what the tradeoff is.
- If the deterministic eligibility summary already flagged this treatment as outside its time window or weight-appropriate for this patient, say so explicitly and prefer "escalate_to_clinician" -- the clinician clicked this button, but the underlying data may not support it.
- Keep rationale short and specific -- a clinician should be able to read it in under 10 seconds and know exactly why you flagged what you flagged.
- Always call the draft_ec_recommendation tool with your answer. Never respond in plain prose.
"""


def _call_claude(system: str, user_content: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it in your .env to let the assistant "
            "draft a recommendation"
        )

    payload = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": system,
        "tools": [DRAFT_TOOL],
        "tool_choice": {"type": "tool", "name": "draft_ec_recommendation"},
        "messages": [{"role": "user", "content": user_content}],
    }
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


def draft_recommendation(case: dict, treatment_name: str, screening: dict) -> dict:
    """Calls Claude to draft a recommendation about ONE specific treatment
    for ONE case. Raises RuntimeError with a clear message if
    ANTHROPIC_API_KEY isn't configured or unreachable -- the caller turns
    that into a 200 response the UI renders as "assistant unavailable,
    needs manual review" rather than a crash."""

    context = {
        "patient_intake": {
            "hours_since_intercourse": case.get("hours_since_intercourse"),
            "weight_lb": case.get("weight_lb"),
            "allergies": case.get("allergies", []),
            "current_medications": case.get("current_medications", []),
        },
        "deterministic_eligibility_summary": case.get("eligibility_summary"),
        "selected_treatment": treatment_name,
        "photon_interaction_allergy_screen": screening.get("alerts", []),
        "photon_screen_data_source": screening.get("_source"),
    }

    user_content = (
        "Here is the patient's intake, the deterministic eligibility engine's "
        "output, the specific treatment the clinician selected, and Photon's "
        "real interaction/allergy screen for that treatment. Draft your "
        "recommendation.\n\n" + json.dumps(context, indent=2, default=str)
    )

    message = _call_claude(SYSTEM_PROMPT, user_content)

    for block in message.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "draft_ec_recommendation":
            result = dict(block["input"])
            result["requires_human_review"] = True
            return result

    raise RuntimeError(f"Model did not return a structured draft (unexpected). Raw: {message}")
