"""Make Ed25519 keys or sign a bounded PlotPilot declarative plugin package."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from infrastructure.ai.signed_plugins import SignedPluginPackage


def local_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    keygen = sub.add_parser('keygen', help='Create new keys; existing files are never overwritten')
    keygen.add_argument('--directory', default='analysis/plugin-signing')
    sign = sub.add_parser('sign', help='Validate and sign manifest + payload JSON')
    sign.add_argument('--input', required=True)
    sign.add_argument('--private-key', required=True)
    sign.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'keygen':
        directory = local_path(args.directory)
        directory.mkdir(parents=True, exist_ok=True)
        private_path, public_path = directory / 'publisher.private.key', directory / 'publisher.public.txt'
        if private_path.exists() or public_path.exists():
            parser.error('Key files already exist; choose a different directory')
        private = Ed25519PrivateKey.generate()
        secret = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        with private_path.open('x', encoding='ascii') as stream:
            stream.write(base64.b64encode(secret).decode() + '\n')
        with public_path.open('x', encoding='ascii') as stream:
            stream.write(base64.b64encode(public).decode() + '\n')
        print(f'Public key: {public_path}\nPrivate key: {private_path}\nFingerprint: {hashlib.sha256(public).hexdigest()}')
        return
    data = json.loads(local_path(args.input).read_text(encoding='utf-8-sig'))
    if set(data) - {'manifest', 'payload', 'signature'}:
        parser.error('Only manifest and payload are allowed')
    data['signature'] = 'A' * 88
    package = SignedPluginPackage.model_validate(data)
    required = set()
    if package.payload.prompts:
        required.add('prompts')
    if package.payload.quality_rules:
        required.add('quality_rules')
    if set(package.manifest.permissions) != required or not required:
        parser.error('Permissions must exactly match package content')
    body = package.signing_bytes()
    if len(body) > 2_000_000:
        parser.error('Package exceeds 2MB')
    secret = base64.b64decode(local_path(args.private_key).read_text(encoding='ascii').strip(), validate=True)
    package.signature = base64.b64encode(Ed25519PrivateKey.from_private_bytes(secret).sign(body)).decode()
    output = local_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(package.model_dump(), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f'Signed package: {output}\nContent hash: {hashlib.sha256(body).hexdigest()}')


if __name__ == '__main__':
    main()
