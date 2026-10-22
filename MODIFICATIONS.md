# 叙织 · TaleWeft 修改声明

本发行版基于 PlotPilot v4.6.0 独立修改维护。上游来源为 https://github.com/shenminglinyi/PlotPilot ，原版权、LICENSE 和 CONTRIBUTORS.md 保留。产品更名不改变 Apache License 2.0 与 Commons Clause 附加条款。

本次品牌调整日期：2026-10-22。首次品牌验证构建标识为 `taleweft-rebrand-4.6.0-20261022`；独立发行版本从 **0.1.0-preview.1** 开始，不沿用上游版本号作为叙织自身版本。

## 本次修改

- 产品显示名统一为“叙织 · TaleWeft”，口号为“编织故事，让灵感成章”，说明为“面向长篇小说的 AI 创作工作台”。
- 首页、侧栏、关于页、公共页脚、浏览器首屏、启动器、API 展示名称与诊断标题采用新品牌，移除原团队推广、抖音号、直播时间与上游官方发布页推广。
- 新增独立的书页与经纬线 SVG 占位图，并更新网页图标、桌面图标与 README 插图。Logo 提示词见 `docs/branding/叙织-TaleWeft-Logo生成提示词.md`。
- 桌面产品名和主程序名称改为 `TaleWeft` / `taleweft.exe`，应用标识改为 `com.taleweft.desktop`；新身份不覆盖上游安装。
- Python 冻结运行与桌面默认数据目录使用同一新身份；增加 `TALEWEFT_PROD_DATA_DIR`，并保留旧环境变量作为回退。明确配置目录时沿用配置目录。
- 下载文件使用 `.taleweft.json` 等新名称；JSON 内部稿件协议与旧版兼容。前端 CSS 命名空间成套更新，避免旧品牌残留。
- 新增关于页“许可与致谢”，保留上游代码和部署设计贡献。构建时同步许可证与归属文本到前端，并在桌面和后端资源中携带。
- 重写当前产品 README，更新相关使用文档；历史分析与验收记录保留原貌。

## 2026-10-22 梗概扩容与公开文档调整

- 新建书目梗概由 2000 字扩容至 10000 字，同步调整首页输入限制、计数提示与创建 API 校验。章节目标字数不受此次修改影响。
- 新增 `tests/integration/test_novel_premise_limit.py`，验证 2001 / 10000 字经 API 完整落库与读回，10001 字拒绝且不创建书目。
- 参考 Dify、Open WebUI、uv 的 README 信息结构，重写根 README，增加功能思维导图、创作与编辑流程图、上游变化对照图及当前前端截图。
- 新增 `docs/UPSTREAM_CHANGES.md` 与 `docs/README_REFERENCES.md`，明确继承能力、实际变化、验证范围和未完成项；图示位于 `docs/assets/`，截图位于 `docs/screenshots/taleweft-home.png`。
- 本次修改源码文件为 `frontend/src/views/Home.vue` 与 `interfaces/api/v1/core/novels.py`；公开文档索引同步更新。测试和图文不会改动正式作品库，不代表新安装包已经生成。

## 2026-10-22 作者书桌与正式书标

- 新书库、三步新建弹层、全局导航、纸张正文、章节搜索、专注模式、九资料入口、墨绿主题与窄屏布局；复用原业务组件和路由，保留原保存保护、任务流、规划、审阅、模型与提示词能力。
- 模型引擎直接显示各角色的思考程度 / 思考预算入口，保留 C / R 协议选择。
- 修复迁移后手稿道具表及持有人校验、关键道具字段、专注面板展开、已有作品重新进入向导、浏览器禁用存储、严格评分 schema、人物姓名一致性误报及完成率显示问题。
- 章末证据不足时增加一次仅扩展原引文的修复，锁定全部事实并严格复验，禁止改引较早记录或删空降级；失败仍明确阻断演进快照。
- 接入用户提供的“织页书标”正式 Logo，保留原图副本和透明度，生成网页及桌面多尺寸图标；替换先前的 SVG 占位图。
- 增加本机 Web 测试启动器、隔离测试书与可复查证据，不改原正式安装和原作品。此轮交付为 Web 运行版，未声称已经重打桌面安装包。

## 2026-10-22 独立预览发行文档

- 公开仓库采用 `MontellaGreat/TaleWeft`，独立版本为 `0.1.0-preview.1`；上游 v4.6.0 继续作为来源基线保留。
- 新增中文使用指南与预览发行说明，更新 README、文档索引和上游改动对照；使用当前真实作者桌截图与正式书标，保留功能思维导图和编辑关联流程图。
- 修正文档中简化模型面板缺少思考控件、Logo 仍为占位图、真实页面关联重算尚未完成等过时表述。
- 将已验证的第三章 R 生成、第一章改名改稿、第二章顺序重算、历史留痕、已登记道具归属更新、五层去味提示词与扫描执行写明；未宣称全程 R 两章、可选自动改写或所有历史恢复组合均已验收。
- 安装包、校验值及发行资产以对应 GitHub Release 实际附加内容为准；源码文档更新本身不证明安装器已经构建或上传。

