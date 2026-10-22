// Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
//! TaleWeft Tauri 主入口
//!
//! 架构概览：
//!   用户双击 exe → Tauri WebView 渲染 Vue3 前端
//!              → Rust 端自动查找/启动 Python FastAPI 后端
//!              → 前端通过 HTTP 请求后端 API（同 localhost）
//!
//! 核心设计原则：
//!   1. 零配置：用户不需要安装 Python、不需要命令行
//!   2. Sidecar 模式：Python 作为子进程被管理
//!   3. 动态端口：自动寻找可用端口，避免冲突
//!   4. 生产数据目录：release 构建向子进程注入 `PLOTPILOT_PROD_DATA_DIR`（并同步旧名 `AITEXT_PROD_DATA_DIR`；见 `application/paths.py`）
//!   5. Windows：子进程纳入 Job Object（KILL_ON_JOB_CLOSE），与 `Drop`/显式 terminate 双保险
//!   6. 关闭窗口：拦截 CloseRequested → 后端 HTTP 优雅停机 → 超时强杀 → `exit(0)`

mod backend;
mod commands;
mod session;
mod updater;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use backend::BackendManager;
use tauri::{Manager, WindowEvent};

const BRAND_DISPLAY_NAME: &str = "叙织 · TaleWeft";
const BRAND_CREDIT: &str = "编织故事，让灵感成章";

/// 防止重复 spawn 多条优雅退出线程（用户连点关闭）
static GRACEFUL_SHUTDOWN_STARTED: AtomicBool = AtomicBool::new(false);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 初始化日志
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    log::info!("🚀 {} 启动中 - {}", BRAND_DISPLAY_NAME, BRAND_CREDIT);

    let builder = tauri::Builder::default();
    #[cfg(feature = "signed-updater")]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    builder
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.unminimize();
                let _ = win.set_focus();
            }
        }))
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // 已经在关闭中，直接忽略后续点击
                if GRACEFUL_SHUTDOWN_STARTED.swap(true, Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                    return;
                }
                api.prevent_close();

                // 立即隐藏窗口，避免关闭时出现最小化/弹回的怪异反馈。
                let _ = window.hide();

                let app_handle = window.app_handle().clone();
                std::thread::spawn(move || {
                    let backend = app_handle.state::<Mutex<BackendManager>>();
                    if let Ok(mgr) = backend.lock() {
                        // 减少超时时间，快速关闭
                        mgr.graceful_shutdown(Duration::from_secs(3));
                    }
                    app_handle.exit(0);
                });
            }
        })
        .setup(|app| {
            let handle = app.handle().clone();

            let smoke_report = std::env::var_os("PLOTPILOT_DESKTOP_SMOKE_REPORT").map(std::path::PathBuf::from);
            if smoke_report.is_some() {
                if backend::configured_prod_data_dir().is_none() {
                    return Err(std::io::Error::other("桌面验证必须指定隔离数据目录").into());
                }
                if let Some(window) = app.get_webview_window("main") {
                    window.hide()?;
                }
            }

            let manager = BackendManager::new(handle.clone());
            app.manage(std::sync::Mutex::new(manager));
            app.manage(updater::UpdateState::default());

            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let backend = app_handle.state::<Mutex<BackendManager>>();
                let port = match backend.lock() {
                    Ok(mut mgr) => match mgr.spawn_only() {
                        Ok(p) => p,
                        Err(e) => {
                            log::error!("❌ 后端进程启动失败: {}", e);
                            return;
                        }
                    },
                    Err(e) => {
                        log::error!("后端管理器锁 poisoned: {}", e);
                        return;
                    }
                };

                let ready = BackendManager::wait_for_ready(port, 120);
                match &ready {
                    Ok(()) => log::info!("✅ 后端已就绪，端口: {}", port),
                    Err(e) => log::error!(
                        "❌ 后端就绪超时或失败: {}（子进程端口 {}，请查叙织日志）",
                        e,
                        port
                    ),
                }
                if let Some(path) = smoke_report {
                    let result = match backend.lock() {
                        Ok(mgr) => {
                            let result = ready.and_then(|_| mgr.desktop_smoke_probe());
                            let graceful = mgr.graceful_shutdown(Duration::from_secs(10));
                            result.and_then(|mut report| {
                                if !graceful {
                                    return Err("桌面验证触发了强制终止，不能记录为优雅关闭".into());
                                }
                                if mgr.is_running() {
                                    return Err("桌面退出后仍有后端进程".into());
                                }
                                report["owned_process_cleanup"] = true.into();
                                report["graceful_shutdown"] = true.into();
                                Ok(report)
                            })
                        },
                        Err(error) => Err(error.to_string()),
                    };
                    let (report, code) = match result {
                        Ok(report) => (report, 0),
                        Err(error) => (serde_json::json!({"status":"failed", "error":error}), 1),
                    };
                    if let Some(parent) = path.parent() { let _ = std::fs::create_dir_all(parent); }
                    if std::fs::write(path, serde_json::to_vec_pretty(&report).unwrap()).is_err() {
                        app_handle.exit(1);
                    } else {
                        app_handle.exit(code);
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_backend_port,
            commands::get_backend_session,
            commands::get_backend_status,
            commands::restart_backend,
            commands::open_in_browser,
            commands::toggle_devtools,
            commands::run_installation,
            commands::check_environment,
            commands::extract_embedded_python,
            updater::updater_status,
            updater::updater_check,
            updater::updater_download,
            updater::updater_install,
        ])
        .run(tauri::generate_context!())
        .expect("error while running TaleWeft");
}
