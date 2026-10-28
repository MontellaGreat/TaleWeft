# 叙织 · TaleWeft 修改声明

本发行版基于 PlotPilot v4.6.0 独立修改维护。上游来源为 https://github.com/shenminglinyi/PlotPilot ，原版权、LICENSE 和 CONTRIBUTORS.md 保留。产品更名不改变 Apache License 2.0 与 Commons Clause 附加条款。

本次品牌调整日期：2026-10-22。首次品牌验证构建标识为 `taleweft-rebrand-4.6.0-20261022`；独立发行版本从 **0.1.0-preview.1** 开始，不沿用上游版本号作为叙织自身版本。

## 2026-10-28 累计更新发行（0.2.0）

- 将独立发行版本统一为 `0.2.0`，同步前端、桌面壳、锁文件、界面版本与后端发行清单，构建标识为 `taleweft-0.2.0-20261028`。
- 汇总首次公开预览版之后的模型控制台、新书 DAG、真实偏离检测与共享语义 Anti-AI 审稿、断点恢复、本地嵌入、作品支撑编辑、角色联动及提示词管理更新。历史修复与验收记录保留，详细内容见 [0.2.0 发布说明](docs/RELEASE_0.2.0.md)和[更新日志](更新日志.md)。
- 叙事结构恢复结果经过成功调用、采纳和提交证据核对后保留；显式启动时先处理尚未应用的恢复结构，即使已有旧卷结构也不跳过，目标变化阻止直接复用。
- 保留上游版权、许可及第三方声明；累计更新不代表重新完成全部模型效果或安装升级卸载矩阵验收，最终验证范围以本次发布说明及安装包校验回执为准。

## 2026-10-27 作品支撑与提示词管理（preview.16）

- 修复作品支撑页长内容、短窗口和底部操作被裁切的问题；创作契约完整开放题材、五维画像、目标篇幅、文风及梗概锁编辑，使用独立版本接口原子保存并回读，不再以简化 Bible 覆盖其他资料。
- 契约修改遵守设定审核锁、生成租约和并发版本校验；结构化五维画像须重新确认，保存作品专属变量与版本来源，历史冻结证据继续按原规则校验。
- 新增角色档案编辑、改名与删除。改名同步当前章节和当前关联资料，保留历史版本与原始审稿证据；编辑持久提交章后分析任务，失败可以从章节历史重跑。删除保留已写正文，清理当前角色关系、出场配置和记忆，并阻止旧记忆重新建立已删除档案。
- 自定义提示词增加确认删除，保护内置项及工作流、调用规格、变量、资源组合、会话草稿和调用历史引用，引用变化时拒绝删除。广场显示已接入调用、按路径生效、生成前片段、仅关联说明等用途；新建提示词不自动用于生成。
- 新书与正文共享语义审稿使用 v4，读取 CPMS `anti-ai-chapter-audit` 当前激活版本作为附加文风规范，并冻结实际版本与 System/User 原文；固定核心继续约束五维审阅、覆盖、证据、JSON 及事实边界。历史 v1/v2/v3 使用原快照，新请求不能借用旧回执。
- 修复全托管叙事结构长时间显示生成中：现场网关 502 后失败未持久化，请求遗留已编译状态，执行器每 30 秒等待已有请求；宏观旁观进度又未覆盖实际调用链，前端将带宏观子步骤的审阅等待误识别为生成。现异常与取消保存真实失败/取消回执，进度按持久请求对账，保留已收到的预览，旁观流结束后继续核对。运行中心提供显式请求恢复；停止不触发自动重试，也不因短等待强行取消正常模型请求。
- 本轮以隔离数据和可控模型响应验证持久化、冲突、接口与浏览器行为，没有新增真实付费模型效果验收；测试统计及交付结果以 [preview.16 发布说明](docs/RELEASE_0.1.0-preview.16.md)和最终校验回执为准。

## 2026-10-27 Anti-AI 段落句号约束（preview.15）

