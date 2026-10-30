# Document convert

Turns office documents into PDFs with LibreOffice on this computer, and reads the plain text out of Word, Excel and PowerPoint files. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Document convert is a PuffPAI tool package (`kind: tool`, no page of its own). It runs one small server on this computer (Python standard library only) and gives the brain these MCP tools at `/mcp`:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `convert_to_pdf` | Makes a PDF from a .docx, .xlsx, .pptx, .odt, .ods, .odp, .txt or .md with LibreOffice, saves it in `made/` and answers its path | yes | turn this Word document into a PDF |
| `read_document_text` | Plain text from a .docx, .xlsx or .pptx (read directly, no LibreOffice needed), or a .txt or .md as it is | no | read me what is in this Word doc |
| `list_converted` | The PDFs made so far, newest first | no | which PDFs have you made |

A file sent from the Desktop or chat ("send this file to Document convert") goes to `convert_to_pdf`. The tools take a file the contract's way: `file_name`, `mime` and `content_b64` (or `text` for a text file).

**What it needs.** LibreOffice, for making PDFs only. No internet, no keys, no brain.

**Setup steps.** One "Your turn" card: **Install LibreOffice** (free, from https://www.libreoffice.org/download/). The tool looks for `soffice` on the PATH and in the usual install folders (`Program Files\LibreOffice\program` on Windows, `/Applications/LibreOffice.app` on a Mac, `/usr/bin`, `/usr/lib/libreoffice`, `/opt/libreoffice*` and `/snap/bin` on Linux) each time it converts, so installing it later works without a restart. Until then, `convert_to_pdf` answers one plain sentence saying LibreOffice is missing; `read_document_text` and `list_converted` work anyway.

**Data.** In its data folder (`PUFFPAI_DATA`): `made/` holds the PDFs; `libreoffice-profile/` is LibreOffice's own settings for this tool, so it never touches the owner's LibreOffice settings; `work/` holds one job at a time and is emptied after each conversion. On remove, the owner is asked whether to keep the PDFs.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only, and every route (`/health` and `/mcp`) refuses a request without the package's token (`Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each file is at most 25 MB. LibreOffice runs as `soffice --headless --convert-to pdf --outdir <job folder> <file>`, from an argument list (never a shell string), one conversion at a time, with no console window, and is stopped after 120 seconds. The file name is cut down to plain characters before it touches the disk.

## Changed from HomeBot

- HomeBot ran LibreOffice inside a bubblewrap jail with no network, on its own Linux box. This package has no jail: LibreOffice runs as the owner's own user, like any program she opens, and is given only files in this tool's data folder.
- HomeBot could convert to several formats and read documents only by converting them. Here the target is PDF only, and reading a .docx, .xlsx or .pptx needs no LibreOffice at all.

## Rough edges

- PDF is the only output. Converting to .docx, .odt, HTML or plain text files (HomeBot could) is not built.
- `read_document_text` does not read .odt, .ods, .odp or PDFs; it says so. Spreadsheet cells come back as their stored values (a formula's last result, a date as a number).
- A document's text in the answer is capped at 20,000 characters.
- Old binary formats (.doc, .xls, .ppt) are not taken, even though LibreOffice could convert them.
- LibreOffice's first run builds its profile and can be slow.

Cypher Sisters. puff puff pass.
