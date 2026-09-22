"""
The ACELO optimization package: what ACELO deploys into a customer workspace.

`optimization_package/manifest.json` declares one asset per domain. This module
loads it, resolves each asset's source file, and computes a content checksum so
provisioning can tell "already installed and current" from "needs updating".

The central rule: **an asset that is not present on disk is never provisioned.**
There is no placeholder, no generated stand-in, and no "empty notebook" path. A
missing asset surfaces as ASSET_MISSING and that domain stays uninstalled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("acelo.package")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "optimization_package"
MANIFEST_PATH = PACKAGE_ROOT / "manifest.json"


@dataclass
class PackageAsset:
    domain: str
    folder: str
    display_name: str
    source: str
    required: bool
    resource_key: str

    @property
    def path(self) -> Path:
        return PACKAGE_ROOT / self.source

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def read_bytes(self) -> bytes:
        if not self.available:
            raise FileNotFoundError(
                f"ACELO package asset for '{self.domain}' is missing: {self.source}"
            )
        return self.path.read_bytes()

    def checksum(self) -> str | None:
        """SHA-256 of the asset source. Persisted per deployed resource so a
        re-provision can detect a genuinely changed notebook."""
        if not self.available:
            return None
        return hashlib.sha256(self.read_bytes()).hexdigest()

    def payload_base64(self) -> str:
        """Fabric's item-definition API takes each part base64-encoded."""
        return base64.b64encode(self.read_bytes()).decode()


@dataclass
class OptimizationPackage:
    name: str
    version: str
    namespace: str
    assets: list[PackageAsset]

    def asset_for(self, domain: str) -> PackageAsset | None:
        return next((a for a in self.assets if a.domain == domain), None)

    @property
    def available_assets(self) -> list[PackageAsset]:
        return [a for a in self.assets if a.available]

    @property
    def missing_assets(self) -> list[PackageAsset]:
        return [a for a in self.assets if not a.available]

    @property
    def has_any_asset(self) -> bool:
        return bool(self.available_assets)

    @property
    def required_missing(self) -> list[PackageAsset]:
        return [a for a in self.assets if a.required and not a.available]


def load_package() -> OptimizationPackage:
    """Reads the manifest. Raises if the manifest itself is missing or malformed —
    that is a deployment defect in ACELO, not a customer-environment problem."""
    if not MANIFEST_PATH.is_file():
        raise FileNotFoundError(f"ACELO package manifest not found at {MANIFEST_PATH}")

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assets = [
        PackageAsset(
            domain=entry["domain"],
            folder=entry["folder"],
            display_name=entry["display_name"],
            source=entry["source"],
            required=bool(entry.get("required", False)),
            resource_key=entry["resource_key"],
        )
        for entry in data.get("assets", [])
    ]
    return OptimizationPackage(
        name=data.get("package_name", "ACELO Optimization Package"),
        version=data.get("package_version", "0.0.0"),
        namespace=data.get("namespace", "ACELO"),
        assets=assets,
    )


def package_availability() -> dict:
    """Safe summary for the API/UI: which domains can actually be deployed.

    Deliberately reports missing assets explicitly rather than hiding them, so
    the UI can say "Storage: asset missing" instead of silently offering fewer
    capabilities than the customer expects.
    """
    package = load_package()
    return {
        "package_name": package.name,
        "package_version": package.version,
        "namespace": package.namespace,
        "deployable": package.has_any_asset,
        "assets": [
            {
                "domain": a.domain,
                "display_name": a.display_name,
                "folder": a.folder,
                "required": a.required,
                "available": a.available,
                "source": a.source,
                "checksum": a.checksum(),
            }
            for a in package.assets
        ],
        "missing": [a.domain for a in package.missing_assets],
    }