- 为新书资料与章节正文真正使用的语义 Anti-AI 审稿加入段落约束：每个自然段最多一个中文句号，段内按语义使用逗号、分号及连接词；同段多个句号归入段落结构问题，要求原文证据和可执行的修订建议。
- 新书 AI 味修复、章节文风重写及生成前行为协议使用相同约束，保留事实、否定、因果及对白信息，不通过机械替换、删除内容或碎片化换行规避检查。标题及问号、叹号收束的段落无需强补句号。
- 调整旧提示词中“2—5 句聚合”“一句独段比例”“用句号衔接”等冲突表述，改为逻辑分句聚合及极短段限制，默认提示词广场同步相关审计、行为协议和文风重写模板。
- 语义审稿新请求采用 v3，保持 v1/v2 历史提示词字节及回执校验兼容，不把旧审稿伪装成执行过新要求；新请求禁止降级为旧版回执。
- 未将用户提供的正面和反面教材加入提示词。详见 docs/RELEASE_0.1.0-preview.15.md。

## 2026-10-27 新书多次审稿请求超时与断点恢复修复（preview.14）

- 修复 `locations_anti_ai` 等多调用步骤共享 600 秒总时限的问题：每次模型请求独立计时，默认 600 秒；审稿与修复步骤另设 3600 秒有限总时限，保留真实偏离和语义 Anti-AI 审稿及修复后复审。
- 单次请求耗尽时限后保留部分输出和已生成候选，不自动追加付费重试；用户停止仍传播取消并关闭流，不伪装成审核通过。
- 从失败步骤继续时，严格核对会话、单元、实际提示词、候选修订身份与内容及真实成功调用，只复用完全匹配的已完成结果并重新校验；无效审核、无改动修复和未完成输出不作为可复用成功结果。
- 修复成功调用保存断点期间取消导致重复请求、随机扫描编号导致相同修复无法复用，以及完整世界观修复后内容回到旧值时错误复用旧拒绝报告的问题；相同正文的新修订仍重新审稿，已有工作流定义保持兼容，无需重新生成地图。
- 进度持久化真实等待起点、收到的字符及单次时限，首块内容立即保存；前端区分等待响应、正在接收、已超时和复用结果，复用结果不表示审核通过。
- 使用正式书库只读备份的私有副本恢复实际超时任务：5 次真实模型调用完成双审稿、修复与复审，10 个地点保存后连续两次回读一致，writer 失败 0；未回写正式书库。
- 详见 docs/RELEASE_0.1.0-preview.14.md。

## 2026-10-26 人物保存回读与旧任务恢复修复（preview.13）

- 修复 Bible 保存后同步到统一人物仓储时漏传声线三字段，导致全部已生成声线被空值覆盖、人物工作流报“characters 保存内容回读不一致”。
- 人物结构化文本使用确定性 JSON 表示；数字、对象、列表声线统一为稳定文本，避免 SQLite TEXT 回读、提交回执键排序造成误差。
- 生成全量替换人物时，明确允许清空旧揭示章节；普通编辑省略字段时继续保留旧值。
- 旧失败任务仅在真实双审稿、持久化采纳、成功提交、完整人物投影和当前执行租约均匹配时补回缺失声线；SQLite writer 原子比较防止并发编辑被覆盖，仅更新声线，不重写其他人物或书目资料。
- 保留全字段回读校验及审核失败阻断。在正式书库只读备份的私有副本中验证：8 人恢复、工作流完成、连续两次回读一致、零新增模型调用；不改动正式书库。
- 详见 docs/RELEASE_0.1.0-preview.13.md。

## 2026-10-26 历史故障复查与恢复链路修复（preview.12）

- 修复世界观原始 JSON 字段错位、解析后缺字段导致结构校验无法续跑；保留原始输出，由模型有界修复当前单元，再执行完整双审计。
- 补齐偏离审核持久化真实调用证据、语义审核完整 system/user 绑定与 DAG 恢复回执来源校验；保持失败不能保存。
- 修复旧版完整世界观已提交后因格式转换无法续跑：严格核对原始采纳与成功提交，重新审稿后回读；需要内容修正时创建新采纳和提交，保存完成后同步调用状态。
- 修复模型控制台异步刷新覆盖新草稿、自动批准绕过规划失败审核、长模型调用时停止不响应。
- 修复章前规划误采纳并发手工正文/标题编辑的 revision，以及记忆派生重算成功后残留旧级联失败状态。
- 新增交叉审查、故障回归和真实模型隔离恢复验证；详细说明见 docs/RECHECK_2026-10-26.md。

