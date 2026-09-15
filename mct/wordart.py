"""Boot-screen word art (figlet ansi_shadow wordmark), baked in so the
runtime does not need pyfiglet."""
from __future__ import annotations

from rich.text import Text

from .theme import DIM, ORANGE, PEACH

WORDMARK = [
    "███████╗███████╗███╗   ██╗███╗   ██╗██╗ █████╗ ",
    "██╔════╝██╔════╝████╗  ██║████╗  ██║██║██╔══██╗",
    "█████╗  █████╗  ██╔██╗ ██║██╔██╗ ██║██║███████║",
    "██╔══╝  ██╔══╝  ██║╚██╗██║██║╚██╗██║██║██╔══██║",
    "██║     ███████╗██║ ╚████║██║ ╚████║██║██║  ██║",
    "╚═╝     ╚══════╝╚═╝  ╚═══╝╚═╝  ╚═══╝╚═╝╚═╝  ╚═╝",
]

WORDMARK_WIDTH = len(WORDMARK[0])


def wordmark(lit_rows: int | None = None) -> Text:
    """ANSI Shadow two-tone: blocks orange, the ╗╝═ shadow dim. Rows at or
    past `lit_rows` render fully dim — used for the power-on animation."""
    if lit_rows is None:
        lit_rows = len(WORDMARK)
    out = Text()
    for i, row in enumerate(WORDMARK):
        for ch in row:
            out.append(ch, style=ORANGE if (ch == "█" and i < lit_rows) else DIM)
        if i < len(WORDMARK) - 1:
            out.append("\n")
    return out


def submark(label: str = "MASTER CONTROL") -> Text:
    """Letter-spaced caps set into a rule, matched to the wordmark width:
    ────────┤ M A S T E R   C O N T R O L ├────────"""
    spaced = "   ".join(" ".join(word) for word in label.upper().split())
    inner = f"┤ {spaced} ├"
    side = max(0, (WORDMARK_WIDTH - len(inner)) // 2)
    out = Text()
    out.append("─" * side, style=DIM)
    out.append("┤ ", style=DIM)
    out.append(spaced, style=f"bold {PEACH}")
    out.append(" ├", style=DIM)
    out.append("─" * (WORDMARK_WIDTH - side - len(inner)), style=DIM)
    return out
