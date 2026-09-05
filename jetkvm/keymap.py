"""Character / key-name -> USB HID usage codes, per keyboard layout.

A USB keyboard reports *physical key positions*, not characters. The character
that actually appears is decided by the layout the target OS has active. This
module therefore models layouts explicitly: physical keys are named by the
character they produce on a US layout, and each layout says what that key emits
at each shift level.

Get the layout wrong and there is no error — the wrong character simply appears
(on a German target, typing `z` yields `y`), which is why the active layout is
configurable (`JETKVM_KEYBOARD_LAYOUT`, or the `keyboard_layout` MCP tool) and
why unmapped characters are reported to the caller instead of dropped.

Modifier bitmask (USB HID): LeftCtrl=0x01 LeftShift=0x02 LeftAlt=0x04 LeftGUI=0x08
                            RightCtrl=0x10 RightShift=0x20 RightAlt=0x40 RightGUI=0x80
RightAlt is AltGr — the third-level modifier every non-US layout hangs `\\`, `|`,
`@`, `{}`, `[]` and `€` off.
"""

from __future__ import annotations

MOD_CTRL = 0x01
MOD_SHIFT = 0x02
MOD_ALT = 0x04
MOD_GUI = 0x08
MOD_ALTGR = 0x40  # RightAlt

_SPACE = 0x2C

# base (unshifted) character -> usage code, on a US layout
_BASE = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0A, "h": 0x0B, "i": 0x0C, "j": 0x0D, "k": 0x0E, "l": 0x0F,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1A, "x": 0x1B,
    "y": 0x1C, "z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22, "6": 0x23,
    "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "\n": 0x28, "\r": 0x28, "\t": 0x2B, " ": _SPACE,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}

# shifted character -> the base char whose key carries it, on a US layout
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}

# Whitespace sits at the same usage code on every layout.
_WHITESPACE = {"\n": 0x28, "\r": 0x28, "\t": 0x2B, " ": _SPACE}

# Physical keys, named by the character the US layout puts on them. "iso" is the
# 102nd key (between LeftShift and Z) that ISO keyboards have and ANSI ones don't
# — non-US layouts put `<>|` or `\|` there, so it must be reachable.
ISO_EXTRA = 0x64
_PHYSICAL = {ch: code for ch, code in _BASE.items() if ch not in _WHITESPACE}
_PHYSICAL["iso"] = ISO_EXTRA

# name -> (unshifted, shifted, altgr, shift+altgr); None where the level is unused
_US_SPEC: dict[str, tuple] = {}
for _ch in _PHYSICAL:
    if _ch == "iso":
        _US_SPEC["iso"] = (None, None)  # ANSI keyboards have no 102nd key
    elif _ch.isalpha():
        _US_SPEC[_ch] = (_ch, _ch.upper())
    else:
        _US_SPEC[_ch] = (_ch, next((s for s, b in _SHIFTED.items() if b == _ch), None))

# ---------------------------------------------------------------------------
# Layouts. Each lists only the keys that differ from US, as
#   physical key name: (unshifted, shifted, altgr, shift+altgr)
# `dead` names the characters this layout produces via a dead key — typing one
# emits nothing until the next keystroke, so we follow it with a space to get
# the standalone character.
# ---------------------------------------------------------------------------

LAYOUTS: dict[str, dict] = {
    "us": {
        "description": "US English (ANSI) — the default",
        "keys": {},
        "dead": "",
    },
    "uk": {
        "description": "United Kingdom (ISO)",
        "keys": {
            "`": ("`", "¬", "¦"),
            "2": ("2", '"'),
            "3": ("3", "£"),
            "4": ("4", "$", "€"),
            "'": ("'", "@"),
            "\\": ("#", "~"),
            "iso": ("\\", "|"),
        },
        "dead": "",
    },
    "de": {
        "description": "German T1 (QWERTZ)",
        "keys": {
            "`": ("^", "°"),
            "2": ("2", '"', "²"),
            "3": ("3", "§", "³"),
            "6": ("6", "&"),
            "7": ("7", "/", "{"),
            "8": ("8", "(", "["),
            "9": ("9", ")", "]"),
            "0": ("0", "=", "}"),
            "-": ("ß", "?", "\\"),
            "=": ("´", "`"),
            "q": ("q", "Q", "@"),
            "e": ("e", "E", "€"),
            "m": ("m", "M", "µ"),
            "y": ("z", "Z"),
            "z": ("y", "Y"),
            "[": ("ü", "Ü"),
            "]": ("+", "*", "~"),
            "\\": ("#", "'"),
            ";": ("ö", "Ö"),
            "'": ("ä", "Ä"),
            ",": (",", ";"),
            ".": (".", ":"),
            "/": ("-", "_"),
            "iso": ("<", ">", "|"),
        },
        "dead": "^´`",
    },
    "fr": {
        "description": "French AZERTY",
        "keys": {
            "`": ("²", None),
            "1": ("&", "1"),
            "2": ("é", "2", "~"),
            "3": ('"', "3", "#"),
            "4": ("'", "4", "{"),
            "5": ("(", "5", "["),
            "6": ("-", "6", "|"),
            "7": ("è", "7", "`"),
            "8": ("_", "8", "\\"),
            "9": ("ç", "9", "^"),
            "0": ("à", "0", "@"),
            "-": (")", "°", "]"),
            "=": ("=", "+", "}"),
            "a": ("q", "Q"),
            "q": ("a", "A"),
            "w": ("z", "Z"),
            "z": ("w", "W"),
            "e": ("e", "E", "€"),
            "m": (",", "?"),
            "[": ("^", "¨"),
            "]": ("$", "£", "¤"),
            "\\": ("*", "µ"),
            ";": ("m", "M"),
            "'": ("ù", "%"),
            ",": (";", "."),
            ".": (":", "/"),
            "/": ("!", "§"),
            "iso": ("<", ">"),
        },
        "dead": "^¨~`",
    },
}

