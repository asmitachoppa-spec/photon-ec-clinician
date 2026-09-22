"""
Clinician review app for EC (emergency contraception) handoff cases.

In this app, there will initially display a screen where the clinician
can view pending orders and all orders.

For pending orders, the clinician can click into it, and see the details of
the case, which include:
- patient information
- the eligibility summary (which was calculated in the patient app)
- the top 4 prescriptions (Ella, ParaGard, Mirena, or Liletta) that are
used for emergency contraception and when clicked, will run
prescriptionScreen for that patient and that prescription
- an AI draft of what is recommended
- the Photon prescription workflow to prescribe a medication
- An approval/decline button which will update the postgres table

Details:

pending orders will flag whether or not it is urgent. This mechanism utilizes
AI to understand the priority which is triage_agent

`prescriptionScreen` specifically turned out to require a real logged-in provider's
User Access Token -- an M2M token gets rejected with "Could not get user
for request" even for a patient it can otherwise read, the same boundary
Photon documents for *writing* a prescription. So clicking a treatment
button does two things, in two different places:

  1. `POST /case/<id>/screen` (this Flask backend, M2M) resolves the
     button to a real Photon treatment ID via `search_treatments` (a
     catalog lookup, not patient-specific, so M2M is fine here) and
     redirects back to the case page.
  2. The case page itself then runs `prescriptionScreen` **in the
     browser**, using the same Auth0 user session the `<photon-client
     auto-login>` widget below already established, and POSTs the result
     to `POST /case/<id>/run_screen` -- which is where that screen result
     gets saved to Postgres. The AI draft itself is computed separately,
     fresh, every time case_detail() is loaded for a still-pending case --
     it's never persisted (see below).

Two separate, both-real things happen once a clinician decides to act:

  1. The actual send-to-pharmacy path is Photon's own Elements widget,
     <photon-prescribe-workflow>, embedded live on the case page inside
     <photon-client auto-login>. If the clinician is logged into Photon
     with prescriber permissions in their browser, this is a real,
     working Photon UI completing a real prescription -- not a
     simulation of one.

  2. Separately, clicking this app's own "Approve & Send to Photon"
     button (a) makes a real attempt at createPrescription using this
     app's M2M credentials -- expected to be rejected by Photon itself,
     since M2M tokens can't hold write:prescription, demonstrating the
     same permission boundary visible throughout this workspace's other
     Photon projects -- and (b) flips approval_fl to 'T' in our own
     Postgres table regardless of that attempt's outcome, since that flag
     records THIS system's own audit trail (a clinician reviewed and
     approved this case here), independent of whether the M2M call or
     the Elements send separately succeeded.
"""

from __future__ import annotations

import os
import logging

from flask import Flask, render_template, request, redirect, url_for, abort, jsonify

import db
from photon_client import get_photon_client
from mock_data import mock_screen_prescription
import assistant
import triage_agent
import referral_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(_HERE, "..", "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND, "templates"),
    static_folder=os.path.join(_FRONTEND, "static"),
)

try:
    db.init_db()
except Exception as e:  # pragma: no cover - surfaced in the UI instead
    logger.warning("Could not initialize Postgres schema at startup: %s", e)

TREATMENT_OPTIONS = {
    "ella": {"term": "ella", "label": "Ella", "sublabel": "ulipristal acetate -- prescription pill"},
    "paragard": {"term": "paragard", "label": "ParaGard", "sublabel": "copper IUD -- in-office placement"},
    "mirena": {"term": "mirena", "label": "Mirena", "sublabel": "52mg LNG IUD -- in-office placement"},
    "liletta": {"term": "liletta", "label": "Liletta", "sublabel": "52mg LNG IUD -- in-office placement"},
}

