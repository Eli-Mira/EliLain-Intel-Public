# Third-party assets and licensing

This repository is licensed under the MIT Licence (see `LICENSE`). **The MIT
Licence covers source code only.** It does not cover, and must not be read as
covering, any third-party artwork, font, dataset or other media asset committed or
embedded in this project.

## Embedded artwork

| Asset | Where | Origin | Licence status |
|---|---|---|---|
| Lain portrait (JPEG, SHA-256 `4f928317acb16f4ab30488a83d34410ce78c884797fef2c00dfcada3163f6135`) | `src/tracker-core/tracker_core/branding.py`, rendered into the public dashboard | Supplied directly by the repository owner as personal avatar/branding | **Third-party artwork. Not MIT-licensed. Rights remain with the respective rights-holder.** |

`Serial Experiments Lain` is a copyrighted character. The image is used as
decorative personal branding, is **not** an official asset, and the repository is
not affiliated with or endorsed by any rights-holder.

The asset is embedded as a digest-bound constant so the exporter can accept exactly
one known image and reject arbitrary data URIs, remote images, SVG, `srcset` and
event handlers. Its provenance is stated in the generated page footer as well.

## If you fork or redistribute this project

You must not assume the MIT Licence grants you rights to this image. Either:

1. **Replace it** — change `AVATAR_SHA256`, `AVATAR_BYTES` and the base64 payload in
   `branding.py` with an image you own or are licensed to use, and update the
   provenance note in `docs/public-dashboard.md`; or
2. **Remove it** — an image-free artifact keeps the stricter `img-src 'none'`
   policy and remains fully valid for export, hosting and delivery; or
3. **Keep it** — accept that you are redistributing third-party artwork under your
   own authority, and clear that separately. This project's licence cannot grant
   that permission.

Verify your substitution locally before publishing:

```bash
python scripts/build_project_ui_artifacts.py "$TMPDIR/dashboard"
python scripts/verify_project_ui_browser.py "$TMPDIR/dashboard/public/index.html"
```

## Dependencies

Python dependencies are pinned in `requirements-lock.txt` and carry their own
upstream licences. `docs/fully-free-summarization.md` records the licence status of
the locally used model runtime; model weights are licensed separately from the
runtime and from this code.