# NSIS template provenance

`installer.nsi` is the official Tauri template from the pinned CLI release
[`tauri-cli-v2.12.2`](https://github.com/tauri-apps/tauri/blob/tauri-cli-v2.12.2/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi).
Upstream licenses: MIT or Apache-2.0.

The local patch declares `ExplicitInstallDir`, captures the directory NSIS parsed
from `/D=` before `MULTIUSER_INIT`, and restores it afterwards. In `both` mode,
upstream `MultiUser.nsh` otherwise overwrites `/D=` with a prior registered or
default install directory. Registry context and interactive mode selection remain
upstream behavior. `/D=` must remain the final argument and must not be quoted.

Installer verification uses a distinct product name, publisher and bundle ID.
An alternate directory alone does not isolate the product's registry identity.
Never test a production-identity installer against an existing user installation.
