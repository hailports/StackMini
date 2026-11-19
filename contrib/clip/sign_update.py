#!/usr/bin/env python3
"""Write the signed self-update feed for a built paisty-update.zip.

    PAISTY_ED_PRIVATE_KEY="$(cat key.pem)" python3 contrib/clip/sign_update.py \
        --zip dist/paisty-update.zip --version 1.1.900 --build 900 \
        --url https://github.com/hailports/StackMini/releases/download/paisty-v1.1.900/paisty-update.zip \
        --out dist/paisty-update.json

The app verifies this signature with the public key in contrib/clip/update-public-key.txt,
then checks the zip's SHA-256 against the feed. The private key never enters the repo.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path


def message(build: int, version: str, sha256: str, url: str) -> bytes:
    return f"paisty-update\n{build}\n{version}\n{sha256.lower()}\n{url}".encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--build", required=True, type=int)
    ap.add_argument("--url", required=True)
    ap.add_argument("--notes", default="")
    ap.add_argument("--key-file", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = os.environ.get("PAISTY_ED_PRIVATE_KEY", "")
    if not pem and args.key_file:
        pem = Path(args.key_file).read_text(encoding="utf-8")
    if not pem.strip():
        print("no signing key", file=sys.stderr)
        return 2
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print("key is not ed25519", file=sys.stderr)
        return 2
    data = Path(args.zip).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    sig = key.sign(message(args.build, args.version, sha, args.url))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    expected = (Path(__file__).with_name("update-public-key.txt")).read_text().strip()
    if base64.b64encode(pub).decode() != expected:
        print("signing key does not match update-public-key.txt", file=sys.stderr)
        return 2
    doc = {"version": args.version, "build": args.build, "url": args.url, "sha256": sha, "size": len(data),
           "notes": args.notes, "sig": base64.b64encode(sig).decode()}
    Path(args.out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"signed {args.version} ({args.build}) sha256 {sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
