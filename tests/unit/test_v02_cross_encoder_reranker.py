"""Pinned local cross-encoder reranker contracts."""

import pytest

from oria.core.types import ACLMetadata, Doc
from oria.rag.rerank import CrossEncoderReranker

pytestmark = pytest.mark.unit


class _Scores:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return self._values


class _FakeCrossEncoder:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.pairs: list[tuple[str, str]] = []

    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> object:
        assert convert_to_numpy is True
        assert show_progress_bar is False
        self.pairs = sentences
        return _Scores(self.scores)


def _doc(doc_id: str, content: str, score: float) -> Doc:
    return Doc(
        id=doc_id,
        version="1",
        tenant_id="local-community",
        content=content,
        metadata={"document_id": doc_id},
        score=score,
        source_uri="fixture://rag",
        acl=ACLMetadata(),
        trust_level="untrusted_data",
        provenance="fixture://rag",
        data_classification="internal",
    )


@pytest.mark.asyncio
async def test_cross_encoder_uses_pinned_safe_model_and_ranks_scores() -> None:
    fake = _FakeCrossEncoder([0.1, 0.9])
    factory_calls: list[tuple[str, str, bool]] = []

    def factory(model: str, *, revision: str, trust_remote_code: bool) -> _FakeCrossEncoder:
        factory_calls.append((model, revision, trust_remote_code))
        return fake

    reranker = CrossEncoderReranker(
        model="fixture/reranker",
        revision="a" * 40,
        trust_remote_code=False,
        model_factory=factory,
    )
    docs = [_doc("first", "基础规则", 0.8), _doc("second", "优惠规则", 0.2)]

    ranked = await reranker.rerank("优惠", docs, object())

    assert factory_calls == [("fixture/reranker", "a" * 40, False)]
    assert fake.pairs == [("优惠", "基础规则"), ("优惠", "优惠规则")]
    assert [doc.id for doc in ranked] == ["second", "first"]
    assert [doc.score for doc in ranked] == [0.9, 0.1]


@pytest.mark.parametrize(
    ("revision", "trust_remote_code"),
    [(None, False), ("a" * 40, True)],
)
def test_cross_encoder_rejects_unpinned_or_remote_code(
    revision: str | None,
    trust_remote_code: bool,
) -> None:
    with pytest.raises(ValueError):
        CrossEncoderReranker(
            model="fixture/reranker",
            revision=revision,
            trust_remote_code=trust_remote_code,
            model_factory=lambda *args, **kwargs: _FakeCrossEncoder([]),
        )
