"""The design fonts declared in tokens.css are all bundled, Latin Extended included."""
import re
from pathlib import Path

CSS = Path(__file__).parent.parent / "buybutler" / "static" / "css"


def test_every_declared_font_file_exists():
    css = (CSS / "tokens.css").read_text()
    files = re.findall(r'url\("(fonts/[^"]+\.woff2)"\)', css)
    assert {"fonts/Anton-Regular-latin-ext.woff2", "fonts/Archivo-Variable-latin-ext.woff2"} <= set(files)
    for name in files:
        assert (CSS / name).is_file(), name


def test_latin_ext_faces_are_limited_by_unicode_range():
    css = (CSS / "tokens.css").read_text()
    for block in re.findall(r"@font-face\s*{[^}]*latin-ext[^}]*}", css):
        assert "unicode-range: U+0100-02BA" in block
