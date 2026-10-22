//! Explicit author actions only. Official plugin IPC permissions are not exposed.
use tauri::{State, WebviewWindow};
#[cfg(feature = "signed-updater")]
use tauri::Manager;

#[cfg(feature = "signed-updater")]
#[path = "../updater_config.rs"]
mod config;

#[derive(Clone, serde::Serialize)]
pub struct UpdateStatus {
    available: bool,
    stage: String,
    version: Option<String>,
    notes: Option<String>,
    message: String,
}

impl UpdateStatus {
    fn new(available: bool, stage: &str, message: &str) -> Self {
        Self { available, stage: stage.into(), version: None, notes: None, message: message.into() }
    }
}

#[derive(Default)]
pub struct UpdateState {
    #[cfg(feature = "signed-updater")]
    inner: tokio::sync::Mutex<PendingUpdate>,
}

/// Stable 2.13.3 verifies download(), but install() itself does not. Reuse
/// the same open-source verifier before shutdown and installation.
#[cfg(feature = "signed-updater")]
fn verify_package(bytes: &[u8], signature: &str, pubkey: &str, version: &str) -> Result<(), String> {
    use base64::Engine;
    fn decode(value: &str) -> Result<String, String> {
        let bytes = base64::engine::general_purpose::STANDARD.decode(value).map_err(|e| e.to_string())?;
        String::from_utf8(bytes).map_err(|e| e.to_string())
    }
    let key = minisign_verify::PublicKey::decode(&decode(pubkey)?).map_err(|e| e.to_string())?;
    let signature = minisign_verify::Signature::decode(&decode(signature)?).map_err(|e| e.to_string())?;
    key.verify(bytes, &signature, true).map_err(|e| e.to_string())?;
    let signed = signature.trusted_comment().split('\t').find_map(|field| field.strip_prefix("version:"))
        .ok_or("更新签名缺少签名版本")?;
    let signed = semver::Version::parse(signed.trim_start_matches('v')).map_err(|e| e.to_string())?;
    let announced = semver::Version::parse(version.trim_start_matches('v')).map_err(|e| e.to_string())?;
    if signed != announced { return Err("更新包签名版本与发布版本不一致".into()); }
    Ok(())
}

#[cfg(feature = "signed-updater")]
#[derive(Default)]
struct PendingUpdate {
    update: Option<tauri_plugin_updater::Update>,
    bytes: Option<Vec<u8>>,
}

fn authorize(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|e| e.to_string())?;
    if crate::session::renderer_allowed(window.label(), url.scheme(), url.host_str(), url.port(), cfg!(debug_assertions)) {
        Ok(())
    } else { Err("该页面不能操作软件更新".into()) }
}

#[cfg(feature = "signed-updater")]
fn status(pending: &PendingUpdate) -> UpdateStatus {
    let mut status = UpdateStatus::new(true, "idle", "点击检查更新时才会连接发布服务器");
    if let Some(update) = &pending.update {
        status.stage = if pending.bytes.is_some() { "verified" } else { "available" }.into();
        status.version = Some(update.version.clone());
        status.notes = update.body.clone();
        status.message = if pending.bytes.is_some() { "更新包已通过签名与版本校验，可以确认安装" }
            else { "发现新版本，请先下载并验证签名" }.into();
    }
    status
}

#[tauri::command]
pub async fn updater_status(window: WebviewWindow, state: State<'_, UpdateState>) -> Result<UpdateStatus, String> {
    authorize(&window)?;
    #[cfg(feature = "signed-updater")]
    { Ok(status(&*state.inner.lock().await)) }
    #[cfg(not(feature = "signed-updater"))]
    { let _ = state; Ok(UpdateStatus::new(false, "unavailable", "此构建未启用签名更新。请使用发布方提供的安装包。")) }
}