## 2026-10-25 语义审稿大批量覆盖回执修复（preview.11）

- 核对两次实际失败报告，90 个字段均有返回，分别有 3 个和 9 个字段哈希抄写错误；原报告实际提出了文风问题，不能冒称通过。
- 语义审稿回执升级为 v2：每个非空文本分配本次请求内唯一短编号，模型完整返回逐字段编号，无须反复抄写 64 位哈希。整份候选哈希、五项文风检查、全部文本覆盖、逐字原文证据与持久化实际调用核对继续强制执行。
- 新书向导与写作流程共用新版审稿服务；重复、漏项、未知编号、错误版本或候选变动均阻止通过，不修补错误报告或自动制造通过结论。
- 保留 v1 原始提示词与严格校验，已完成的历史审核仍可读取及核对；前端识别两版有效回执，继续展示真实文风问题。
- 修复后在隔离数据副本中重测相同地图候选，不修改用户书库；本轮更新网页版，详见 `docs/RELEASE_0.1.0-preview.11.md`。

## 2026-10-24 新书审核网关 524 超时处理（preview.10）

- 根据实际地图工作流检查点，确认内容生成及校验成功，偏离检测／语义审稿的非流式请求遭遇上游 120 秒代理超时；成功候选和步骤保留，未绕过审核保存。
- 新书偏离检测、Anti-AI 语义审稿和修复统一使用流式接收，在完整正常结束后解析与校验；每次调用保留独立尝试、部分输出、失败记录和来源证据。
- 首次尚未收到内容的临时错误最多自动重试一次，遵守服务端 Retry-After 秒数、日期、毫秒以及 JSON retry_after；524 无提示时默认等待 120 秒。等待支持停止，超过 120 秒的冷却提示交由手动继续。
- 已收到部分输出、鉴权／参数错误、服务端明确禁止重试或存在书目预算限制时不自动追加调用；取消和失败不能变成审核通过。
- 进度区及运行中节点详情显示流式接收状态、重试次数和等待倒计时；最终 524 给出中文原因及从失败步骤继续的操作说明。
- 使用网页书库的私有副本与已保存模型，原样重放超时的地图语义审稿请求：约 22 秒开始返回正文、约 92 秒结束，90 个字段全部覆盖且报告有效，检测到 1 项文风问题。此验证未修改原书，也未冒称问题已经修复通过。
- 详见 `docs/RELEASE_0.1.0-preview.10.md`，本轮发布网页版。

## 2026-10-24 结构化文风与旧失败任务恢复

- 修复模型返回对象、列表或 JSON 字符串文风时误报“文风公约为空”的问题，复用开源代码内已有的世界观正文转换器，保留所有非空正文与层级标签，生成、修复及完整复审使用同一规范化。
- 支持继续已生成成功但结构校验失败的旧任务，转换原候选并保留原始调用来源；转换后的新内容重新走偏离检测与 Anti-AI 审计，不复用旧哈希的通过记录。
- 拒绝空结构、纯数字／布尔状态、审核报告和接口错误信封；规范化恢复前核对原始调用成功状态、会话归属和内容一致性。
- 文风生成契约明确要求字符串，同时保留旧市场提示词已知转换的严格兼容验证。
- 提供项目内隔离的网页版测试入口，使用正式书库只读快照和本机已保存模型配置，网页修改不回写正式桌面书库。
- 独立版本更新为 `0.1.0-preview.7`；详见 `docs/RELEASE_0.1.0-preview.7.md`。

## 2026-10-24 新书工作流市场画像误拦截修复

- 修复“市场画像或提示词已变化，请重新编译并审阅后生成”的内部误报：分步生成修改提示词后，重新冻结实际提示词及匹配的市场证据。
- 偏离审计和两类修复使用各自独立的提示词快照与证据；成功、失败、取消均不会将临时证据覆盖到主生成会话，调用记录保留可核对的证据。
- 兼容 preview.5 已成功准备、却卡在生成节点的旧任务；仅在原始来源、画像哈希、单元、基础设定和旧变换均一致时迁移，允许从断点继续。
- 保留真实画像变化和未知提示词修改的阻断，并在恢复、下一单元准备及提交前后复核完整画像，避免过期审核结果写入资料。
- 增加结构化五维市场画像的四阶段真实管线测试、故障恢复、修复重审及缺失／篡改证据的对抗测试。独立版本更新为 `0.1.0-preview.6`；详见 `docs/RELEASE_0.1.0-preview.6.md`。

