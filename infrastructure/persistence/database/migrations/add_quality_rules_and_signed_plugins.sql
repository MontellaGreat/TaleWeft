-- Immutable author policies and signed declarative prompt/rule packages.
CREATE TABLE IF NOT EXISTS quality_rule_versions (
    id TEXT PRIMARY KEY, config_json TEXT NOT NULL, note TEXT NOT NULL,
    source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quality_rule_active (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_rule_snapshots (
    digest TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS signed_plugin_keys (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, public_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL, permissions_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS signed_plugin_packages (
    id TEXT PRIMARY KEY, version TEXT NOT NULL, signer_id TEXT NOT NULL,
    digest TEXT NOT NULL, package_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(signer_id) REFERENCES signed_plugin_keys(id)
);
CREATE TABLE IF NOT EXISTS signed_plugin_resources (
    plugin_id TEXT PRIMARY KEY NOT NULL, template_id TEXT NOT NULL,
    FOREIGN KEY(plugin_id) REFERENCES signed_plugin_packages(id) ON DELETE CASCADE,
    FOREIGN KEY(template_id) REFERENCES prompt_templates(id) ON DELETE CASCADE
);
