# Document search

Keeps your documents on this computer and searches their words, so the bot can find a document and read it back. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Document search is a PuffPAI tool package (`kind: tool`, no page of its own). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain these MCP tools at `/mcp`:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `add_document` | Keeps a .txt, .md, .csv, .json, .docx, .html or other text file and indexes its words | yes | keep this document so I can search it later |
| `search_documents` | The ten best matches for some words, each with its id and a short snippet | no | what did I save about the roof warranty |
| `read_document` | The full text of one document, by id | no | read me the whole of document 3 |
| `list_documents` | The kept documents, newest first, with their ids | no | what documents have I saved |
| `remove_document` | Removes one document and its words from the index, by id | yes | remove document 4 from my documents |

A file sent from the Desktop or chat ("send this file to Document search") goes to `add_document`, as `file_name`, `mime` and `text` (a text file) or `content_b64` (any other file). A .docx is read with zipfile and xml.etree; an .html file has its tags, scripts and styles stripped. The same text added twice is kept once.

**What it needs.** Nothing to install, no internet, no keys, no brain. It uses the SQLite full-text search (FTS5) built into Python's `sqlite3`. At start it checks FTS5 is there; if this Python's SQLite lacks it, every tool answers one plain sentence saying so.

**Setup steps.** None.

**Data.** One file, `documents.sqlite`, in the tool's data folder (`PUFFPAI_DATA`): a `docs` table (name, type, date added, size, a fingerprint for spotting the same text twice) and an FTS5 index holding each document's text. A file that will not open as a database is never written over. On remove, the owner is asked whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only, and every route (`/health` and `/mcp`) refuses a request without the package's token (`Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each document is at most 10 MB. The owner's search words are turned into quoted terms before they reach FTS5, so no search can run FTS5 syntax: all the words first, then any of them when nothing has all.

## Changed from HomeBot

- HomeBot's `search_my_documents`, `fetch_my_document` and `list_my_documents` searched with an embedding model in ChromaDB and also searched its offline knowledge library in the same answer. This package matches words with SQLite FTS5 (with English word stems, so "warranties" finds "warranty") and searches only the owner's own documents.
- HomeBot fetched a document by a part of its file name. Here search and list give each document an id, and `read_document` and `remove_document` take that id.

## Rough edges

- Words, not meanings: a search for "car" does not find "vehicle".
- PDFs, pictures (no reading of text in photos), spreadsheets and old .doc files are not taken. HomeBot read some of those.
- `read_document` answers at most 20,000 characters; a longer document is cut, and says so.
- HomeBot's suggested uploads (`list_pending_suggestions`) and its Knowledge Manager page are not built.

Cypher Sisters. puff puff pass.
