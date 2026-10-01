# Vendored assets

This directory contains third-party browser assets vendored (checked into the
repo) so `render.py` can produce a single self-contained HTML file that
displays correctly offline. None of these files are modified from upstream.

## alphatab.min.js

- **Project**: [alphaTab](https://www.alphatab.net/) — a cross-platform music
  notation and guitar tab rendering + playback engine.
  Source: <https://github.com/CoderLine/alphaTab>
- **Package**: `@coderline/alphatab` on npm
- **Version vendored**: `1.8.4` (the `latest` dist-tag on npm as of 2026-07-10;
  confirmed via `https://data.jsdelivr.com/v1/packages/npm/@coderline/alphatab`)
- **Fetched from**:
  `https://cdn.jsdelivr.net/npm/@coderline/alphatab@1.8.4/dist/alphaTab.min.js`
  (upstream file name is `alphaTab.min.js`; renamed to `alphatab.min.js` here
  only as our local storage convention — content is byte-for-byte identical)
- **SHA-256**: `2d0335501b875453d52359de23cd9cebfcf71aed3d5739f1cf95117acfd52bec`
  (also verified against jsDelivr's published integrity hash for this exact
  version/file, `sha256-LQM1UBuHVFPVI1neI82c6/z3Gu09Vznxz5URes/VK+w=`)
- **Size**: 1,120,736 bytes
- **License**: MPL-2.0 (Mozilla Public License 2.0)
  — full text: <https://github.com/CoderLine/alphaTab/blob/v1.8.4/LICENSE>
  — SPDX: <https://spdx.org/licenses/MPL-2.0.html>
- **Copyright**: © Daniel Kuschny and Contributors

### Why a link + license name is sufficient here (MPL-2.0 §3.2 compliance note)

MPL-2.0 requires that anyone who redistributes Covered Software in Executable
Form (which this minified build is) make the corresponding Source Code Form
available and give a notice of where to get it. We rely on a link rather than
re-vendoring the full source tree because:

1. **The file's own header already carries the required notice, unmodified.**
   The first ~50 lines of `alphatab.min.js` are an untouched comment banner
   reading *"This Source Code Form is subject to the terms of the Mozilla
   Public License, v. 2.0. If a copy of the MPL was not distributed with this
   file, You can obtain one at http://mozilla.org/MPL/2.0/."*, plus the
   copyright line and a list of bundled third-party libraries and their own
   licenses (TinySoundFont/SFZero/Haxe stdlib/SharpZipLib — MIT; NVorbis —
   MIT; libvorbis — BSD-3-Clause). We never strip or alter this banner, so the
   notice travels with the file wherever it goes, satisfying §3.3 (no removal
   of license notices).
2. **The exact corresponding source is permanently, publicly available at an
   immutable reference**: npm package versions are immutable once published,
   and the matching tag `v1.8.4` exists in the public GitHub repository
   (<https://github.com/CoderLine/alphaTab/tree/v1.8.4>). This satisfies the
   "how to get the Source Code Form" disclosure without us needing to mirror
   the whole source tree in this repo.
3. **We made zero modifications** to the file, so none of the "describe what
   you changed" obligations that apply to *modified* Covered Software (§3.3
   (b)/(c)) apply.

Given (1)-(3), a link + exact version/hash + license name is a complete and
verifiable substitute for re-hosting the full source and license text, and is
consistent with how the vast majority of the JS ecosystem consumes vendored
MPL-2.0 builds.

### Runtime CDN references (not vendored, fetched only when online)

`render.py` also points alphaTab, at *runtime*, to two more files from
the same pinned version/CDN — these are **not** copied into this repo; the
generated HTML only ever fetches them over the network, and only if the
viewer's machine is online:

- SoundFont (audio sample data for playback):
  `https://cdn.jsdelivr.net/npm/@coderline/alphatab@1.8.4/dist/soundfont/sonivox.sf3`
  Licensed Apache-2.0, © Sonic Network Inc. (SONiVOX EAS, via the Android Open
  Source Project). See
  `https://cdn.jsdelivr.net/npm/@coderline/alphatab@1.8.4/dist/soundfont/LICENSE`.
  `.sf3` (compressed SoundFont) was chosen over `.sf2` to keep the download
  ~30% smaller (977,208 vs 1,351,896 bytes) since it is only ever fetched
  on-demand, never embedded.
- The pinned copy of `alphatab.min.js` itself, referenced via
  `core.scriptFile` purely so alphaTab *can* spin up its background
  Web Worker / AudioWorklet when a network is available. The already-inlined
  copy in the page is a byte-identical fallback used to build a same-content
  `blob:` URL, so display rendering never depends on this succeeding (see
  `viewer.html.j2` comments).

## Bravura.woff2

alphaTab does not embed its music notation glyphs as vector data — it draws
them as text using the SMuFL-compliant **Bravura** font, loaded as a regular
web font. Without a font source configured, alphaTab fails outright with
`[AlphaTab][Font] Loading Failed, rendering cannot start` (confirmed by
testing the vendored build in a real browser with no font configured). Since
the default lookup (a `font/` folder "beside the script file") only works
when alphaTab is loaded via `<script src="...">`, and our HTML inlines the
script instead, we vendor the font ourselves and embed it as a base64
`data:` URI via `Settings.core.smuflFontSources`, so notation renders
correctly with zero network access.

- **Font**: Bravura, by Steinberg Media Technologies GmbH
  Source: <https://github.com/steinbergmedia/bravura>
- **Version vendored**: bundled with `@coderline/alphatab@1.8.4`
- **Fetched from**:
  `https://cdn.jsdelivr.net/npm/@coderline/alphatab@1.8.4/dist/font/Bravura.woff2`
- **SHA-256**: `181e0e7c4889f9ad57dde0a11988fa61b941617aa499ecdb9dfd4713896c2b19`
  (matches jsDelivr's published hash `sha256-GB4OfEiJ+a1X3eChGYj6YblBYXqkmezbnf1HE4lsKxk=`)
- **Size**: 313,348 bytes (~418 KB once base64-encoded into the page)
- **License**: SIL Open Font License 1.1 — full text vendored verbatim as
  [`Bravura-OFL.txt`](./Bravura-OFL.txt) in this directory (same file
  alphaTab itself ships), also at <https://scripts.sil.org/OFL>
- **Copyright**: © 2015, Steinberg Media Technologies GmbH, with Reserved
  Font Name "Bravura"

Only the `.woff2` variant is vendored (not `.woff`/`.otf`/`.eot`/`.svg`):
WOFF2 has been supported by every evergreen browser (Chrome/Firefox/Safari/
Edge) since 2016+, and shipping one format keeps the generated HTML smaller.

## Bravura-OFL.txt

Verbatim copy of the SIL Open Font License 1.1 text distributed alongside
Bravura in the alphaTab npm package (same source/version as above). Kept in
full (rather than just linked) because we redistribute the actual font binary
embedded in generated HTML files, and OFL redistribution practice is to keep
the license text bundled with the font.

## Re-vendoring / upgrading

To bump the vendored alphaTab version:

1. Check the current stable (non-alpha) tag: `curl -s https://data.jsdelivr.com/v1/packages/npm/@coderline/alphatab | python3 -m json.tool` (look at `tags.latest`).
2. `curl -sL -o alphatab.min.js "https://cdn.jsdelivr.net/npm/@coderline/alphatab@<version>/dist/alphaTab.min.js"`
3. `curl -sL -o Bravura.woff2 "https://cdn.jsdelivr.net/npm/@coderline/alphatab@<version>/dist/font/Bravura.woff2"` (re-fetch only if it changed; check its hash against the jsDelivr package listing)
4. Update the version/hash/size values in this file and in
   `render.py`'s `ALPHATAB_VERSION` constant (which also drives the
   pinned soundfont CDN URL).
5. Re-run `uv run pytest -q`.
