"""Photon Health API client for the clinician review app.

This app does the three things the patient-facing app deliberately leaves
alone, because all three require clinical judgment or clinician-level
Photon permissions:

  1. search_treatments -- resolve whichever of the four buttons (Ella,
     ParaGard, Mirena, Liletta) the clinician clicked to a real Photon
     treatment ID.
  2. screen_prescription -- Photon's real interaction/allergy screen,
     run against the patient record the patient-facing app already
     created (with its real allergy/medication history on file).
  3. not through the client but with the prescribe-workflow element in
     case.html, we can call createPrescription. When attempting to do that
     in this client with the app's M2M credentials, there seemed to be a
     permissions issue, so I used the Elements workflow

Same real, documented Neutron GraphQL schema; every call falls back
to mock_data.py and is tagged `_source: "live" | "mock"`.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

from env_loader import load_dotenv
from mock_data import (
    mock_search_treatments,
    mock_screen_prescription,
    mock_attempt_write_prescription,
    mock_get_patient,
)

load_dotenv()

logger = logging.getLogger("photon_client")


class PhotonAuthError(Exception):
    pass


class PhotonClient:
    def __init__(self) -> None:
        self.client_id = os.getenv("PHOTON_CLIENT_ID")
        self.client_secret = os.getenv("PHOTON_CLIENT_SECRET")
        self.org_id = os.getenv("PHOTON_ORG_ID")
        self.audience = os.getenv("PHOTON_AUDIENCE", "https://api.neutron.health")
        self.token_url = os.getenv("PHOTON_TOKEN_URL", "https://auth.neutron.health/oauth/token")
        self.api_url = os.getenv("PHOTON_API_URL", "https://api.neutron.health/graphql")
        self.clinical_api_url = os.getenv(
            "PHOTON_CLINICAL_API_URL", "https://clinical-api.neutron.health/graphql"
        )

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._live_disabled_reason: Optional[str] = None

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        if not (self.client_id and self.client_secret):
            raise PhotonAuthError("Missing PHOTON_CLIENT_ID / PHOTON_CLIENT_SECRET")
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "audience": self.audience,
            "grant_type": "client_credentials",
        }
        try:
            resp = requests.post(self.token_url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise PhotonAuthError(f"Token exchange failed: {e}") from e
        if "access_token" not in data:
            raise PhotonAuthError(f"Token exchange returned no access_token: {data}")
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    def is_live(self) -> bool:
        if self._live_disabled_reason is not None:
            return False
        try:
            self._get_token()
            return True
        except PhotonAuthError as e:
            self._live_disabled_reason = str(e)
            logger.warning("Photon live API unavailable, falling back to mock data: %s", e)
            return False

    @staticmethod
    def _handle_response(resp: "requests.Response", label: str) -> dict:
        """`resp.raise_for_status()` on its own discards the response body,
        so a non-2xx status surfaces as a bare "400 Client Error: Bad
        Request" with no indication of *why*. Read the body first (JSON if
        possible, else raw text) and include it in whatever gets raised."""
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            detail = body if body is not None else resp.text[:1000]
            raise RuntimeError(f"HTTP {resp.status_code} from {label}: {detail}")
        if body is None:
            raise RuntimeError(f"{label} returned a non-JSON 2xx response: {resp.text[:1000]}")
        if body.get("errors"):
            raise RuntimeError(body["errors"])
        return body["data"]

    def _post_clinical(self, query: str, variables: dict) -> dict:
        token = self._get_token()
        resp = requests.post(
            self.clinical_api_url,
            json={"query": query, "variables": variables},
            headers={
                "x-photon-auth-token": token,
                "x-photon-auth-token-type": "auth0",
            },
            timeout=15,
        )
        return self._handle_response(resp, "clinical API")

    def _post_core(self, query: str, variables: dict) -> dict:
        token = self._get_token()
        resp = requests.post(
            self.api_url,
            json={"query": query, "variables": variables},
            headers={"authorization": f"Bearer {token}"},
            timeout=15,
        )
        return self._handle_response(resp, "core API")

    # ------------------------------------------------------------------
    # Queries a patient's name, allergies, and current medications live
    # from Photon by photon_patient_id, on the core API -- confirmed live
    # against the sandbox (this exact shape, including allergen.rxcui and
    # medicationHistory.medication.name, round-tripped correctly against
    # both a freshly-created test patient and a real case's patient with a
    # documented allergy). This replaces the first_name/last_name/
    # allergies/current_medications columns this app's shared
    # `handoff_cases` table used to duplicate locally -- Photon is the
    # canonical source for a patient's record, and photon-ec-patient
    # already writes allergies/medications there via createPatient, so
    # there's no reason for this app to keep a second, driftable copy. See
    # both READMEs.
    #
    # Note this is a DIFFERENT type shape than clinical-API Patient (which
    # has no allergies field at all, confirmed live earlier in this
    # project) -- this deliberately queries the core API, not the clinical
    # API.
    # ------------------------------------------------------------------
    def get_patient(self, patient_id: str) -> dict:
        query = """
        query Patient($id: ID!) {
          patient(id: $id) {
            id
            name { full }
            allergies { allergen { id name rxcui } comment onset }
            allergyStatus
            medicationHistory { medication { id name } comment active }
          }
        }
        """
        try:
            data = self._post_core(query, {"id": patient_id})
            patient = data["patient"] or {}
            name = patient.get("name") or {}
            return {
                "name": name.get("full"),
                "allergies": [
                    a["allergen"]["name"]
                    for a in (patient.get("allergies") or [])
                    if a.get("allergen")
                ],
                "allergy_status": patient.get("allergyStatus"),
                # medicationHistory can also hold medicalEquipment/substance
                # entries (see the introspected PatientMedication shape) --
                # only entries with an actual `medication` and still
                # `active: true` are shown as "current medications" here.
                "current_medications": [
                    m["medication"]["name"]
                    for m in (patient.get("medicationHistory") or [])
                    if m.get("active") and m.get("medication")
                ],
                "_source": "live",
            }
        except Exception as e:
            logger.warning("get_patient(%r) falling back to mock: %s", patient_id, e)
            return mock_get_patient(patient_id)

    def search_treatments(self, term: str) -> dict:
        query = """
        query Treatments($filter: TreatmentFilter!) {
          treatments(filter: $filter) { id name }
        }
        """
        try:
            data = self._post_clinical(query, {"filter": {"term": term}})
            return {"treatments": data["treatments"], "_source": "live"}
        except Exception as e:
            logger.warning("search_treatments(%r) falling back to mock: %s", term, e)
            return mock_search_treatments(term)

    def screen_prescription(self, patient_id: str, treatment_id: str, treatment_name: str, inducer_names_for_mock: list[str]) -> dict:
        query = """
        query Query($draftedPrescriptions: [DraftedPrescriptionInput!]!, $patientId: ID!) {
          prescriptionScreen(draftedPrescriptions: $draftedPrescriptions, patientId: $patientId) {
            alerts {
              description
              involvedEntities {
                ... on PrescriptionScreeningAlertInvolvedAllergen { id name }
                ... on PrescriptionScreeningAlertInvolvedDraftedPrescription { id name }
                ... on PrescriptionScreeningAlertInvolvedExistingPrescription { id name }
              }
              severity
              type
            }
          }
        }
        """
        # DraftedPrescriptionTreatmentInput has exactly one field, `id: ID!`
        # -- the docs' flat {"treatmentId": ...} example is stale; the real
        # shape nests it under `treatment`.
        drafted = [{"treatment": {"id": treatment_id}}]
        try:
            data = self._post_clinical(
                query, {"draftedPrescriptions": drafted, "patientId": patient_id}
            )
            return {**data["prescriptionScreen"], "_source": "live"}
        except Exception as e:
            logger.warning("screen_prescription falling back to mock: %s", e)
            return mock_screen_prescription(treatment_name, inducer_names_for_mock)


_client: Optional[PhotonClient] = None


def get_photon_client() -> PhotonClient:
    global _client
    if _client is None:
        _client = PhotonClient()
    return _client
