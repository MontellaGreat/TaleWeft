# 用户提供的正式书标

`woven-book-original.png` 是 `F:\plotpilot\织页书标.png` 的逐字节副本，原文件未修改。2048×2048，RGBA；角落 alpha=0，原图已有透明度和柔光，不是需要去除的实心黑底。本接入不裁剪、不抠图、不重新绘制，仅使用 Lanczos 等比缩放，保留原有透明度和发光。

重新生成全部尺寸：

```powershell
F:\plotpilot\.ws8-verify-venv\Scripts\python.exe frontend/scripts/generate-branding.py
node frontend/scripts/sync-branding.mjs
node frontend/scripts/sync-branding.mjs --check
```

Python 脚本需要 Pillow。生成的 55 个资源保存在 `generated/`，清单记录 SHA256。常规前端构建仅用现有 Node 同步已生成资源，无需 Python。网页继续沿用 `/taleweft-mark.svg` 和 `/favicon.svg`，SVG 嵌入 512px 压缩 PNG，因此现有生产静态路由无需修改。桌面 PNG / 多分辨率 ICO / ICNS 及已有移动端尺寸同步更新；不会修改 Tauri 打包配置或平台 XML。

书库、侧栏、关于页面及 README 原本已引用相同 mark 路径，自动使用新书标；作者书桌原来的「织」字按钮改为同源书标，行为及可访问名称不变。
