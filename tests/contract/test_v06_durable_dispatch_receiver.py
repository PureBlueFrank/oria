"""Persistent synthetic acceptance, with no real adapter capability claim."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from tests.contract.durable_dispatch_receiver import DurableReferenceReceiver
from tests.contract.test_v06_dispatch_contract import _job, _Payload, _request

from oria.core.dispatch import DispatchRejected

pytestmark = [pytest.mark.contract, pytest.mark.security, pytest.mark.recovery]


def test_restart_and_concurrent_duplicate(tmp_path):
    path = tmp_path / "receiver.db"
    receiver = DurableReferenceReceiver(path)
    receiver.replace_authority(_job())
    request, payload = _request(), _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))
    assert receiver.execute(request).state == "not_found"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: DurableReferenceReceiver(path).execute(request, payload), range(8))
        )
    assert all(result == results[0] for result in results)
    assert DurableReferenceReceiver(path).execute(request) == results[0]
    with receiver._connect() as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1


def test_authority_change_and_conflict(tmp_path):
    receiver = DurableReferenceReceiver(tmp_path / "receiver.db")
    receiver.replace_authority(_job())
    payload = _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))
    original = receiver.execute(_request(), payload)
    newer = _job(lease_epoch=2, lease_owner="worker-b")
    receiver.replace_authority(newer)
    with pytest.raises(DispatchRejected):
        receiver.execute(_request(), payload)
    assert receiver.execute(_request(newer), payload) == original
    changed = _Payload(coupon_id="coupon-a", amount=Decimal("20.00"))
    with pytest.raises(DispatchRejected):
        receiver.execute(_request(newer, changed), changed)
    receiver.replace_authority(newer, allowed=False)
    with pytest.raises(PermissionError):
        receiver.execute(_request(newer))


@pytest.mark.parametrize("lookup", [False, True])
def test_lease_expiring_during_transaction_rejects_without_new_effect(
    tmp_path, monkeypatch, lookup
):
    from datetime import timedelta

    from tests.contract import durable_dispatch_receiver as module
    from tests.contract.test_v06_dispatch_contract import NOW

    receiver = DurableReferenceReceiver(tmp_path / "receiver.db")
    receiver.replace_authority(_job())
    payload = _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))
    if lookup:
        receiver.execute(_request(), payload)
        original = module.validate_dispatch_history
    else:
        original = module.validate_dispatch_payload

    def advance(*args, **kwargs):
        original(*args, **kwargs)
        monkeypatch.setattr(module, "NOW", NOW + timedelta(minutes=1))

    name = "validate_dispatch_history" if lookup else "validate_dispatch_payload"
    monkeypatch.setattr(module, name, advance)
    with pytest.raises(DispatchRejected):
        receiver.execute(_request(), None if lookup else payload)
    with receiver._connect() as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == int(lookup)


_PROCESS_SCRIPT = """
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from tests.contract.durable_dispatch_receiver import DurableReferenceReceiver
from tests.contract.test_v06_dispatch_contract import _job, _Payload, _request
from oria.core.dispatch import DispatchRejected

path, mode = Path(sys.argv[1]), sys.argv[2]
receiver = DurableReferenceReceiver(path)
payload = _Payload(coupon_id='coupon-a', amount=Decimal('10.00'))

class CrashConnection(sqlite3.Connection):
    def commit(self):
        if mode == 'before_commit':
            os._exit(71)
        super().commit()
        os._exit(72)

if mode in {'before_commit', 'after_commit'}:
    receiver._connect = lambda: sqlite3.connect(path, factory=CrashConnection)
    receiver.execute(_request(), payload)
elif mode == 'admit':
    print(receiver.execute(_request(), payload).model_dump_json())
elif mode == 'lookup':
    print(receiver.execute(_request()).model_dump_json())
elif mode == 'stale':
    print('ready', flush=True)
    sys.stdin.readline()
    try:
        receiver.execute(_request(), payload)
    except DispatchRejected:
        print('rejected', flush=True)
    else:
        raise AssertionError('stale epoch admitted')
