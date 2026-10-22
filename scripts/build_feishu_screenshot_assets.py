"""Prepare cropped, source-linked Feishu screenshots without redrawing message content."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs/demo/feishu"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        parser.error("FFmpeg must already be installed.")
    story = json.loads((DESTINATION / "native-story.json").read_text(encoding="utf-8"))
    output = DESTINATION / "screenshots"
    output.mkdir(exist_ok=True)
    manifest = []
    for asset in story["assets"]:
        source = args.source_dir / asset["source"]
        raw = source.read_bytes()
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Expected PNG source")
        width, height = struct.unpack(">II", raw[16:24])
        x, y, crop_width, crop_height = asset["crop"]
        if min(x, y) < 0 or crop_width <= 0 or crop_height <= 0:
            raise ValueError("Invalid crop")
        if x + crop_width > width or y + crop_height > height:
            raise ValueError(f"Crop exceeds source bounds: {asset['source']}")
        filters = []
        for mask in asset.get("masks", []):
            mx, my, mw, mh = mask
            filters.append(f"drawbox=x={mx}:y={my}:w={mw}:h={mh}:color=0xFAFBFC:t=fill")
        filters.append(f"crop={crop_width}:{crop_height}:{x}:{y}")
        destination = output / (asset["id"] + ".png")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                ",".join(filters),
                "-frames:v",
                "1",
                "-map_metadata",
                "-1",
                str(destination),
            ],
            check=True,
        )
        manifest.append(
            {
                **asset,
                "source_size": [width, height],
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "asset_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
    story["assets"] = manifest
    (DESTINATION / "native-data.js").write_text(
        "window.ORIA_SCREENSHOT_TOUR = " + json.dumps(story, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"Prepared {len(manifest)} screenshot crops and {len(story['scenes'])} scenes.")


if __name__ == "__main__":
    main()
