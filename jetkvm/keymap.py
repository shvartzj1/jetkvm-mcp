"""ASCII / key-name -> USB HID usage codes (US layout).

Used to translate `type_text` and `press_key` into JetKVM keyboardReport calls.
Modifier bitmask (USB HID): LeftCtrl=0x01 LeftShift=0x02 LeftAlt=0x04 LeftGUI=0x08
"""

MOD_CTRL = 0x01
MOD_SHIFT = 0x02
MOD_ALT = 0x04
MOD_GUI = 0x08

# base (unshifted) character -> usage code
_BASE = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0A, "h": 0x0B, "i": 0x0C, "j": 0x0D, "k": 0x0E, "l": 0x0F,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1A, "x": 0x1B,
    "y": 0x1C, "z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22, "6": 0x23,
    "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "\n": 0x28, "\r": 0x28, "\t": 0x2B, " ": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}

# shifted character -> (base char) ; we apply MOD_SHIFT
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}

# named keys for press_key("enter"), key combos, etc.
NAMED = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "spacebar": 0x2C,
    "capslock": 0x39, "delete": 0x4C, "del": 0x4C, "insert": 0x49, "ins": 0x49,
    "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pgup": 0x4B,
    "pagedown": 0x4E, "pgdn": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "menu": 0x65, "application": 0x65,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
}

MODIFIERS = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT, "option": MOD_ALT,
    "gui": MOD_GUI, "win": MOD_GUI, "windows": MOD_GUI,
    "cmd": MOD_GUI, "super": MOD_GUI, "meta": MOD_GUI,
}


def char_to_report(ch: str):
    """Return (modifier, usage_code) for a single character, or None if unmapped."""
    if ch in _BASE:
        return (0, _BASE[ch])
    if ch.isalpha() and ch.lower() in _BASE:  # uppercase letter
        return (MOD_SHIFT, _BASE[ch.lower()])
    if ch in _SHIFTED:
        return (MOD_SHIFT, _BASE[_SHIFTED[ch]])
    return None


def combo_to_report(combo: str):
    """Parse 'ctrl+alt+delete' / 'cmd+shift+4' / 'enter' -> (modifier, usage_code).

    Raises ValueError on an unknown token. The final non-modifier token is the key.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    modifier = 0
    key = None
    for p in parts:
        if p in MODIFIERS:
            modifier |= MODIFIERS[p]
        elif p in NAMED:
            key = NAMED[p]
        elif len(p) == 1:
            r = char_to_report(p)
            if r is None:
                raise ValueError(f"unmapped key: {p!r}")
            m, code = r
            modifier |= m
            key = code
        else:
            raise ValueError(f"unknown key token: {p!r}")
    if key is None:
        raise ValueError(f"no main key in combo: {combo!r}")
    return (modifier, key)
