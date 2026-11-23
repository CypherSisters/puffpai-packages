"""A Word file from the book's sections as they stand. No outside package: a .docx is a zip of a little XML. It reads the
text and writes a new file; it never changes anything it read.

Two styles. The default is standard manuscript format, as commonly asked for: 12-point Courier, double spaced, one-inch
margins, first lines indented, a title page with the author's details and the word count rounded to the nearest hundred,
each chapter on a new page, and the title and page number at the top of every page after the first. ``plain=True`` is an
ordinary reading copy instead: Times New Roman 12, 1.15 line spacing, first lines indented, each chapter a Word
"Heading 1" on a new page, a simple title page and no running header.

Both keep her italics and bold: a section file holds them as ``*italic*``, ``**bold**`` and ``***both***`` (marks.py),
and each becomes an italic or bold run. Text with no marks reads the same as before.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

import marks

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_COURIER = '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/>'
_TIMES = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>'
_SIZE = '<w:sz w:val="24"/>'
_FONT = _COURIER + _SIZE                 # the header's runs, as before
_BREAK = re.compile(r"(\*\s*\*\s*\*|-{3,}|#|~)")
# paragraph settings per style: standard manuscript format, then the reading copy
_PROPS = {
    "body": '<w:spacing w:line="480" w:lineRule="auto" w:after="0"/><w:ind w:firstLine="720"/>',
    "center": '<w:spacing w:line="480" w:lineRule="auto" w:after="0"/><w:jc w:val="center"/>',
    "single": '<w:spacing w:line="240" w:lineRule="auto" w:after="0"/>',
    "plain-body": '<w:spacing w:line="276" w:lineRule="auto" w:after="0"/><w:ind w:firstLine="360"/>',
    "plain-center": '<w:spacing w:line="276" w:lineRule="auto" w:after="0"/><w:jc w:val="center"/>',
    "plain-title": '<w:spacing w:before="2880" w:after="240"/><w:jc w:val="center"/>',
    "": "",
}


def _run(text: str, italic: bool = False, bold: bool = False, font: str = _COURIER, size: str = _SIZE) -> str:
    look = font + ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + size
    return f'<w:r><w:rPr>{look}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _para(text: str | list = "", style: str = "", page_break: bool = False, font: str = _COURIER,
          word_style: str = "", size: str = _SIZE) -> str:
    """One paragraph. ``text``: plain words, or (words, italic, bold) pieces."""
    props = _PROPS[style]
    if page_break:
        props = "<w:pageBreakBefore/>" + props
    if word_style:
        props = f'<w:pStyle w:val="{word_style}"/>' + props
    pieces = [(text, False, False)] if isinstance(text, str) else text
    return f"<w:p><w:pPr>{props}</w:pPr>{''.join(_run(w, i, b, font, size) for w, i, b in pieces if w)}</w:p>"


def _clean(text: str) -> list[list[tuple[str, bool, bool]]]:
    """Each paragraph as (words, italic, bold) pieces, its spaces made single. Marks are read line by line, as a
    section file keeps them, then the lines of one paragraph are joined with a space."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        pieces: list[tuple[str, bool, bool]] = []
        for line in block.split("\n"):
            line = " ".join(line.split())
            if not line:
                continue
            if pieces:
                pieces.append((" ", False, False))
            pieces += marks.runs(line)
        if pieces:
            out.append(pieces)
    return out


def _words(pieces: list[tuple[str, bool, bool]]) -> str:
    return "".join(p[0] for p in pieces)


def _styles() -> str:
    """The reading copy's styles: Normal in Times New Roman 12, and Word's own Heading 1."""
    return (f'{_HEAD}<w:styles {_W}><w:docDefaults><w:rPrDefault><w:rPr>{_TIMES}{_SIZE}</w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
            f'<w:rPr>{_TIMES}{_SIZE}</w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
            '<w:pPr><w:keepNext/><w:keepLines/><w:pageBreakBefore/><w:spacing w:before="480" w:after="480"/>'
            '<w:jc w:val="center"/><w:outlineLvl w:val="0"/></w:pPr>'
            f'<w:rPr>{_TIMES}<w:b/><w:sz w:val="32"/></w:rPr></w:style></w:styles>')


