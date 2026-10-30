# Book shelves

Find books in your Calibre library and keep your own shelves of them, by asking your bot.

## PuffPAI

### What it does

| Tool | What it does | Changes |
|---|---|---|
| `set_library` | Sets the Calibre library folder to read (the one that holds `metadata.db`). | Yes |
| `find_books` | Searches by title words, author, series or tag. Up to 20 books, with authors, series, year and formats. | No |
| `book_details` | One book: authors, series, year, formats, tags, its description, and which of your shelves it is on. | No |
| `shelves` | Your shelves, or the books on one shelf. | No |
| `shelf_add` | Puts a book on one of your shelves; makes the shelf if it is new. | Yes |
| `shelf_remove` | Takes a book off a shelf. A shelf that ends up empty is gone. | Yes |
| `recommend` | A few books to read: the newest added, by a tag or an author, not on a shelf called Read or Finished. Plain rules, not the brain. | No |
| `library_stats` | How many books, authors, series and tags, the formats, and the newest book. | No |

Things you might say: "do we have any books by Agatha Christie", "put Dracula on my to-read shelf", "recommend a mystery to read".

### What it needs

- A Calibre library on this computer (Calibre is free: https://calibre-ebook.com/download). Calibre itself does not need to be running.
- No internet, no keys, no brain. About 0.2 GB of memory.

### Setup steps

One step, "Point it at your Calibre library": open Calibre, click the Library button (the books icon) and choose Switch/create library. The folder shown there holds `metadata.db`. Then tell your bot "my Calibre library is in <that folder>". Any local folder works.

### Data

- `shelves.json` in this package's own data folder: the library folder you set and your shelves (each book's Calibre number and title).
- The Calibre library is opened read-only (`mode=ro`) every time. Nothing is ever written to it.
- On remove, PuffPAI asks whether to keep `shelves.json`.

### How it runs

A small Python server (standard library only) on 127.0.0.1, started by PuffPAI. It answers MCP at `/mcp` and refuses any call without its PuffPAI token.

## Rough edges

- Your shelves live in this package, not in Calibre or Calibre-web. Shelves you made in Calibre-web do not show here, and these do not show there. HomeBot wrote Calibre-web's own shelves; this package does not.
- Calibre has no "read" mark of its own, so `recommend` treats books on a shelf called Read, Finished, Done, Have read or Already read as read.
- Shelves remember a book by its Calibre number. Pointing at a different library can make old shelf entries name the wrong book.
- Not built (HomeBot had these): moving a book between shelves in one step, showing a book on a screen in a reader, sorting comics apart, and the household's own category words.
- One library at a time.

Cypher Sisters. puff puff pass.
