# Maps

See your novel as an illustrated atlas. Maps takes what Manuscript has already read about your book, the story bible, and lays it out as a book of pictures: the cast as portraits, the world as a painted map with its places pinned on it, the family tree with faces, the story as a strip of painted scenes, and a cover mock. Click anything and the manuscript pane opens that passage. Maps never reads your manuscript text and never changes it.

The pictures are painted by a picture program on another computer on your home network. A picture program cannot be trusted with words or structure, so it paints only the pictures; Maps puts every name, pin, label and line on top with code. Your bot never paints them.

## PuffPAI

### The atlas

Open Maps beside Manuscript and pick a manuscript your bot has read. Its page shows, top to bottom:

| Part | What you see |
|---|---|
| Cast | a portrait for each character, with the name, role and importance under it (main, supporting or minor). Click one for the character's page: the portrait large, the write-up, who they are tied to (with their portraits), the sections they are in, the places they are seen, and See in Manuscript |
| World | the painted world map filling the board, each place pinned on it with its name on a label. Pins start where the drawn world map puts them; drag them where they belong and Save layout. Click a pin for the place's page: its own painting, its notes, who is there, what happens there |
| Family | the family tree, drawn by code, with each person's portrait in their circle above the name |
| Story | up to twelve key events, each as a card with its painted scene, the event's words as the caption and a button for its section, side by side under a timeline line; scroll sideways |
| Cover | a cover mock: the painted cover with the title put on by code, the one-line summary and the themes |
| Charts | folded: the eight drawn boards, every picture made for the book, and the Draw box (see Charts below) |

Everything shows before any picture exists. A picture not painted yet is a soft card with the name and a Paint button, and a spinner while it is being painted. Without a picture maker set up, each card says "No picture maker yet" with a button to the Picture maker box, and the world shows the drawn schematic instead of the painting.

### The proof under each picture

Under every picture is its proof: the writing that told the picture program what to draw, one line per character or place, like "Odrey Varrow: grey hair pulled back (section 1), a burn on her left hand (section 1)". Click a "(section n)" and the manuscript pane opens that section at those words. Under the painted world map the same line gives the distances and directions the text tells ("The salt flats is a day's walk on the tether line from Brine Station (section 2)"), and a thin dashed line joins the two places on the map with the text's words as its label. A small fold, "What the painter was told", shows the whole description and the style line. A book read before Manuscript kept looks and sections says so under its pictures, "Painted from the role and traits", and lists them; read the manuscript again in Manuscript to get details with sections. When your bot paints a picture or starts Illustrate, its answer ends with one line of that proof, so it can tell you what the picture was drawn from.

Every picture has Paint again with a small note field: type what you want different, like "older, grey hair", and the note joins the description. A character keeps one seed, and so does each place, so a repaint keeps the same face unless your note asks for a new one ("a different face"). Paint again with no note paints a new variation, and that face is the one kept from then on.

### Illustrate

One button on each manuscript's page, and you can ask your bot ("illustrate Salt Moon"). It paints what the book needs, one picture at a time: a portrait of every main and supporting character (tick "minor characters too" for the rest), every place, the world map, up to twelve key events and the cover. The key events are spread across the sections, preferring the ones with more characters in them. A line on the page says how far it is, like "Painting 7 of 23: Odrey Varrow. About 12 minutes left.", timed from how long your picture program has taken so far.

A picture already painted for the same description is skipped, so Illustrate again paints only what changed: a new character, a place with new notes, a new style line. If Maps is restarted in the middle, Illustrate again paints the rest. Your notes from Paint again stay with each picture.

### Storyboard panels (2.2)

When you press "Have Wren paint these" on the Storyboard in Editor's Desk, Editor's Desk leaves the panels in `storyboard.json` in the book's folder. While Maps runs it looks there every few seconds and paints each panel once, at a scene's wide size, from the panel's notes only (what we see, the framing, the place and the mood, with names swapped for plain descriptions and the book's style line at the end). Your own words never go. A new press paints them again; an older file, or a second look, paints nothing. Without a picture maker set up Maps does nothing with them, and Editor's Desk says so. The pictures show in Editor's Desk and under All pictures here, as kind `panel`, with Retry for one that failed.

