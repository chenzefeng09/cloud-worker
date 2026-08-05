#!/usr/bin/env python3
"""Resolve MATLAB installer payload archives from product metadata."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("common_metadata_zip", type=Path)
    parser.add_argument("glnxa64_metadata_zip", type=Path)
    parser.add_argument("products", nargs="+")
    return parser.parse_args()


def product_metadata_name(
    archive: zipfile.ZipFile, platform: str, product: str
) -> str:
    pattern = re.compile(
        rf"^{re.escape(platform)}/productdata_{re.escape(product)}"
        rf"\d+_{re.escape(platform)}\.xml$"
    )
    matches = [name for name in archive.namelist() if pattern.match(name)]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {platform} product metadata file for {product}, "
            f"found {matches}"
        )
    return matches[0]


def component_archives(
    metadata_zip: Path, platform: str, products: list[str]
) -> set[str]:
    payloads: set[str] = set()
    with zipfile.ZipFile(metadata_zip) as archive:
        for product in products:
            metadata_name = product_metadata_name(archive, platform, product)
            root = ET.fromstring(archive.read(metadata_name))
            for component in root.findall("./dependsOn/component"):
                supported_platforms = {
                    (item.text or "").strip()
                    for item in component.findall("./platformList/platform")
                }
                # The R2019b installer reads locale-specific components even
                # during an English silent install, so retain every locale for
                # components supported on Linux.
                if "glnxa64" not in supported_platforms:
                    continue
                name = (component.findtext("name") or "").strip()
                version = (component.findtext("version") or "").strip()
                if not name or not version:
                    raise RuntimeError(
                        f"incomplete component in {metadata_name}: "
                        f"name={name!r} version={version!r}"
                    )
                payloads.add(f"archives/{platform}/{name}_{version}.enc")
    return payloads


def main() -> int:
    args = parse_args()
    payloads = component_archives(
        args.common_metadata_zip, "common", args.products
    )
    payloads.update(
        component_archives(
            args.glnxa64_metadata_zip, "glnxa64", args.products
        )
    )
    for payload in sorted(payloads):
        print(payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(f"failed to resolve MATLAB archives: {exc}", file=sys.stderr)
        raise SystemExit(1)
