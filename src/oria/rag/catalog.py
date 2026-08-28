"""SQLite truth-source repository for document lifecycle and derived snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.types import ACLMetadata
from oria.rag.errors import CatalogError
from oria.rag.models import CatalogVersion, DocumentIngestRequest


class SQLiteKnowledgeCatalog:
    """Keep catalog/object identities authoritative; vector entries remain projections."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def begin_ingestion(
        self,
        *,
        tenant_id: str,
        run_id: str,
        request: DocumentIngestRequest,
        content_hash: str,
        object_ref: str,
        chunking_version: str,
        embedding_profile: str,
    ) -> bool:
        now = datetime.now(UTC)
        acl_json = _canonical_json(request.acl.model_dump(mode="json"))
        metadata_json = _canonical_json(request.metadata)
        try:
            async with self._sessions.begin() as session:
                document = (
                    (
                        await session.execute(
                            text(
                                "SELECT source_uri, owner_ref, data_classification FROM documents "
                                "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                            ),
                            {"tenant_id": tenant_id, "document_id": request.document_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if document is None:
                    await session.execute(
                        text(
                            "INSERT INTO documents "
                            "(tenant_id, document_id, source_uri, owner_ref, data_classification, "
                            "created_at, deleted_at) VALUES "
                            "(:tenant_id, :document_id, :source_uri, :owner_ref, "
                            ":data_classification, :created_at, NULL)"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "document_id": request.document_id,
                            "source_uri": request.source_uri,
                            "owner_ref": request.owner_ref,
                            "data_classification": request.data_classification,
                            "created_at": now,
                        },
                    )
                elif (
                    str(document["source_uri"]),
                    str(document["owner_ref"]),
                    str(document["data_classification"]),
                ) != (request.source_uri, request.owner_ref, request.data_classification):
                    raise CatalogError("document identity conflicts with the existing catalog")
                else:
                    await session.execute(
                        text(
                            "UPDATE documents SET deleted_at = NULL "
                            "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                        ),
                        {"tenant_id": tenant_id, "document_id": request.document_id},
                    )

                version = (
                    (
                        await session.execute(
                            text(
                                "SELECT content_hash, object_ref, acl_json, metadata_json, "
                                "chunking_version, embedding_profile, deleted_at "
                                "FROM document_versions WHERE tenant_id = :tenant_id "
                                "AND document_id = :document_id AND version = :version"
                            ),
                            {
                                "tenant_id": tenant_id,
                                "document_id": request.document_id,
                                "version": request.version,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                expected = (
                    content_hash,
                    object_ref,
                    acl_json,
                    metadata_json,
                    chunking_version,
                )
                if version is None:
                    await session.execute(
                        text(
                            "INSERT INTO document_versions "
                            "(tenant_id, document_id, version, content_hash, object_ref, "
                            "created_at, acl_json, metadata_json, chunking_version, "
                            "embedding_profile, deleted_at) VALUES "
                            "(:tenant_id, :document_id, :version, :content_hash, :object_ref, "
                            ":created_at, :acl_json, :metadata_json, :chunking_version, "
                            ":embedding_profile, NULL)"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "document_id": request.document_id,
                            "version": request.version,
                            "content_hash": content_hash,
                            "object_ref": object_ref,
                            "created_at": now,
                            "acl_json": acl_json,
                            "metadata_json": metadata_json,
                            "chunking_version": chunking_version,
                            "embedding_profile": embedding_profile,
                        },
                    )
                else:
                    observed = tuple(
                        str(version[name])
                        for name in (
                            "content_hash",
                            "object_ref",
                            "acl_json",
                            "metadata_json",
                            "chunking_version",
                        )
                    )
                    if observed != expected:
                        raise CatalogError("document version conflicts with immutable content")
                    await session.execute(
                        text(
                            "UPDATE document_versions SET deleted_at = NULL "
                            "WHERE tenant_id = :tenant_id AND document_id = :document_id "
                            "AND version = :version"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "document_id": request.document_id,
                            "version": request.version,
                        },
                    )
                    completed = await session.scalar(
                        text(
                            "SELECT COUNT(*) FROM ingestion_runs WHERE tenant_id = :tenant_id "
                            "AND document_id = :document_id AND document_version = :version "
                            "AND status = 'completed'"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "document_id": request.document_id,
                            "version": request.version,
                        },
                    )
                    if int(completed or 0) > 0 and version["deleted_at"] is None:
                        return True

                await session.execute(
                    text(
                        "INSERT INTO ingestion_runs "
                        "(tenant_id, run_id, document_id, document_version, status, started_at, "
                        "completed_at) VALUES (:tenant_id, :run_id, :document_id, :version, "
                        "'started', :started_at, NULL)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "document_id": request.document_id,
                        "version": request.version,
                        "started_at": now,
                    },
                )
            return False
        except CatalogError:
            raise
        except (IntegrityError, SQLAlchemyError) as exc:
            raise CatalogError("knowledge catalog write failed") from exc

    async def finish_ingestion(self, tenant_id: str, run_id: str, *, success: bool) -> None:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE ingestion_runs SET status = :status, completed_at = :completed_at "
                        "WHERE tenant_id = :tenant_id AND run_id = :run_id"
                    ),
                    {
                        "status": "completed" if success else "failed",
                        "completed_at": datetime.now(UTC),
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                    },
                )
        except SQLAlchemyError as exc:
            raise CatalogError("knowledge ingestion status update failed") from exc

    async def get_active_version(
        self, tenant_id: str, document_id: str, version: str
    ) -> CatalogVersion | None:
        try:
            async with self._sessions() as session:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT d.source_uri, d.owner_ref, d.data_classification, "
                                "v.tenant_id, v.document_id, v.version, v.content_hash, "
                                "v.object_ref, v.acl_json, v.metadata_json, "
                                "v.chunking_version, v.embedding_profile "
                                "FROM document_versions v JOIN documents d ON "
                                "d.tenant_id = v.tenant_id AND d.document_id = v.document_id "
                                "WHERE v.tenant_id = :tenant_id AND v.document_id = :document_id "
                                "AND v.version = :version AND v.deleted_at IS NULL "
                                "AND d.deleted_at IS NULL "
                                "AND EXISTS (SELECT 1 FROM ingestion_runs i WHERE "
                                "i.tenant_id = v.tenant_id AND i.document_id = v.document_id "
                                "AND i.document_version = v.version AND i.status = 'completed')"
                            ),
                            {
                                "tenant_id": tenant_id,
                                "document_id": document_id,
                                "version": version,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
            return None if row is None else _catalog_version(row)
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CatalogError("knowledge catalog read failed") from exc

    async def list_active_versions(self, tenant_id: str) -> tuple[CatalogVersion, ...]:
        try:
            async with self._sessions() as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT d.source_uri, d.owner_ref, d.data_classification, "
                                "v.tenant_id, v.document_id, v.version, v.content_hash, "
                                "v.object_ref, v.acl_json, v.metadata_json, "
                                "v.chunking_version, v.embedding_profile "
                                "FROM document_versions v JOIN documents d ON "
                                "d.tenant_id = v.tenant_id AND d.document_id = v.document_id "
                                "WHERE v.tenant_id = :tenant_id AND v.deleted_at IS NULL "
                                "AND d.deleted_at IS NULL AND EXISTS "
                                "(SELECT 1 FROM ingestion_runs i WHERE i.tenant_id = v.tenant_id "
                                "AND i.document_id = v.document_id "
                                "AND i.document_version = v.version "
                                "AND i.status = 'completed') ORDER BY v.document_id, v.version"
                            ),
                            {"tenant_id": tenant_id},
                        )
                    )
                    .mappings()
                    .all()
                )
            return tuple(_catalog_version(row) for row in rows)
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CatalogError("knowledge catalog listing failed") from exc

    async def list_document_versions(
        self, tenant_id: str, document_id: str
    ) -> tuple[CatalogVersion, ...]:
        """Return every known version so interrupted deletion can be retried."""
        try:
            async with self._sessions() as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT d.source_uri, d.owner_ref, d.data_classification, "
                                "v.tenant_id, v.document_id, v.version, v.content_hash, "
                                "v.object_ref, v.acl_json, v.metadata_json, "
                                "v.chunking_version, v.embedding_profile "
                                "FROM document_versions v JOIN documents d ON "
                                "d.tenant_id = v.tenant_id AND d.document_id = v.document_id "
                                "WHERE v.tenant_id = :tenant_id AND v.document_id = :document_id "
                                "ORDER BY v.version"
                            ),
                            {"tenant_id": tenant_id, "document_id": document_id},
                        )
                    )
                    .mappings()
                    .all()
                )
            return tuple(_catalog_version(row) for row in rows)
        except (SQLAlchemyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CatalogError("knowledge catalog version listing failed") from exc

    async def mark_document_deleted(
        self, tenant_id: str, document_id: str
    ) -> tuple[CatalogVersion, ...]:
        versions = tuple(
            item
            for item in await self.list_active_versions(tenant_id)
            if item.document_id == document_id
        )
        if not versions:
            return ()
        now = datetime.now(UTC)
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE documents SET deleted_at = :deleted_at "
                        "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                    ),
                    {
                        "deleted_at": now,
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                    },
                )
                await session.execute(
                    text(
                        "UPDATE document_versions SET deleted_at = :deleted_at "
                        "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                    ),
                    {
                        "deleted_at": now,
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                    },
                )
            return versions
        except SQLAlchemyError as exc:
            raise CatalogError("knowledge catalog deletion failed") from exc

    async def get_snapshot_row(
        self, tenant_id: str, *, snapshot_id: str | None = None, snapshot_hash: str | None = None
    ) -> tuple[str, str, str] | None:
        if (snapshot_id is None) == (snapshot_hash is None):
            raise ValueError("exactly one snapshot lookup key is required")
        column = "snapshot_id" if snapshot_id is not None else "snapshot_hash"
        value = snapshot_id if snapshot_id is not None else snapshot_hash
        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        text(
                            f"SELECT snapshot_id, snapshot_hash, payload_json "
                            f"FROM rule_snapshot_cache WHERE tenant_id = :tenant_id "
                            f'AND "{column}" = :value'
                        ),
                        {"tenant_id": tenant_id, "value": value},
                    )
                ).first()
            return None if row is None else (str(row[0]), str(row[1]), str(row[2]))
        except SQLAlchemyError as exc:
            raise CatalogError("rule snapshot cache read failed") from exc

    async def insert_snapshot(
        self,
        tenant_id: str,
        snapshot_id: str,
        snapshot_hash: str,
        payload_json: str,
    ) -> None:
        try:
            async with self._sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO rule_snapshot_cache "
                        "(tenant_id, snapshot_id, snapshot_hash, payload_json, created_at) "
                        "VALUES (:tenant_id, :snapshot_id, :snapshot_hash, "
                        ":payload_json, :created_at)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "snapshot_id": snapshot_id,
                        "snapshot_hash": snapshot_hash,
                        "payload_json": payload_json,
                        "created_at": datetime.now(UTC),
                    },
                )
        except IntegrityError:
            return
        except SQLAlchemyError as exc:
            raise CatalogError("rule snapshot cache write failed") from exc


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _catalog_version(row: Any) -> CatalogVersion:
    acl_value = json.loads(str(row["acl_json"]))
    metadata_value = json.loads(str(row["metadata_json"]))
    if not isinstance(acl_value, dict) or not isinstance(metadata_value, dict):
        raise ValueError("catalog JSON payload is invalid")
    return CatalogVersion(
        tenant_id=str(row["tenant_id"]),
        document_id=str(row["document_id"]),
        version=str(row["version"]),
        source_uri=str(row["source_uri"]),
        owner_ref=str(row["owner_ref"]),
        data_classification=str(row["data_classification"]),
        content_hash=str(row["content_hash"]),
        object_ref=str(row["object_ref"]),
        acl=ACLMetadata.model_validate(acl_value),
        metadata=metadata_value,
        chunking_version=str(row["chunking_version"]),
        embedding_profile=str(row["embedding_profile"]),
    )
