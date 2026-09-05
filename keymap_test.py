"""Offline check of the keyboard layout tables — no device needed.

The German expectations come from issue #2, where they were measured against a
real German-layout Windows 11 target: the reporter typed the character on the
left (US mapping) and the character on the right appeared. So for us, "the key
US calls X must produce Y under the German layout".

    python keymap_test.py
"""
import sys

from jetkvm import keymap as k

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        failures.append(what)


def report(ch: str, layout: str):
    return k.char_to_report(ch, layout)


# ── the layout is what decides which key we press ───────────────────────────
# issue #2: type-this (US) -> get-this (German target)
OBSERVED_DE = {
    "/": "-", ">": ":", "&": "/", "?": "_", "*": "(", "(": ")", ")": "=",
    "z": "y", "Z": "Y", "y": "z", "Y": "Z", "@": '"', "|": "'",
}
for us_char, de_char in OBSERVED_DE.items():
    check(
        report(de_char, "de") == report(us_char, "us"),
        f"de: {de_char!r} should be the key US calls {us_char!r} "
        f"(got {report(de_char, 'de')}, want {report(us_char, 'us')})",
    )

# The reverse of the same fact: with the layout set, we no longer emit the US key.
check(report("z", "de") != report("z", "us"), "de: 'z' must not use the US 'z' key")
check(report("y", "de") == report("z", "us"), "de: 'y' is on the US 'z' key")


# ── the characters the issue called unreachable ─────────────────────────────
for ch in ("|", "\\", "{", "}", "[", "]", "@", "~"):
    rep = report(ch, "de")
    check(rep is not None, f"de: {ch!r} must be reachable")
    if rep is not None:
        check(
            rep[0] & k.MOD_ALTGR == k.MOD_ALTGR,
            f"de: {ch!r} lives on AltGr, got modifier {rep[0]:#04x}",
        )
check(report("|", "de") == (k.MOD_ALTGR, k.ISO_EXTRA), "de: '|' is AltGr + the 102nd key")
check(report("\\", "de") == (k.MOD_ALTGR, 0x2D), "de: '\\' is AltGr + the ß key")

# UK moves backslash to the 102nd key and @ to the quote key.
check(report("\\", "uk") == (0, k.ISO_EXTRA), "uk: '\\' is the 102nd key")
check(report("|", "uk") == (k.MOD_SHIFT, k.ISO_EXTRA), "uk: '|' is shift + the 102nd key")
check(report("@", "uk") == (k.MOD_SHIFT, 0x34), "uk: '@' is shift + the US quote key")
check(report('"', "uk") == (k.MOD_SHIFT, 0x1F), "uk: '\"' is shift+2")
check(report("£", "uk") == (k.MOD_SHIFT, 0x20), "uk: '£' is shift+3")

# French AZERTY swaps whole letter positions.
check(report("a", "fr") == report("q", "us"), "fr: 'a' is on the US 'q' key")
check(report("q", "fr") == report("a", "us"), "fr: 'q' is on the US 'a' key")
check(report("z", "fr") == report("w", "us"), "fr: 'z' is on the US 'w' key")
check(report("m", "fr") == report(";", "us"), "fr: 'm' is on the US ';' key")
check(report("|", "fr") == (k.MOD_ALTGR, 0x23), "fr: '|' is AltGr+6")


# ── every layout must still reach the ASCII a machine is driven with ────────
ASCII_ESSENTIAL = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " \t\n"
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
)
for layout in k.LAYOUTS:
    missing = [c for c in ASCII_ESSENTIAL if k.char_to_report(c, layout) is None]
    check(not missing, f"{layout}: unreachable ASCII {missing!r}")

# US must be byte-for-byte what it always was — this is the default path.
check(report("a", "us") == (0, 0x04), "us: 'a' unchanged")
check(report("A", "us") == (k.MOD_SHIFT, 0x04), "us: 'A' unchanged")
check(report("|", "us") == (k.MOD_SHIFT, 0x31), "us: '|' unchanged")
check(report("?", "us") == (k.MOD_SHIFT, 0x38), "us: '?' unchanged")
check(k.char_to_report("\n", "us") == (0, 0x28), "us: newline unchanged")


# ── dead keys need the trailing space, plain characters do not ──────────────
check(k.char_to_strokes("a", "de") == [(0, 0x04)], "de: 'a' is one stroke")
check(
    k.char_to_strokes("^", "de") == [(0, 0x35), (0, 0x2C)],
    f"de: '^' is a dead key + space, got {k.char_to_strokes('^', 'de')}",
)
check(
    k.char_to_strokes("¨", "fr") == [(k.MOD_SHIFT, 0x2F), (0, 0x2C)],
    "fr: '¨' is a dead key + space",
)
check(k.char_to_strokes("~", "us") == [(k.MOD_SHIFT, 0x35)], "us: '~' is not dead")

# Unreachable characters are reported, never silently dropped.
check(k.char_to_strokes("€", "us") is None, "us: '€' is unreachable and says so")
check(k.char_to_strokes("€", "de") is not None, "de: '€' is reachable (AltGr+E)")


# ── no key position may claim two characters at the same modifier level ─────
for layout in k.LAYOUTS:
    seen: dict[tuple[int, int], str] = {}
    for ch, rep in k.table(layout).items():
        if ch in "\r\n\t":  # newline and carriage return legitimately share Enter
            continue
        if rep in seen and seen[rep] != ch:
            failures.append(f"{layout}: {rep} claimed by both {seen[rep]!r} and {ch!r}")
        seen[rep] = ch


# ── combos and layout selection ─────────────────────────────────────────────
check(k.combo_to_report("ctrl+alt+delete") == (k.MOD_CTRL | k.MOD_ALT, 0x4C), "ctrl+alt+del")
check(k.combo_to_report("altgr+q", "de") == (k.MOD_ALTGR, 0x14), "altgr+q on de")
check(k.combo_to_report("F5") == (0, 0x3E), "combo names are case-insensitive")
check(k.normalize_layout("de-DE") == "de", "de-DE normalizes")
check(k.normalize_layout("German") == "de", "German normalizes")
check(k.normalize_layout("en-GB") == "uk", "en-GB normalizes")
try:
    k.normalize_layout("klingon")
    failures.append("unknown layout must raise")
except ValueError:
    pass

# ctrl+z on a German target must press the key that *is* z there, not the US one.
prev = k.current_layout()
k.set_layout("de")
check(k.current_layout() == "de", "set_layout takes effect")
check(k.combo_to_report("ctrl+z") == (k.MOD_CTRL, 0x1C), "de: ctrl+z uses the German z key")
check(k.char_to_report("z") == (0, 0x1C), "de: bare 'z' follows the active layout")
k.set_layout(prev)
check(k.current_layout() == "us", "set_layout restores")


if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"ok — {len(k.LAYOUTS)} layouts: {', '.join(sorted(k.LAYOUTS))}")
