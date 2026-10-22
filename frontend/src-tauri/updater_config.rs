//! Shared by the build script and runtime; no network access occurs here.
use base64::Engine;
use serde_json::Value;

pub fn validate_https(value: &str) -> Result<url::Url, String> {
    if value.chars().any(|ch| ch.is_whitespace() || ch.is_control()) {
        return Err("更新地址含空白或控制字符".into());
    }
    let parsed = url::Url::parse(value).map_err(|_| "更新地址无效")?;
    let host = parsed.host_str().ok_or("更新地址缺少主机")?;
    let blocked = host == "localhost" || host.ends_with(".localhost")
        || host.ends_with(".invalid") || host.ends_with(".example")
        || host.ends_with(".test") || host == "example.com" || host.ends_with(".example.com")
        || host == "example.org" || host.ends_with(".example.org")
        || host == "example.net" || host.ends_with(".example.net");
    if parsed.scheme() != "https" || !parsed.username().is_empty() || parsed.password().is_some()
        || parsed.fragment().is_some() || blocked || host.parse::<std::net::IpAddr>().is_ok() {
        return Err("更新地址必须使用真实 HTTPS 域名，且不得包含凭据、片段或占位地址".into());
    }
    Ok(parsed)
}

pub fn validate_config(config: &Value) -> Result<(), String> {
    let endpoints = config["endpoints"].as_array().ok_or("未配置更新 endpoints")?;
    if endpoints.is_empty() { return Err("更新 endpoints 不能为空".into()); }
    for endpoint in endpoints {
        validate_https(endpoint.as_str().ok_or("更新 endpoint 必须是字符串")?)?;
    }
    if config["requireSignedVersion"] != true || config["allowDowngrades"] == true || config["allow-downgrades"] == true
        || ["dangerousInsecureTransportProtocol", "dangerousAcceptInvalidCerts", "dangerousAcceptInvalidHostnames"]
            .iter().any(|key| config[*key] == true)
        || ["dangerous-insecure-transport-protocol", "dangerous-accept-invalid-certs", "dangerous-accept-invalid-hostnames"]
            .iter().any(|key| config[*key] == true) {
        return Err("必须验证签名版本并禁止降级或不安全 TLS".into());
    }
    let encoded = config["pubkey"].as_str().ok_or("未配置更新签名公钥")?;
    let decoded = base64::engine::general_purpose::STANDARD.decode(encoded)
        .map_err(|_| "更新公钥必须是 base64 minisign 公钥")?;
    let text = std::str::from_utf8(&decoded).map_err(|_| "更新公钥文本无效")?;
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() != 2 || !lines[0].starts_with("untrusted comment: minisign public key:") {
        return Err("更新公钥格式无效；不得提供私钥或占位文本".into());
    }
    let key = base64::engine::general_purpose::STANDARD.decode(lines[1])
        .map_err(|_| "更新公钥内容无效")?;
    if key.len() != 42 || &key[..2] != b"Ed" || key[10..].iter().all(|value| *value == 0) {
        return Err("更新公钥内容无效".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn valid() -> Value {
        serde_json::json!({"endpoints":["https://github.com/tauri-apps/plugins-workspace"],
            "pubkey":base64::engine::general_purpose::STANDARD.encode(
                "untrusted comment: minisign public key: A49EF4746FA6E6D7\nRWTX5qZvdPSepDGWg2bz8n6ArQnBMwgQK1geVosorcbLzcunD+KJ+cYe\n"),
            "requireSignedVersion":true})
    }
    #[test]
    fn release_policy_rejects_tls_aliases_downgrades_and_whitespace() {
        assert!(validate_config(&valid()).is_ok());
        for key in ["allowDowngrades", "allow-downgrades", "dangerousAcceptInvalidCerts",
            "dangerous-accept-invalid-certs", "dangerousAcceptInvalidHostnames",
            "dangerous-accept-invalid-hostnames", "dangerousInsecureTransportProtocol",
            "dangerous-insecure-transport-protocol"] {
            let mut config = valid(); config[key] = true.into();
            assert!(validate_config(&config).is_err(), "{key}");
        }
        for url in ["https://github.com/\nupdate", "https://github.com/update ", "https://127.0.0.1/update",
            "https://example.com/update", "http://github.com/update", "https://a:b@github.com/update"] {
            assert!(validate_https(url).is_err(), "{url}");
        }
    }
    #[test]
    fn release_policy_requires_signed_version_and_nonzero_minisign_key() {
        let mut missing_version = valid(); missing_version["requireSignedVersion"] = false.into();
        assert!(validate_config(&missing_version).is_err());
        let mut placeholder = valid(); placeholder["pubkey"] = "YOUR_PUBLIC_KEY".into();
        assert!(validate_config(&placeholder).is_err());
        let mut empty_key = vec![0_u8; 42]; empty_key[..2].copy_from_slice(b"Ed");
        placeholder["pubkey"] = base64::engine::general_purpose::STANDARD.encode(format!(
            "untrusted comment: minisign public key: empty\n{}\n", base64::engine::general_purpose::STANDARD.encode(empty_key))).into();
        assert!(validate_config(&placeholder).is_err());
    }
}
