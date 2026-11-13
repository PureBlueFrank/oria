"""Local synthetic coupon receiver history, never a remote/business adapter.

Only the trusted Job service calls this store while holding authoritative platform
and business locks. A caller-supplied snapshot alone is not dispatch authority.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from oria.core.dispatch import (
    DispatchAdmission,
    DispatchObservation,
    DispatchRejected,
    DispatchRequest,
    validate_dispatch_history,
    validate_dispatch_payload,
)
from oria.domain.launch import MATERIALIZE_TOOL_NAME, MaterializeCouponBatchArgs
from oria.domain.ledger import Receipt


class LocalCouponReferenceStore:
    """Private service storage; the history row itself is the synthetic effect."""

    adapter_id = "local_reference_coupon"

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def observe(
        self,
        request: DispatchRequest,
        args: MaterializeCouponBatchArgs,
        *,
        admit: bool,
        check: Callable[[], None],
        clock: Callable[[], datetime],
    ) -> DispatchObservation:
        request = DispatchRequest.model_validate(request.model_dump(), strict=True)
        validate_dispatch_payload(request, args, schema=MaterializeCouponBatchArgs)
        if (
            request.operation.adapter_id != self.adapter_id
            or request.operation.tool_name != MATERIALIZE_TOOL_NAME
            or request.operation.tool_schema_version != 1
            or request.operation.resource_ref != f"coupon_batch:{args.coupon_batch_id}"
        ):
            raise DispatchRejected("local reference operation differs")
        check()
        if not admit and not self.path.exists():
            return DispatchObservation(
                operation=request.operation, state="not_found", observed_at=clock()
            )
        # No blocking wait behind another receiver writer while holding Job locks.
        # Failure is an unknown observation, never an implicit retry grant.
        with closing(sqlite3.connect(self.path, timeout=0)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                tables = {
                    r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if tables and (tables != {"local_coupon_history"} or version != 1):
                    raise ValueError("reference file is not a supported receiver database")
                if not tables:
                    if version != 0:
                        raise ValueError("unsupported reference schema version")
                    if not admit:
                        check()
                        return DispatchObservation(
                            operation=request.operation, state="not_found", observed_at=clock()
                        )
                    db.execute(
                        "CREATE TABLE local_coupon_history "
                        "(operation_key TEXT PRIMARY KEY, observation TEXT NOT NULL)"
                    )
                    db.execute("PRAGMA user_version = 1")
                key = json.dumps(request.operation.deduplication_key)
                row = db.execute(
                    "SELECT observation FROM local_coupon_history WHERE operation_key = ?", (key,)
                ).fetchone()
                result: DispatchObservation
                if row is not None:
                    result = DispatchAdmission.model_validate_json(row[0])
                    validate_dispatch_history(request, result)
                elif not admit:
                    result = DispatchObservation(
                        operation=request.operation, state="not_found", observed_at=clock()
                    )
                else:
                    now = clock()
                    digest = hashlib.sha256(key.encode()).hexdigest()
                    result = DispatchAdmission(
                        operation=request.operation,
                        accepted_fence=request.fence,
                        state="succeeded",
                        observed_at=now,
                        receipt=Receipt(
                            receipt_id=f"localref_{digest}",
                            adapter_id=self.adapter_id,
                            resource_ref=request.operation.resource_ref,
                            external_id=f"synthetic_{digest[:16]}",
                            status="accepted",
                            received_at=now,
                            summary_hash=f"sha256:{digest}",
                        ),
                    )
                    db.execute(
                        "INSERT INTO local_coupon_history VALUES (?, ?)",
                        (key, result.model_dump_json()),
                    )
                check()
                if admit:
                    db.commit()
                else:
                    db.rollback()
                return result
            except BaseException:
                db.rollback()
                raise