"""


def _process(path, mode):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-c", _PROCESS_SCRIPT, str(path), mode],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "phase,code,expected",
    [
        ("before_commit", 71, "not_found"),
        ("after_commit", 72, "succeeded"),
    ],
)
def test_process_crash_at_commit_and_independent_lookup(tmp_path, phase, code, expected):
    from oria.core.dispatch import DispatchObservation, reconcile_dispatch

    path = tmp_path / "receiver.db"
    receiver = DurableReferenceReceiver(path)
    receiver.replace_authority(_job())
    crashed = _process(path, phase)
    assert crashed.returncode == code, crashed.stderr
    recovered = _process(path, "lookup")
    assert recovered.returncode == 0, recovered.stderr
    observation = DispatchObservation.model_validate_json(recovered.stdout)
    assert observation.state == expected
    # Absence after a crash is not permission for the orchestrator to redispatch.
    assert reconcile_dispatch(_request(), observation) == (
        "succeeded" if expected == "succeeded" else "unknown"
    )
    with receiver._connect() as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == int(
            expected == "succeeded"
        )


def test_independent_processes_duplicate_admission(tmp_path):
    from oria.core.dispatch import DispatchObservation

    path = tmp_path / "receiver.db"
    receiver = DurableReferenceReceiver(path)
    receiver.replace_authority(_job())
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = list(pool.map(lambda _: _process(path, "admit"), range(2)))
    assert all(result.returncode == 0 for result in completed), [r.stderr for r in completed]
    results = [DispatchObservation.model_validate_json(result.stdout) for result in completed]
    assert results[0] == results[1]
    with receiver._connect() as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1


def test_authority_commit_precedes_stale_process_admission(tmp_path):
    import json
    import subprocess
    import sys
    from contextlib import closing

    path = tmp_path / "receiver.db"
    receiver = DurableReferenceReceiver(path)
    receiver.replace_authority(_job())
    child = subprocess.Popen(
        [sys.executable, "-c", _PROCESS_SCRIPT, str(path), "stale"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Read readiness with a bounded helper, never sleep to establish ordering.
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(child.stdout.readline)
            try:
                assert ready.result(timeout=30).strip() == "ready"
            except BaseException:
                child.kill()
                raise
        with closing(receiver._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            newer = _job(lease_epoch=2, lease_owner="worker-b")
            db.execute(
                "UPDATE authority SET job = ? WHERE scope = ?",
                (
                    newer.model_dump_json(),
                    json.dumps([newer.tenant_id, newer.job_id]),
                ),
            )
            child.stdin.write("go\n")
            child.stdin.flush()
            db.commit()
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        assert out.strip() == "rejected"
        assert receiver.execute(_request(newer)).state == "not_found"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_commit_failure_rolls_back_and_closes_connection(tmp_path, monkeypatch):
    import sqlite3

    receiver = DurableReferenceReceiver(tmp_path / "receiver.db")
    receiver.replace_authority(_job())
    connections = []

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("synthetic commit failure")

    def connect():
        db = sqlite3.connect(receiver.path, factory=FailedCommit)
        connections.append(db)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(receiver, "_connect", connect)
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            receiver.execute(_request(), _Payload(coupon_id="coupon-a", amount=Decimal("10.00")))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert receiver.execute(_request()).state == "not_found"


@pytest.mark.parametrize(
    "change",
    [
        {"adapter_id": "other-adapter"},
        {"tool_name": "other-tool"},
        {"tool_schema_version": 2},
    ],
)
def test_unsupported_target_cannot_create_or_query_history(tmp_path, change):
    receiver = DurableReferenceReceiver(tmp_path / "receiver.db")
    receiver.replace_authority(_job())
    request = _request()
    request = request.model_copy(update={"operation": request.operation.model_copy(update=change)})
    for payload in (None, _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))):
        with pytest.raises(DispatchRejected, match="unsupported"):
            receiver.execute(request, payload)
    assert receiver.execute(_request()).state == "not_found"


@pytest.mark.parametrize("reason", ["expired", "cancelled", "tenant", "revoked"])
def test_current_authority_rejects_new_and_historical_requests(tmp_path, reason):
    from datetime import timedelta

    from tests.contract.test_v06_dispatch_contract import NOW

    now = NOW
    receiver = DurableReferenceReceiver(tmp_path / "receiver.db", clock=lambda: now)
    receiver.replace_authority(_job())
    payload = _Payload(coupon_id="coupon-a", amount=Decimal("10.00"))
    receiver.execute(_request(), payload)
    request = _request()
    if reason == "expired":
        now += timedelta(minutes=1)
    elif reason == "cancelled":
        receiver.replace_authority(_job(cancel_requested=True))
    elif reason == "tenant":
        request = _request(_job(tenant_id="tenant-b"))
    else:
        receiver.replace_authority(_job(), allowed=False)
    for body in (None, payload):
        with pytest.raises((DispatchRejected, PermissionError)):
            receiver.execute(request, body)
    with receiver._connect() as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1