def _chapters(book):
    """(heading or None, paragraphs as pieces) for each section, the heading only where a new chapter starts."""
    last_head = None
    for s in book.sections:
        head = re.sub(r",\s*part\s+\d+\s*$", "", marks.strip(str(s.heading)), flags=re.I)
        new = head if head != last_head else None
        last_head = head
        raw = getattr(s, "raw", None)                   # the file's text, marks and all, when the section offers it
        yield new, _clean(raw if isinstance(raw, str) else s.text)


def _zip(doc: str, extra: dict[str, tuple[str, str, str]]) -> bytes:
    """The package. ``extra``: part name -> (its XML, its content type, its relationship type)."""
    over = "".join(f'<Override PartName="/word/{n}" ContentType="{t}"/>' for n, (_x, t, _r) in extra.items())
    types = (f'{_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
             f'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>{over}</Types>')
    rels = (f'{_HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    links = "".join(f'<Relationship Id="rId{k}" Type="{r}" Target="{n}"/>' for k, (n, (_x, _t, r)) in enumerate(extra.items(), 2))
    doc_rels = f'{_HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{links}</Relationships>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        for n, (x, _t, _r) in extra.items():
            z.writestr(f"word/{n}", x)
    return out.getvalue()


def _plain(book, name: str) -> bytes:
    """The reading copy: a simple title page, then each chapter a Heading 1 on a new page."""
    font = _TIMES
    body = [_para(book.title, "plain-title", font=font, size='<w:sz w:val="48"/>')]
    if name:
        body.append(_para("by " + name, "plain-center", font=font))
    for head, paras in _chapters(book):
        if head is not None:
            body.append(_para(head, "", font=font, word_style="Heading1", size='<w:sz w:val="32"/>'))
        for p in paras:
            if _BREAK.fullmatch(_words(p)):
                body.append(_para(_words(p), "plain-center", font=font))
            else:
                body.append(_para(p, "plain-body", font=font))
    sect = ('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
            'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
    doc = f'{_HEAD}<w:document {_W} {_R}><w:body>{"".join(body)}{sect}</w:body></w:document>'
    return _zip(doc, {"styles.xml": (_styles(), "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
                                     "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles")})


def build(book, author: str = "", plain: bool = False) -> tuple[bytes, str]:
    """(the .docx bytes, its plain name). ``author``: what she typed for the title page, her name and how to reach her.
    ``plain``: an ordinary reading copy instead of standard manuscript format."""
    lines = [x.strip() for x in re.split(r"[,\n]", str(author or "")) if x.strip()]
    if plain:
        return _plain(book, lines[0] if lines else ""), f"{book.title}, reading copy"
    total = sum(s.words for s in book.sections)
    rounded = max(100, round(total, -2))
    lines = lines or ["Your name", "Your address", "Your email and phone"]
    surname = lines[0].split()[-1] if lines[0] != "Your name" else "Surname"
    body = [_para(x, "single") for x in lines]
    body.append(_para(f"About {rounded:,} words", "single"))
    body += [_para() for _i in range(12)]
    body += [_para(book.title, "center"), _para("by " + lines[0], "center")]
    for head, paras in _chapters(book):
        if head is not None:
            body.append(_para(head, "center", page_break=True))
        for p in paras:
            if _BREAK.fullmatch(_words(p)):
                body.append(_para("#", "center"))
            else:
                body.append(_para(p, "body"))
    sect = ('<w:sectPr><w:headerReference w:type="default" r:id="rId2"/><w:titlePg/>'
            '<w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
    doc = f'{_HEAD}<w:document {_W} {_R}><w:body>{"".join(body)}{sect}</w:body></w:document>'
    short = " ".join(book.title.split()[:3])
    header = (f'{_HEAD}<w:hdr {_W}><w:p><w:pPr><w:jc w:val="right"/></w:pPr>'
              f'{_run(f"{surname} / {short} / ")}<w:r><w:rPr>{_FONT}</w:rPr><w:fldChar w:fldCharType="begin"/></w:r>'
              f'<w:r><w:rPr>{_FONT}</w:rPr><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
              f'<w:r><w:rPr>{_FONT}</w:rPr><w:fldChar w:fldCharType="end"/></w:r></w:p></w:hdr>')
    return _zip(doc, {"header1.xml": (header, "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
                                      "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header")}), \
        f"{book.title}, submission copy"
