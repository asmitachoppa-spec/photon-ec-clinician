"""Fallback mock data for photon-ec-clinician, used only when the live
Photon sandbox can't be reached (see photon_client.py). Every value is
tagged `_source: "mock"`.

Treatment IDs mirror what's been confirmed in the real Photon sandbox
catalog elsewhere in this workspace (levonorgestrel, "ella", "paragard"),
plus Mirena and Liletta (the two 52mg levonorgestrel IUD brand names) added
for this project's four clinician-facing options. If your sandbox's real
catalog doesn't stock Mirena/Liletta as named treatments, search_treatments
will fall back to this mock data automatically -- see README.
"""

from __future__ import annotations

MOCK_TREATMENTS = {
    "levonorgestrel": [
        {"id": "med_mock_lng_planb", "name": "Levonorgestrel 1.5 MG Oral Tablet (Plan B One-Step)"},
    ],
    "ella": [
        {"id": "med_mock_ella", "name": "Ulipristal Acetate 30 MG Oral Tablet (ella)"},
    ],
    "paragard": [
        {"id": "med_mock_paragard", "name": "Copper Intrauterine Device (Paragard)"},
    ],
    "mirena": [
        {"id": "med_mock_mirena", "name": "Levonorgestrel 52 MG Intrauterine System (Mirena)"},
    ],
    "liletta": [
        {"id": "med_mock_liletta", "name": "Levonorgestrel 52 MG Intrauterine System (Liletta)"},
    ],
    "rifampin": [
        {"id": "med_mock_rifampin", "name": "Rifampin 300 MG Oral Capsule"},
    ],
    "carbamazepine": [
        {"id": "med_mock_carbamazepine", "name": "Carbamazepine 200 MG Oral Tablet"},
    ],
    "topiramate": [
        {"id": "med_mock_topiramate", "name": "Topiramate 100 MG Oral Tablet"},
    ],
}


def mock_search_treatments(term: str) -> dict:
    key = term.strip().lower()
    for k, v in MOCK_TREATMENTS.items():
        if k in key or key in k:
            return {"treatments": v, "_source": "mock"}
    return {"treatments": [], "_source": "mock"}


def mock_screen_prescription(drafted_treatment_name: str, inducer_names: list[str]) -> dict:
    """Canned interaction-screen results keyed by which inducer (if any) was
    already flagged by eligibility.py's own list, so the demo can show a
    realistic MODERATE alert coming back from Photon's real alert shape
    even when the live API isn't reachable from here."""
    if not inducer_names:
        return {"alerts": [], "_source": "mock"}
    alerts = []
    for name in inducer_names:
        alerts.append(
            {
                "description": (
                    f"{name} is a CYP3A4 enzyme inducer and may reduce the effectiveness "
                    f"of {drafted_treatment_name} by increasing its metabolic clearance."
                ),
                "severity": "MODERATE",
                "type": "DRUG_DRUG",
                "involvedEntities": [
                    {"id": "drafted", "name": drafted_treatment_name},
                    {"id": f"existing_{name.lower()}", "name": name},
                ],
            }
        )
    return {"alerts": alerts, "_source": "mock"}


def mock_get_patient(patient_id: str) -> dict:
    """Fallback for photon_client.get_patient() when Photon can't be
    reached. Unlike the other mocks in this file, this one is honestly
    limited: this app no longer stores the patient's reported name,
    allergies, or current medications locally (see db.py -- they're queried
    live from Photon by design, not duplicated into this table), so if
    Photon itself is unreachable there is no local fallback data to show.
    This returns an obviously-placeholder record with a `_note` explaining
    why, rather than fabricating a name or an allergy list that was never
    actually reported."""
    return {
        "name": None,
        "allergies": [],
        "allergy_status": None,
        "current_medications": [],
        "_source": "mock",
        "_note": (
            f"Could not reach Photon for patient {patient_id}, and this app doesn't "
            "keep a local copy of name/allergies/medications to fall back to -- "
        ),
    }


def mock_attempt_write_prescription() -> dict:
    """Mirrors the real rejection Photon returns for an M2M token attempting
    createPrescription -- that mutation requires an authenticated, logged-in
    prescriber (a User Access Token), by Photon's own design. Used only when
    the live call can't be reached at all (a connectivity issue, not
    Photon's own rejection)."""
    return {
        "blocked": True,
        "reason": (
            "Could not reach Photon to attempt the write -- this sandbox has no "
            "network access to neutron.health, so this is a connectivity issue, "
            "not Photon's permission check. Run this with real network access "
            "to see the actual GraphQL rejection Photon returns for an M2M "
            "token attempting createPrescription."
        ),
        "_source": "mock",
    }