## 2026-10-24 四阶段审核展示与节点详情修复

- 修复旧版执行快照遮住新版工作流定义的问题；文风 / 世界观、人物、地图、剧情总纲均可查看当前定义中的偏离检测和 Anti-AI 审计节点。
- 旧记录保留原始执行状态，并明确提示缺少新版审核。展示流程定义不等于执行过审核，不给历史结果补造通过状态。
- 新书向导支持点击阶段、节点与审核入口查看只读详情，区分节点当时输出与当前候选，展示报告、错误、修复记录与执行回执；外部图点击不改变写作页工作流状态。
- 切换阶段后使用该阶段自己的运行记录和状态，避免人物或世界观页面显示最近地图任务的标题。
- 封堵旧版一键生成、后台别名及流式生成接口覆盖未完成审核资料的旁路；模型返回后、实际保存前再次检查审核锁，旧书没有新版未完成流程时保持兼容。
- 独立版本更新为 `0.1.0-preview.5`；详见 `docs/RELEASE_0.1.0-preview.5.md`。

## 品牌与原有修改

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

## 2026-10-24 模型设置与新书 DAG

- 移除厂商预设导入和配置档案预设选择；OpenAI C / R 合并为 OpenAI 自动，拉取模型后直接提供模型选择列表，保留思考参数与其他协议。
- 自动协议仅对明确端点不支持做回退，保留鉴权、限流、502、超时与参数错误；修复 Responses 扩展参数的 SDK 传参方式。
- 桌面守护进程复用持久化控制循环，修复启动 pending 与租约占用引起的 DAG 409，增加状态说明；过期启动请求不自动复活。
- 文风 / 世界观、人物、地图、剧情总纲接入真实 DAG，增加进度、当前动作、流程图、停止、重连与持久化恢复，保留逐步编辑确认。
- 修复排队事务内变量绑定读取、绑定失败假成功、开书保存回读类型错误、空世界观通过前置校验及取消/锁丢失等并发边界。
- 独立版本更新为 `0.1.0-preview.2`。详细行为与验证边界见 `docs/RELEASE_0.1.0-preview.2.md`。

## 2026-10-24 创建前检查、模型控制台与国内模型下载

- “创建第一本书”“新建书目”和侧栏创建入口统一检查语言模型与嵌入配置；缺少有效配置、本地依赖或完整模型文件时提示“请先配置好模型”，确认后打开模型控制台的对应页签。提交前再次检查，避免填写期间切换配置后误创建。
- 全部当前 UI 入口改称“模型控制台”，思考程度 / 思考预算移动到模型名正下方；原协议、档案、参数与高级设置保留。
- 嵌入默认使用运行时数据目录中的本地 BGE，保留明确配置的云端服务、凭据与已有模型路径。只读就绪检查不进行远端请求、不加载模型权重、不返回凭据；网络连通性仍通过连接测试或实际调用确认。
- 重写本地准备流程：依赖安装使用国内 PyPI 镜像失败切换，模型依次探测魔搭、HF 国内镜像、Hugging Face 官方的元数据、配置与权重端点；下载后检查文件长度和哈希，完整后原子发布，失败保留原模型。
- 修复错误分支缺少 SSE 终止事件、安装锁未释放、静默下载无心跳、取消等待被 stdout 阻塞及冻结程序被错误当作 pip Python 的问题。冻结版寻找 ABI 与位数相符且包含 pip 的真实 Python；缺失时显示明确安装指引。
- 本地依赖安装到可写数据目录，经实际导入验证后启用；状态分别检查依赖与用户保存的模型路径，不把目录存在等同于模型就绪。迟到的读取、保存或下载事件不会覆盖作者刚修改的配置或取消状态，下载完成不会自动覆盖云端配置。
- 补齐冻结包中可选模型运行时所需的 `filecmp`、`pickletools`、性能分析等标准库模块；修复外部 Python 子进程继承冻结 DLL 路径、ASGI socket 断线未取消任务，以及依赖检查线程异常退出或超时后永久停留在检查中的问题。
- 修复安装本地依赖后重启时将 FAISS 二进制索引误当旧 JSON 加载、导致向量库被禁用的问题；兼容旧便携索引转为 FAISS，已有 Simple 对象继续安全保存，缺依赖时保留原索引并明确报错。启动恢复先查询待处理章节，空队列不加载模型，有任务时在线程中初始化，避免阻塞 API 启动。
- 独立版本更新为 `0.1.0-preview.3`。详细说明与验证边界见 `docs/RELEASE_0.1.0-preview.3.md`。

