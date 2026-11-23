# Document convert

Turns a PDF of a book into a Word file with real chapter headings, turns office documents into PDFs with LibreOffice on this computer, and reads the plain text out of PDFs, Word, Excel and PowerPoint files. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Document convert is a PuffPAI tool package (`kind: tool`, no page of its own). It runs one small server on this computer (Python standard library, plus pypdf, cryptography and python-docx for PDFs) and gives the brain these MCP tools at `/mcp`:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `convert_to_pdf` | Makes a PDF from a .docx, .xlsx, .pptx, .odt, .ods, .odp, .txt or .md with LibreOffice, saves it in `made/` and answers its path | yes | turn this Word document into a PDF |
| `read_document_text` | Plain text from a .docx, .xlsx, .pptx or .pdf (read directly, no LibreOffice needed), or a .txt or .md as it is | no | read me what is in this PDF |
| `pdf_to_word` | Makes a .docx with real Word heading styles from a PDF of a book, saves it in `My files/Converted`, and answers what it found and where the file is. No LibreOffice needed | yes | convert this PDF of my novel to Word |
| `list_converted` | The PDFs made so far, newest first, and the Word files made from PDFs | no | which PDFs have you made |

A file sent from the Desktop or chat ("send this file to Document convert") goes to `convert_to_pdf`, and a `.pdf` goes to `pdf_to_word`. The tools take a file the contract's way: `file_name`, `mime` and `content_b64` (or `text` for a text file).

**What it does to a PDF.** `pdf_text.py` (the same file as in Manuscript) joins hard line wraps into paragraphs, puts back words split at a line end, takes out running heads and page numbers, marks chapter headings, and keeps scene breaks. No word is invented or reworded. Its report gives pages, words, pages with no text, running heads removed, hyphens joined and chapters found, with a plain warning when it found no chapters. A PDF that is pictures of pages is refused, because reading scans is not built, and so is one locked with a password; one that limits printing but opens with no password is read.

**What it needs.** LibreOffice, for making PDFs only. pypdf 6.21.4, cryptography 50.0.2 and python-docx 1.2.0, which its install fetches from PyPI into its own folder. No internet after that, no keys, no brain. `needs.owner_files` is `read`, which lets it ask the Core where My files is (`GET /v1/owner-files`). It writes one kind of file there, a new .docx in `Converted`, and it names that in its consent text. It is not `write`: the Core treats a listed change with a `file_name` argument as changing her file and hides and refuses it until she turns on changing her documents (D140), and that would turn off `convert_to_pdf` and `pdf_to_word`, which change none of her files.

**Setup steps.** One "Your turn" card: **Install LibreOffice** (free, from https://www.libreoffice.org/download/). The tool looks for `soffice` on the PATH and in the usual install folders (`Program Files\LibreOffice\program` on Windows, `/Applications/LibreOffice.app` on a Mac, `/usr/bin`, `/usr/lib/libreoffice`, `/opt/libreoffice*` and `/snap/bin` on Linux) each time it converts, so installing it later works without a restart. Until then, `convert_to_pdf` answers one plain sentence saying LibreOffice is missing; `read_document_text` and `list_converted` work anyway.

**Data.** In its data folder (`PUFFPAI_DATA`): `made/` holds the PDFs; `libreoffice-profile/` is LibreOffice's own settings for this tool, so it never touches the owner's LibreOffice settings; `work/` holds one job at a time and is emptied after each conversion. On remove, the owner is asked whether to keep the PDFs. The Word files are in My files, in `Converted`, and stay there; in the window she finds them under Knowledge and documents, Your files, and Open sends one to the computer she is at.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only, and every route (`/health` and `/mcp`) refuses a request without the package's token (`Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each file is at most 25 MB. LibreOffice runs as `soffice --headless --convert-to pdf --outdir <job folder> <file>`, from an argument list (never a shell string), one conversion at a time, with no console window, and is stopped after 120 seconds. The file name is cut down to plain characters before it touches the disk.

## Changed from HomeBot

- HomeBot ran LibreOffice inside a bubblewrap jail with no network, on its own Linux box. This package has no jail: LibreOffice runs as the owner's own user, like any program she opens, and is given only files in this tool's data folder.
- HomeBot could convert to several formats and read documents only by converting them. Here the target is PDF only, and reading a .docx, .xlsx or .pptx needs no LibreOffice at all.

## Rough edges

- PDF is the only output for office files. Converting them to .odt, HTML or plain text files (HomeBot could) is not built.
- `pdf_to_word` is only as good as the PDF. Columns, tables, poetry and block quotes can come out in the wrong order or wrong paragraphs; it says so for columns and tables. A scan's hidden text keeps the scanner's mistakes, and a font with no letter map gives wrong letters; it warns about both. Bold and italics do not come across, and pictures are left out.
- `read_document_text` does not read .odt, .ods or .odp; it says so. Spreadsheet cells come back as their stored values (a formula's last result, a date as a number).
- A document's text in the answer is capped at 20,000 characters.
- Old binary formats (.doc, .xls, .ppt) are not taken, even though LibreOffice could convert them.
- LibreOffice's first run builds its profile and can be slow.

Cypher Sisters. puff puff pass.