## 数据与兼容边界

新桌面身份默认使用 `%APPDATA%\com.taleweft.desktop\data`。本次没有改动或自动导入 `%APPDATA%\com.plotpilot.desktop\data`，也没有改动原安装目录。新身份首次启动显示空作品库属于目录隔离的结果。

迁移前请在原版本创建并验证备份，另行保留素材文件；使用应用已有的稿件导入或数据库恢复入口，并按 `docs/RECOVERY_AND_SUPPORT.md` 核验。数据库恢复是明确的用户操作，本次不会静默复制原数据或凭据。

下列旧标识保留用于兼容，不表示继续使用上游产品品牌：

- `plotpilot.db`、旧版 `aitext.db` 及备份清单内的数据库名称。
- 稿件交换格式、诊断包 `kind`、分类 schema、章节计划 schema、凭据引用格式、确定性实体命名空间等已持久化契约。
- 浏览器草稿与偏好存储键、工作台内部事件、会话 URL fragment、鉴权环境变量，以及已有自动化使用的别名。
- 冻结后端内部产物 `plotpilot-backend` 及兼容启动入口 `tools/plotpilot.bat`；新入口为 `tools/taleweft.bat`。
- 原上游 LICENSE、NOTICE 中的来源归属、CONTRIBUTORS.md 与原提示词作者字段；历史报告、基线安装包及其验证身份。

## 发行说明

本次源码更名不等于已经产出新版安装包。以前构建的安装包不含本次改名。新构建须重新生成前端、冻结后端和桌面安装器，不能混装旧后端。

本次携带项目许可证的改动不等于法律意见。`THIRD_PARTY_NOTICES/` 提供锁文件范围内的组件清单、许可原文、校验值和可获得的源码包；EPUB 已改用 MIT 许可的 `epublib`，PDF 使用 LGPL-3.0-only 的 `fpdf2` 并以可替换源码模式打包。lxml Windows 原生扩展静态依赖的精确重建和整套替换验证仍是发行核对项，发布者应阅读清单中的限制并自行履行适用义务。

## 本次修改文件索引

以下文件在本次独立品牌调整中新增或修改；JSON 配置的修改说明在此集中列出。源代码同时保留文件级修改声明。初次占位图已由用户提供的正式「织页书标」替换，当前图标为其等比缩放派生资源。