## 2026-10-24 新书向导分步生成、审计与失败恢复

- 文风和世界观五维改为独立单元，按文风、核心法则、地理生态、社会结构、历史文化、沉浸感细节顺序运行；每次提示词只请求当前单元，并带入已经审核保存的前置资料。
- 每个单元增加真实的结构校验、基础设定偏离审核及有界修复、Anti-AI 叶文本扫描及修复，再保存、回读；五维结束后追加完整世界观复审。人物、地图和剧情总纲使用相同的审核门禁。
- 逐节点持久化候选、会话、审计和提交回执；失败、取消或重启后默认从首个未成功节点继续，不重复已经完成的生成。恢复时核对基础设定与前置资料，拒绝复用过期审核。另提供需明确确认的从头重新生成。
- 审核证据与候选内容哈希绑定；修复后重新检查基础设定和 AI 味。缺失报告、检查失败、无效修复或保存回读不一致时不能宣称完成。
- 阶段未完整通过时只读展示已保存的资料，锁定编辑、确认和跳步；服务端写入与调用续接同样保护，防止绕过界面直接覆盖审核中的资料。旧版本记录保留兼容，不因缺少新审核永久锁定。
- 向导复用写作页面同一个 DAG 画布与节点组件，隔离运行状态；底部操作区支持换行与有限窗口布局，修复按钮溢出弹窗。
- 独立版本更新为 `0.1.0-preview.4`；具体测试范围、安装产物和限制见 `docs/RELEASE_0.1.0-preview.4.md`。

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

## 2026-10-24 / preview.8：审核执行链修复

由 TaleWeft 维护方修改。新书向导和章节 DAG 接入共享模型语义文风审稿，保留独立规则扫描及章节尾段检查；审核失败或缺少真实回执时阻止提交。修复人物写保护与完整回读、资料字段漏扫、文风禁词引用误报、提示词约束丢失、修复调用来源、终章状态及可选注入导致普通章跳过的问题。模型保存、就绪检查和 Provider 共用参数约束，避免保存确定无法执行的配置。

主要修改范围：`application/onboarding/`、`application/audit/services/`、`application/engine/dag/`、`application/world/services/`、`application/ai/llm_control_service.py`、`infrastructure/ai/providers/`、相关人物及 DAG API、新书向导审核详情组件、版本元数据与回归测试。具体行为、兼容边界及证据入口见 `docs/RELEASE_0.1.0-preview.8.md`。原有开源许可、作者和第三方声明保留。

## 2026-10-24 / preview.9：新书工作流节点详情精简

由 TaleWeft 维护方修改。修复新书 DAG 节点默认铺开全部字段扫描、重复展示同一检查点报告，以及隐藏区域仍渲染大量原始数据的问题。默认展示扫描汇总和有问题/未完成字段，全部字段每页 8 项；语义问题、偏离问题、命中、历史和调用记录分页。候选、回执和原始证据按需加载；相同报告去重，不同版本保留，失败检查点报告自动展开。完整扫描、保存门禁、语义审稿及已保存数据均保留。

主要修改范围：`frontend/src/components/onboarding/SetupDAGAuditReport.vue`、`SetupDAGNodeDetail.vue`、新增 `SetupDetailDisclosure.vue` 和 `SetupReportList.vue`、前端回归测试及版本元数据。详见 `docs/RELEASE_0.1.0-preview.9.md`。

- 2026-10-27：停止状态的宏观规划恢复结果持久保存，显式启动后复用落库；手动批准与重试采用原子状态比较和执行租约保护，保留原上游归属。
