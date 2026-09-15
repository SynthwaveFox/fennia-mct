"""Fennia palette — lifted from the orange rice (waybar/mako/niri)."""
from textual.theme import Theme

ORANGE = "#ff8a00"   # hero accent
PEACH = "#fab387"    # info accent
AMBER = "#ffb347"    # warning / battery.warning
BORDER = "#4d443b"   # warmed mocha border
DIM = "#6c5f52"      # inactive workspace
BASE = "#181825"     # waybar / mako background
SURFACE = "#1e1e2e"  # tooltip background
TEXT = "#cdd6f4"
ROSEWATER = "#f5e0dc"
RED = "#f38ba8"
URGENT = "#9b0000"
GREEN = "#a6e3a1"

FENNIA = Theme(
    name="fennia",
    primary=ORANGE,
    secondary=PEACH,
    accent=AMBER,
    warning=AMBER,
    error=RED,
    success=GREEN,
    foreground=TEXT,
    background=BASE,
    surface=SURFACE,
    panel="#232130",
    dark=True,
    variables={
        "border": BORDER,
        "block-cursor-background": ORANGE,
        "block-cursor-foreground": BASE,
        "block-cursor-text-style": "bold",
        "footer-key-foreground": ORANGE,
        "footer-description-foreground": DIM,
        "footer-background": BASE,
        "input-selection-background": ORANGE + " 35%",
        "datatable--header-color": PEACH,
    },
)
