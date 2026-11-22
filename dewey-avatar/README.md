# Dewey, the editor's face

Dewey is the editor-in-chief: a woman in her sixties with a silver bob, round tortoiseshell glasses, a coral cardigan
and a kind, sharp look. She is an invented character, not a picture of any real person.

## PuffPAI

**What it does.** A `kind: avatar` package: a face for the bot. The Desktop shows the active avatar in the top bar, on
Home and in Looks; the owner picks it in Looks.

**What it needs.** Nothing. It runs nothing and uses no internet.

**Setup steps.** Add the package, then pick Dewey in Looks.

**Files.**
- `dewey.png`: the portrait, 768 x 768, ink and wash on cream paper. Painted on the owner's own PC with ComfyUI
  (FLUX schnell), from a written description. No photo and no real person went into it.
- `preview.png`: the same picture cut to a round frame, 512 x 512, clear outside the circle.
- `face.html`: the live face. It shows the portrait in a round frame. While the bot answers in chat, a small dark
  opening between her lips opens and closes, on the line of her mouth measured in the picture (corners at 345, 369 and
  423, 368). When she stops, it goes away and her painted smile shows. She nods very slightly.

**How the Desktop drives the face.** The same way as Puff's: the Desktop frames the page with the id `face` and posts
`{"type": "puffpai.avatar", "talking": true | false, "pulse": <characters just streamed>}`. The face reads nothing else
and sends nothing.

**Voice.** None yet. `avatar.voice` is null.

Cypher Sisters. puff puff pass.