- `.env.example`
- `.github/workflows/backend-ci.yml`
- `.gitignore`
- `CLAUDE.md`
- `MODIFICATIONS.md`
- `NOTICE`
- `README.md`
- `README_LOGGING.md`
- `__init__.py`
- `application/brand.py`
- `application/core/services/export_service.py`
- `application/core/services/manuscript_transfer.py`
- `application/paths.py`
- `cli.py`
- `docs/ARCHITECTURE.md`
- `docs/BUILD_INSTALLER.md`
- `docs/README.md`
- `docs/RECOVERY_AND_SUPPORT.md`
- `docs/branding/叙织-TaleWeft-Logo生成提示词.md`
- `frontend/index.html`
- `frontend/package-lock.json`
- `frontend/package.json`
- `frontend/public/favicon.png`
- `frontend/public/favicon.svg`
- `frontend/public/taleweft-mark.svg`
- `frontend/scripts/sync-branding.mjs`
- `frontend/scripts/sync-release.mjs`
- `frontend/src-tauri/Cargo.lock`
- `frontend/src-tauri/Cargo.toml`
- `frontend/src-tauri/icons/128x128.png`
- `frontend/src-tauri/icons/128x128@2x.png`
- `frontend/src-tauri/icons/32x32.png`
- `frontend/src-tauri/icons/64x64.png`
- `frontend/src-tauri/icons/GENERATE_ICONS.md`
- `frontend/src-tauri/icons/Square107x107Logo.png`
- `frontend/src-tauri/icons/Square142x142Logo.png`
- `frontend/src-tauri/icons/Square150x150Logo.png`
- `frontend/src-tauri/icons/Square284x284Logo.png`
- `frontend/src-tauri/icons/Square30x30Logo.png`
- `frontend/src-tauri/icons/Square310x310Logo.png`
- `frontend/src-tauri/icons/Square44x44Logo.png`
- `frontend/src-tauri/icons/Square71x71Logo.png`
- `frontend/src-tauri/icons/Square89x89Logo.png`
- `frontend/src-tauri/icons/StoreLogo.png`
- `frontend/src-tauri/icons/icon-source-1024.png`
- `frontend/src-tauri/icons/icon-source.png`
- `frontend/src-tauri/icons/icon.icns`
- `frontend/src-tauri/icons/icon.ico`
- `frontend/src-tauri/icons/icon.png`
- `frontend/src-tauri/src/backend.rs`
- `frontend/src-tauri/src/lib.rs`
- `frontend/src-tauri/src/main.rs`
- `frontend/src-tauri/tauri.conf.json`
- `frontend/src/assets/styles/main.css`
- `frontend/src/assets/styles/tokens-layout.css`
- `frontend/src/assets/styles/workbench-panels.css`
- `frontend/src/components/StoryStructureTree.vue`
- `frontend/src/components/brand/BrandFooter.vue`
- `frontend/src/components/panels/BiblePanel.vue`
- `frontend/src/components/settings/sections/AboutBrandSection.vue`
- `frontend/src/components/settings/sections/CredentialManagementSection.vue`
- `frontend/src/components/settings/sections/DatabaseBackupSection.vue`
- `frontend/src/components/settings/sections/ManuscriptTransferSection.vue`
- `frontend/src/components/settings/sections/TracePrivacySection.vue`
- `frontend/src/components/stats/StatsSidebar.vue`
- `frontend/src/components/stats/StatsTopBar.vue`
- `frontend/src/components/workbench/ChapterCastManager.vue`
- `frontend/src/components/workbench/ChapterList.vue`
- `frontend/src/components/workbench/ChapterWorkbenchShell.vue`
- `frontend/src/components/workbench/CharacterNavigator.vue`
- `frontend/src/components/workbench/CharacterProfile.vue`
- `frontend/src/components/workbench/ConsistencyReportPanel.vue`
- `frontend/src/components/workbench/CurrentChapterContextPanel.vue`
- `frontend/src/components/workbench/DialogueCorpus.vue`
- `frontend/src/components/workbench/ForeshadowLedgerPanel.vue`
- `frontend/src/components/workbench/LLMControlPanel.vue`
- `frontend/src/components/workbench/MacroPlanModal.vue`
- `frontend/src/components/workbench/SettingsPanel.vue`
- `frontend/src/components/workbench/StoryDetailPanel.vue`
- `frontend/src/components/workbench/StoryNavigator.vue`
- `frontend/src/components/workbench/StoryTimeline.vue`
- `frontend/src/components/workbench/StorylinePanel.vue`
- `frontend/src/components/workbench/TimelinePanel.vue`
- `frontend/src/components/workbench/WorkArea.vue`
- `frontend/src/components/workbench/WorldlineDAG.vue`
- `frontend/src/constants/brand.ts`
- `frontend/src/generated/release.ts`
- `frontend/src/settings/registry.ts`
- `frontend/src/support/feedbackGlobalInstall.ts`
- `frontend/src/support/feedbackIncident.ts`
- `frontend/src/support/feedbackNotifier.ts`
- `frontend/src/types/axios-taleweft.d.ts`
- `frontend/src/types/global.d.ts`
- `frontend/src/views/Home.vue`
- `frontend/src/views/Workbench.vue`
- `infrastructure/ai/prompt_manager.py`
- `infrastructure/ai/prompt_packages/bundle_meta.yaml`
- `infrastructure/ai/prompt_seed/export_legacy.py`
- `infrastructure/ai/prompt_seed/loader.py`
- `infrastructure/runtime/data_directory_environment.py`
- `interfaces/api/middleware/logging_config.py`
- `interfaces/api/v1/workbench/llm_control.py`
- `interfaces/main.py`
- `interfaces/runtime.py`
- `release-manifest.json`
- `scripts/README.md`
- `scripts/build_backend_pyinstaller.py`
- `scripts/build_verification_installer.py`
- `scripts/generate_sbom.py`
- `scripts/hub.py`
- `scripts/install/__init__.py`
- `scripts/install/__main__.py`
- `scripts/install/build_exe.py`
- `scripts/install/env_check.py`
- `scripts/install/hub.py`
- `scripts/install/installer.py`
- `scripts/install/launcher.py`
- `scripts/install/packer.py`
- `scripts/install/theme.py`
- `scripts/install/ui_base.py`
- `scripts/install/utils.py`
- `scripts/repack_nsis_verification_baseline.py`
- `scripts/run_local_secure.py`
- `scripts/verify_windows_installer.py`
- `tests/integration/test_api_endpoints.py`
- `tests/unit/application/core/test_manuscript_transfer.py`
- `tests/unit/application/test_paths.py`
- `tests/unit/infrastructure/runtime/test_data_directory_environment.py`
- `tests/unit/interfaces/api/test_brand_static_resources.py`
- `tests/unit/interfaces/test_runtime.py`
- `tests/unit/scripts/test_qa_artifact_identity.py`
- `tools/plotpilot.bat`
- `tools/taleweft.bat`

移除的无引用旧品牌资产：`frontend/src/assets/plotpilot-logo.png`、`frontend/src/assets/hero.png`、`docs/plotpilot-readme.256.png`。原类型声明文件名 `frontend/src/types/axios-plotpilot.d.ts` 更名为 `axios-taleweft.d.ts`，声明字段保持兼容。
