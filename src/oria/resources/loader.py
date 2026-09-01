"""Fail-closed readers for versioned resources bundled in the installed package."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

from oria.domain.models import CampaignRuleSet, MerchantSeedSet

_DEMO_MANIFEST_SHA256 = "6bd1a51f62fb7244f1cf1584d3e139c21425c826be9268cefdcb2e64190539fc"
_MIGRATION_MANIFEST_SHA256 = "eb9c456a19eb2bcdc1a2d2e07ffa5834b44f84849ac61ffc44bb3d9b869f475f"
RULE_CATEGORIES = (
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
)


class PackageAssetError(RuntimeError):
    """Raised when a required installed resource is missing, malformed, or modified."""


@dataclass(frozen=True, slots=True)
class VerifiedAssetManifest:
    dataset_id: str
    version: str
    files: tuple[str, ...]
    rule_categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DemoDataBundle:
    manifest: VerifiedAssetManifest
    rules: CampaignRuleSet
    merchants: MerchantSeedSet


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Traversable, label: str) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        raise PackageAssetError(f"required package asset is unavailable: {label}") from exc


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageAssetError(f"package asset is not valid JSON: {label}") from exc
    if not isinstance(value, dict):
        raise PackageAssetError(f"package asset must contain a JSON object: {label}")
    return value


def _verified_manifest(
    root: Traversable,
    *,
    expected_manifest_hash: str,
    label: str,
) -> dict[str, Any]:
    data = _read_bytes(root.joinpath("manifest.json"), f"{label}/manifest.json")
    if _sha256(data) != expected_manifest_hash:
        raise PackageAssetError(f"package asset manifest integrity check failed: {label}")
    return _json_object(data, f"{label}/manifest.json")


def _verify_files(root: Traversable, files: object, label: str) -> tuple[str, ...]:
    if not isinstance(files, dict) or not files:
        raise PackageAssetError(f"package asset manifest has no files: {label}")
    verified: list[str] = []
    for name, expected_hash in sorted(files.items()):
        if not isinstance(name, str) or not isinstance(expected_hash, str):
            raise PackageAssetError(f"package asset manifest has invalid file metadata: {label}")
        actual = _sha256(_read_bytes(root.joinpath(name), f"{label}/{name}"))
        if actual != expected_hash:
            raise PackageAssetError(f"package asset integrity check failed: {label}/{name}")
        verified.append(name)
    return tuple(verified)


def _migration_tree_files(root: Traversable, target: str) -> frozenset[str]:
    chain_root = root.joinpath(target)
    versions_root = chain_root.joinpath("versions")
    try:
        files = {f"{target}/env.py"} if chain_root.joinpath("env.py").is_file() else set()
        files.update(
            f"{target}/versions/{path.name}"
            for path in versions_root.iterdir()
            if path.is_file() and path.name.endswith(".py") and path.name != "__init__.py"
        )
    except OSError as exc:
        raise PackageAssetError(
            f"required package asset is unavailable: migrations/{target}"
        ) from exc
    return frozenset(files)


def _verify_demo_tree(root: Traversable) -> VerifiedAssetManifest:
    manifest = _verified_manifest(
        root,
        expected_manifest_hash=_DEMO_MANIFEST_SHA256,
        label="demo_data",
    )
    categories = manifest.get("rule_categories")
    if categories != list(RULE_CATEGORIES):
        raise PackageAssetError(
            "demo data manifest does not declare the six required rule categories"
        )
    if manifest.get("source") != "synthetic" or manifest.get("contains_real_entities") is not False:
        raise PackageAssetError("demo data manifest is not declared synthetic and de-identified")
    dataset_id = manifest.get("dataset_id")
    version = manifest.get("version")
    if (
        not isinstance(dataset_id, str)
        or not dataset_id
        or not isinstance(version, str)
        or not version
    ):
        raise PackageAssetError("demo data manifest identity is invalid")
    files = _verify_files(root, manifest.get("files"), "demo_data")
    required = {"campaign_rules.v1.json", "merchants.v1.json"}
    if set(files) != required:
        raise PackageAssetError("demo data manifest does not contain the exact required resources")
    return VerifiedAssetManifest(dataset_id, version, files, RULE_CATEGORIES)


def _verify_migration_tree(root: Traversable) -> dict[str, str]:
    manifest = _verified_manifest(
        root,
        expected_manifest_hash=_MIGRATION_MANIFEST_SHA256,
        label="migrations",
    )
    chains = manifest.get("chains")
    if not isinstance(chains, dict) or set(chains) != {"platform", "business"}:
        raise PackageAssetError("migration manifest must contain exactly two independent chains")
    heads: dict[str, str] = {}
    for target in ("platform", "business"):
        chain = chains.get(target)
        if not isinstance(chain, dict):
            raise PackageAssetError(f"migration chain metadata is invalid: {target}")
        head = chain.get("head")
        if not isinstance(head, str) or not head.startswith(f"{target}_"):
            raise PackageAssetError(f"migration chain head has the wrong namespace: {target}")
        files = _verify_files(root, chain.get("files"), f"migrations/{target}")
        if frozenset(files) != _migration_tree_files(root, target):
            raise PackageAssetError(
                f"migration manifest does not match the exact installed tree: {target}"
            )
        heads[target] = head
    return heads


def verify_migration_assets() -> dict[str, str]:
    """Verify both installed migration chains and return their declared heads."""
    return _verify_migration_tree(resources.files("oria.migrations"))


def load_demo_data() -> DemoDataBundle:
    """Load verified synthetic rules and merchants from package resources."""
    root = resources.files("oria.resources.demo_data")
    manifest = _verify_demo_tree(root)
    rules_data = _json_object(
        _read_bytes(root.joinpath("campaign_rules.v1.json"), "demo_data/campaign_rules.v1.json"),
        "demo_data/campaign_rules.v1.json",
    )
    merchants_data = _json_object(
        _read_bytes(root.joinpath("merchants.v1.json"), "demo_data/merchants.v1.json"),
        "demo_data/merchants.v1.json",
    )
    try:
        rules = CampaignRuleSet.model_validate(rules_data)
        merchants = MerchantSeedSet.model_validate(merchants_data)
    except ValueError as exc:
        raise PackageAssetError("verified demo data does not satisfy the domain schema") from exc
    if rules.tenant_id != merchants.tenant_id or rules.version != merchants.version:
        raise PackageAssetError("demo rule and merchant resource versions are inconsistent")
    return DemoDataBundle(manifest, rules, merchants)


def verify_package_assets() -> tuple[VerifiedAssetManifest, dict[str, str]]:
    """Verify every T03 resource from its installed-package location."""
    demo_root = resources.files("oria.resources.demo_data")
    return _verify_demo_tree(demo_root), verify_migration_assets()
