"""EPUB metadata and Beautiful Soup XML builders are discovered dynamically."""
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = collect_all('epublib')
datas += copy_metadata('epublib')
hiddenimports += ['bs4.builder._lxml']
