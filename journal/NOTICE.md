# Notice

Journal is by Cypher Sisters, under the Apache-2.0 licence. It was built with help from these works.

## Code

- **snarkdown**, MIT licence. https://github.com/developit/snarkdown
  The Markdown renderer in `web/index.html` is adapted from `src/index.js`. Changes: it is a plain script (no module), the entry text is escaped before it is read so no tag in an entry can reach the page, links keep their address only for http and https, and pictures show as their words instead of loading. The page then keeps only a short list of tags and attributes from what the renderer makes.

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

## Ideas (no code taken)

- **Memos**, MIT licence. https://github.com/usememos/memos
  Ideas: inline #tags in the text, a calendar with a mark on days that have notes, plain Markdown notes.
- **DailyTxT**, MIT licence. https://github.com/PhiTux/DailyTxT
  Ideas: one entry view per day, a calendar to move between days, looking back at the same day in earlier years, search across all entries.

## Import shapes

- **jrnl**'s JSON export, as described in its documentation (`jrnl --export json`): entries with title, body, date, time, tags and starred. https://jrnl.sh/en/stable/formats/
- **Day One**'s JSON export: entries with creationDate, text, tags and starred. Day One does not publish a full description of this file; the fields were taken from what open converter tools read, so Journal reads them carefully and skips what it can not read. https://dayoneapp.com/
