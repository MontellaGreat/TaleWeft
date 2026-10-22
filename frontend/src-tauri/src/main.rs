// Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    taleweft_lib::run()
}
