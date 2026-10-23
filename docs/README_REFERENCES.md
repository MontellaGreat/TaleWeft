# README 结构参考记录

参考日期：2026-10-22。本次实际读取以下 GitHub 热门项目的公开 README，借鉴信息结构与说明方式；叙织的文案、SVG 和截图按自身源码重新制作，没有复制其品牌、截图、图表、性能结论或项目数据。

| 项目 | 读取来源 | 借鉴方式 |
| --- | --- | --- |
| Dify | [README](https://github.com/langgenius/dify/blob/main/README.md) | 首屏产品定位和导航；把快速开始、主要能力、进阶文档分开 |
| Open WebUI | [README](https://github.com/open-webui/open-webui/blob/main/README.md) | 用界面截图解释产品；按用户任务列能力，再给安装与故障帮助 |
| uv | [README](https://github.com/astral-sh/uv/blob/main/README.md) | 简短价值说明、可执行安装命令、独立功能章节与清晰文档链接 |

## 叙织采用的组织方式

产品定位 → 真实作者书桌截图 → 功能思维导图与功能表 → 快速开始 → 创作与编辑流程 → 上游差异 → 数据迁移 → 开发验证 → 文档与许可。

本版增加了三个针对小说软件的说明：分级分类与自填、去 AI 味策略的实际行为、修改正文后的关联重算。对照图将继承能力、新增、修复、优化与待完成项分开，避免把上游已有能力说成本版原创。

仓库为 [MontellaGreat/TaleWeft](https://github.com/MontellaGreat/TaleWeft)，下载链接指向本仓库 Releases。没有填充未经核实的 Stars、CI、下载量或在线演示徽章，也没有把上游下载地址用作本版安装包地址。

## 可核对的读取记录

来源为各仓库 `main` 分支的 `README.md`，分支后续可能变化。读取内容的 SHA-256 如下，仅用于核对本次参考版本，不代表当前在线文件未变化：

| 项目 | SHA-256 |
| --- | --- |
| Dify | `3c7fe487f1e89e6faec52f5421c71e749c1cecc3486725be3e03fb3fbb62e484` |
| Open WebUI | `da3d46f4d7e6c158e13ed91e5e46250d22d278b972d8d1b028f5e0bb62be612b` |
| uv | `57a84a5a635f30e3a26541ebc16ca690944a7750f05713b1af92d3170b58f704` |

原文下载仅保存在项目内本地参考目录 `out/readme-refresh/references/`，不作为叙织文档资产提交。

## 中英文切换

2026-10-23 再次读取 Dify 的公开 README，参考其顶部语言链接方式。叙织保留根目录 `README.md` 为默认中文首页，增加 `README.en.md` 英文版，两版顶部互相链接。英文版配套三张英文 SVG 图示，实际界面截图保持原样，并明确详细指南当前使用中文。仅参考语言导航方式，未复制 Dify 的文案与品牌素材。
