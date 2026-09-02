"""Card rendering for the Golden Pagoda bot.

Pure Pillow/numpy rendering for the verification progress card and the
/profile user card, extracted from bot.py. This module imports nothing
from bot.py: in production the bot runs as ``python -u bot.py`` (module
name ``__main__``), so a back-import would re-import the whole bot as a
second module. The only cross-module dependency is the pure mastery-rank
label formatter in logic.py.
"""
from __future__ import annotations

import functools
import io
import logging
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from logic import _mastery_label_value

logger = logging.getLogger(__name__)


# ---------- Progress card (rendered PNG, shared by the verify flow) ---------

# Visual styling for the rendered progress card. The card composites the
# member's avatar (circular, left) with a rounded gradient progress bar
# and inline text overlay so a single PNG carries the whole message.
_PROGRESS_CARD_W = 860
_PROGRESS_RADIUS = 24                   # rounded panel corners
# Warframe-inspired slate panel: a faint vertical gradient from a lighter
# top to a darker base gives the card depth instead of a flat fill.
_PROGRESS_BG_TOP = (38, 41, 47)        # lighter slate (top edge)
_PROGRESS_BG_BOTTOM = (21, 22, 25)     # darker slate (bottom edge)
_PROGRESS_BORDER = (58, 62, 70)        # crisp outer hairline
_PROGRESS_FILL_START = (93, 208, 243)  # Warframe energy cyan
_PROGRESS_FILL_GOLD = (208, 162, 80)   # Orokin gold for finished bars
_PROGRESS_FILL_GOLD_END = (240, 214, 140)  # warm gold highlight end
_PROGRESS_TEXT = (236, 238, 240)
_PROGRESS_MUTED = (163, 166, 170)
_PROGRESS_ACCENT = (212, 168, 87)      # gold accent (footer / pct)
# Warm sandstone tint for the faint Golden Pagoda watermark in the card
# backdrop — echoes the landmark without pulling the slate palette warm.
_PAGODA_TINT = (200, 156, 102)
# Pale warm moon/sun disc that sits behind the pagoda in the profile
# card's scenic backdrop (echoes the celestial disc in the reference art).
_PAGODA_DISC = (236, 226, 198)
_PROGRESS_AVATAR_SIZE = 112
_PROGRESS_AVATAR_RING = (212, 168, 87)
# Supersample factor: the card is laid out in logical units then rendered
# at this multiple so text, icons, and the bar stay crisp on Discord's
# HiDPI clients (the previous 1x output looked soft when scaled).
_PROGRESS_SS = 2
# Max title rows drawn in the /profile Titles panel before the remainder
# folds into a "+N" overflow indicator (keeps the panel dimensions fixed).
_PROFILE_MAX_TITLE_CHIPS = 3


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load DejaVu Sans at ``size``; fall back to PIL default on any error.

    Results are memoized — fonts are immutable for a given (size, bold)
    pair and the truetype open is the single hottest call inside
    ``_render_profile_card_png`` (invoked several times per render).
    """
    return _load_font_cached(size, bold)


@functools.lru_cache(maxsize=32)
def _load_font_cached(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    win_dir = os.environ.get("WINDIR") or r"C:\\Windows"
    windows_fonts = os.path.join(win_dir, "Fonts")
    win_names = (
        ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "calibrib.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "calibri.ttf"]
    )
    mac_names = (
        ["Arial Bold.ttf", "Helvetica.ttc"]
        if bold
        else ["Arial.ttf", "Helvetica.ttc"]
    )
    candidates = [
        # Linux (devcontainer / common distros)
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        # Windows
        *[os.path.join(windows_fonts, n) for n in win_names],
        # macOS
        *[f"/System/Library/Fonts/Supplemental/{n}" for n in mac_names],
        # Final bare-name fallback (works when font is discoverable by PIL)
        name,
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    logger.warning(
        "cards: no truetype font found (size=%s, bold=%s); using PIL default",
        size,
        bold,
    )
    return ImageFont.load_default()  # type: ignore[return-value]


def warm_font_cache() -> None:
    """Pre-open the truetype fonts at the sizes the cards use.

    The truetype open is the hottest call inside a cold card render (each
    size probes a list of filesystem paths); warming the memoized loader at
    startup moves that cost off the first member's /profile or verify reply.
    Sizes are the logical values used by the card layouts multiplied by the
    supersample factor. Safe to call from a worker thread.
    """
    logical_sizes = (10, 14, 15, 16, 17, 18, 44)
    for size in logical_sizes:
        for bold in (False, True):
            _load_font(size * _PROGRESS_SS, bold=bold)


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    """Trim ``text`` (appending an ellipsis) until it fits within ``max_w``
    pixels for ``font``. Shared by every card-text field that has to budget
    a label/value to a column width."""
    text = text or ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > max_w:
        text = text[:-1]
    return text + "\u2026"


def _vertical_gradient(
    width: int, height: int,
    top: tuple[int, int, int], bottom: tuple[int, int, int],
) -> Image.Image:
    """Return an RGBA image filled with a top→bottom colour gradient."""
    t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]  # (h, 1)
    top_a = np.array(top, dtype=np.float32)[None, :]              # (1, 3)
    bot_a = np.array(bottom, dtype=np.float32)[None, :]           # (1, 3)
    col = top_a + (bot_a - top_a) * t                            # (h, 3)
    arr = np.empty((height, width, 4), dtype=np.uint8)
    arr[..., :3] = col[:, None, :].astype(np.uint8)
    arr[..., 3] = 255
    return Image.fromarray(arr, mode="RGBA")


@functools.lru_cache(maxsize=8)
def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    """Antialiased rounded-rectangle alpha mask (rendered at 2x).

    The card is already drawn at ``_PROGRESS_SS`` so a 2x mask is plenty
    of edge antialiasing without the memory cost of a 4x buffer on the
    full panel.

    Memoised: the mask depends only on its (width, height, radius) and is
    used purely as a read-only paste/composite mask, so the same handful of
    card geometries reuse one buffer instead of rebuilding the supersampled
    rounded-rect + LANCZOS downscale on every render (~20% of a render).
    """
    scale = 2
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale, fill=255,
    )
    return mask.resize((width, height), Image.LANCZOS)


@functools.lru_cache(maxsize=8)
def _avatar_shadow_layer(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    blur: int,
) -> Image.Image:
    """Full-canvas RGBA layer holding the soft drop shadow under the avatar.

    The shadow is a single blurred ellipse whose geometry is fixed by the
    card size + avatar placement, so the (otherwise per-render) full-canvas
    Gaussian blur — the single most expensive blur in a profile render — is
    memoised across the handful of card layouts. Composited read-only via
    ``alpha_composite``, so sharing one buffer is safe.
    """
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(box, fill=(0, 0, 0, 120))
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def _radial_gradient(
    width: int, height: int, *,
    center: tuple[float, float], radius: float,
    color: tuple[int, int, int], inner_alpha: int,
    outer_alpha: int = 0, falloff: float = 1.6,
) -> Image.Image:
    """Return an RGBA radial glow: ``color`` fading from ``inner_alpha`` at
    ``center`` to ``outer_alpha`` at ``radius`` px out, with a smooth power
    ``falloff``.

    Computed entirely in numpy so the falloff is perfectly smooth — no
    drawn-shape stepping or scatter noise — which keeps overlaid text and
    icons crisp. Used to lift the slate card backdrop with soft focal
    glows instead of busy ornament.
    """
    yy, xx = np.ogrid[:height, :width]
    dx = xx.astype(np.float32) - np.float32(center[0])
    dy = yy.astype(np.float32) - np.float32(center[1])
    dist = np.sqrt(dx * dx + dy * dy) / np.float32(max(1.0, radius))
    t = np.clip(1.0 - dist, 0.0, 1.0).astype(np.float32) ** np.float32(falloff)
    a = np.float32(outer_alpha) + np.float32(inner_alpha - outer_alpha) * t
    arr = np.empty((height, width, 4), dtype=np.uint8)
    arr[..., 0] = color[0]
    arr[..., 1] = color[1]
    arr[..., 2] = color[2]
    arr[..., 3] = np.clip(a, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _vignette(
    width: int, height: int, *,
    strength: int, start: float = 0.45, falloff: float = 1.7,
) -> Image.Image:
    """Return a black RGBA vignette: transparent through the centre,
    darkening toward the corners up to ``strength`` alpha.

    ``start`` is the normalised radius (0 centre … 1 corner) where the
    darkening begins. Pure-numpy so the gradient is smooth; the caller
    composites it over the backdrop to frame the content and deepen the
    panel edges.
    """
    yy, xx = np.ogrid[:height, :width]
    nx = (xx.astype(np.float32) - np.float32(width) / 2.0) / (
        np.float32(width) / 2.0
    )
    ny = (yy.astype(np.float32) - np.float32(height) / 2.0) / (
        np.float32(height) / 2.0
    )
    dist = np.sqrt(nx * nx + ny * ny) / np.float32(np.sqrt(2.0))
    span = max(1e-3, 1.0 - start)
    t = np.clip((dist - np.float32(start)) / np.float32(span), 0.0, 1.0)
    t = t.astype(np.float32) ** np.float32(falloff)
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[..., 3] = (t * np.float32(strength)).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _pagoda_silhouette(
    width: int, height: int, *,
    color: tuple[int, int, int], alpha: int,
) -> Image.Image:
    """Return an RGBA image (``width`` × ``height``) holding a faint, clean
    silhouette of a multi-tiered pagoda anchored to the bottom-centre,
    transparent everywhere else.

    The Golden Pagoda landmark rendered as a *solid filled* silhouette
    (never line-art) so it reads as a tasteful architectural watermark
    rather than scratchy ornament. Drawn supersampled then downscaled and
    lightly blurred for smooth, anti-aliased eaves, tinted ``color`` and
    capped at ``alpha`` so it stays subtle enough never to compete with
    the avatar, name, or profile fields drawn on top.
    """
    ss = 2
    W, H = max(1, width) * ss, max(1, height) * ss
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    cx = W * 0.5

    def body(top: float, half_w: float, h: float) -> None:
        d.rectangle(
            [cx - half_w * W, top * H, cx + half_w * W, (top + h) * H],
            fill=255,
        )

    def roof(top: float, half_w: float, rh: float) -> None:
        """One pagoda tier roof: a flat top ridge, wide eaves that sweep
        out, and tips that flick up at the corners — read clearly as a
        roof (not an arrowhead) even at watermark opacity."""
        hw = half_w * W
        t, h = top * H, rh * H
        ridge = 0.30 * hw
        d.polygon(
            [
                (cx - ridge, t),                 # flat ridge, left
                (cx + ridge, t),                 # flat ridge, right
                (cx + 0.70 * hw, t + 0.50 * h),  # slope down-out
                (cx + hw, t + 0.34 * h),         # eave tip flicks UP
                (cx + 0.80 * hw, t + h),         # underside, right corner
                (cx - 0.80 * hw, t + h),         # underside, left corner
                (cx - hw, t + 0.34 * h),         # left eave tip flicks up
                (cx - 0.70 * hw, t + 0.50 * h),  # slope down-out
            ],
            fill=255,
        )

    # Classic five-roof pagoda. Each tier is a wide upswept roof over a
    # substantial body wall (~0.6x the roof's half-width), the bodies
    # overlapping the roofs above so the whole tower reads as one connected
    # silhouette — not floating chevrons — even at watermark opacity. Drawn
