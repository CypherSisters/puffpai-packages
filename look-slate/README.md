# Slate

A look for the PuffPAI Desktop (kind `look`, D76). Slate: calm blue-grey surfaces, a teal accent and soft small corners, in Nunito. Darker slate at night, light grey by day. No glow, no clouds, a little less rounded than Puff.

It is values only: CSS variables the Desktop already reads, in `puffpai.json`. No CSS file, no script, no font file, nothing loaded from the internet. Add it from Apps, PuffPAI store, then pick it on Looks.

`preview.svg` shows it at night (left) and by day (right).

## PuffPAI

**What it does.** A `kind: look` package: colours, corners and fonts for the Desktop, one set for night and one for day. The Desktop switches between them by itself at 07:00 and 19:00, or follows your Day or night choice.

**What it needs.** Nothing. It runs nothing, opens no port and uses no internet.

**Setup steps.** None. Add it from Apps, PuffPAI store, say Yes, then pick it on Looks.

**Files.** `puffpai.json` (the values, under `look`), `preview.svg` (night on the left, day on the right), this README.

**Rough edges.** None known.

## Changes in 1.1.0

- At night the teal is a little brighter and buttons use dark text on it, so button text and the teal both read more clearly on the slate ground.
- Sets the Desktop's "no frame inside a panel" value itself (flat, as before) instead of relying on the Desktop's default.
- `preview.svg` now shows the real colours (the teal and the faint text had drifted).