### Style

Each manuscript has one style line, shown on its page under Illustrate, that ends every picture's description, so the cast and the world look like one book. It starts as the picture maker's style ("painterly, soft light, book illustration" to begin with); change it for one book on that book's page, or clear it to go back to the picture maker's.

### Charts

Under Charts are the eight boards drawn by code, as before. Each opens fitted to the pane and keeps your zoom while the page is open: scroll the wheel to zoom, drag empty space to move around, Fit to see all of it, and Export PNG to save a picture of the whole board to your downloads. Labels on the lines hide when you zoom out below 60 percent and come back when you zoom in.

| Board | Drawn from |
|---|---|
| Family tree | each character's parents, children and partners; one tree per family of two or more |
| Relationship map | each character's relationships; bigger circles are in more sections. Drag them and Save layout |
| Timeline | the events in the order they are told; when the bible gives a story order that differs, a second line in story order with thin lines joining the same event |
| Character touchpoints | a grid of characters by sections, a dot where each one is, a line down a section joining everyone in it |
| Arcs | one curve for each character in four or more sections, a point per beat (hover for the beat), what they want and fear in the key |
| Story structure | the sections along the bottom with bars, and each thread's setups and payoffs as pins above |
| World map | the places, close together when they share a section. A schematic, not geography: drag them where you see them and Save layout. The painted world map uses the same layout |
| Thread board | each thread as a lane across the sections, its setups and payoffs as pins, open threads flagged at the right edge |

Ask your bot to draw something and it picks the board from your words and keeps it under "Drawn from asks", with what you asked. "Show me all the supporting characters and whether any of them interact" draws a relationship map of only the characters who are not the protagonist or the antagonist, with a line wherever two share a section or have a tie, and says which of them never meet anyone. Naming a character in an ask for arcs or touchpoints draws only them.

Click a character, an event, a pin or a bar, and Manuscript opens that section and highlights the name or the words.

All pictures, under Charts, shows every picture made for the book as cards, with what was sent for each, Retry for one that failed, Save to put the file in your downloads, and Remove.

### What your bot can do

| Tool | What it does | Changes |
|---|---|---|
| `maps_list` | Lists the boards for each manuscript, how many pictures there are, and whether an Illustrate is running. | No |
| `maps_draw` | Draws a board from the story bible and keeps it with the ask. | Only its own files |
| `maps_point` | Opens the passage behind one item on a board in Manuscript, by the item's name or id. | Only its own files |
| `maps_picture` | Has the picture program paint a portrait, a place, a world map, a scene from one event or a cover, with an optional note. It answers at once; the picture is ready a few minutes later. | Only its own files |
| `maps_illustrate` | Paints everything a manuscript needs, one at a time, skipping what is already painted. It answers at once with how many it will paint and about how long that takes. | Only its own files |

Things you might say: "illustrate Salt Moon", "paint the scene where the Lantern goes dark", "paint a portrait of Tamsin, older, grey hair", "draw the family tree", "show me who interacts with whom", "show me where Quill is on the touchpoints".

### The picture maker