_ALIASES = {
    "en": "us", "enus": "us", "usa": "us", "american": "us", "ansi": "us",
    "gb": "uk", "engb": "uk", "british": "uk", "ukgb": "uk", "gbuk": "uk",
    "german": "de", "dede": "de", "deutsch": "de", "qwertz": "de",
    "french": "fr", "frfr": "fr", "azerty": "fr",
}

_LEVELS = (0, MOD_SHIFT, MOD_ALTGR, MOD_SHIFT | MOD_ALTGR)


def normalize_layout(name: str) -> str:
    """'de-DE' / 'German' / 'qwertz' -> 'de'. Raises ValueError if unknown."""
    key = "".join(c for c in (name or "").lower() if c.isalnum())
    key = _ALIASES.get(key, key)
    if key not in LAYOUTS:
        raise ValueError(
            f"unknown keyboard layout {name!r}; known: {', '.join(sorted(LAYOUTS))}"
        )
    return key


def _build(layout: str) -> dict[str, tuple[int, int]]:
    """char -> (modifier, usage code) for one layout.

    Levels are walked cheapest-first across every key, so a character that sits
    on more than one key is reached with the fewest modifiers.
    """
    spec = dict(_US_SPEC)
    spec.update(LAYOUTS[layout]["keys"])
    table: dict[str, tuple[int, int]] = {}
    occupied: set[tuple[int, int]] = set()  # (usage, modifier) already spoken for
    for level, modifier in enumerate(_LEVELS):
        for name, code in _PHYSICAL.items():
            entry = spec[name]
            ch = entry[level] if level < len(entry) else None
            if ch is None:
                continue
            occupied.add((code, modifier))
            table.setdefault(ch, (modifier, code))
    for ch, code in _WHITESPACE.items():
        table[ch] = (0, code)
    # Uppercase of a character the layout only defines unshifted (e.g. German
    # 'ß'): shift it, but only if that key's shifted level is genuinely free —
    # otherwise shifting would emit the other character instead, silently.
    for ch, (modifier, code) in list(table.items()):
        upper = ch.upper()
        if modifier == 0 and upper != ch and upper not in table:
            if (code, MOD_SHIFT) not in occupied:
                table[upper] = (MOD_SHIFT, code)
    return table


_TABLES: dict[str, dict[str, tuple[int, int]]] = {}
_active = "us"


def table(layout: str | None = None) -> dict[str, tuple[int, int]]:
    """The char -> (modifier, usage) table for a layout, built once and cached."""
    name = _active if layout is None else normalize_layout(layout)
    if name not in _TABLES:
        _TABLES[name] = _build(name)
    return _TABLES[name]


def set_layout(name: str) -> str:
    """Set the layout every later call uses by default. Returns the canonical name."""
    global _active
    _active = normalize_layout(name)
    table(_active)  # build now so a bad layout fails here, not mid-typing
    return _active


def current_layout() -> str:
    return _active


def available_layouts() -> dict[str, str]:
    return {name: spec["description"] for name, spec in LAYOUTS.items()}


# named keys for press_key("enter"), key combos, etc. — position, not character,
# so these are layout-independent
NAMED = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "tab": 0x2B, "space": _SPACE, "spacebar": _SPACE,
    "capslock": 0x39, "delete": 0x4C, "del": 0x4C, "insert": 0x49, "ins": 0x49,
    "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pgup": 0x4B,
    "pagedown": 0x4E, "pgdn": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "menu": 0x65, "application": 0x65,
    "iso": ISO_EXTRA, "102nd": ISO_EXTRA,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
}

MODIFIERS = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT, "option": MOD_ALT,
    "altgr": MOD_ALTGR, "ralt": MOD_ALTGR, "rightalt": MOD_ALTGR,
    "gui": MOD_GUI, "win": MOD_GUI, "windows": MOD_GUI,
    "cmd": MOD_GUI, "super": MOD_GUI, "meta": MOD_GUI,
}


def char_to_report(ch: str, layout: str | None = None):
    """Return (modifier, usage_code) for a character, or None if this layout
    can't reach it. For a dead-key character this is only the first stroke —
    use char_to_strokes to type it."""
    return table(layout).get(ch)


def char_to_strokes(ch: str, layout: str | None = None) -> list[tuple[int, int]] | None:
    """Every keystroke needed to make `ch` appear, or None if unreachable.

    Usually one stroke. Dead-key characters (German `^`, French `¨`) need a
    following space, since the key alone only arms the accent."""
    name = _active if layout is None else normalize_layout(layout)
    rep = table(name).get(ch)
    if rep is None:
        return None
    if ch in LAYOUTS[name]["dead"]:
        return [rep, (0, _SPACE)]
    return [rep]


def combo_to_report(combo: str, layout: str | None = None):
    """Parse 'ctrl+alt+delete' / 'cmd+shift+4' / 'enter' -> (modifier, usage_code).

    Raises ValueError on an unknown token. The final non-modifier token is the key.
    """
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    modifier = 0
    key = None
    for p in parts:
        low = p.lower()
        if low in MODIFIERS:
            modifier |= MODIFIERS[low]
        elif low in NAMED:
            key = NAMED[low]
        elif len(p) == 1:
            r = char_to_report(p, layout)
            if r is None:
                raise ValueError(
                    f"unmapped key {p!r} on the {layout or _active!r} keyboard layout"
                )
            m, code = r
            modifier |= m
            key = code
        else:
            raise ValueError(f"unknown key token: {p!r}")
    if key is None:
        raise ValueError(f"no main key in combo: {combo!r}")
    return (modifier, key)
