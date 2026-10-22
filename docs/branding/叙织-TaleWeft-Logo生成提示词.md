# 叙织 · TaleWeft：1:1 Logo 生成提示词

本文件为 2026-10-22 新增的独立品牌设计说明。产品名称为“叙织 · TaleWeft”，口号为“编织故事，让灵感成章”，产品说明为“面向长篇小说的 AI 创作工作台”。

当前产品已使用用户提供的正式「织页书标」。以下提示词保留为设计参考；接入说明已更新为当前实际资源流程。

## 推荐：书页与经纬线

直接复制下面整段。先生成纯图形，文字在后期排版，适合桌面图标、启动页与 GitHub 头像。

```text
为独立长篇小说创作软件“叙织 · TaleWeft”设计一个原创品牌标志。

画布为严格的 1:1 正方形，2048×2048 像素。只生成一个居中的纯图形 Logo，不包含中文、英文、字母、数字、口号或水印。

核心概念：故事像经纬线一样交织成书。将“打开的书页”和“两条有清晰穿插关系的叙事丝线”融合成一个简洁、独特、整体连贯的几何符号。书页不要写实，丝线不要像毛线球；通过少量粗线和明确留白表达交织关系，图案在 24×24 像素时仍然容易辨认。优先保证外轮廓清楚，避免密集细线与装饰。

整体气质：安静、理性、有文学感，有创作工具的可靠感。现代编辑出版品牌风格，平面矢量观感，轮廓克制，适当柔和的转角，视觉重量平衡。

配色：深墨青 #142E34、暖纸白 #F4E9D5、少量哑光金 #D8AE68，最多三种颜色。纯色填充，不用渐变、光效、阴影或金属质感。

背景透明。主体位于画布中央，四周保留约 15% 安全留白，不画外部展示框，不提供多款拼图，只输出单个完整标志。需要适应浅色和深色软件界面。全新设计，不模仿已有软件、出版社或 AI 产品标志。
```

英文版本：

```text
Design one original icon-only brand mark for TaleWeft, an independent long-form fiction writing workspace. Strict square 1:1 canvas, 2048 by 2048 pixels, transparent background.

Fuse the silhouette of an open book with two interwoven narrative ribbons into a single coherent geometric symbol. Show a clear over-and-under crossing using negative space. Use bold, simple shapes and generous counters so the mark remains recognizable at 24 pixels. Avoid detailed pages, dense threads, yarn balls, or literal illustrations.

Quiet literary character, thoughtful editorial design, balanced visual weight, softly rounded corners, flat vector appearance. Use no more than three solid colors: deep ink teal #142E34, warm paper #F4E9D5, muted gold #D8AE68. Center one mark with about 15 percent clear space on all sides. No gradients, shadows, glow, 3D, mockups, frames, text, letters, numbers, watermarks, or multiple variations in the same image. Original design with no resemblance to existing brand logos.
```

## 统一负面提示词

支持负面提示词的模型可单独粘贴；其他模型可接在主提示词后面。

```text
文字，中文乱码，英文字母，数字，水印，签名，机器人，大脑，电路，芯片，魔法棒，羽毛笔，船舵，罗盘，飞机，毛线球，复杂花纹，过细线条，密集网格，写实书本，摄影，人物，3D，浮雕，金属，玻璃，霓虹，发光，投影，渐变，样机展示，透视场景，多款拼图，边缘裁切，模仿现有商标
```

## 两种备选方向

每次只使用一个方向，替换主提示词中的“核心概念”段落即可。

**方向二：抽象叙事织结**

```text
用两条宽窄一致的带状线组成一个清晰的交织节点，形成带有书脊与书页联想的抽象符号。通过留白表达章节之间的连接和故事的层次。整体保持开放，不要完全封闭成绳结，不画无穷大符号，不画编织纹样背景。小尺寸仍需一眼分辨轮廓。
```

**方向三：书页生长**

```text
以两片展开的抽象书页为基础，内侧的一条叙事线自然穿插并向上延伸，表达灵感逐渐成章。避免植物、树叶、火焰和翅膀的直白图案。只保留书页与一处交织关系，采用平面几何图形和明确留白。
```

## 选定图案后的适配提示词

黑白版本：

```text
严格保持参考图标的外轮廓、结构、线宽与交织关系，制作纯黑图形、透明背景的单色版本。仅用黑色与透明留白，不用灰阶。确保交叉处不会粘连，图案缩至 24 像素仍然清楚。1:1，2048×2048，不添加文字或装饰。
```

桌面图标底板版本：

```text
保持参考 Logo 图形完全一致。使用深墨青 #142E34 的纯色圆角正方形底板，内部图形为暖纸白 #F4E9D5，交织处只保留少量哑光金 #D8AE68。图形居中，四周安全留白不少于 15%，无阴影、渐变、发光或文字。1:1，2048×2048，输出单个正视图标。
```

## 导出和接入

- 保留原始 2048 方图、透明 PNG、单色版。有矢量工具时重新描绘为 SVG；生成图里的矢量风格不等于真实 SVG。
- 产品名使用“叙织 · TaleWeft”。需要包含名称的横向组合标时，在图形旁后期排版，不让生成模型绘制文字。
- 正式原图保存在 [`frontend/branding-source/woven-book-original.png`](../../frontend/branding-source/woven-book-original.png)。当前 `frontend/public/taleweft-mark.svg` 和 `favicon.svg` 是嵌入等比缩放 PNG 的包装文件，并非占位图或矢量描摹。
- 需要更换原图时，在项目根用已安装 Pillow 的 Python 执行 `python frontend/scripts/generate-branding.py`，再运行 `node frontend/scripts/sync-branding.mjs` 与 `node frontend/scripts/sync-branding.mjs --check`。生成资源和 SHA-256 清单位于 `frontend/branding-source/`，同步覆盖网页和桌面所需尺寸。
- 当前接入保留正式原图透明度与柔光，不裁剪、不抠图、不重新绘制。网页、作者书桌、关于页和 README 复用同一书标；界面与安装器均需重新构建才会包含更新。

## 原推广文案的替换

主展示：**叙织 · TaleWeft｜编织故事，让灵感成章**

副说明：**面向长篇小说的 AI 创作工作台**

关于页补充：**创作由你主导，AI 协助组织与推演**

上游来源、原作者版权和贡献者归属放在“许可与致谢”，不作为新品牌的团队宣传文案。
