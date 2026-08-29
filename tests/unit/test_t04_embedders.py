"""Deterministic Fixture and pinned BGE embedder unit contracts."""

from __future__ import annotations

import math

import pytest

from oria.providers.embeddings import BGEEmbedder, FixtureEmbedder

pytestmark = pytest.mark.unit


class _FakeSentenceTransformer:
    def get_embedding_dimension(self) -> int:
        return 2

    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> list[list[float]]:
        assert normalize_embeddings is True
        assert convert_to_numpy is True
        assert show_progress_bar is False
        return [[0.6, 0.8] for _ in texts]


@pytest.mark.asyncio
async def test_fixture_embedder_is_deterministic_normalized_and_finite() -> None:
    embedder = FixtureEmbedder(dim=16)

    first = await embedder.embed(["招商", "规则"], object())
    second = await embedder.embed(["招商", "规则"], object())

    assert first == second
    assert len(first) == 2
    assert all(len(vector) == 16 for vector in first)
    assert all(math.isclose(sum(value * value for value in vector), 1.0) for vector in first)
    assert all(math.isfinite(value) for vector in first for value in vector)


@pytest.mark.asyncio
async def test_bge_embedder_pins_revision_disables_remote_code_and_checks_shape() -> None:
    calls: list[tuple[str, str, bool]] = []

    def factory(model: str, *, revision: str, trust_remote_code: bool):
        calls.append((model, revision, trust_remote_code))
        return _FakeSentenceTransformer()

    embedder = BGEEmbedder(
        model="BAAI/bge-small-zh-v1.5",
        revision="e534609e6b53ac54bd42d8e87995d21a73b90bad",
        trust_remote_code=False,
        model_factory=factory,
    )

    assert embedder.dim == 2
    assert await embedder.embed(["规则"], object()) == [[0.6, 0.8]]
    assert calls == [
        (
            "BAAI/bge-small-zh-v1.5",
            "e534609e6b53ac54bd42d8e87995d21a73b90bad",
            False,
        )
    ]


@pytest.mark.parametrize(
    ("revision", "trust_remote_code"),
    [(None, False), ("revision", True)],
)
def test_bge_embedder_rejects_unpinned_or_remote_code_models(
    revision: str | None,
    trust_remote_code: bool,
) -> None:
    with pytest.raises(ValueError):
        BGEEmbedder(
            model="BAAI/bge-small-zh-v1.5",
            revision=revision,
            trust_remote_code=trust_remote_code,
            model_factory=lambda *_args, **_kwargs: _FakeSentenceTransformer(),
        )


@pytest.mark.asyncio
async def test_bge_embedder_rejects_non_normalized_model_output() -> None:
    class NonNormalizedModel(_FakeSentenceTransformer):
        def encode(
            self,
            texts: list[str],
            *,
            normalize_embeddings: bool,
            convert_to_numpy: bool,
            show_progress_bar: bool,
        ) -> list[list[float]]:
            return [[3.0, 4.0] for _ in texts]

    embedder = BGEEmbedder(
        model="BAAI/bge-small-zh-v1.5",
        revision="e534609e6b53ac54bd42d8e87995d21a73b90bad",
        trust_remote_code=False,
        model_factory=lambda *_args, **_kwargs: NonNormalizedModel(),
    )

    with pytest.raises(ValueError, match="normalized"):
        await embedder.embed(["规则"], object())
