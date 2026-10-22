# Windows 安装包（Tauri / NSIS）

## 仓库里需要知道的


| 项 | 说明 |
| --- | --- |
| **Tauri 源码** | `frontend/src-tauri/` **在仓库中**；**构建产物** `frontend/src-tauri/target/` 由 `.gitignore` 忽略，需本地 `cargo tauri build` 生成 |
| **命令** | 先构建前端与冻结后端，再在 `frontend` 运行 `npm run tauri build`；隔离验收使用 `scripts/build_verification_installer.py` |
| **冻结后端** | `python scripts/build_backend_pyinstaller.py`，产出 `out/tauri/plotpilot-backend/plotpilot-backend.exe` |
| **构建机 Python** | **Python 3.14**，在专用 venv 使用 `requirements.lock` 与 `requirements-build.lock` 的 hash 锁定依赖，勿使用混装 torch/faiss 的全局环境 |
| **生成目录** | `out/` 整体忽略，不随源码提交；安装器构建前必须先生成完整冻结后端 |
| **安装包路径**      | `frontend/src-tauri/target/release/bundle/nsis/*.exe`                                                                                                            |
| **运行时**        | Tauri 优先启动 `plotpilot-backend.exe`；若无则回退 `python -m uvicorn`（本地开发）                                                                                               |

## 构建目录

PyInstaller 使用 `out/pyinstaller` 工作目录，输出 `out/tauri/plotpilot-backend`；前端构建输出 `frontend/dist`。资源打包仅包含 allowlist 源码/配置/SQL/提示词与前端，不包含作者数据和运行日志。运行中的旧测试后端必须退出后再覆盖冻结目录。

## NSIS 体积限制与常见失败

若冻结目录 `out/tauri/plotpilot-backend/` **解压后总体积过大**（例如全局 Python 里已安装 **torch / transformers** 等，被 PyInstaller 整包收集），`cargo tauri build` 在 **NSIS（`makensis`）** 阶段可能出现：

`Internal compiler error #12345: error mmapping file ... is out of range`

这与 NSIS 对超大安装包的限制有关（量级约 2 GiB）。使用仅含 runtime/build 锁定依赖的干净虚拟环境，避免把重型 ML 栈或用户文件打进安装包。

## 推荐：专用 venv（Python 3.14）构建

与日常开发的 `.venv` 分开，避免把本机其它包装进冻结后端：

```powershell
py -3.14 -m venv .venv-nsis
.\.venv-nsis\Scripts\activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --require-hashes -r requirements-build.lock
Push-Location frontend
npm ci
npm run build
Pop-Location
python scripts/build_backend_pyinstaller.py
```

仅重打冻结后端时，同样先 `activate` 该 venv，再执行 `python scripts/build_backend_pyinstaller.py`。脚本本身会刷新工作目录，无 `--force` 参数。

## 手动构建

```bash
cd frontend
npm ci
npm run build
npm run tauri build
```

直接 `tauri build` 前须已存在完整冻结目录 `out/tauri/plotpilot-backend/`，并保留仓库中的 `THIRD_PARTY_NOTICES/`。空目录不能构成可用安装包。

## 隔离 QA 安装器

使用 `python scripts/build_verification_installer.py --suffix <本次唯一标识> --artifact-out out/qa-installers/candidate.exe` 生成 QA 产品。它使用独立 productName/publisher/identifier，并保存绑定安装器 SHA-256 和 backend build id 的 `.verification.json` 回执。

回滚基线可通过 `--backend-resource <项目内旧冻结后端目录> --artifact-out out/qa-installers/baseline.exe` 构建；基线必须采用相同 QA suffix。禁止运行正式叙织身份的旧安装器作为基线。

`scripts/verify_windows_installer.py` 会先核对两份 QA 回执、文件 hash 与隔离身份，再执行 current-user 安装、替换、重装、卸载保留数据演练。不同开发 build id 可以验证程序替换和回滚；相同 build id 只验证同版本重装。两者都不能替代跨数据库 schema 降级验证。

若 MSVC `link.exe` 不可用，可在项目内下载开源 Rust GNU toolchain 与 w64devkit，使用 `--target x86_64-pc-windows-gnu`。选择对应 `RUSTUP_TOOLCHAIN`，将 GCC bin 放入 PATH，并设置 `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER`。Rust 的 GNU `libgcc/libgcc_eh` 位于对应 sysroot 的 `lib/rustlib/x86_64-pc-windows-gnu/lib/self-contained`；将该目录放入 `LIBRARY_PATH`，同时供 host build scripts 与目标链接器查找。不要修改系统工具链或给缺失库制造空占位文件。

## 叙织品牌构建（2026-10-22）

本次修改将产品名改为 `TaleWeft`、主程序改为 `taleweft.exe`、桌面身份改为 `com.taleweft.desktop`；内部冻结后端路径继续兼容。运行 `npm run build` 时会同步 `frontend/public/legal/` 的许可和修改说明，Tauri 与冻结后端打包也携带根目录的 LICENSE、NOTICE、CONTRIBUTORS.md、MODIFICATIONS.md。

QA 新身份为 `TaleWeft-Verify-<suffix>`。验证脚本保留历史 QA 回执和旧主程序名称的兼容处理，用于历史基线回滚。

此前的安装包不含本次更名。必须依次重建前端、冻结后端、桌面包；不要把旧冻结后端当成叙织新版发布。第三方原文许可、组件清单及对应源码见 `THIRD_PARTY_NOTICES/`，发布前仍需核对其与实际产物一致。
