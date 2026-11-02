"""Extract the visible text from a reportlab PDF, in pure Python.

Why this exists: a PDF's BYTES are not proof of its contents. The rule
this repo lives by is "a 200 download only proves bytes arrived" -- you
read the text to know what the document says. `pdftotext` proves that
outside the test suite, but the suite must not depend on a binary that
may not be installed on every machine that clones the repo.

How reportlab writes a PDF:

  * page content is in a `stream ... endstream` block;
  * the stream is `/ASCII85Decode` then `/FlateDecode` (this is why
    plain `zlib.decompress` on the raw bytes fails with "incorrect
    header check" -- the bytes are ASCII85, not zlib);
  * text is drawn as `(literal) Tj` or `[(a) -2 (b)] TJ`, with the
    literal in PDF string syntax.

So: pull out each stream, ASCII85-decode it, inflate it, then collect
the string literals.

This is deliberately a TEST HELPER, not production code: the app serves
PDF bytes and the consumer's PDF reader does the rest.
"""

import base64
import re
import zlib

_ENDSTREAM = re.compile(rb'endstream')
_STREAM = b'stream'
# A PDF string literal: ( ... ) with backslash escapes.
_LITERAL = re.compile(r'\((?:\\.|[^()\\])*\)')


def extract_text(pdf_bytes):
    """Return the text a PDF reader would show, as one string."""
    chunks = []
    for m in _ENDSTREAM.finditer(pdf_bytes):
        i = m.start()
        j = pdf_bytes.rfind(_STREAM, 0, i)
        if j == -1:
            continue
        body = pdf_bytes[j + len(_STREAM):i].lstrip(b'\r\n')
        raw = body
        try:
            raw = base64.a85decode(body, adobe=True)
        except Exception:
            pass  # not ASCII85 -- try it as a plain zlib stream
        for attempt in (zlib.decompress,
                        lambda b: zlib.decompressobj().decompress(b)):
            try:
                chunks.append(attempt(raw).decode('latin-1'))
                break
            except Exception:
                continue

    content = '\n'.join(chunks)
    out = []
    for tm in _LITERAL.finditer(content):
        s = tm.group(0)[1:-1]
        s = s.replace('\\(', '(').replace('\\)', ')').replace('\\\\', '\\')
        out.append(s)
    return '\n'.join(out)
