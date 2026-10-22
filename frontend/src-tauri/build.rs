mod updater_config;

fn main() {
    println!("cargo:rerun-if-env-changed=TAURI_CONFIG");
    println!("cargo:rerun-if-changed=updater_config.rs");
    if std::env::var_os("CARGO_FEATURE_SIGNED_UPDATER").is_some() {
        let config: serde_json::Value = serde_json::from_str(
            &std::env::var("TAURI_CONFIG").expect("signed-updater requires a generated release override via --config"),
        ).expect("invalid TAURI_CONFIG JSON");
        updater_config::validate_config(&config["plugins"]["updater"])
            .expect("unsafe or missing signed updater release configuration");
        assert_eq!(config["bundle"]["createUpdaterArtifacts"], true,
                   "signed-updater requires createUpdaterArtifacts=true");
    }
    // Tauri normally embeds the manifest only in binaries. Library test
    // executables also load Common Controls v6 through the desktop libraries.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(
            tauri_build::WindowsAttributes::new_without_app_manifest(),
        ))
        .expect("Tauri resource build failed");
        embed_resource::compile_for_everything("windows-manifest.rc", embed_resource::NONE)
            .manifest_required()
            .unwrap();
    } else {
        tauri_build::build();
    }
}
