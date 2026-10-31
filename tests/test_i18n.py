"""The interface's languages: every file has every text, with the same
placeholders, and every message the server can send has a translation."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from buybutler import auth

PACKAGE = Path(__file__).parent.parent / "buybutler"
STATIC = PACKAGE / "static"
I18N = STATIC / "i18n"
# Files whose messages never reach the interface: settings, the command line
# and the start-up checks of the database.
NOT_SHOWN = {"config.py", "__main__.py", "logging_setup.py", "db.py"}
# Raised for the code's own use, never sent as they are.
INTERNAL = {"step {ref} not sent yet", "{kind}"}
PLACEHOLDER = re.compile(r"\{([\w.]+)\}")


def load(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


ENGLISH = load("en")


def _template(node: ast.expr) -> str | None:
    """A message as the interface's key: f-string parts become {placeholders}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    return None


def server_messages() -> set[str]:
    """Every text the server puts in an error: HTTPException details and the
    messages of the errors the endpoints pass on (ValueError from validators,
    AuthError, ListError …)."""
    found = set()
    for path in PACKAGE.glob("*.py"):
        if path.name in NOT_SHOWN:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).split(".")[-1]
            if name == "HTTPException" and len(node.args) >= 2:
                text = _template(node.args[1])
            elif (name.endswith("Error") or name == "TooManyChallenges") and node.args:
                text = _template(node.args[0])
            elif name == "JSONResponse" and node.args and isinstance(node.args[0], ast.Dict):
                text = _template(node.args[0].values[0])
            else:
                continue
            if text is not None and text not in INTERNAL:
                found.add(text)
    return found


def test_every_language_has_a_file():
    assert sorted(p.stem for p in I18N.glob("*.json")) == sorted(auth.LANGUAGES)


def test_the_interface_offers_every_language():
    js = (STATIC / "app.js").read_text()
    offered = re.search(r"const LANGUAGES = \{(.*?)\};", js, re.DOTALL).group(1)
    assert sorted(re.findall(r"\b([a-z]{2}):", offered)) == sorted(auth.LANGUAGES)


def test_every_server_message_has_an_english_key():
    missing = server_messages() - set(ENGLISH["server"])
    assert not missing, f"add these to en.json under server: {sorted(missing)}"


def _keys_used() -> set[str]:
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    used = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html))
    used |= set(re.findall(r'\bt\("([^"]+)"', js))
    return used


def test_every_key_used_is_in_english():
    missing = _keys_used() - set(ENGLISH)
    assert not missing, f"add these to en.json: {sorted(missing)}"


def test_no_unused_keys():
    js = (STATIC / "app.js").read_text()
    # Chosen with a condition: t(totp ? "login.hint_totp" : "login.hint_passkey").
    named = set(re.findall(r'"([a-z]+\.[a-z_]+)"', js))
    # Built at run time: t(`bulk.${status}`).
    prefixes = set(re.findall(r'\bt\(`([a-z_]+\.)\$\{', js))
    unused = {k for k in set(ENGLISH) - _keys_used() - named - {"server"} if not any(k.startswith(p) for p in prefixes)}
    assert not unused, f"not used anywhere: {sorted(unused)}"


def _forms(value) -> dict[str, str]:
    return value if isinstance(value, dict) else {"": value}


@pytest.mark.parametrize("lang", [lang for lang in auth.LANGUAGES if lang != "en"])
def test_translation_is_complete(lang):
    other = load(lang)
    assert set(other) == set(ENGLISH), f"{lang}: missing {set(ENGLISH) - set(other)}, extra {set(other) - set(ENGLISH)}"
    assert set(other["server"]) == set(ENGLISH["server"])
    pairs = [(k, ENGLISH[k], other[k]) for k in ENGLISH if k != "server"]
    pairs += [(k, k, v) for k, v in other["server"].items()]
    for key, english, translated in pairs:
        wanted = {p for text in _forms(english).values() for p in PLACEHOLDER.findall(text)}
        assert isinstance(translated, dict) == isinstance(english, dict), f"{lang} {key}: plural forms or not"
        for form, text in _forms(translated).items():
            assert text.strip(), f"{lang} {key} {form}: empty"
            assert set(PLACEHOLDER.findall(text)) == wanted, f"{lang} {key} {form}: placeholders differ"
        if isinstance(translated, dict):
            assert "other" in translated, f"{lang} {key}: needs the form 'other'"


def test_plural_forms_fit_the_language():
    # The forms Intl.PluralRules gives for whole numbers (CLDR).
    needed = {"pl": {"one", "few", "many", "other"}, "tr": {"one", "other"}}
    for lang, forms in needed.items():
        for key, value in load(lang).items():
            if isinstance(value, dict) and key != "server":
                assert forms <= set(value), f"{lang} {key}: needs {forms - set(value)}"
