"""Shared Postgres handoff table -- read/update side.

See the patient-facing app's db.py for the schema and the write side
(INSERT only, approval_fl='F'). This app is the only thing that reads
pending cases and the only thing that ever sets approval_fl='T' or 'D'.
"""

from __future__ import annotations

import os
import json
import logging

import psycopg2
import psycopg2.extras

from env_loader import load_dotenv

load_dotenv()

logger = logging.getLogger("db")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/photon_ec")

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoff_cases (
    id                      SERIAL PRIMARY KEY,
    external_id             TEXT UNIQUE NOT NULL,
    photon_patient_id       TEXT,
    hours_since_intercourse NUMERIC,
    weight_lb               NUMERIC,
    interacting_meds_found  JSONB NOT NULL DEFAULT '[]',
    eligibility_summary     JSONB,
    approval_fl             CHAR(1) NOT NULL DEFAULT 'F',
    chosen_treatment        TEXT,
    chosen_treatment_id     TEXT,
    screen_result           JSONB,
    rx_write_attempt        JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at             TIMESTAMPTZ,
    declined_at             TIMESTAMPTZ
);
"""

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
        logger.info("handoff_cases table ready")
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    return dict(row)


def list_pending() -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM handoff_cases WHERE approval_fl = 'F' ORDER BY created_at ASC;"
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_all() -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM handoff_cases ORDER BY created_at DESC;")
            return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_case(case_id: int) -> dict | None:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM handoff_cases WHERE id = %s;", (case_id,))
            row = cur.fetchone()
            return _row_to_dict(row) if row else None
    finally:
        conn.close()


def save_chosen_treatment(
    case_id: int, chosen_treatment: str, chosen_treatment_id: str | None
) -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE handoff_cases
                SET chosen_treatment = %s,
                    chosen_treatment_id = %s,
                    screen_result = NULL
                WHERE id = %s;
                """,
                (chosen_treatment, chosen_treatment_id, case_id),
            )
    finally:
        conn.close()


def save_screen_result(
    case_id: int,
    chosen_treatment: str,
    chosen_treatment_id: str | None,
    screen_result: dict,
) -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE handoff_cases
                SET chosen_treatment = %s,
                    chosen_treatment_id = %s,
                    screen_result = %s
                WHERE id = %s;
                """,
                (
                    chosen_treatment,
                    chosen_treatment_id,
                    json.dumps(screen_result),
                    case_id,
                ),
            )
    finally:
        conn.close()


def approve_case(case_id: int) -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE handoff_cases
                SET approval_fl = 'T',
                    approved_at = now()
                WHERE id = %s;
                """,
                (case_id,),
            )
    finally:
        conn.close()


def decline_case(case_id: int) -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE handoff_cases
                SET approval_fl = 'D',
                    declined_at = now()
                WHERE id = %s;
                """,
                (case_id,),
            )
    finally:
        conn.close()
