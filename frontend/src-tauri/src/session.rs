//! OS-backed random capabilities and the trusted renderer boundary.

pub fn new_session_token() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    fill_random(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{:02x}", byte)).collect())
}

#[cfg(target_os = "windows")]
fn fill_random(bytes: &mut [u8]) -> Result<(), String> {
    #[link(name = "bcrypt")]
    extern "system" {
        fn BCryptGenRandom(algorithm: *mut core::ffi::c_void, buffer: *mut u8,
                           length: u32, flags: u32) -> i32;
    }
    // BCRYPT_USE_SYSTEM_PREFERRED_RNG selects Windows' cryptographic RNG.
    let result = unsafe { BCryptGenRandom(core::ptr::null_mut(), bytes.as_mut_ptr(),
                                         bytes.len() as u32, 0x00000002) };
    if result == 0 { Ok(()) } else { Err("无法生成安全的本地会话密钥".to_string()) }
}

#[cfg(not(target_os = "windows"))]
fn fill_random(bytes: &mut [u8]) -> Result<(), String> {
    use std::io::Read;
    std::fs::File::open("/dev/urandom").and_then(|mut file| file.read_exact(bytes))
        .map_err(|_| "无法生成安全的本地会话密钥".to_string())
}

pub fn renderer_allowed(label: &str, scheme: &str, host: Option<&str>,
                        port: Option<u16>, development: bool) -> bool {
    if label != "main" { return false; }
    if scheme == "tauri" && host == Some("localhost") && port.is_none() { return true; }
    if (scheme == "http" || scheme == "https") && host == Some("tauri.localhost")
        && port.is_none() { return true; }
    development && scheme == "http" && (host == Some("localhost") || host == Some("127.0.0.1"))
        && port == Some(3000)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn session_tokens_are_random_and_256_bits() {
        let first = new_session_token().unwrap();
        let second = new_session_token().unwrap();
        assert_eq!(first.len(), 64);
        assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }
    #[test]
    fn only_the_main_local_renderer_can_receive_a_capability() {
        assert!(renderer_allowed("main", "tauri", Some("localhost"), None, false));
        assert!(renderer_allowed("main", "http", Some("tauri.localhost"), None, false));
        assert!(renderer_allowed("main", "http", Some("127.0.0.1"), Some(3000), true));
        assert!(!renderer_allowed("other", "tauri", Some("localhost"), None, false));
        assert!(!renderer_allowed("main", "http", Some("127.0.0.1"), Some(3000), false));
        assert!(!renderer_allowed("main", "https", Some("evil.example"), None, true));
        assert!(!renderer_allowed("main", "http", Some("tauri.localhost.evil.example"), None, true));
    }
}
