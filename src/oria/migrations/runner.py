"""Programmatic Alembic runner for the two installed-package revision chains."""

from __future__ import annotations

import sqlite3
import warnings
from importlib import resources
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from oria.config.models import ResolvedRuntimeConfig
from oria.core.types import ValueModel
from oria.resources.loader import PackageAssetError, verify_migration_assets
from oria.storage.urls import sqlalchemy_postgres_url

ColumnSignature = tuple[str, str, int, int]
ForeignKeySignature = tuple[str, tuple[tuple[str, str], ...]]


def _business_columns(
    entity_id: str,
    *columns: ColumnSignature,
) -> tuple[ColumnSignature, ...]:
    return (
        ("tenant_id", "VARCHAR", 1, 1),
        (entity_id, "VARCHAR", 1, 2),
        ("version", "INTEGER", 1, 0),
        ("created_at", "DATETIME", 1, 0),
        ("updated_at", "DATETIME", 1, 0),
        *columns,
    )


_EXPECTED_COLUMNS: dict[str, dict[str, tuple[ColumnSignature, ...]]] = {
    "platform": {
        "documents": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("document_id", "VARCHAR", 1, 2),
            ("source_uri", "VARCHAR", 1, 0),
            ("owner_ref", "VARCHAR", 1, 0),
            ("data_classification", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("deleted_at", "DATETIME", 0, 0),
        ),
        "document_versions": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("document_id", "VARCHAR", 1, 2),
            ("version", "VARCHAR", 1, 3),
            ("content_hash", "VARCHAR", 1, 0),
            ("object_ref", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("acl_json", "TEXT", 1, 0),
            ("metadata_json", "TEXT", 1, 0),
            ("chunking_version", "VARCHAR", 1, 0),
            ("embedding_profile", "VARCHAR", 1, 0),
            ("deleted_at", "DATETIME", 0, 0),
            ("owner_ref", "VARCHAR", 1, 0),
            ("data_classification", "VARCHAR", 1, 0),
            ("superseded_at", "DATETIME", 0, 0),
        ),
        "ingestion_runs": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("run_id", "VARCHAR", 1, 2),
            ("document_id", "VARCHAR", 1, 0),
            ("document_version", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("started_at", "DATETIME", 1, 0),
            ("completed_at", "DATETIME", 0, 0),
        ),
        "rule_snapshot_cache": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("snapshot_id", "VARCHAR", 1, 2),
            ("snapshot_hash", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("created_at", "DATETIME", 1, 0),
        ),
        "read_policy": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("subject_id", "VARCHAR", 1, 2),
            ("allowed_roles_json", "TEXT", 1, 0),
            ("allowed_classifications_json", "TEXT", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
        "audit_events": (
            ("event_id", "VARCHAR", 1, 1),
            ("occurred_at", "DATETIME", 1, 0),
            ("tenant_id", "VARCHAR", 1, 0),
            ("actor", "VARCHAR", 1, 0),
            ("action", "VARCHAR", 1, 0),
            ("resource_type", "VARCHAR", 1, 0),
            ("resource_id", "VARCHAR", 1, 0),
            ("resource_tenant_id", "VARCHAR", 1, 0),
            ("decision", "VARCHAR", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("args_hash", "VARCHAR", 1, 0),
            ("result", "VARCHAR", 1, 0),
            ("correlation_id", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
        ),
        "outbox": (
            ("event_id", "VARCHAR", 1, 1),
            ("tenant_id", "VARCHAR", 1, 0),
            ("topic", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("occurred_at", "DATETIME", 1, 0),
            ("available_at", "DATETIME", 1, 0),
            ("published_at", "DATETIME", 0, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("last_error_code", "VARCHAR", 0, 0),
        ),
        "approvals": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("approval_id", "VARCHAR", 1, 2),
            ("approval_action", "VARCHAR", 1, 0),
            ("tool_name", "VARCHAR", 1, 0),
            ("canonical_args_hash", "VARCHAR", 1, 0),
            ("checkpoint_id", "VARCHAR", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("expires_at", "DATETIME", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("requester", "VARCHAR", 1, 0),
            ("decider", "VARCHAR", 0, 0),
            ("decision", "VARCHAR", 0, 0),
            ("reason", "TEXT", 0, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
            ("decided_at", "DATETIME", 0, 0),
            ("campaign_id", "VARCHAR", 0, 0),
            ("enrollment_version", "INTEGER", 0, 0),
            ("link_version", "INTEGER", 0, 0),
            ("selection_version", "VARCHAR", 0, 0),
            ("selection_hash", "VARCHAR", 0, 0),
            ("rule_snapshot_hash", "VARCHAR", 0, 0),
        ),
        "approval_binding_invalidations": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("event_id", "VARCHAR", 1, 2),
            ("campaign_id", "VARCHAR", 1, 0),
            ("enrollment_version", "INTEGER", 1, 0),
            ("link_version", "INTEGER", 1, 0),
            ("selection_version", "VARCHAR", 1, 0),
            ("selection_hash", "VARCHAR", 0, 0),
            ("rule_snapshot_hash", "VARCHAR", 1, 0),
            ("reason", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("last_error_code", "VARCHAR", 0, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
        "external_waits": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("wait_id", "VARCHAR", 1, 2),
            ("event_type", "VARCHAR", 1, 0),
            ("resource_type", "VARCHAR", 1, 0),
            ("resource_id", "VARCHAR", 1, 0),
            ("expected_version", "INTEGER", 1, 0),
            ("checkpoint_id", "VARCHAR", 1, 0),
            ("expires_at", "DATETIME", 1, 0),
            ("timeout_action", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("resolved_at", "DATETIME", 0, 0),
        ),
        "integration_event_inbox": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("adapter_id", "VARCHAR", 1, 2),
            ("source_event_id", "VARCHAR", 1, 3),
            ("schema_version", "INTEGER", 1, 0),
            ("event_type", "VARCHAR", 1, 0),
            ("resource_version", "INTEGER", 1, 0),
            ("signature_subject", "VARCHAR", 1, 0),
            ("redacted_payload_json", "TEXT", 1, 0),
            ("payload_hash", "VARCHAR", 1, 0),
            ("processing_status", "VARCHAR", 1, 0),
            ("wait_id", "VARCHAR", 0, 0),
            ("received_at", "DATETIME", 1, 0),
            ("processed_at", "DATETIME", 0, 0),
        ),
        "memory_items": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("subject_id", "VARCHAR", 1, 2),
            ("memory_id", "VARCHAR", 1, 3),
            ("content", "TEXT", 1, 0),
            ("provenance", "VARCHAR", 1, 0),
            ("confidence", "FLOAT", 1, 0),
            ("sensitivity", "VARCHAR", 1, 0),
            ("expires_at", "DATETIME", 0, 0),
            ("score", "FLOAT", 1, 0),
            ("content_hash", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
            ("deleted_at", "DATETIME", 0, 0),
        ),
        "memory_embeddings": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("subject_id", "VARCHAR", 1, 2),
            ("memory_id", "VARCHAR", 1, 3),
            ("projection_id", "VARCHAR", 1, 0),
            ("embedding_json", "TEXT", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
    },
    "business": {
        "product_snapshots": _business_columns(
            "product_snapshot_id",
            ("product_ref", "VARCHAR", 1, 0),
            ("product_version", "VARCHAR", 1, 0),
            ("catalog_snapshot_id", "VARCHAR", 1, 0),
            ("attributes_json", "TEXT", 1, 0),
            ("merchant_id", "VARCHAR", 1, 0),
        ),
        "campaign_rule_snapshot_refs": _business_columns(
            "campaign_rule_snapshot_ref_id",
            ("snapshot_id", "VARCHAR", 1, 0),
            ("snapshot_hash", "VARCHAR", 1, 0),
        ),
        "campaigns": _business_columns(
            "campaign_id",
            ("rule_snapshot_ref_id", "VARCHAR", 1, 0),
            ("enrollment_mode", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "coupon_batches": _business_columns(
            "coupon_batch_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("coupon_spec_hash", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "launch_saga_states": _business_columns(
            "launch_saga_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("checkpoint", "VARCHAR", 1, 0),
        ),
        "recruitment_publications": _business_columns(
            "recruitment_publication_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("merchant_scope_hash", "VARCHAR", 1, 0),
            ("material_version", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("request_id", "VARCHAR", 0, 0),
            ("receipt_id", "VARCHAR", 0, 0),
        ),
        "enrollments": _business_columns(
            "enrollment_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("merchant_id", "VARCHAR", 1, 0),
            ("mode", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "enrollment_items": _business_columns(
            "enrollment_item_id",
            ("enrollment_id", "VARCHAR", 1, 0),
            ("campaign_id", "VARCHAR", 1, 0),
            ("merchant_id", "VARCHAR", 1, 0),
            ("product_ref", "VARCHAR", 1, 0),
            ("product_version", "VARCHAR", 1, 0),
            ("product_snapshot_id", "VARCHAR", 1, 0),
            ("mode", "VARCHAR", 1, 0),
            ("sources_json", "TEXT", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "enrollment_coupon_links": _business_columns(
            "enrollment_coupon_link_id",
            ("enrollment_item_id", "VARCHAR", 1, 0),
            ("coupon_batch_id", "VARCHAR", 1, 0),
            ("benefit_tier", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "confirmation_tasks": _business_columns(
            "confirmation_task_id",
            ("enrollment_item_id", "VARCHAR", 1, 0),
            ("subject_type", "VARCHAR", 1, 0),
            ("subject_id", "VARCHAR", 1, 0),
            ("sequence", "INTEGER", 1, 0),
            ("due_at", "DATETIME", 1, 0),
            ("timeout_action", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
        ),
        "campaign_approval_bindings": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("campaign_id", "VARCHAR", 1, 2),
            ("enrollment_version", "INTEGER", 1, 0),
            ("link_version", "INTEGER", 1, 0),
            ("selection_version", "VARCHAR", 1, 0),
            ("rule_snapshot_hash", "VARCHAR", 1, 0),
            ("selection_hash", "VARCHAR", 0, 0),
        ),
        "assortment_submissions": _business_columns(
            "assortment_submission_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("submission_version", "VARCHAR", 1, 0),
            ("assortment_policy_ref", "VARCHAR", 1, 0),
            ("assortment_policy_version", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("selection_version", "VARCHAR", 0, 0),
            ("selection_hash", "VARCHAR", 0, 0),
        ),
        "assortment_submission_items": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("campaign_id", "VARCHAR", 1, 2),
            ("submission_version", "VARCHAR", 1, 3),
            ("enrollment_item_id", "VARCHAR", 1, 4),
            ("created_at", "DATETIME", 1, 0),
        ),
        "selection_decisions": _business_columns(
            "selection_decision_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("submission_version", "VARCHAR", 1, 0),
            ("selection_version", "VARCHAR", 1, 0),
            ("enrollment_item_id", "VARCHAR", 1, 0),
            ("decision", "VARCHAR", 1, 0),
            ("reason_code", "VARCHAR", 0, 0),
        ),
        "consumer_placements": _business_columns(
            "consumer_placement_id",
            ("campaign_id", "VARCHAR", 1, 0),
            ("selection_version", "VARCHAR", 1, 0),
            ("placement_spec_hash", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("request_id", "VARCHAR", 0, 0),
            ("receipt_id", "VARCHAR", 0, 0),
        ),
        "merchant_notifications": _business_columns(
            "merchant_notification_id",
            ("merchant_id", "VARCHAR", 1, 0),
            ("campaign_id", "VARCHAR", 1, 0),
            ("result_version", "VARCHAR", 1, 0),
            ("template_id", "VARCHAR", 1, 0),
            ("channel", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("receipt_id", "VARCHAR", 0, 0),
        ),
        "tool_executions": (
            ("execution_id", "VARCHAR", 1, 1),
            ("tenant_id", "VARCHAR", 1, 0),
            ("tool_name", "VARCHAR", 1, 0),
            ("idempotency_key", "VARCHAR", 1, 0),
            ("canonical_args_hash", "VARCHAR", 1, 0),
            ("checkpoint_id", "VARCHAR", 1, 0),
            ("status", "VARCHAR", 1, 0),
            ("receipt_id", "VARCHAR", 0, 0),
            ("compensation_status", "VARCHAR", 0, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
            ("executed_at", "DATETIME", 0, 0),
            ("receipt_summary_hash", "VARCHAR", 0, 0),
        ),
        "tool_execution_requests": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("tool_name", "VARCHAR", 1, 2),
            ("request_idempotency_key", "VARCHAR", 1, 3),
            ("canonical_args_hash", "VARCHAR", 1, 0),
            ("execution_id", "VARCHAR", 1, 0),
            ("created_at", "DATETIME", 1, 0),
        ),
        "domain_events": (
            ("event_id", "VARCHAR", 1, 1),
            ("tenant_id", "VARCHAR", 1, 0),
            ("aggregate_type", "VARCHAR", 1, 0),
            ("aggregate_id", "VARCHAR", 1, 0),
            ("event_type", "VARCHAR", 1, 0),
            ("event_version", "INTEGER", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("occurred_at", "DATETIME", 1, 0),
            ("correlation_id", "VARCHAR", 1, 0),
        ),
        "audit_events": (
            ("event_id", "VARCHAR", 1, 1),
            ("occurred_at", "DATETIME", 1, 0),
            ("tenant_id", "VARCHAR", 1, 0),
            ("actor", "VARCHAR", 1, 0),
            ("action", "VARCHAR", 1, 0),
            ("resource_type", "VARCHAR", 1, 0),
            ("resource_id", "VARCHAR", 1, 0),
            ("resource_tenant_id", "VARCHAR", 1, 0),
            ("decision", "VARCHAR", 1, 0),
            ("policy_version", "VARCHAR", 1, 0),
            ("args_hash", "VARCHAR", 1, 0),
            ("result", "VARCHAR", 1, 0),
            ("correlation_id", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
        ),
        "outbox": (
            ("event_id", "VARCHAR", 1, 1),
            ("tenant_id", "VARCHAR", 1, 0),
            ("topic", "VARCHAR", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("occurred_at", "DATETIME", 1, 0),
            ("available_at", "DATETIME", 1, 0),
            ("published_at", "DATETIME", 0, 0),
            ("attempt_count", "INTEGER", 1, 0),
            ("last_error_code", "VARCHAR", 0, 0),
        ),
        "merchants": (
            ("tenant_id", "VARCHAR", 1, 1),
            ("merchant_id", "VARCHAR", 1, 2),
            ("version", "INTEGER", 1, 0),
            ("display_name", "VARCHAR", 1, 0),
            ("categories_json", "TEXT", 1, 0),
            ("cities_json", "TEXT", 1, 0),
            ("enrollment_systems_json", "TEXT", 1, 0),
            ("sales_org_code", "VARCHAR", 1, 0),
            ("active", "BOOLEAN", 1, 0),
            ("created_at", "DATETIME", 1, 0),
            ("updated_at", "DATETIME", 1, 0),
        ),
    },
}
_EXPECTED_FOREIGN_KEYS: dict[str, dict[str, frozenset[ForeignKeySignature]]] = {
    "platform": {
        "documents": frozenset(),
        "document_versions": frozenset(
            {
                (
                    "documents",
                    (("tenant_id", "tenant_id"), ("document_id", "document_id")),
                )
            }
        ),
        "ingestion_runs": frozenset(
            {
                (
                    "document_versions",
                    (
                        ("tenant_id", "tenant_id"),
                        ("document_id", "document_id"),
                        ("document_version", "version"),
                    ),
                )
            }
        ),
        "rule_snapshot_cache": frozenset(),
        "read_policy": frozenset(),
        "audit_events": frozenset(),
        "outbox": frozenset(),
        "approvals": frozenset(),
        "approval_binding_invalidations": frozenset(),
        "external_waits": frozenset(),
        "integration_event_inbox": frozenset(
            {
                (
                    "external_waits",
                    (("tenant_id", "tenant_id"), ("wait_id", "wait_id")),
                )
            }
        ),
        "memory_items": frozenset(),
        "memory_embeddings": frozenset(
            {
                (
                    "memory_items",
                    (
                        ("tenant_id", "tenant_id"),
                        ("subject_id", "subject_id"),
                        ("memory_id", "memory_id"),
                    ),
                )
            }
        ),
    },
    "business": {
        "product_snapshots": frozenset(),
        "campaign_rule_snapshot_refs": frozenset(),
        "campaigns": frozenset(
            {
                (
                    "campaign_rule_snapshot_refs",
                    (
                        ("tenant_id", "tenant_id"),
                        ("rule_snapshot_ref_id", "campaign_rule_snapshot_ref_id"),
                    ),
                )
            }
        ),
        "coupon_batches": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "launch_saga_states": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "recruitment_publications": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "enrollments": frozenset(
            {
                ("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id"))),
                ("merchants", (("tenant_id", "tenant_id"), ("merchant_id", "merchant_id"))),
            }
        ),
        "enrollment_items": frozenset(
            {
                (
                    "enrollments",
                    (
                        ("tenant_id", "tenant_id"),
                        ("campaign_id", "campaign_id"),
                        ("merchant_id", "merchant_id"),
                    ),
                ),
                (
                    "enrollments",
                    (("tenant_id", "tenant_id"), ("enrollment_id", "enrollment_id")),
                ),
                (
                    "product_snapshots",
                    (
                        ("tenant_id", "tenant_id"),
                        ("merchant_id", "merchant_id"),
                        ("product_ref", "product_ref"),
                        ("product_version", "product_version"),
                        ("product_snapshot_id", "product_snapshot_id"),
                    ),
                ),
            }
        ),
        "enrollment_coupon_links": frozenset(
            {
                (
                    "enrollment_items",
                    (("tenant_id", "tenant_id"), ("enrollment_item_id", "enrollment_item_id")),
                ),
                (
                    "coupon_batches",
                    (("tenant_id", "tenant_id"), ("coupon_batch_id", "coupon_batch_id")),
                ),
            }
        ),
        "confirmation_tasks": frozenset(
            {
                (
                    "enrollment_items",
                    (("tenant_id", "tenant_id"), ("enrollment_item_id", "enrollment_item_id")),
                )
            }
        ),
        "campaign_approval_bindings": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "assortment_submissions": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "assortment_submission_items": frozenset(
            {
                (
                    "assortment_submissions",
                    (
                        ("tenant_id", "tenant_id"),
                        ("campaign_id", "campaign_id"),
                        ("submission_version", "submission_version"),
                    ),
                ),
                (
                    "enrollment_items",
                    (
                        ("tenant_id", "tenant_id"),
                        ("campaign_id", "campaign_id"),
                        ("enrollment_item_id", "enrollment_item_id"),
                    ),
                ),
            }
        ),
        "selection_decisions": frozenset(
            {
                (
                    "assortment_submissions",
                    (
                        ("tenant_id", "tenant_id"),
                        ("campaign_id", "campaign_id"),
                        ("submission_version", "submission_version"),
                    ),
                ),
                (
                    "enrollment_items",
                    (("tenant_id", "tenant_id"), ("enrollment_item_id", "enrollment_item_id")),
                ),
            }
        ),
        "consumer_placements": frozenset(
            {("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id")))}
        ),
        "merchant_notifications": frozenset(
            {
                ("campaigns", (("tenant_id", "tenant_id"), ("campaign_id", "campaign_id"))),
                ("merchants", (("tenant_id", "tenant_id"), ("merchant_id", "merchant_id"))),
            }
        ),
        "tool_executions": frozenset(),
        "tool_execution_requests": frozenset(
            {
                (
                    "tool_executions",
                    (("tenant_id", "tenant_id"), ("execution_id", "execution_id")),
                )
            }
        ),
        "domain_events": frozenset(),
        "audit_events": frozenset(),
        "outbox": frozenset(),
        "merchants": frozenset(),
    },
}
_EXPECTED_TABLES = {target: frozenset(tables) for target, tables in _EXPECTED_COLUMNS.items()}
_VERSION_TABLES = {
    "platform": "alembic_version_platform",
    "business": "alembic_version_business",
}


class MigrationError(RuntimeError):
    """Safe migration failure without SQL, filesystem internals, or resource contents."""


class MigrationResult(ValueModel):
    platform_revision: str | None = None
    business_revision: str | None = None


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


MigrationTarget = Literal["platform", "business", "all"]


def _assert_paths(config: ResolvedRuntimeConfig) -> tuple[Path, Path]:
    if config.edition == "production" and not config.data_dir.is_absolute():
        raise MigrationError("production data initialization requires an absolute data directory")
    paths = config.data_paths
    root = paths.root.resolve(strict=False)
    database_paths = (paths.platform_db, paths.business_db)
    for database_path in database_paths:
        resolved = database_path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise MigrationError("database path escapes the configured data directory")
    return database_paths


def _target_url(config: ResolvedRuntimeConfig, target: Literal["platform", "business"]) -> str:
    paths = config.data_paths
    if target == "platform":
        if config.storage.platform_db == "postgres":
            if config.storage.platform_url is None:
                raise MigrationError("platform PostgreSQL configuration is incomplete")
            return sqlalchemy_postgres_url(config.storage.platform_url)
        return _sqlite_url(paths.platform_db)
    if config.storage.biz_db == "postgres":
        if config.storage.business_url is None:
            raise MigrationError("business PostgreSQL configuration is incomplete")
        return sqlalchemy_postgres_url(config.storage.business_url)
    return _sqlite_url(paths.business_db)


def _upgrade_target(target: str, database_url: str) -> None:
    script = resources.files(f"oria.migrations.{target}")
    if not script.is_dir():
        raise MigrationError("installed migration chain is unavailable")
    config = Config()
    config.set_main_option("script_location", str(script))
    config.set_main_option("sqlalchemy.url", database_url)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Unnamed CHECK constraint on reflected table .* is being omitted.*",
                category=UserWarning,
                module=r"alembic\.operations\.batch",
            )
            command.upgrade(config, "head")
    except (CommandError, OSError, SQLAlchemyError, sqlite3.Error) as exc:
        raise MigrationError(f"{target} database migration failed") from exc


def _installed_migration_heads() -> dict[str, str]:
    heads: dict[str, str] = {}
    for target in ("platform", "business"):
        script = resources.files(f"oria.migrations.{target}")
        config = Config()
        config.set_main_option("script_location", str(script))
        try:
            actual_heads = ScriptDirectory.from_config(config).get_heads()
        except (CommandError, OSError) as exc:
            raise MigrationError(f"{target} migration tree verification failed") from exc
        if len(actual_heads) != 1:
            raise MigrationError(f"{target} migration tree must have exactly one head")
        heads[target] = actual_heads[0]
    return heads


def _column_signature(connection: sqlite3.Connection, table: str) -> tuple[ColumnSignature, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)


def _foreign_key_signature(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[ForeignKeySignature]:
    rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    grouped: dict[int, list[tuple[int, str, str, str]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(
            (int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        )
    return frozenset(
        (
            sorted(parts)[0][1],
            tuple((source, destination) for _, _, source, destination in sorted(parts)),
        )
        for parts in grouped.values()
    )


def _validate_schema(connection: sqlite3.Connection, target: str) -> None:
    for table, expected_columns in _EXPECTED_COLUMNS[target].items():
        if _column_signature(connection, table) != expected_columns:
            raise MigrationError(f"{target} database schema verification failed")
        if _foreign_key_signature(connection, table) != _EXPECTED_FOREIGN_KEYS[target][table]:
            raise MigrationError(f"{target} database schema verification failed")


def _validate_target(target: str, database_path: Path, expected_head: str) -> None:
    try:
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            tables = {str(row[0]) for row in rows}
            version_table = _VERSION_TABLES[target]
            if version_table not in tables or not _EXPECTED_TABLES[target].issubset(tables):
                raise MigrationError(f"{target} database schema verification failed")
            _validate_schema(connection, target)
            revisions = connection.execute(f'SELECT version_num FROM "{version_table}"').fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"{target} database schema verification failed") from exc
    if revisions != [(expected_head,)]:
        raise MigrationError(f"{target} database revision verification failed")
    other_target = "business" if target == "platform" else "platform"
    foreign_tables = _EXPECTED_TABLES[other_target].difference(_EXPECTED_TABLES[target])
    if tables.intersection(foreign_tables):
        raise MigrationError(f"{target} database contains tables from the other revision chain")


def _portable_column_type(type_: object, expected: str) -> bool:
    if expected == "BOOLEAN":
        return isinstance(type_, Boolean)
    if expected == "INTEGER":
        return isinstance(type_, Integer)
    if expected == "FLOAT":
        return isinstance(type_, Float)
    if expected == "DATETIME":
        return isinstance(type_, DateTime)
    if expected == "TEXT":
        return isinstance(type_, Text)
    if expected == "VARCHAR":
        return isinstance(type_, String) and not isinstance(type_, Text)
    return False


def _postgres_foreign_keys(connection: Connection, table: str) -> frozenset[ForeignKeySignature]:
    inspector = inspect(connection)
    return frozenset(
        (
            str(foreign_key["referred_table"]),
            tuple(
                zip(
                    (str(value) for value in foreign_key["constrained_columns"]),
                    (str(value) for value in foreign_key["referred_columns"]),
                    strict=True,
                )
            ),
        )
        for foreign_key in inspector.get_foreign_keys(table)
    )


def _validate_postgres_schema(connection: Connection, target: str) -> None:
    inspector = inspect(connection)
    for table, expected_columns in _EXPECTED_COLUMNS[target].items():
        columns = inspector.get_columns(table)
        primary_key = inspector.get_pk_constraint(table).get("constrained_columns") or ()
        expected_primary_key = tuple(
            name
            for name, _, _, position in sorted(expected_columns, key=lambda item: item[3])
            if position
        )
        if tuple(str(name) for name in primary_key) != expected_primary_key:
            raise MigrationError(f"{target} database schema verification failed")
        if [str(column["name"]) for column in columns] != [
            name for name, _, _, _ in expected_columns
        ]:
            raise MigrationError(f"{target} database schema verification failed")
        for column, (_, expected_type, not_null, _) in zip(columns, expected_columns, strict=True):
            if bool(column["nullable"]) == bool(not_null) or not _portable_column_type(
                column["type"], expected_type
            ):
                raise MigrationError(f"{target} database schema verification failed")
        if _postgres_foreign_keys(connection, table) != _EXPECTED_FOREIGN_KEYS[target][table]:
            raise MigrationError(f"{target} database schema verification failed")


def _validate_postgres_rls(connection: Connection, target: str) -> None:
    rows = connection.execute(
        text(
            "SELECT tablename, rowsecurity, forcerowsecurity FROM pg_tables "
            "WHERE schemaname = current_schema()"
        )
    ).mappings()
    protected = {
        str(row["tablename"])
        for row in rows
        if bool(row["rowsecurity"]) and bool(row["forcerowsecurity"])
    }
    policies = {
        str(row[0])
        for row in connection.execute(
            text(
                "SELECT tablename FROM pg_policies WHERE schemaname = current_schema() "
                "AND policyname = 'oria_tenant_isolation'"
            )
        )
    }
    if not _EXPECTED_TABLES[target].issubset(protected.intersection(policies)):
        raise MigrationError(f"{target} database tenant isolation verification failed")


def _validate_postgres_target(target: str, database_url: str, expected_head: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            version_table = _VERSION_TABLES[target]
            if version_table not in tables or not _EXPECTED_TABLES[target].issubset(tables):
                raise MigrationError(f"{target} database schema verification failed")
            _validate_postgres_schema(connection, target)
            revisions = [
                tuple(row)
                for row in connection.execute(text(f'SELECT version_num FROM "{version_table}"'))
            ]
            _validate_postgres_rls(connection, target)
    except SQLAlchemyError as exc:
        raise MigrationError(f"{target} database schema verification failed") from exc
    finally:
        engine.dispose()
    if revisions != [(expected_head,)]:
        raise MigrationError(f"{target} database revision verification failed")
    other_target = "business" if target == "platform" else "platform"
    foreign_tables = _EXPECTED_TABLES[other_target].difference(_EXPECTED_TABLES[target])
    if tables.intersection(foreign_tables):
        raise MigrationError(f"{target} database contains tables from the other revision chain")


def _prepare_sqlite_target(
    config: ResolvedRuntimeConfig, target: Literal["platform", "business"]
) -> Path | None:
    platform_path, business_path = _assert_paths(config)
    if target == "platform" and config.storage.platform_db == "sqlite":
        platform_path.parent.mkdir(parents=True, exist_ok=True)
        return platform_path
    if target == "business" and config.storage.biz_db == "sqlite":
        business_path.parent.mkdir(parents=True, exist_ok=True)
        return business_path
    return None


def _upgrade_and_validate(
    config: ResolvedRuntimeConfig,
    target: Literal["platform", "business"],
    expected_head: str,
) -> None:
    database_url = _target_url(config, target)
    sqlite_path = _prepare_sqlite_target(config, target)
    _upgrade_target(target, database_url)
    if sqlite_path is not None:
        _validate_target(target, sqlite_path, expected_head)
    else:
        _validate_postgres_target(target, database_url, expected_head)


def upgrade_databases(
    config: ResolvedRuntimeConfig,
    *,
    target: MigrationTarget = "all",
) -> MigrationResult:
    """Upgrade one or both chains using verified installed-package resources."""
    if target not in {"platform", "business", "all"}:
        raise MigrationError("migration target must be platform, business, or all")
    try:
        heads = verify_migration_assets()
    except PackageAssetError as exc:
        raise MigrationError("installed migration assets failed verification") from exc
    if _installed_migration_heads() != heads:
        raise MigrationError("installed migration heads do not match the verified manifest")
    platform_revision: str | None = None
    business_revision: str | None = None
    if target in {"platform", "all"}:
        _upgrade_and_validate(config, "platform", heads["platform"])
        platform_revision = heads["platform"]
    if target in {"business", "all"}:
        try:
            _upgrade_and_validate(config, "business", heads["business"])
            business_revision = heads["business"]
        except MigrationError as exc:
            if target == "all":
                raise MigrationError(
                    "business migration failed after platform upgrade; correct the failure and "
                    "rerun initialization to converge both databases"
                ) from exc
            raise
    return MigrationResult(
        platform_revision=platform_revision,
        business_revision=business_revision,
    )
