"""Resize the user-supplied TaleWeft artwork without cropping or retouching."""
from pathlib import Path
from io import BytesIO
import base64
import hashlib
import json
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'frontend/branding-source'
art = Image.open(SOURCE / 'woven-book-original.png').convert('RGBA')
outputs = {}

def emit(target, data):
    destination = SOURCE / 'generated' / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    outputs[target] = hashlib.sha256(data).hexdigest()

def png(size):
    buffer = BytesIO()
    art.resize((size, size), Image.Resampling.LANCZOS).save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()

embedded = base64.b64encode(png(512)).decode('ascii')
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">'
       '<title>叙织 TaleWeft</title><image width="512" height="512" href="data:image/png;base64,' + embedded + '"/></svg>').encode('utf-8')
for name in ('taleweft-mark.svg', 'favicon.svg'):
    emit('frontend/public/' + name, svg)
emit('frontend/public/favicon.png', png(128))
icons = ROOT / 'frontend/src-tauri/icons'
for target in sorted(icons.rglob('*.png')):
    size = Image.open(target).size
    buffer = BytesIO()
    art.resize(size, Image.Resampling.LANCZOS).save(buffer, format='PNG', optimize=True)
    emit(target.relative_to(ROOT).as_posix(), buffer.getvalue())
for extension in ('ico', 'icns'):
    buffer = BytesIO()
    image = art.resize((1024, 1024), Image.Resampling.LANCZOS)
    options = {'sizes': [(n,n) for n in (16,24,32,48,64,128,256)]} if extension == 'ico' else {}
    image.save(buffer, format=extension.upper(), **options)
    emit('frontend/src-tauri/icons/icon.' + extension, buffer.getvalue())
(SOURCE / 'manifest.json').write_text(json.dumps({'source_sha256': hashlib.sha256((SOURCE / 'woven-book-original.png').read_bytes()).hexdigest(), 'outputs': outputs}, indent=2) + '\n', encoding='utf-8')
print(f'Generated {len(outputs)} branding resources; source artwork unchanged.')
