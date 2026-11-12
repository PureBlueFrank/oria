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