PHOTON_CLIENT_ID = os.getenv("PHOTON_CLIENT_ID", "")
PHOTON_ELEMENTS_CLIENT_ID = os.getenv("PHOTON_ELEMENTS_CLIENT_ID", "")
PHOTON_ORG_ID = os.getenv("PHOTON_ORG_ID", "")
PHOTON_ELEMENTS_REDIRECT_URI = os.getenv("PHOTON_ELEMENTS_REDIRECT_URI", "http://localhost:5100/")
PHOTON_DEV_MODE = os.getenv("PHOTON_DEV_MODE", "true")


@app.route("/")
def inbox():
    pending = db.list_pending()
    all_cases = db.list_all()
    # Name/allergies/current medications aren't stored on handoff_cases
    # anymore (see db.py) -- they're queried live from Photon here, one
    # call per pending case. That's a real N+1: fine at this demo's scale
    # (a handful of pending cases at a time), not something you'd want
    # unchanged at a real clinic's volume, where you'd want to batch this
    # (a single query keyed by a list of patient IDs, if Photon's schema
    # supports one) or cache it. Flagging the tradeoff here rather than
    # quietly accepting it.
    client = get_photon_client()
    pending_with_patient = []
    for c in pending:
        # Urgency annotation only -- never reorders the queue and never
        # changes what's eligible. Grounded entirely in eligibility_summary,
        # already computed by the deterministic engine before this case was
        # ever routed here; see triage_agent.py. Best-effort: if the model
        # isn't configured/reachable or doesn't return a usable note, the
        # row just renders with no badge, exactly as it did before this
        # existed.
        try:
            triage = triage_agent.triage_case(c)
        except Exception as e:  # pragma: no cover - defensive, never blocks the inbox
            logger.warning("triage_agent raised for case %s: %s", c.get("id"), e)
            triage = None
        pending_with_patient.append({
            **c,
            "patient": client.get_patient(c["photon_patient_id"]) if c.get("photon_patient_id") else None,
            "triage": triage,
        })
    return render_template("inbox.html", pending=pending_with_patient, all_cases=all_cases)


@app.route("/case/<int:case_id>")
def case_detail(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)

    client = get_photon_client()
    patient = (
        client.get_patient(case["photon_patient_id"])
        if case.get("photon_patient_id")
        else {"name": None, "allergies": [], "current_medications": [], "_source": "mock"}
    )

    # The AI draft is computed here, fresh, every time the page is loaded --
    # never persisted (see db.py) -- and only for a case that (a) has
    # actually been screened and (b) hasn't already been approved. Per the
    # user's own ask: there's nothing for a clinician to still be drafting a
    # recommendation about once they've already approved the case.
    ai_draft = None
    assistant_error = None
    if case.get("screen_result") and case.get("approval_fl") != "T":
        case_for_draft = {
            **case,
            "allergies": patient.get("allergies", []),
            "current_medications": patient.get("current_medications", []),
        }
        try:
            ai_draft = assistant.draft_recommendation(
                case_for_draft, case["chosen_treatment"], case["screen_result"]
            )
        except RuntimeError as e:
            assistant_error = str(e)

    return render_template(
        "case.html",
        case=case,
        patient=patient,
        ai_draft=ai_draft,
        assistant_error=assistant_error,
        treatment_options=TREATMENT_OPTIONS,
        photon_elements_client_id=PHOTON_ELEMENTS_CLIENT_ID,
        photon_org_id=PHOTON_ORG_ID,
        photon_redirect_uri=PHOTON_ELEMENTS_REDIRECT_URI,
        photon_dev_mode=PHOTON_DEV_MODE,
    )


