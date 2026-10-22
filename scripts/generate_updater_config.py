"""Create an opt-in Tauri signed updater override from real release inputs."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def validate_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    blocked = host == "localhost" or host.endswith((".localhost", ".invalid", ".example", ".test"))
    blocked |= any(host == name or host.endswith("." + name) for name in ("example.com", "example.net", "example.org"))
    try:
        ipaddress.ip_address(host)
        blocked = True
    except ValueError:
        pass
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment or blocked:
        raise ValueError("更新 endpoint 必须是真实 HTTPS 域名，不得包含凭据、片段或占位地址")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("更新 endpoint 含空白或控制字符")
    return value


def encode_public_key(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) != 2 or not lines[0].startswith("untrusted comment: minisign public key:"):
        raise ValueError("需要 minisign 公钥文件；不能使用私钥或占位文本")
    try:
        key = base64.b64decode(lines[1], validate=True)
    except ValueError as exc:
        raise ValueError("无效的 minisign 公钥") from exc
    if len(key) != 42 or key[:2] != b"Ed" or not any(key[10:]):
        raise ValueError("无效的 minisign 公钥")
    return base64.b64encode(("\n".join(lines) + "\n").encode()).decode()


def make_config(endpoints: list[str], public_key: str, certificate: str | None = None,
                timestamp_url: str | None = None) -> dict:
    if not endpoints:
        raise ValueError("至少需要一个真实发布 endpoint")
    result = {"plugins": {"updater": {"endpoints": [validate_endpoint(value) for value in endpoints],
        "pubkey": encode_public_key(public_key), "requireSignedVersion": True, "allowDowngrades": False,
        "dangerousInsecureTransportProtocol": False, "dangerousAcceptInvalidCerts": False,
        "dangerousAcceptInvalidHostnames": False, "windows": {"installMode": "passive"}}},
        "bundle": {"createUpdaterArtifacts": True}}
    if certificate or timestamp_url:
        if not certificate or not re.fullmatch(r"[A-Fa-f0-9]{40}", certificate) or not timestamp_url:
            raise ValueError("Windows 签名需要 40 位证书指纹与时间戳 HTTPS 地址")
        result["bundle"]["windows"] = {"certificateThumbprint": certificate,
            "timestampUrl": validate_endpoint(timestamp_url), "digestAlgorithm": "sha256"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--certificate-thumbprint")
    parser.add_argument("--timestamp-url")
    parser.add_argument("--output", type=Path, default=Path("out/tauri/updater.release.json"))
    args = parser.parse_args()
    config = make_config(args.endpoint, args.public_key.read_text(encoding="utf-8"),
                         args.certificate_thumbprint, args.timestamp_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
