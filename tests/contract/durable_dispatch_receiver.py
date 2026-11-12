"""Synthetic SQLite receiver: authority and acceptance share one durable boundary.

Test support only. The trusted harness owns authority writes; this is not network
 authentication, an Oria Job authority replica, or a production adapter.
"""

import json
import sqlite3
from pathlib import Path

from tests.contract.test_v06_dispatch_contract import NOW, _Payload, _receipt

from oria.core.dispatch import (
    DispatchAdmission,
    DispatchObservation,
    DispatchRejected,
    DispatchRequest,
    validate_dispatch_admission,
    validate_dispatch_history,
    validate_dispatch_payload,
)
from oria.core.jobs import Job


class DurableReferenceReceiver:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS authority "
                "(scope TEXT PRIMARY KEY, job TEXT, allowed INTEGER)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history (key TEXT PRIMARY KEY, result TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def replace_authority(self, job: Job, *, allowed: bool = True) -> None:
        job = Job.model_validate(job.model_dump(), strict=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR REPLACE INTO authority VALUES (?, ?, ?)",
                (json.dumps([job.tenant_id, job.job_id]), job.model_dump_json(), int(allowed)),
            )

    def execute(
        self, request: DispatchRequest, payload: _Payload | None = None
    ) -> DispatchObservation:
        request = DispatchRequest.model_validate(request.model_dump(), strict=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job, allowed FROM authority WHERE scope = ?",
                (json.dumps([request.operation.tenant_id, request.operation.job_id]),),
            ).fetchone()
            if row is None or not row[1]:
                raise PermissionError("synthetic authority denied")
            validate_dispatch_admission(request, Job.model_validate_json(row[0]), NOW)
            if payload is not None:
                validate_dispatch_payload(request, payload, schema=_Payload)
                if request.operation.resource_ref != f"coupon_batch:{payload.coupon_id}":
                    raise DispatchRejected("synthetic resource binding differs")
            key = json.dumps(request.operation.deduplication_key)
            prior = db.execute("SELECT result FROM history WHERE key = ?", (key,)).fetchone()
            if prior is not None:
                result = DispatchAdmission.model_validate_json(prior[0])
                validate_dispatch_history(request, result)
                return result
            if payload is None:
                return DispatchObservation(
                    operation=request.operation, state="not_found", observed_at=NOW
                )
            # The history row IS the synthetic effect. There is no external call.
            result = DispatchAdmission(
                operation=request.operation,
                accepted_fence=request.fence,
                state="succeeded",
                observed_at=NOW,
                receipt=_receipt(request, "accepted"),
            )
            db.execute("INSERT INTO history VALUES (?, ?)", (key, result.model_dump_json()))
            return result