#[tauri::command]
pub async fn updater_check(window: WebviewWindow, app: tauri::AppHandle, state: State<'_, UpdateState>) -> Result<UpdateStatus, String> {
    authorize(&window)?;
    #[cfg(feature = "signed-updater")]
    {
        use tauri_plugin_updater::UpdaterExt;
        config::validate_config(&app.config().plugins.0["updater"])?;
        let mut pending = state.inner.lock().await;
        // A failed or repeated check cannot leave an old install candidate.
        pending.update = None;
        pending.bytes = None;
        let update = app.updater_builder().timeout(std::time::Duration::from_secs(30))
            .configure_client(|client| client.redirect(reqwest::redirect::Policy::none()))
            .build().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())?;
        if let Some(update) = &update { config::validate_https(update.download_url.as_str())?; }
        pending.update = update;
        let mut result = status(&pending);
        if pending.update.is_none() { result.message = "当前已是最新版本".into(); }
        Ok(result)
    }
    #[cfg(not(feature = "signed-updater"))]
    { let _ = (app, state); Err("此构建未启用签名更新".into()) }
}

#[tauri::command]
pub async fn updater_download(window: WebviewWindow, app: tauri::AppHandle, state: State<'_, UpdateState>) -> Result<UpdateStatus, String> {
    authorize(&window)?;
    #[cfg(feature = "signed-updater")]
    {
        let mut pending = state.inner.lock().await;
        pending.bytes = None;
        let update = pending.update.as_ref().ok_or("请先检查更新")?;
        config::validate_https(update.download_url.as_str())?;
        // download() verifies the package; no backend shutdown happens on failure.
        let bytes = update.download(|_, _| {}, || {}).await.map_err(|e| format!("下载或签名校验失败: {}", e))?;
        verify_package(&bytes, &update.signature, app.config().plugins.0["updater"]["pubkey"].as_str().ok_or("未配置更新公钥")?, &update.version)
            .map_err(|e| format!("签名校验失败: {}", e))?;
        pending.bytes = Some(bytes);
        Ok(status(&pending))
    }
    #[cfg(not(feature = "signed-updater"))]
    { let _ = (app, state); Err("此构建未启用签名更新".into()) }
}

#[tauri::command]
pub async fn updater_install(window: WebviewWindow, app: tauri::AppHandle,
    state: State<'_, UpdateState>, author_confirmed: bool) -> Result<(), String> {
    authorize(&window)?;
    if !author_confirmed { return Err("请先确认保存更改并同意停止后台、安装更新".into()); }
    #[cfg(feature = "signed-updater")]
    {
        let pending = state.inner.lock().await;
        let update = pending.update.as_ref().ok_or("没有可安装的更新")?;
        let bytes = pending.bytes.as_ref().ok_or("更新包尚未通过签名校验")?;
        verify_package(bytes, &update.signature, app.config().plugins.0["updater"]["pubkey"].as_str().ok_or("未配置更新公钥")?, &update.version)
            .map_err(|e| format!("安装前签名校验失败: {}", e))?;
        let shutdown_app = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let manager = shutdown_app.state::<std::sync::Mutex<crate::backend::BackendManager>>();
            let manager = manager.lock().map_err(|e| e.to_string())?;
            manager.shutdown_for_update(std::time::Duration::from_secs(30))
        }).await.map_err(|e| e.to_string())??;
        // Only retained, freshly verified bytes reach the official installer.
        update.install(bytes).map_err(|e| format!("安装未完成，请重启软件后重试: {}", e))
    }
    #[cfg(not(feature = "signed-updater"))]
    { let _ = (app, state); Err("此构建未启用签名更新".into()) }
}

#[cfg(all(test, feature = "signed-updater"))]
mod tests {
    use super::{config, verify_package};
    use base64::Engine;
    use tauri_plugin_updater::UpdaterExt;

