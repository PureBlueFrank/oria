"""Real PostgreSQL V0.6-T01 migration, repository, saver, and RLS checks."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text

from oria.config import resolve_runtime_config
from oria.core.approvals import Approval
from oria.domain.models import MerchantSeed, MerchantSeedSet
from oria.migrations.runner import upgrade_databases
from oria.orchestrator.checkpoint import open_tenant_postgres_saver
from oria.storage.database import DatabaseResources, set_session_tenant_context
from oria.storage.platform import SQLiteApprovalRepository
from oria.storage.repositories import SQLiteMerchantRepository

pytestmark = [pytest.mark.enterprise, pytest.mark.integration]


@pytest.fixture(scope="module")
def postgres_enterprise_environment() -> dict[str, str]:
    """Require explicit PostgreSQL selection and both real-database DSNs."""

    if os.environ.get("ORIA_RUN_ENTERPRISE") != "1":
        pytest.skip("ORIA_RUN_ENTERPRISE=1 is required for enterprise verification")
    targets = {
        target.strip().lower()
        for target in os.environ.get("ORIA_ENTERPRISE_TARGETS", "").split(",")
        if target.strip()
    }
    if "postgres" not in targets:
        pytest.skip("ORIA_ENTERPRISE_TARGETS must contain postgres")

    environment: dict[str, str] = {}
    for name in ("ORIA_TEST_PLATFORM_POSTGRES_DSN", "ORIA_TEST_BUSINESS_POSTGRES_DSN"):
        value = os.environ.get(name)
        if value is None or not value.strip():
            pytest.skip(f"{name} is required for real PostgreSQL verification")
        environment[name] = value
    return environment


def _config(tmp_path: Path, environment: dict[str, str]):
    config_path = tmp_path / "postgres.yaml"
    config_path.write_text(
        """\
environment: test
storage:
  platform_db: postgres
  biz_db: postgres
  platform_url: ${ORIA_TEST_PLATFORM_POSTGRES_DSN}
  business_url: ${ORIA_TEST_BUSINESS_POSTGRES_DSN}
""",
        encoding="utf-8",
    )
    return resolve_runtime_config(
        config_path=config_path,
        environ=environment,
        cwd=tmp_path,
    )


def _ctx(tenant_id: str) -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_real_postgres_revision_chains_repository_contract_and_rls_reset(
    tmp_path: Path,
    postgres_enterprise_environment: dict[str, str],
) -> None:
    config = _config(tmp_path, postgres_enterprise_environment)
    run_prefix = uuid4().hex[:8]
    tenant_a = f"tenant-a-{run_prefix}"
    tenant_b = f"tenant-b-{run_prefix}"
    approval_id = f"approval-{run_prefix}"
    merchant_id = f"merchant-{run_prefix}"
    revisions = upgrade_databases(config)
    assert revisions.platform_revision == "platform_0009"
    assert revisions.business_revision == "business_0011"

    async with DatabaseResources(config) as databases:
        approval = Approval(
            approval_id=approval_id,
            tenant_id=tenant_a,
            approval_action="launch_approval",
            tool_name="LaunchPlan",
            canonical_args_hash="sha256:" + "a" * 64,
            checkpoint_id=f"checkpoint-{run_prefix}",
            policy_version="policy-v1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            requester="requester-a",
        )
        approval_repository = SQLiteApprovalRepository(databases.platform_sessions)
        await approval_repository.add(approval)
        assert await approval_repository.get(tenant_a, approval_id) == approval
        assert await approval_repository.get(tenant_b, approval_id) is None

        repository = SQLiteMerchantRepository(databases.business_sessions)
        for tenant_id in (tenant_a, tenant_b):
            await repository.seed(
                MerchantSeedSet(
                    tenant_id=tenant_id,
                    version="v1",
                    merchants=(
                        MerchantSeed(
                            merchant_id=merchant_id,
                            version=1,
                            display_name=tenant_id,
                            categories=("food",),
                            cities=("Shanghai",),
                            enrollment_systems=("oria",),
                            sales_org_code="internal",
                            active=True,
                        ),
                    ),
                )
            )

        assert len(await repository.list_for_eligibility(_ctx(tenant_a))) == 1
        assert len(await repository.list_for_eligibility(_ctx(tenant_b))) == 1

        async with databases.business_sessions() as session:
            rows = await session.scalar(text("SELECT count(*) FROM merchants"))
            setting = await session.scalar(text("SELECT current_setting('oria.tenant_id', true)"))
            assert rows == 0
            assert setting in (None, "")

        # A raw :tenant_id bind never establishes the RLS identity.
        async with databases.business_sessions() as session:
            visible = await session.scalar(
                text("SELECT count(*) FROM merchants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_a},
            )
            setting = await session.scalar(text("SELECT current_setting('oria.tenant_id', true)"))
            assert visible == 0
            assert setting in (None, "")

        # Only the explicit trusted API establishes the RLS identity.
        async with databases.business_sessions() as session:
            await set_session_tenant_context(session, tenant_a)
            visible = await session.scalar(
                text("SELECT count(*) FROM merchants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_a},
            )
            setting = await session.scalar(text("SELECT current_setting('oria.tenant_id', true)"))
            assert visible == 1
            assert setting == tenant_a

        # A new transaction on a pooled connection starts with no tenant context.
        async with databases.business_sessions() as session:
            setting = await session.scalar(text("SELECT current_setting('oria.tenant_id', true)"))
            rows = await session.scalar(text("SELECT count(*) FROM merchants"))
            assert setting in (None, "")
            assert rows == 0


@pytest.mark.asyncio
async def test_real_official_postgres_saver_through_tenant_adapter(
    tmp_path: Path,
    postgres_enterprise_environment: dict[str, str],
) -> None:
    config = _config(tmp_path, postgres_enterprise_environment)
    run_prefix = uuid4().hex[:8]
    tenant_id = f"tenant-a-{run_prefix}"
    thread_id = f"shared-{run_prefix}"
    upgrade_databases(config, target="platform")
    assert config.storage.platform_url is not None
    async with open_tenant_postgres_saver(config.storage.platform_url) as saver:
        config_a = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "oria_tenant_id": tenant_id,
            }
        }
        saved = await saver.aput(
            config_a,
            empty_checkpoint(),
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        await saver.aput_writes(
            saved,
            (("messages", "value"),),
            f"task-{run_prefix}",
            "path",
        )
        loaded = await saver.aget_tuple(saved)
        assert loaded is not None
        assert loaded.config["configurable"]["thread_id"] == thread_id
        assert loaded.pending_writes is not None
