"""
Arabic in server-made PDFs (the tax invoice PDF of the invoice archive).

reportlab draws glyphs exactly as given, left to right, and its built-in
fonts have no Arabic. So Arabic text is first joined into its connected
letter forms (arabic-reshaper), then put in display order (python-bidi),
and drawn with a font that has Arabic glyphs: Amiri (SIL Open Font
Licence, Latin included), kept in common/fonts/.

Nothing falls back silently: without the font files or the two libraries
register() raises PdfUnavailable, and the caller says so (the archive
lists the PDF as "missing" with the reason).
"""

import re
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"
REGULAR = "Amiri"
BOLD = "Amiri-Bold"
FILES = {REGULAR: "Amiri-Regular.ttf", BOLD: "Amiri-Bold.ttf"}

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_registered = False


class PdfUnavailable(Exception):
    """The Arabic font or shaping libraries aren't installed."""


def register():
    """Registers the Amiri fonts with reportlab (once)."""
    global _registered
    if _registered:
        return
    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
    except ImportError as exc:
        raise PdfUnavailable(f"Arabic PDF libraries not installed ({exc.name})") from exc
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, filename in FILES.items():
        path = FONT_DIR / filename
        if not path.exists():
            raise PdfUnavailable(f"Arabic font not installed ({filename} in common/fonts/)")
        pdfmetrics.registerFont(TTFont(name, str(path)))
    _registered = True


def shape(text):
    """`text` ready to draw: Arabic joined and in display order; text
    without Arabic is returned as it is."""
    text = "" if text is None else str(text)
    if not _ARABIC.search(text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))
