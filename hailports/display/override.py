"""Build and read a display scale-resolutions override.

Each logical WxH contributes three records, in this order:

* 1x points, flag 1, tail 0x200000
* 2x framebuffer, flag 9, tail 0x200000
* a bare 2x width and height pair

Those flags are the ones macOS already accepts for a HiDPI override.
"""

from __future__ import annotations

import plistlib
import struct

FLAG_BASE = 1
FLAG_2X = 9
TAIL = 0x200000


def entries(width: int, height: int) -> list[bytes]:
    if width < 1 or height < 1:
        raise ValueError("mode must be a positive WxH")
    return [
        struct.pack(">IIII", width, height, FLAG_BASE, TAIL),
        struct.pack(">IIII", width * 2, height * 2, FLAG_2X, TAIL),
        struct.pack(">II", width * 2, height * 2),
    ]


def parse_mode(text: str) -> tuple[int, int]:
    piece = text.lower().replace(" ", "").replace("*", "x")
    if "x" not in piece:
        raise ValueError("mode must look like 1440x900")
    width, height = piece.split("x", 1)
    return int(width, 10), int(height, 10)


def build_plist(vendor: int, product: int, modes: list[tuple[int, int]], name: str = "HiDPI override") -> bytes:
    if not modes:
        raise ValueError("at least one WxH mode is required")
    blob: list[bytes] = []
    for width, height in modes:
        blob.extend(entries(width, height))
    payload = {
        "DisplayVendorID": int(vendor),
        "DisplayProductID": int(product),
        "DisplayProductName": name,
        "scale-resolutions": blob,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def parse_plist(blob: bytes) -> dict:
    payload = plistlib.loads(blob)
    logical: list[tuple[int, int]] = []
    hidpi: list[tuple[int, int]] = []
    pairs: list[tuple[int, int]] = []
    for item in payload.get("scale-resolutions") or []:
        raw = bytes(item)
        if len(raw) == 16:
            width, height, flag, tail = struct.unpack(">IIII", raw)
            if flag == FLAG_BASE and tail == TAIL:
                logical.append((width, height))
            elif flag == FLAG_2X and tail == TAIL:
                hidpi.append((width, height))
        elif len(raw) == 8:
            pairs.append(struct.unpack(">II", raw))
    return {
        "vendor": int(payload["DisplayVendorID"]),
        "product": int(payload["DisplayProductID"]),
        "name": payload.get("DisplayProductName") or "",
        "modes": logical,
        "hidpi": hidpi,
        "pairs": pairs,
    }


def install_hint(vendor: int, product: int, plist_path: str) -> str:
    vendor_hex = format(int(vendor), "x")
    product_hex = format(int(product), "x")
    folder = f"DisplayVendorID-{vendor_hex}"
    filename = f"DisplayProductID-{product_hex}"
    return (
        "The override file is ready. Installing it needs an administrator once.\n"
        f"  sudo mkdir -p /Library/Displays/Contents/Resources/Overrides/{folder}\n"
        f"  sudo cp {plist_path} /Library/Displays/Contents/Resources/Overrides/{folder}/{filename}\n"
        f"  sudo chown root:wheel /Library/Displays/Contents/Resources/Overrides/{folder}/{filename}\n"
        "Then log out, or unplug and reconnect the display, so the new modes are advertised.\n"
        "Do not kill the window server to pick this up."
    )
