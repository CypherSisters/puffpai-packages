# High contrast

A look for the PuffPAI Desktop (kind `look`, D76). High contrast: black ground, white text, white lines and a yellow accent at night; white ground, black text and a dark blue accent by day. Square corners and larger text.

It is values only: CSS variables the Desktop already reads, in `puffpai.json`. No CSS file, no script, no font file, nothing loaded from the internet. Add it from Apps, PuffPAI store, then pick it on Looks.

`preview.svg` shows it at night (left) and by day (right).

## PuffPAI

**What it does.** A `kind: look` package: colours, corners and fonts for the Desktop, one set for night and one for day. The Desktop switches between them by itself at 07:00 and 19:00, or follows your Day or night choice.

**What it needs.** Nothing. It runs nothing, opens no port and uses no internet.

**Setup steps.** None. Add it from Apps, PuffPAI store, say Yes, then pick it on Looks.

**Files.** `puffpai.json` (the values, under `look`), `preview.svg` (night on the left, day on the right), this README.

**Rough edges.** The fonts are the ones already on your computer; on a computer without Segoe UI the text falls back to the system font.

## Changes in 1.1.0

- Sets the Desktop's "no frame inside a panel" value itself (flat, as before) instead of relying on the Desktop's default.
- The one-line summary on the store card now says the accent is yellow at night and blue by day.