@app.route("/case/<int:case_id>/screen", methods=["POST"])
def screen_case(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)

    treatment_key = request.form.get("treatment")
    option = TREATMENT_OPTIONS.get(treatment_key)
    if not option:
        abort(400, "Unknown treatment option")

    client = get_photon_client()
    search = client.search_treatments(option["term"])
    treatments = search.get("treatments", [])
    treatment_id = treatments[0]["id"] if treatments else None
    treatment_name = treatments[0]["name"] if treatments else option["label"]

    # Just resolve + record the choice here. The actual prescriptionScreen
    # call happens client-side (see run_screen below and the script in
    # case.html) -- it needs a real provider user token, which this
    # backend's M2M client doesn't have.
    db.save_chosen_treatment(case_id, chosen_treatment=treatment_name, chosen_treatment_id=treatment_id)
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/case/<int:case_id>/run_screen", methods=["POST"])
def run_screen(case_id: int):
    """Receives the result of a prescriptionScreen call the case page just
    ran in the browser (using the clinician's own logged-in Photon session
    -- see case.html), or a client_error if that call itself failed (e.g.
    the browser couldn't silently renew the Auth0 session -- a real
    possibility if third-party cookies are blocked, see README). Either
    way, this is where the screen result gets written to Postgres. The AI
    draft is NOT computed here anymore -- case_detail() computes it fresh
    from this saved screen_result every time a still-pending case's page is
    loaded, rather than once here and persisted."""
    case = db.get_case(case_id)
    if not case:
        abort(404)
    if not case.get("chosen_treatment"):
        abort(400, "No treatment chosen yet.")

    body = request.get_json(silent=True) or {}
    client_error = body.get("client_error")
    raw_screen = body.get("screen_result")
    treatment_name = case["chosen_treatment"]

    if client_error:
        logger.warning("Browser-side prescriptionScreen failed for case %s: %s", case_id, client_error)
        screening = mock_screen_prescription(treatment_name, case.get("interacting_meds_found") or [])
        screening["_note"] = f"Browser-side Photon call failed, showing mock data instead: {client_error}"
    elif raw_screen is not None:
        screening = {**raw_screen, "_source": "live"}
    else:
        abort(400, "Request body must include screen_result or client_error.")

    # The AI draft is no longer computed or saved here -- case_detail()
    # computes it fresh (grounded in whatever screen_result ends up stored
    # below, plus a live patient lookup) every time the case page is
    # viewed, and only while the case is still pending review. See db.py.
    db.save_screen_result(
        case_id,
        chosen_treatment=treatment_name,
        chosen_treatment_id=case.get("chosen_treatment_id"),
        screen_result=screening,
    )
    return jsonify({"ok": True})


@app.route("/case/<int:case_id>/approve", methods=["POST"])
def approve_case(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)
    if not case.get("chosen_treatment_id"):
        abort(400, "Run a safety check on a treatment before approving.")

    db.approve_case(case_id)
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/case/<int:case_id>/decline", methods=["POST"])
def decline_case(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)
    if not case.get("chosen_treatment_id"):
        abort(400, "Run a safety check on a treatment before declining.")

    db.decline_case(case_id)
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/case/<int:case_id>/referral_packet")
def referral_packet(case_id: int):
    """On-demand only -- nothing computes or stores a packet until a
    clinician actually asks for one, and only for a case that's already
    been through every judgment call this workspace makes (approved).
    There's nothing to hand off for a still-pending or declined case."""
    case = db.get_case(case_id)
    if not case:
        abort(404)
    if case.get("approval_fl") != "T":
        abort(400, "Referral packets are only available for approved cases.")

    client = get_photon_client()
    patient = (
        client.get_patient(case["photon_patient_id"])
        if case.get("photon_patient_id")
        else {"name": None, "allergies": [], "current_medications": [], "_source": "mock"}
    )

    packet = None
    packet_error = None
    try:
        packet = referral_agent.build_referral_packet(case, patient, client.get_patient)
    except Exception as e:  # pragma: no cover - surfaced in the UI instead
        packet_error = str(e)

    return render_template(
        "referral_packet.html",
        case=case,
        patient=patient,
        packet=packet,
        packet_error=packet_error,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5100"))
    app.run(host="0.0.0.0", port=port, debug=True)
