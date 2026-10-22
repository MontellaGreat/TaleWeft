# 叙织图标生成

当前图标来自用户提供的正式书标，原始副本为 `frontend/branding-source/woven-book-original.png`。从项目根重新生成并同步：

```powershell
python frontend/scripts/generate-branding.py
node frontend/scripts/sync-branding.mjs
node frontend/scripts/sync-branding.mjs --check
```

需要 Pillow；默认构建使用项目内已生成文件，不依赖 Python。图标仅缩放保留原画与透明度，详见 `frontend/branding-source/README.md`。
