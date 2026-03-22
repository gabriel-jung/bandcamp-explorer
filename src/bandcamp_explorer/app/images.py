"""Inline terminal image display using iTerm2/Kitty protocols. No extra dependencies."""

import base64
import os
import sys


def _detect_protocol() -> str | None:
    """Detect which inline image protocol the terminal supports."""
    if not sys.stdout.isatty():
        return None

    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    lc_terminal = os.environ.get("LC_TERMINAL", "")

    if "kitty" in term:
        return "kitty"

    # iTerm2 protocol is supported by: iTerm2, WezTerm, Mintty, Konsole
    if term_program in {"iTerm.app", "WezTerm", "mintty"}:
        return "iterm2"
    if lc_terminal == "iTerm2":
        return "iterm2"

    return None


def _iterm2_image(data: bytes, width: str = "auto", height: str = "8") -> str:
    """Generate iTerm2 inline image escape sequence."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"\033]1337;File=inline=1;width={width};height={height}:{b64}\a"


def _kitty_image(data: bytes, rows: int = 8, cols: int = 20) -> str:
    """Generate Kitty inline image escape sequence."""
    b64 = base64.b64encode(data).decode("ascii")
    chunks = [b64[i : i + 4096] for i in range(0, len(b64), 4096)]
    result = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        m = 0 if is_last else 1
        if i == 0:
            result.append(f"\033_Ga=T,f=100,m={m},r={rows},c={cols};{chunk}\033\\")
        else:
            result.append(f"\033_Gm={m};{chunk}\033\\")
    return "".join(result)


def get_image_escape(data: bytes, height: int = 8, width: int = 20) -> str | None:
    """Return the raw escape sequence string for an inline image, or None."""
    protocol = _detect_protocol()
    if protocol == "iterm2":
        return _iterm2_image(data, width=str(width), height=str(height))
    elif protocol == "kitty":
        return _kitty_image(data, rows=height, cols=width)
    return None
