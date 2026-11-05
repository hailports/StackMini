"""Image prep for the vision model. Pillow, plus pillow-heif for HEIC. No sips."""

from __future__ import annotations

import io
from pathlib import Path


def to_png_bytes(path: Path) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Install pillow to attach images") from exc
    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif"}:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError as exc:
            raise RuntimeError(
                "HEIC images need the optional pillow-heif extra. "
                'Install it with: python -m pip install "hailports-appliance[heif]"'
            ) from exc
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        return buf.getvalue()
