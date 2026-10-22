# 签名插件与文风规则版本

插件签名复用开源 [cryptography](https://github.com/pyca/cryptography) 的 Ed25519 实现（Apache-2.0 或 BSD-3-Clause）。不自写签名算法。仅接收声明式 JSON 数据，不加载插件代码、动态合约模块、文件路径、网络钩子或任意正则。提示词只支持简单变量 `{context}` / `{{ context }}`。

## 作者使用

在设置的“签名插件”中添加发布者标识、公钥及允许内容。先从可信渠道核对 SHA-256 公钥指纹，再选择签名 JSON 包，验证签名和权限，检查提示词/规则数量并安装。包内权限必须准确对应 `prompts` / `quality_rules`。升级时先卸载旧版本，再验证新版本。

提示词存入既有 PromptManager 表，节点命名为 `plugin-包标识-提示词标识`，可在提示广场查看和使用；安装不覆盖作者已有提示词，也不自动替换系统写作提示。质量规则立即参与章末、DAG、候选改写复验和工作流规则扫描。卸载只移除该插件的提示词和规则；移除发布者信任会同时卸载其所有插件。已有审计与规则快照保留。

在“文风规则版本”中选择停用的内置规则，添加字面匹配规则，填写版本说明并保存。每次保存、恢复都创建不可变版本。并发编辑会返回版本冲突，要求刷新。试扫使用已保存的当前规则。每次有效扫描保存精确规则快照哈希；旧报告可由该哈希查询原始规则，而不受后续版本/插件变化影响。

## 制作插件

以下命令在项目根目录运行，输出默认在项目内。私钥仅留在发布者本机，向作者提供公钥文件、指纹与签名包。

```powershell
python scripts/sign_data_plugin.py keygen --directory analysis/plugin-signing
python scripts/sign_data_plugin.py sign --input analysis/plugin-signing/unsigned.json --private-key analysis/plugin-signing/publisher.private.key --output analysis/plugin-signing/style.signed.json
```

未签名输入示例（工具会补字段默认值并规范化签名数据）：

```json
{
  "manifest": {
    "schema_version": 1,
    "id": "concrete-prose",
    "name": "具体叙述提示",
    "version": "1.0.0",
    "signer_id": "writer-studio",
    "permissions": ["prompts", "quality_rules"]
  },
  "payload": {
    "prompts": [{
      "key": "scene",
      "name": "具体场景",
      "category": "generation",
      "system_prompt": "通过具体动作表现人物。参考：{{ context }}",
      "user_template": "展开场景：{outline}"
    }],
    "quality_rules": [{
      "name": "避免套话",
      "text": "命运的齿轮开始转动",
      "severity": "warning",
      "category": "俗套",
      "replacement_hint": "写发生的具体事件"
    }]
  }
}
```

签名范围为完整 `manifest` 与 `payload` 的规范化 UTF-8 JSON：对象键排序、无空格分隔、保留 Unicode、禁止 NaN。`signature` 为64字节签名的 Base64，公钥为32字节原始 Ed25519 公钥的 Base64。安装前与提交时都会检查信任与权限；篡改、未知字段、可执行模板语法、超限包及命名冲突均拒绝且不产生部分安装。

## API

所有端点沿用本地会话认证。前缀 `/api/v1/extensions`。

- `GET /plugins`，`POST /plugins/keys`，`DELETE /plugins/keys/{signer_id}`。
- `POST /plugins/verify`，`POST /plugins/install`（包含验证结果 `expected_digest`），`DELETE /plugins/{plugin_id}`。
- `GET /quality-rules`，`POST /quality-rules`（包含 `expected_version`）。
- `POST /quality-rules/versions/{version_id}/rollback`，`POST /quality-rules/scan`。
- `GET /quality-rules/snapshots/{digest}`。