Maps sends picture jobs to [ComfyUI](https://github.com/comfyanonymous/ComfyUI), the picture program, running on another computer on your home network. In the Picture maker box at the bottom of Maps' list, type its address once, like `http://192.168.1.20:8188`, and press Save. Your bot asks for that one address and port, and a Yes card shows on the Bridge page; nothing reaches it until you say Yes there. Test shows the line for that ask ("Waiting for your Yes on the Bridge page", "Allowed", or why not), then reaches the picture program and lists the models it found and which way Maps would paint with them.

What it needs:

- The picture program running on a computer on your home network, and its address typed once in Maps.
- Your Yes on the Bridge page for that address, and the Bridge open.
- A model there. With FLUX schnell as a GGUF file (and the GGUF loader node, the t5xxl and clip_l text encoders and the ae VAE), Maps uses it in four steps. Otherwise it uses the first checkpoint in the picture program's checkpoints folder (four steps for a "turbo" one, twenty for others). With neither, Test says what is missing.

What travels: only a short description built from the story bible, which you can read on each card under "What was sent" in All pictures. A portrait uses first what the text says the character looks like, every detail in order, and the text's own words on age, then the role, traits and the setting words from the bible (like "moon", "station", "mining"); a place uses what the text says it looks, sounds and smells like, or its kind and notes when the bible has none; a world map lists the places and their kinds, the text's distances and directions between them, and the visible marks of the era (lamps, carts, domes); a scene uses the event's own short line with each name swapped for a plain description ("the curious person"), who is there described the same way with their looks, the place and what it looks like, and time-of-day words like "night" or "supper"; a cover uses the one-line summary and the theme names. Characters' names are taken out. Your note, when you give one, and the style line go at the end. Your manuscript text, and the quotes in the story bible, never go.

### Which files it may change

Your manuscript is never read or changed by Maps. In each manuscript's folder, `My files\Manuscripts\<title>\`, it reads `bible.json`, `views.json`, `boards.json`, `corrections.json` (when there is one), `pointer.json` and `storyboard.json` (Editor's Desk's panels to paint, when there is one), and writes only these:

- `boards.json`, the boards drawn from asks and the places you dragged things to;
- `pointer.json`, which tells Manuscript where to scroll when you click something;
- `pictures.json`, the list of pictures, what was sent for each, and each character's and place's seed (a storyboard panel's picture also says which panel and which press), and `pictures\<id>.png`, the pictures themselves;
- `style.json`, this book's style line, when you set one;
- `illustrate.json`, how far Illustrate got.

### What it needs

- Manuscript, and a manuscript your bot has read. Maps draws from that reading. A bible from Manuscript 0.3 adds each character's importance, personality and write-up to the cast; an older one still gives a cast, with every character in four or more sections (and the protagonist and antagonist) counted as main.
- For the charts, nothing else: no model, no keys, no downloads. It doesn't use the internet.
- For pictures, a picture program on your home network (see The picture maker above).

### Rough edges

Not yet tried on the owner's computer or in the window's side pane: the tests check the atlas's data, the pictures' records and the files, not how the page looks, and the atlas has not been opened in a browser yet. Pictures have run only against a stand-in picture program in the tests, not yet against a real one or through the Bridge; this was written for one setup (FLUX schnell GGUF or a plain checkpoint), and picture programs with other node sets or model names may need a fix. A fixed seed keeps a face only as well as the picture program does: the same seed and a slightly different description usually give a similar face, not always the same one. Labels on the painted map are sized from the number of letters, so a long place name can overlap a close neighbour; drag the pins apart and Save layout. The prompts are simple word lists from the bible, so a portrait knows only the looks the reading found, and a scene shows what its one line says. The dashed distance lines show on the painted map, not on the drawn schematic. The story structure board shows words per section only when the bible carries word counts, and the timeline shows a second story-order line only when the bible gives a story order of its own. Asks are matched on words like "family", "timeline", "touchpoint", "interact", "arc", "structure", "world", "map" and "thread"; an ask with none of them gets a plain line listing the boards. A click points at a name or a summary line, not an exact quote, so Manuscript highlights the closest words it finds. If Manuscript rewrites `boards.json` without Maps' boards in it, they are gone. A picture that is being made when Maps restarts shows as failed, with Retry, and Illustrate again paints it.

### Data

Everything about your books is in your manuscripts' folders in My files. Maps' own data folder keeps one file, `maker.json`: the picture program's address, your style words, the last answer to the ask, and how long the last ten pictures took. On remove they stay.

### How it runs

A small Python server on 127.0.0.1, started by PuffPAI, using only Python's standard library. It serves its page at `/`, answers MCP at `/mcp`, and refuses any call without its PuffPAI token. It asks PuffPAI where My files is each time. Picture jobs go one at a time, through the Bridge's proxy, to the one address you allowed; Maps waits up to ten minutes for each. The page is its own code, with no outside scripts or fonts, and light or dark to match your computer or the Desktop's look.
