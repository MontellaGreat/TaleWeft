# lxml Windows native provenance

## What the release contains

The Windows runtime lock contains lxml 6.1.3. Its `lxml.etree` extension reports libxml2 2.11.9, libxslt 1.1.45 and the `iconv` feature. The locked wheel's extensions are copied unchanged into the frozen backend: the seven `.pyd` files in the table below were measured in the frozen directory and match the packaging virtual environment byte-for-byte. This fingerprints the shipped extension files; it does not establish that this project rebuilt them from source.

## Upstream provenance

The selected upstream recipe is `lxml/libxml2-win-binaries`, tag `2026.10.05` (annotated tag object `300d69d1031f3c530cfb0e5bda02d80049b6e9e6`, peeled commit `bed89b4f0819fa3a7bf456d55780ee7a4ebb325e`). It publishes the matching Windows assets below. The source archives are also included at the paths and commits recorded in `inventory.json`.

| win64 asset | SHA-256 |
|---|---|
| iconv-1.19.win64.zip | `4a92826a127789941ce920eeb6cc4289e40e241bf2bcab3067b00246dce608b5` |
| libxml2-2.11.9.win64.zip | `86dacde1187a9bdf46d12649ba1f7bcd00a041dd60e9461b033ea8546ee4d932` |
| libxslt-1.1.45.win64.zip | `32764ee260d1b5991afd2d1c49b133de31cebbc49c839e49316f029795043534` |
| zlib-1.3.2.win64.zip | `f78a02794a598afbe09ad7523be69dbd019665626562af798bf0ce971262ffb3` |

Release: https://github.com/lxml/libxml2-win-binaries/releases/tag/2026.10.05

The four bundled winlibs source archives correspond to these commits:

| component | source commit |
|---|---|
| libiconv | `accac417318a3eef7402685a268cab8037e62ba1` |
| libxml2 | `bb84678855e3407fc88180295c992f9c984e28e0` |
| libxslt | `c0076eaa6e0d1f99e01107e8db225a308eb1e543` |
| zlib | `0089522e62e6b5d4950e62bd20f0cb06a9288413` |

## Frozen lxml extension fingerprints

| extension | SHA-256 |
|---|---|
| `_elementpath.cp314-win_amd64.pyd` | `222be8036ba2f3bea485a4f68b8f8f484ccd7197d96fd60518ab778854c3151a` |
| `builder.cp314-win_amd64.pyd` | `5af41a278c7a9294bf34ddac3840b3ed40ed4f7e201d9a8c8e8c4a5e504ee1a4` |
| `etree.cp314-win_amd64.pyd` | `191eee78b34979c31ba08d952ca3cafabafe2f86d81adef19f0bcd4ad2ea53c9` |
| `objectify.cp314-win_amd64.pyd` | `6e7e0479add68e3c3764efcf72bd0d0660b13868c07755f72515246340687961` |
| `sax.cp314-win_amd64.pyd` | `9c48b8acb2c073ff3b79dfdbd80b0f1b60da99579f15fd61b57f8ebc09d74280` |
| `html/_difflib.cp314-win_amd64.pyd` | `5f5a5731a9ae72b156153dac0421ccb8244df0a28727ebe3b3b6bdd83467ce5c` |
| `html/diff.cp314-win_amd64.pyd` | `33d8838f47cb45b6d32b671851c724c71311d171a6ed4cd05de9b97a43b504d3` |

## Relinking guidance and verification boundary

1. Unpack the lxml 6.1.3 source distribution and the four winlibs source archives from `inventory.json`; retain their license files and notices.
2. Follow the pinned upstream Windows build recipe and its `build.ps1`, `.gitmodules`, `libiconv.patch` and CI workflow included under `sources/native/lxml-winlibs-2026.10.05/`. Build the native libraries from the included source commits, including libxml2 with iconv and zlib support.
3. Build lxml 6.1.3 against those source-built libraries, produce a replacement wheel, and replace the seven corresponding extensions in the PyInstaller environment before rebuilding the backend and installer.
4. Verify the replacement with the import/version checks in the lxml source tree, compare the resulting extension hashes, and exercise EPUB import/export and installer startup. Keep the modified application relinkable by distributing the replacement extension or a documented rebuild path with the corresponding object files/source as required by the applicable license terms.

For this preview, upstream release metadata, source archive commits and hashes, and frozen extension hashes have been recorded. **The native libraries and lxml extensions were not locally rebuilt from these source archives, and the replacement/relink procedure has not been rehearsed against the installer.** Do not interpret the provenance records as a successful local LGPL relink test or a legal opinion. This verification limitation remains open for a later release.
