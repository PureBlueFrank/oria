"""Synthetic SQLite receiver: authority and acceptance share one durable boundary.

Test support only. The trusted harness owns authority writes; this is not network
 authentication, an Oria Job authority replica, or a production adapter.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
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
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._clock = clock or (lambda: NOW)
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS authority "
                "(scope TEXT PRIMARY KEY, job TEXT, allowed INTEGER)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history (key TEXT PRIMARY KEY, result TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's connection context commits/rolls back but does not close.
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def replace_authority(self, job: Job, *, allowed: bool = True) -> None:
        """Trusted harness setup only; never callable through an adapter request."""
        job = Job.model_validate(job.model_dump(), strict=True)
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO authority VALUES (?, ?, ?)",
                (json.dumps([job.tenant_id, job.job_id]), job.model_dump_json(), int(allowed)),
            )

    def _authorize(self, db: sqlite3.Connection, request: DispatchRequest) -> None:
        row = db.execute(
            "SELECT job, allowed FROM authority WHERE scope = ?",
            (json.dumps([request.operation.tenant_id, request.operation.job_id]),),
        ).fetchone()
        if row is None or not row[1]:
            raise PermissionError("synthetic authority denied")
        validate_dispatch_admission(request, Job.model_validate_json(row[0]), self._clock())

    def _validate_payload(self, request: DispatchRequest, payload: BaseModel) -> None:
        validate_dispatch_payload(request, payload, schema=_Payload)
        value = _Payload.model_validate(payload.model_dump(), strict=True)
        if request.operation.resource_ref != f"coupon_batch:{value.coupon_id}":
            raise DispatchRejected("synthetic resource binding differs")

    def execute(
        self, request: DispatchRequest, payload: BaseModel | None = None
    ) -> DispatchObservation:
        request = DispatchRequest.model_validate(request.model_dump(), strict=True)
        if (
            request.operation.adapter_id != "fixture-coupon"
            or request.operation.tool_name != "materialize_coupon_batch"
            or request.operation.tool_schema_version != 1
        ):
            raise DispatchRejected("unsupported synthetic dispatch target")
        with self._transaction() as db:
            self._authorize(db, request)
            if payload is not None:
                self._validate_payload(request, payload)
            key = json.dumps(request.operation.deduplication_key)
            prior = db.execute("SELECT result FROM history WHERE key = ?", (key,)).fetchone()
            result: DispatchObservation
            if prior is not None:
                result = DispatchAdmission.model_validate_json(prior[0])
                validate_dispatch_history(request, result)
            elif payload is None:
                result = DispatchObservation(
                    operation=request.operation, state="not_found", observed_at=self._clock()
                )
            else:
                # The history row IS the synthetic effect. There is no external call.
                now = self._clock()
                receipt = _receipt(request, "accepted").model_copy(update={"received_at": now})
                result = DispatchAdmission(
                    operation=request.operation,
                    accepted_fence=request.fence,
                    state="succeeded",
                    observed_at=now,
                    receipt=receipt,
                )
                db.execute("INSERT INTO history VALUES (?, ?)", (key, result.model_dump_json()))
            # Authority writers share this transaction lock. Recheck wall-clock expiry
            # at the last synchronous boundary before COMMIT, including history reads.
            self._authorize(db, request)
        return result
