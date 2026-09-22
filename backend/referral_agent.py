"""Referral / handoff packet assembly agent.

Design intent, deliberately narrow -- same family as catalog_agent.py in the
sibling patient app: this never makes or revisits a clinical judgment. By
the time this ever runs, the case has already been through every judgment
call this workspace makes: the deterministic eligibility engine decided
which options were time/weight eligible, the clinician picked one, Photon's
real interaction/allergy screen ran against it, and the clinician approved
it. This agent's only job is to assemble those already-decided facts into a
short, readable handoff document -- the kind of thing you'd hand to a
pharmacy or another provider -- using only what's already in the case record
plus (optionally) a fresh read of the patient's real Photon record. It can
never invent a diagnosis, a new interaction, a dose, or an instruction that
wasn't already decided elsewhere in this system.

Only gated to already-approved cases (approval_fl == 'T') -- there's nothing
to hand off yet for a case still pending review, and a declined case has
nothing to hand off at all.

Same tool-use-loop shape as catalog_agent.py: get_patient is optional (the
route already has a patient snapshot from case_detail(), but the agent may
re-fetch if it wants a fresher read before finalizing), and
finalize_referral_packet is the one terminal tool. Same raw-urllib Messages
API pattern as this app's other Claude call sites.
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Callable, Optional

logger = logging.getLogger("referral_agent")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TURNS = 3

GET_PATIENT_TOOL = {
    "name": "get_patient",
    "description": (
        "Look up the patient's real Photon record by id -- name, allergies, "
        "and current medications on file. Optional: the case data you were "
        "given already includes a snapshot of this; only call it if you want "
        "a fresher read before finalizing the packet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string"},
        },
        "required": ["patient_id"],
    },
}

FINALIZE_TOOL = {
    "name": "finalize_referral_packet",
    "description": (
        "Call this exactly once, when the packet is assembled. Every field "
        "must be drawn only from the case data you were given or from a "
        "get_patient result -- never invent a fact, and never add a new "
        "clinical judgment, dose, or instruction that wasn't already decided "
        "elsewhere in this system."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patient_name": {"type": ["string", "null"]},
            "treatment_selected": {"type": "string"},
            "eligibility_basis": {
                "type": "string",
                "description": "One or two sentences restating, in plain language, why the deterministic engine considered the selected treatment eligible for this patient -- pull from that option's own reasons/efficacy_note, don't add new reasoning.",
            },
            "safety_screen_result": {
                "type": "string",
                "description": "Restate what Photon's real interaction/allergy screen found for this treatment -- alert descriptions/severities if any were returned, or a plain statement that none were found. Don't characterize alerts you weren't given.",
            },
            "allergies_on_file": {"type": "array", "items": {"type": "string"}},
            "current_medications_on_file": {"type": "array", "items": {"type": "string"}},
            "approval_record": {
                "type": "string",
                "description": "One sentence recording that a clinician reviewed and approved this case in this system, using the approved_at timestamp you were given.",
            },
            "summary": {
                "type": "string",
                "description": "A short paragraph (3-4 sentences) tying the above together for whoever receives this referral -- restate, don't reinterpret.",
            },
        },
        "required": [
            "treatment_selected",
            "eligibility_basis",
            "safety_screen_result",
            "approval_record",
            "summary",
        ],
    },
}

SYSTEM_PROMPT = """You assemble a referral/handoff packet for an emergency contraception (EC) case that a clinician has ALREADY reviewed and approved in this system.

Every judgment call on this case has already been made by the time you run: a deterministic, non-AI eligibility engine decided which options were time/weight eligible, the clinician picked one, Photon's real interaction/allergy screen already ran against it, and the clinician already approved it. You have no clinical authority here and are not being asked to exercise any -- your only job is to organize those already-decided facts into a short, clear document, the kind you'd hand to a pharmacy or another provider picking up this case.

Ground rules:
- Use only the case data you were given, and (if you call it) whatever get_patient actually returns. Never invent a diagnosis, interaction, dose, or instruction that isn't already present in that data.
- Do not re-evaluate eligibility, second-guess the clinician's approval, or suggest a different treatment.
- get_patient is optional -- the case data already includes a patient snapshot; only call it if you want a fresher read.
- Call finalize_referral_packet exactly once, when you're done.
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


def build_referral_packet(case: dict, patient: dict, get_patient_fn: Callable[[str], dict]) -> Optional[dict]:
    """case: the full handoff_cases row (must be approval_fl == 'T' -- the
    caller enforces that, this function doesn't check). patient: the
    case_detail()-style patient snapshot ({"name","allergies",
    "current_medications","_source"}) already fetched for the page.
    get_patient_fn: PhotonClient.get_patient, bound, for the model's
    optional get_patient tool call.

    Returns the finalized packet dict on success, or None if the model is
    unavailable/unreachable or never finalizes -- the caller shows "packet
    unavailable, here are the raw case facts" instead, same graceful-
    degradation pattern as this app's assistant.py."""

    case_facts = {
        "external_id": case.get("external_id"),
        "photon_patient_id": case.get("photon_patient_id"),
        "chosen_treatment": case.get("chosen_treatment"),
        "eligibility_summary": case.get("eligibility_summary"),
        "screen_result": case.get("screen_result"),
        "approved_at": str(case.get("approved_at")) if case.get("approved_at") else None,
        "patient_snapshot": patient,
    }

    messages = [
        {
            "role": "user",
            "content": (
                "Assemble a referral packet for this approved case. Case "
                "facts:\n\n" + json.dumps(case_facts, indent=2, default=str)
            ),
        }
    ]

    for _ in range(MAX_TURNS):
        try:
            message = _post({
                "model": MODEL,
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "tools": [GET_PATIENT_TOOL, FINALIZE_TOOL],
                "tool_choice": {"type": "auto"},
                "messages": messages,
            })
        except RuntimeError as e:
            logger.warning("referral_agent unavailable for case %s: %s", case.get("id"), e)
            return None

        content = message.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            logger.info("referral_agent gave up on case %s without a tool call", case.get("id"))
            return None

        tool_results = []
        finalized = None
        for block in tool_uses:
            if block["name"] == "get_patient":
                patient_id = block["input"].get("patient_id", "")
                try:
                    result = get_patient_fn(patient_id) if patient_id else {}
                except Exception as e:  # pragma: no cover - defensive
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result, default=str),
                })
            elif block["name"] == "finalize_referral_packet":
                finalized = block["input"]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": "recorded",
                })

        if finalized is not None:
            logger.info("referral_agent finalized a packet for case %s", case.get("id"))
            return dict(finalized)

        messages.append({"role": "user", "content": tool_results})

    logger.warning("referral_agent gave up on case %s after %d turns without finalizing", case.get("id"), MAX_TURNS)
    return None
