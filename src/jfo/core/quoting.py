from __future__ import annotations

import re


class QuoteError(ValueError):
    pass


_CONTROL_RE = re.compile(r"[\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e-\x1f\x7f]")


def assert_safe_text(value: str, *, what: str = "text") -> None:
    """Reject obviously dangerous / unrenderable strings.

    - NUL is disallowed (cannot exist in POSIX file paths anyway)
    - other control chars are disallowed because they break logs and scripts
    """

    if "\x00" in value:
        raise QuoteError(f"{what} contains NUL byte, refusing.")
    if _CONTROL_RE.search(value):
        raise QuoteError(f"{what} contains control character, refusing.")


def bash_quote(arg: str) -> str:
    """Return a Bash-safe single-quoted string literal.

    Uses the classic pattern: 'foo'"'"'bar' to embed single quotes.

    This is safe against globbing, whitespace, &, (), *, etc.
    """

    assert_safe_text(arg, what="path")
    if arg == "":
        return "''"
    # Close quote, insert escaped single quote, reopen.
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def bash_array_literal(items: list[str]) -> str:
    return "(" + " ".join(bash_quote(x) for x in items) + ")"


def ps_here_string(text: str) -> str:
    """PowerShell here-string literal for displaying an equivalent command.

    We use @' ... '@ to avoid escaping most characters.
    """

    # PowerShell here-string terminator must be at line start.
    if "\n'@" in text:
        # Extremely unlikely for our scripts; if it happens, we degrade gracefully.
        text = text.replace("\n'@", "\n' @")
    return "@'\n" + text + "\n'@"
