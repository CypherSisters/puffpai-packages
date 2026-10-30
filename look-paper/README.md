# Paper

A look for the PuffPAI Desktop (kind `look`, D76). Paper: warm cream paper, dark ink and one coral accent, with serif headings and small corners. A dark sepia version at night. No glow, no clouds.

It is values only: CSS variables the Desktop already reads, in `puffpai.json`. No CSS file, no script, no font file, nothing loaded from the internet. Add it from Apps, PuffPAI store, then pick it on Looks.

`preview.svg` shows it at night (left) and by day (right).

## PuffPAI

**What it does.** A `kind: look` package: colours, corners and fonts for the Desktop, one set for night and one for day. The Desktop switches between them by itself at 07:00 and 19:00, or follows your Day or night choice.

**What it needs.** Nothing. It runs nothing, opens no port and uses no internet.

**Setup steps.** None. Add it from Apps, PuffPAI store, say Yes, then pick it on Looks.

**Files.** `puffpai.json` (the values, under `look`), `preview.svg` (night on the left, day on the right), this README.

**Rough edges.** The serif headings use a font already on your computer (Iowan Old Style, Palatino Linotype or Georgia); without any of them they fall back to a plain serif.

## Changes in 1.1.0

- At night the coral is a little brighter and buttons use dark ink on it, so button text and the coral both read more clearly on the sepia ground.
- Sets the Desktop's "no frame inside a panel" value itself (flat, as before) instead of relying on the Desktop's default.
- `preview.svg` now shows the real colours (the coral and the faint text had drifted).
