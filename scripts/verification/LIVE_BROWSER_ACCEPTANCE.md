# 真实浏览器章节编辑验收

复用项目内已安装的开源 Vite、Vue 与 Playwright。执行真实源码前端和后端，不拦截、不替换 API。章节正文、标题、保存和手动重跑均由真实页面操作；已登记道具的测试前置条件通过公开 props API 创建/调整，单列为 `public_API_baseline_setup`，不写数据库。截图、JSON 和隔离数据均保存在项目 `out/verification`。

运行前，真实全流程 driver 必须已经生成并保存两章，且两章的派生任务及级联均已完成。运行时只复制该隔离测试库，不读取或修改作者正式小说库。不得在全流程仍调用模型时运行浏览器验收。

仅校验步骤，不启动进程、不读取密钥、不调用模型：

```powershell
& 'F:\plotpilot\.ws8-verify-venv\Scripts\python.exe' scripts\verification\live_browser_edit.py --source-run out\verification\live-responses-fullflow-20261016 --output-dir out\verification\live-browser-responses-20261016 --dry-run
```

开始真实验收（会按源测试库的模型配置运行章后分析，产生实际模型请求）：

```powershell
& 'F:\plotpilot\.ws8-verify-venv\Scripts\python.exe' scripts\verification\live_browser_edit.py --source-run out\verification\live-responses-fullflow-20261016 --output-dir out\verification\live-browser-responses-20261016 --node 'C:\Program Files\nodejs\node.exe' --browser-executable 'C:\Users\81576\AppData\Local\ms-playwright\chromium-1169\chrome-win\chrome.exe'
```

脚本会在新的隔离数据副本上启动源码后端，核对复制后的第1章末明确由沈舟保管钥匙；通过公开 props API 登记「验潮钥匙」（真实 unified_character ID，`introduced_chapter=1`，可复用同名已登记道具），并回读校验，不调用模型、不生成或伪造道具事件。此步骤是测试登记前置条件，不代表自动发现。随后启动真实 Vite 代理，使用安全启动 fragment 进入章节页面，确认 fragment 被清除。通过 UI 将第一章改名为「浏览器验收·一百零七号钥匙」，调整正文中的钥匙编号及最终持有人为林雁，点击保存，并独立验证版本、SHA-256、持久保存回执、刷新回读及历史标题。

脚本等待第一章关联分析与第二章级联分析完成，确认第二章正文和版本没有被悄悄改写；再通过「历史版本 → 重跑章后关联分析」按钮验证用户手动重跑入口及第二轮级联。最后检查提取快照、活动自动记忆、旧证据撤销、提纲章节标题以及第二章旧事实和冲突状态。

产物：`public-api-baseline-setup.json` 是明确的前置登记回执；`browser-result.json` 包含实际页面操作及前置登记证据；`result.json` 增加数据库交叉验证和语义限制；`ui-*.png` 包含编辑前、未保存、已保存、刷新以及两轮派生完成截图。失败只写安全的阶段名与自定义诊断，不写第三方原始异常、headers、会话 token、模型 key 或 provider URL；不启用 Playwright trace、HAR 或 storageState。服务日志只保存在该隔离目录，勿将其当作脱敏报告公开。

两章派生完成证明重算运行成功，不代表自动纠正了全书语义。第二章正文如仍提及旧编号，应保留并独立报告人工复核需求，不能为了通过测试删事实或改写正文。

持有人验收另存 `holder-evidence.json`。它从历史版本确认原始第1章确有74号事实，区分“历史交接事件”与“章末当前持有状态”，分别核查活动自动记忆、第1章道具快照/变更日志、第1章演进 `items.owner_id` 与 `characters.inventory`。只出现新编号不算持有人通过；最新浏览器验收缺少任一核心结构化关联时会失败。旧源码代际缺表或尚未建立领域时，会明确标记 `not_recorded`/`schema_unavailable`，不会当作已通过。

也可对正在运行的隔离测试库单独做零模型、只读证据检查（不会发起或重试业务工作流）：

```powershell
& 'F:\plotpilot\.ws8-verify-venv\Scripts\python.exe' scripts\verification\linked_holder_evidence.py --run-dir out\verification\live-chat-fullflow-20261016 --expected-owner 沈舟 --expected-number 92 --report-name holder-evidence-readonly.json
```
