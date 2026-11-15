# Notice

Notes is by Cypher Sisters, under the Apache-2.0 licence. It was built with help from these works.

## Code

- **snarkdown**, MIT licence. https://github.com/developit/snarkdown
  The Markdown renderer in `web/index.html` is adapted from `src/index.js`, taken as it is from this project's Journal package. Changes: it is a plain script (no module), the note text is escaped before it is read so no tag in a note can reach the page, links keep their address only for http and https, and pictures show as their words instead of loading. The page then keeps only a short list of tags and attributes from what the renderer makes. Checklist lines (- [ ] and - [x]) are drawn as tick boxes by the page's own code, around the renderer.

  MIT licence text for snarkdown:

  > The MIT License (MIT)
  >
  > Copyright (c) 2017 Jason Miller
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## File shapes (no code taken)

- **Markdown files with a front matter block** at the top (lines of `key: value` between two `---` lines), the shape many open note tools and static site tools read and write. Notes reads the keys id, title, created (or date), updated, tags, pinned and notebook, and leaves the rest of the file as the note's text.
- **Task list lines** (`- [ ]` and `- [x]`), as written in many Markdown tools.

## Import and export caps

The zip caps (size, number of files, unpacked size) and the refusal of unsafe paths follow this project's own My Papers package.
