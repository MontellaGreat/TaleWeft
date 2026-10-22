"""One release manifest shared by API, frontend and Windows package."""
import json
from pathlib import Path

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / 'release-manifest.json').read_text(encoding='utf-8'))
APP_RELEASE_VERSION = MANIFEST['version']
BACKEND_BUILD_ID = MANIFEST['build_id']
