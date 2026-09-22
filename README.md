# photon-ec-clinician

The clinician-facing half of a two-app system, paired with
[photon-ec-patient](../photon-ec-patient). 

## Background

The whole point of Photon is to be able to streamline getting your prescriptions in a much better way. Fastening the process and eliminating unnecessary appointments have a lot of pros -- you save the doctor’s time, you get your medications quicker, but the one that we’re going to focus on is -- you can avoid uncomfortable conversations. And this superpower would be very useful for women who need emergency contraceptives

The target audience is young women who might be uncomfortable booking a doctor’s appointment and speaking with someone about needing this medication maybe without their parent’s knowledge. This workflow would create a seamless way for these women to prioritize their health in a comfortable way.

This is an app that clinics can incorporate into their systems to work with both patients and clinicians to prescribe these emergency contraceptives. I have built both sides of the app but this one in particulat is the clinician side

Clinicians can view different orders (priority of them is flagged by AI) that have been places by young women for EC. For each case, they can view that patient information (which is stored in Photon) and their specific case details (time since sex, weight) and make a prescription. They will then be able to click on the top 4 EC prescriptions and run a screen on them to see if that patient can take this medication (based on their allergies/medication history). There would also be an AI draft that recommends a treatment (if the clinician wants to use that). The clinician can then make a prescription or even decline the order. If approved, a referral packet is generated (through AI) about details about this case/patient/prescription.

## Data flow

`photon-ec-patient` writes a row to `handoff_cases` (`approval_fl='F'`)
whenever a patient's intake needs more than an OTC recommendation, already
having created a real Photon patient record with that patient's allergies
and medication history on file. This app is everything downstream of that:

1. **Inbox** (`/`): every case where `approval_fl = 'F'`.
2. **Case review** (`/case/<id>`): the patient's reported intake, plus the
   deterministic eligibility engine's own read on which options are
   time/weight-eligible for this patient (ported unchanged from
   `eligibility.py` -- no LLM in that judgment, same as the patient app).
3. **Four buttons -- Ella, ParaGard, Mirena, Liletta.** Clicking one runs,
   for real:
   - `search_treatments` to resolve the button to a real Photon treatment
     ID (Flask backend, M2M -- a catalog lookup, not patient-specific, so
     M2M is fine here).
   - `prescriptionScreen` -- Photon's real interaction/allergy check,
     against the patient record `photon-ec-patient` already populated.
     **This one runs in the browser, not the backend** -- see "Why
     prescriptionScreen runs client-side" below.
   - A Claude-drafted recommendation (`assistant.py`) grounded *only* in
     that screen result and the intake data -- it never re-decides
     eligibility, only comments on the specific treatment just selected.
4. **Sending it, for real, two separate ways** (see below).
5. **Approve & Send** flips `approval_fl` to `'T'` in Postgres --
   `photon-ec-patient` never sees this case again.

## Patient data is queried live from Photon, not stored locally

`handoff_cases` used to duplicate the patient's name, allergies, and
current medications (plus an `ai_draft` column) from the patient-facing
intake. It no longer does -- `photon-ec-patient` already writes all of that
onto the real Photon patient record via `createPatient` (see its README),
and `photon_client.get_patient(photon_patient_id)` here queries it back
live (core API `patient { name { full } allergies { ... } medicationHistory { ... } } }`, confirmed against the live sandbox) whenever a case is
displayed, instead of keeping a second, driftable copy in this app's own
database. `ai_draft` is similarly no longer persisted: `case_detail()`
computes it fresh, every page load, for a case that's been screened and
isn't already approved -- there's nothing left to draft once a clinician
has approved a case.

Tradeoff:

- **The inbox does one live Photon query per pending case** to show
  name/allergies/medications in the table. Fine at this demo's scale (a
  handful of pending cases); at a real clinic's volume you'd want to batch
  this (a single query keyed by a list of patient IDs, if Photon's schema
  supports one) or cache it.

## Setup

```
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in PHOTON_*, DATABASE_URL, ANTHROPIC_API_KEY
python3 app.py
# -> http://localhost:5100
```

Uses the **same** `DATABASE_URL` as `photon-ec-patient` -- point both
`.env` files at the same Postgres database.

`PHOTON_ELEMENTS_REDIRECT_URI` should match wherever this app is actually
reachable (defaults to `http://localhost:5100/`) -- Elements' `auto-login`
redirects back to this URL after a successful Auth0 login.

## What I'd build next

- TODO