    const PUBLIC: &str = "untrusted comment: minisign public key: A49EF4746FA6E6D7\nRWTX5qZvdPSepDGWg2bz8n6ArQnBMwgQK1geVosorcbLzcunD+KJ+cYe\n";
    const SIGNATURE: &str = "untrusted comment: signature from tauri secret key\nRUTX5qZvdPSepPF2zsjvP/L/TQtBLRnWRvaq+4omK1UEugtQp/USrGiI1v762sDy3id3aor7bRC9ETyeot6lMHHrWoZ5hjUM1Qc=\ntrusted comment: timestamp:1791649051\tfile:payload\tversion:2.0.0\nDDq2CLOEl5cQmC2eJRHlnZPVViOZsu1CjpPKQoei4A/XBfkDqGO0B+QMrcrHgEQ5qV3mne75tdFWhJpqUXh9AA==\n";

    fn fixture() -> tauri::App<tauri::test::MockRuntime> {
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        // Only the mock test context permits localhost transport. Application
        // build/runtime validation always rejects these options and addresses.
        context.config_mut().plugins.0.insert("updater".into(), serde_json::json!({
            "pubkey": base64::engine::general_purpose::STANDARD.encode(PUBLIC),
            "endpoints": ["http://127.0.0.1:1/update"],
            "requireSignedVersion": true, "dangerousInsecureTransportProtocol": true,
        }));
        tauri::test::mock_builder().plugin(tauri_plugin_updater::Builder::new().build()).build(context).unwrap()
    }

    fn release(url: String, version: &str) -> serde_json::Value {
        serde_json::json!({"version": version, "url": url,
            "signature": base64::engine::general_purpose::STANDARD.encode(SIGNATURE)})
    }

    #[test]
    fn production_configuration_rejects_insecure_downloads_and_tls_bypass() {
        for address in ["http://github.com/a", "https://127.0.0.1/a", "https://example.com/a", "https://u:p@github.com/a"] {
            assert!(config::validate_https(address).is_err());
        }
        let mut valid = serde_json::json!({"endpoints": ["https://github.com/tauri-apps/plugins-workspace"],
            "pubkey": base64::engine::general_purpose::STANDARD.encode(PUBLIC), "requireSignedVersion":true});
        assert!(config::validate_config(&valid).is_ok());
        valid["dangerousAcceptInvalidCerts"] = true.into();
        assert!(config::validate_config(&valid).is_err());
    }

    #[test]
    fn package_install_guard_rejects_tampered_bytes_and_version() {
        let key = base64::engine::general_purpose::STANDARD.encode(PUBLIC);
        let signature = base64::engine::general_purpose::STANDARD.encode(SIGNATURE);
        assert!(verify_package(b"test", &signature, &key, "2.0.0").is_ok());
        assert!(verify_package(b"tampered", &signature, &key, "2.0.0").is_err());
        assert!(verify_package(b"test", &signature, &key, "2.0.1").is_err());
    }

    #[tokio::test]
    async fn official_download_verifies_localhost_package_and_signed_version() {
        use std::io::{Read, Write};
        for (bytes, version, expected) in [(b"test".as_slice(), "2.0.0", true),
            (b"tampered".as_slice(), "2.0.0", false), (b"test".as_slice(), "2.0.1", false)] {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let manifest = serde_json::to_vec(&release(format!("http://{}/package", address), version)).unwrap();
            let bytes = bytes.to_vec();
            let server = std::thread::spawn(move || {
                for body in [manifest, bytes] {
                    let (mut stream, _) = listener.accept().unwrap();
                    let mut request = [0u8; 4096];
                    let _ = stream.read(&mut request).unwrap();
                    write!(stream, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
                    stream.write_all(&body).unwrap();
                }
            });
            let app = fixture();
            let updater = app.updater_builder().version_comparator(|_, _| true)
                .endpoints(vec![format!("http://{}/update", address).parse().unwrap()]).unwrap()
                .timeout(std::time::Duration::from_secs(3)).build().unwrap();
            let update = updater.check().await.unwrap().unwrap();
            assert_eq!(update.download(|_, _| {}, || {}).await.is_ok(), expected);
            server.join().unwrap();
        }
    }
}
