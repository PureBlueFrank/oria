"""Synthetic, label-free Scenario B history used by local tool verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from oria.core.types import ACLMetadata

if TYPE_CHECKING:
    from oria.rag.models import DocumentIngestRequest


def attribution_history_document() -> DocumentIngestRequest:
    """Return guidance that describes an investigation method, not a fixture answer."""
    from oria.rag.models import DocumentIngestRequest

    return DocumentIngestRequest(
        document_id="synthetic-attribution-history",
        version="v1",
        source_uri="synthetic://oria/attribution/history/v1",
        owner_ref="oria-synthetic-fixture",
        data_classification="internal",
        content=(
            "招商转化异常排查经验: 先比较漏斗相邻阶段, 再按区域和类目下钻; "
            "随后核对活动时间窗, 并用同期大盘判断异常是局部变化还是整体变化。"
            "证据不足或不同来源冲突时, 应保留多个假设并明确停止归因。"
        ),
        acl=ACLMetadata(allowed_roles=("operator",), classification="internal"),
        metadata={
            "document_kind": "attribution_history",
            "source": "synthetic",
            "contains_real_entities": False,
        },
    )
