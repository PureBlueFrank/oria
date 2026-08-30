"""Verify packaged V0.2-T05 eval assets without running reviewed-only gates."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import oria
from oria.eval import (
    load_nightly_config,
    load_rag_baseline,
    load_rag_dataset,
    load_rag_eval_config,
    load_rag_gates,
)


def main() -> None:
    package_file = Path(oria.__file__).resolve()
    if "site-packages" not in package_file.parts:
        raise AssertionError("Oria was not imported from an installed wheel environment")
    root = resources.files("oria").joinpath("eval_assets")
    manifest = Path(str(root.joinpath("datasets", "rag", "v1.manifest.json")))
    rag_config = Path(str(root.joinpath("config", "rag.yaml")))
    gates_path = Path(str(root.joinpath("config", "rag-gates.yaml")))
    nightly_config = Path(str(root.joinpath("config", "nightly.yaml")))
    baseline_path = Path(str(root.joinpath("baselines", "rag", "1.json")))
    dataset = load_rag_dataset(manifest, require_human_review=False)
    pinned = load_rag_eval_config(rag_config)
    gates = load_rag_gates(gates_path)
    baseline = load_rag_baseline(baseline_path)
    nightly = load_nightly_config(nightly_config)
    if (
        dataset.manifest.case_count != 60
        or dataset.manifest.development_critical_case_count != 6
        or dataset.manifest.holdout_critical_case_count != 6
        or not dataset.manifest.baseline_created
        or pinned.dataset_version != "1"
        or gates.dataset_version != "1"
        or baseline.dataset_sha256 != dataset.manifest.dataset_sha256
    ):
        raise AssertionError("installed RAG eval assets are invalid")
    if tuple(target.target_id for target in nightly.targets) != ("deepseek",):
        raise AssertionError("installed nightly targets are invalid")
    print(f"verified installed V0.2-T05 eval assets from {package_file}")


if __name__ == "__main__":
    main()
