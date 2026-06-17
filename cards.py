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
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        name,
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()  # type: ignore[return-value]


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


def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    """Antialiased rounded-rectangle alpha mask (rendered at 2x).

    The card is already drawn at ``_PROGRESS_SS`` so a 2x mask is plenty
    of edge antialiasing without the memory cost of a 4x buffer on the
    full panel.
    """
    scale = 2
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale, fill=255,
    )
    return mask.resize((width, height), Image.LANCZOS)


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
    # bodies-first so the eaves overhang; widens toward a stone plinth that
    # meets the ground line.
    body(0.228, 0.100, 0.078)   # body under the crown roof
    body(0.368, 0.130, 0.078)   # tier-2 body
    body(0.510, 0.162, 0.080)   # tier-3 body
    body(0.655, 0.196, 0.085)   # tier-4 body
    body(0.808, 0.235, 0.082)   # ground-floor hall
    body(0.884, 0.298, 0.058)   # base step
    body(0.938, 0.348, 0.040)   # plinth meeting the ground line
    roof(0.150, 0.168, 0.082)   # crown roof (smallest)
    roof(0.292, 0.212, 0.086)
    roof(0.432, 0.258, 0.090)
    roof(0.576, 0.305, 0.095)
    roof(0.726, 0.352, 0.100)   # ground-floor eaves (widest)
    # Finial: a slender mast rising to a ring and a crowning jewel.
    mast = 0.013 * W
    d.rectangle([cx - mast, 0.052 * H, cx + mast, 0.150 * H], fill=255)
    ring_r, ring_cy = 0.028 * W, 0.086 * H
    d.ellipse(
        [cx - ring_r, ring_cy - ring_r, cx + ring_r, ring_cy + ring_r],
        fill=255,
    )
    sph_r, sph_cy = 0.022 * W, 0.030 * H
    d.ellipse(
        [cx - sph_r, sph_cy - sph_r, cx + sph_r, sph_cy + sph_r], fill=255,
    )

    fw, fh = max(1, width), max(1, height)
    mask = mask.resize((fw, fh), Image.LANCZOS)
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))
    mask = mask.point(lambda v: int(v * alpha / 255))
    out = Image.new("RGBA", (fw, fh), (color[0], color[1], color[2], 0))
    out.putalpha(mask)
    return out


def _paste_full(
    sub: Image.Image, x: int, y: int, width: int, height: int
) -> Image.Image:
    """Place ``sub`` at ``(x, y)`` on a fresh full-size transparent RGBA
    layer (paste clips safely past the edges), ready to ``alpha_composite``
    onto the scene so partially out-of-bounds elements don't raise."""
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    layer.paste(sub, (x, y))
    return layer


def _mountain_band(
    width: int, height: int, *, base_y: int,
    ridges: list[tuple[float, tuple[int, int, int], int]],
) -> Image.Image:
    """Return faint, layered mountain ridgelines sitting on ``base_y``.

    Each ``ridges`` entry is ``(peak_height_frac, color, alpha)`` — back
    ridges taller and fainter, front ridges lower and a touch stronger, so
    the band reads with depth. Kept soft (blurred, low alpha) so it frames
    the pagoda base without competing with the card content above it.
    """
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    # Fixed, irregular ridge profile (deterministic — no RNG so the cached
    # backdrop stays byte-stable across renders).
    xs = (0.0, 0.16, 0.33, 0.52, 0.70, 0.86, 1.0)
    hs = (0.32, 0.84, 0.52, 1.0, 0.46, 0.74, 0.28)
    for peak_frac, color, alpha in ridges:
        peak = int(height * peak_frac)
        mask = Image.new("L", (width, height), 0)
        d = ImageDraw.Draw(mask)
        pts = [(0, base_y)]
        pts += [
            (int(width * xf), base_y - int(peak * hf))
            for xf, hf in zip(xs, hs)
        ]
        pts += [(width, base_y), (width, height), (0, height)]
        d.polygon(pts, fill=alpha)
        mask = mask.filter(ImageFilter.GaussianBlur(1.0))
        tint = Image.new("RGBA", (width, height), tuple(color) + (0,))
        tint.putalpha(mask)
        out.alpha_composite(tint)
    return out


def _moon_disc(
    width: int, height: int, *, cx: int, cy: int, r: float, alpha: int,
) -> Image.Image:
    """Return a soft, faint full moon: a pale disc shaded like a sphere —
    gentle limb-darkening toward the rim plus a directional brightening
    from the upper-left — with two very subtle, low-contrast maria. No
    hard "bowling-ball" holes. Computed in numpy on the alpha channel
    (the moon is a single pale-warm tint whose visibility rides on alpha),
    then lightly blurred so the rim stays smooth."""
    w = max(1, width)
    h = max(1, height)
    yy, xx = np.ogrid[:h, :w]
    rr = max(1.0, float(r))
    dx = (xx.astype(np.float32) - np.float32(cx)) / np.float32(rr)
    dy = (yy.astype(np.float32) - np.float32(cy)) / np.float32(rr)
    dist2 = dx * dx + dy * dy
    inside = dist2 <= 1.0
    # Limb darkening (dimmer toward the rim) + a soft light from upper-left.
    shade = 1.0 - 0.42 * np.clip(dist2, 0.0, 1.0)
    light = 1.0 - 0.26 * np.clip(dx * 0.7 + dy * 0.7, -1.0, 1.0)
    val = np.float32(alpha) * shade * light
    # Two faint, asymmetric maria — small and low-contrast so they read as
    # gentle shading, never as punched holes.
    for fx, fy, fr, dim in ((0.26, -0.20, 0.22, 0.16), (-0.20, 0.24, 0.16, 0.12)):
        cdx = (dx - fx) / fr
        cdy = (dy - fy) / fr
        crater = np.clip(1.0 - (cdx * cdx + cdy * cdy), 0.0, 1.0)
        val = val * (1.0 - np.float32(dim) * crater)
    arr = np.where(inside, np.clip(val, 0.0, 255.0), 0.0).astype(np.uint8)
    m = Image.fromarray(arr, "L").filter(ImageFilter.GaussianBlur(0.6))
    out = Image.new("RGBA", (w, h), _PAGODA_DISC + (0,))
    out.putalpha(m)
    return out


def _pagoda_scene(width: int, height: int) -> Image.Image:
    """Return the faint scenic motif for the /profile card: a crisp moon
    in the sky, layered mountain ridges, and the multi-tiered pagoda
    grounded on the base line with a soft shadow at its foot — all kept
    subtle so overlaid text/avatar stay crisp.

    Composed lower-right so the avatar (left) and the headline stay clear,
    and returned as a full-size RGBA layer the backdrop composites in.
    """
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pag_h = int(height * 0.62)
    pag_w = max(1, int(pag_h * 0.88))
    anchor_cx = int(width * 0.66)
    # Lower to the ground: the base sits on a ground line a touch above the
    # bottom edge (clear of the darkest vignette band so the base reads).
    ground_y = height - max(1, int(height * 0.05))
    pag_top = ground_y - pag_h
    px = anchor_cx - pag_w // 2

    # Soft full moon in the sky, up and to the right of the pagoda crown,
    # kept clear of the top edge: a gentle halo behind a softly-shaded disc.
    disc_r = min(pag_w * 0.26, height * 0.20)
    disc_cx = anchor_cx + int(pag_w * 0.24)
    disc_cy = int(disc_r) + max(1, int(height * 0.08))
    layer.alpha_composite(_radial_gradient(
        width, height, center=(disc_cx, disc_cy),
        radius=disc_r * 2.4, color=_PAGODA_DISC, inner_alpha=8, falloff=2.4,
    ))
    layer.alpha_composite(_moon_disc(
        width, height, cx=disc_cx, cy=disc_cy, r=disc_r, alpha=28,
    ))

    # Faint cloud wisps drifting across the moon (kept very subtle).
    clouds = Image.new("L", (width, height), 0)
    dc = ImageDraw.Draw(clouds)
    band_w = max(2, int(height * 0.024))
    for fy, x0f, x1f, a in (
        (-0.40, 0.54, 0.82, 6),
        (-0.02, 0.48, 0.86, 8),
        (0.34, 0.56, 0.78, 5),
    ):
        yy = disc_cy + int(disc_r * fy)
        dc.line(
            [(int(width * x0f), yy), (int(width * x1f), yy)],
            fill=a, width=band_w,
        )
    clouds = clouds.filter(ImageFilter.GaussianBlur(max(1, int(height * 0.022))))
    cloud_layer = Image.new("RGBA", (width, height), _PAGODA_DISC + (0,))
    cloud_layer.putalpha(clouds)
    layer.alpha_composite(cloud_layer)

    # Layered mountain ridges resting on the ground line, behind the pagoda.
    layer.alpha_composite(_mountain_band(
        width, height, base_y=ground_y,
        ridges=[
            (0.34, (74, 82, 96), 13),   # back ridge: taller, faint
            (0.20, (54, 60, 73), 18),   # front ridge: lower, stronger
        ],
    ))

    # A soft shadow pooled at the pagoda's foot grounds it on the base line
    # (replaces the old water reflection now that it sits on land), drawn
    # under the silhouette.
    shadow = Image.new("L", (width, height), 0)
    dsh = ImageDraw.Draw(shadow)
    sh_w, sh_h = int(pag_w * 0.40), max(2, int(height * 0.014))
    dsh.ellipse(
        [anchor_cx - sh_w, ground_y - sh_h, anchor_cx + sh_w, ground_y + sh_h],
        fill=30,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, int(height * 0.012))))
    shadow_layer = Image.new("RGBA", (width, height), (8, 10, 14, 0))
    shadow_layer.putalpha(shadow)
    layer.alpha_composite(shadow_layer)

    # The pagoda itself, grounded on the base line.
    pag = _pagoda_silhouette(pag_w, pag_h, color=_PAGODA_TINT, alpha=23)
    layer.alpha_composite(_paste_full(pag, px, pag_top, width, height))
    return layer


@functools.lru_cache(maxsize=8)
def _card_backdrop_cached(
    width: int, height: int, scenic: bool = False
) -> Image.Image:
    """Build the finished card backdrop sized ``(width, height)``.

    Memoised because the backdrop is deterministic per
    ``(width, height, scenic)`` yet expensive — it allocates several
    full-size numpy layers and draws the supersampled pagoda silhouette.
    Card dimensions repeat across renders (the progress card is a fixed
    size; profile heights cluster on a few values), so a small LRU keeps
    the heavy build off the hot path on the 512MB box. ``_card_backdrop``
    hands callers a defensive copy so this shared instance is never
    mutated.

    When ``scenic`` is set (the /profile card) the lone pagoda watermark
    is replaced by a fuller, still-faint scene — a soft moon disc, layered
    mountain ridges, the multi-tiered pagoda, and its water reflection.
    The verification card keeps the simpler single-silhouette watermark.
    """
    panel = _vertical_gradient(
        width, height, _PROGRESS_BG_TOP, _PROGRESS_BG_BOTTOM
    )
    longest = float(max(width, height))
    # Warm gold focal glow anchored over the avatar (upper-left) — gives
    # the card depth and a subtle halo behind the portrait.
    panel.alpha_composite(_radial_gradient(
        width, height, center=(width * 0.12, height * 0.40),
        radius=longest * 0.62, color=_PROGRESS_ACCENT,
        inner_alpha=28, falloff=1.7,
    ))
    # Cool energy-cyan bloom from the upper-right balances the warm glow
    # with a hint of Warframe energy.
    panel.alpha_composite(_radial_gradient(
        width, height, center=(width * 0.97, height * 0.12),
        radius=longest * 0.72, color=_PROGRESS_FILL_START,
        inner_alpha=17, falloff=2.0,
    ))
    if scenic:
        # Profile card: the full faint pagoda scene (disc + mountains +
        # pagoda + reflection), layered over the glows, under the vignette.
        panel.alpha_composite(_pagoda_scene(width, height))
    else:
        # Verification card: a single subtle Golden Pagoda watermark behind
        # the name, anchored low and right-of-centre. Composited through a
        # full-size transparent layer (paste clips safely for any card
        # height) and kept very faint so it never disrupts the content.
        pag_h = int(height * 0.72)
        pag_w = int(pag_h * 0.95)
        pagoda = _pagoda_silhouette(pag_w, pag_h, color=_PAGODA_TINT, alpha=15)
        motif = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        motif.paste(pagoda, (int(width * 0.62) - pag_w // 2,
                             height - pag_h - int(height * 0.03)))
        panel.alpha_composite(motif)
    # Vignette frames the content and deepens the panel corners.
    panel.alpha_composite(_vignette(width, height, strength=64))
    return panel


def _card_backdrop(
    width: int, height: int, scenic: bool = False
) -> Image.Image:
    """Return the shared card backdrop sized ``(width, height)``.

    The shared backdrop for both the profile and progress cards so they
    read as one family: a slate vertical gradient lifted by a warm gold
    focal glow over the avatar zone (left), balanced by a faint Warframe
    energy-cyan bloom bleeding from the upper-right, a faint Golden Pagoda
    silhouette settled low and right-of-centre as an architectural
    watermark, and finished with a soft vignette that frames the corners.
    Built from smooth gradients plus that single solid silhouette — no
    scattered glyphs or scratchy line-art — so it carries depth and a
    distinct identity without the noise/artifacts of drawn ornament, and
    overlaid text/icons stay crisp. The caller composites it onto the
    canvas through the rounded-corner mask.

    Returns a fresh copy of the memoised build (see ``_card_backdrop_cached``)
    so callers may safely composite onto it. ``scenic`` selects the fuller
    pagoda scene used by the /profile card.
    """
    return _card_backdrop_cached(width, height, scenic).copy()


def _circular_avatar(
    avatar_bytes: bytes | None, size: int
) -> Image.Image:
    """Return an RGBA avatar cropped into a circle with a thin gold ring.

    If ``avatar_bytes`` fails to decode (or is None) a flat gold disc is
    used as a graceful placeholder.
    """
    diameter = size
    if avatar_bytes:
        try:
            src = Image.open(io.BytesIO(avatar_bytes))
            src.load()  # force full decode now to surface truncation
            src = src.convert("RGBA")
        except Exception:
            logger.warning(
                "progress: avatar decode failed (%d bytes)",
                len(avatar_bytes), exc_info=True,
            )
            src = Image.new(
                "RGBA", (diameter, diameter), _PROGRESS_AVATAR_RING + (255,)
            )
    else:
        src = Image.new(
            "RGBA", (diameter, diameter), _PROGRESS_AVATAR_RING + (255,)
        )

    # src from Image.open needs to be closed after processing.
    needs_close = avatar_bytes is not None
    try:
        src = ImageOps.fit(src, (diameter, diameter), Image.LANCZOS)

        # Antialiased circular mask: render at 4x and downsample.
        scale = 4
        mask = Image.new("L", (diameter * scale, diameter * scale), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, diameter * scale - 1, diameter * scale - 1), fill=255
        )
        mask = mask.resize((diameter, diameter), Image.LANCZOS)

        avatar = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        avatar.paste(src, (0, 0), mask)

        ring = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(
            (0, 0, diameter - 1, diameter - 1),
            outline=_PROGRESS_AVATAR_RING + (255,),
            width=max(2, diameter // 28),
        )
        avatar.alpha_composite(ring)
        return avatar
    finally:
        if needs_close:
            src.close()


def _paste_emoji_icon(
    canvas: Image.Image, emoji_bytes: bytes | None,
    x: int, cy: int, icon_px: int, *, label: str = "",
) -> bool:
    """Composite an emoji PNG onto ``canvas`` with its left edge at ``x``
    and vertically centred on ``cy``.

    The art is aspect-preserved (``ImageOps.contain``) and centred inside
    a square ``icon_px`` box so non-square emoji never stretch. Returns
    True when drawn, False on empty input or a decode error so callers can
    fall back to a bullet glyph.
    """
    if not emoji_bytes:
        return False
    try:
        src = Image.open(io.BytesIO(emoji_bytes))
        src.load()
        src = src.convert("RGBA")
        src = ImageOps.contain(src, (icon_px, icon_px), Image.LANCZOS)
        box = Image.new("RGBA", (icon_px, icon_px), (0, 0, 0, 0))
        box.alpha_composite(
            src, ((icon_px - src.width) // 2, (icon_px - src.height) // 2)
        )
        canvas.alpha_composite(box, (x, cy - icon_px // 2))
        box.close()
        src.close()
        return True
    except Exception:
        logger.warning(
            "progress: emoji decode failed for %r", label, exc_info=True
        )
        return False


def _render_profile_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    info_lines: list[tuple] | None = None,
    in_game_name: str | None = None,
) -> bytes:
    """Render the "user profile" card and return PNG bytes.

    Laid out as two columns split by a gold divider. The left
    column stacks a gold "USER PROFILE" eyebrow, the member's in-game
    handle (``in_game_name``, e.g. ``PlayerName#123``) as the headline —
    with the Discord server nickname (``display_name``) demoted to a small
    muted subtitle when the two differ — a row of icons (platform with a
    soft gold glow, trailed by syndicate flags), then a plain-text Clan
    row and a Mastery Rank pill (a gold capsule badge). The right column
    is a top-aligned "TITLES" header over a capped, right-indented vertical
    list of the member's cosmetic titles, each gold diamond bullet + name
    underlined by a perforated golden dotted separator, split from the
    left column by a gold divider. ``info_lines`` come from
    :func:`_member_profile_info_lines`. Rendered at ``_PROGRESS_SS``x for
    crisp HiDPI output.
    """
    s = _PROGRESS_SS

    def sc(v: float) -> int:
        return int(round(v * s))

    # Pull the categories the profile card cares about out of info_lines.
    # Clan/Platform/Mastery are single rows; Syndicate is a per-faction
    # list of (name, accent_rgb|None, emoji_bytes). Tolerant of legacy
    # 3-tuple rows and a plain-string Syndicate value (older callers/tests).
    clan_row: tuple[str, str, bytes | None] | None = None
    clan_color: tuple[int, int, int] | None = None
    platform_row: tuple[str, str, bytes | None] | None = None
    mastery_row: tuple[str, str, bytes | None] | None = None
    syndicate_factions: list[tuple[str, tuple[int, int, int] | None, bytes | None]] = []
    titles: list[str] = []
    for entry in info_lines or []:
        label = entry[0]
        if label == "Clan":
            clan_row = (label, entry[1], entry[2] if len(entry) >= 3 else None)
            clan_color = entry[3] if len(entry) >= 4 else None
        elif label == "Platform":
            platform_row = (
                label, entry[1], entry[2] if len(entry) >= 3 else None
            )
        elif label == "Mastery Rank":
            mastery_row = (
                label, entry[1], entry[2] if len(entry) >= 3 else None
            )
        elif label == "Syndicate" and len(entry) >= 2 and isinstance(
            entry[1], list
        ):
            syndicate_factions = [
                (str(f[0]), f[1] if len(f) >= 2 else None,
                 f[2] if len(f) >= 3 else None)
                for f in entry[1]
            ]
        elif label == "Titles" and len(entry) >= 2 and isinstance(
            entry[1], list
        ):
            titles = [str(t).strip() for t in entry[1] if str(t).strip()]

    pad = 22
    # Identity: headline with the scanned in-game handle when we have it,
    # falling back to the server nickname. When both exist and differ, the
    # server nickname rides beneath as a small muted subtitle.
    in_game = (in_game_name or "").strip()
    server_nick = (display_name or "").strip()
    headline = in_game or server_nick or "Member"
    subtitle = (
        server_nick
        if in_game and server_nick
        and server_nick.casefold() != in_game.casefold()
        else None
    )

    # Header zone holds the avatar + eyebrow + headline + (optional
    # subtitle) + platform/syndicate icon row. It grows a touch when a
    # subtitle is shown so the small nick line has breathing room.
    header_h = 152 if subtitle else 140
    clan_pill_h = 20
    mr_pill_h = 32

    # The card is a two-column composition: a wide left content column
    # (identity + Clan/Mastery pills) and a narrow right column that
    # showcases a SINGLE "hero" title (the newest) as an ornate centered
    # emblem — echoing Warframe's Honoria system, where a player displays
    # one chosen title. Lay both out so the canvas height is known up front.
    right_panel_w = 188
    panel_x1 = _PROGRESS_CARD_W - pad
    panel_x0 = panel_x1 - right_panel_w
    col_divider_x = panel_x0 - 16
    panel_top = pad
    shown_titles = titles[:_PROFILE_MAX_TITLE_CHIPS]
    more_count = max(0, len(titles) - len(shown_titles))

    # Ornament metrics for the titles crest (logical units, shared with the
    # drawing pass so the panel height matches the rendered crest exactly).
    _T_EB_H = 16        # eyebrow band
    _T_EB_GAP = 9       # eyebrow -> top flourish
    _T_FL_H = 2         # flourish line thickness
    _T_FL_GAP = 12      # flourish <-> first title
    _T_MORE_GAP = 9     # titles -> "+N more"
    _T_MORE_H = 16      # "+N more" band
    _T_NAME_FS = 11     # title text size (matches the clan name)
    _T_NAME_LH = 17     # per-title row height

    # Each shown title is laid out on a single line, ellipsized to fit the
    # column at the clan-name text size.
    _t_inner_w = sc(right_panel_w - 36)
    _t_mdraw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def _t_wrap(text: str, font, max_w: int, max_lines: int) -> list[str]:
        words = (text or "").split()
        lines: list[str] = []
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if not cur or _t_mdraw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    cur = ""
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if not lines:
            return [""]
        # Ellipsize the final line if any content overflowed the budget.
        if " ".join(lines).strip() != (text or "").strip():
            last = lines[-1]
            while last and _t_mdraw.textlength(
                last + "\u2026", font=font
            ) > max_w:
                last = last[:-1]
            lines[-1] = (last + "\u2026") if last else "\u2026"
        return lines

    _t_name_font = _load_font(sc(_T_NAME_FS), bold=True)
    title_lines = [
        _t_wrap(t, _t_name_font, _t_inner_w, 1)[0] for t in shown_titles
    ]

    if title_lines:
        title_group_h = (
            _T_EB_H + _T_EB_GAP + _T_FL_H + _T_FL_GAP
            + len(title_lines) * _T_NAME_LH
            + (_T_MORE_GAP + _T_MORE_H if more_count else 0)
        )
    else:
        title_group_h = _T_EB_H + _T_EB_GAP + 20

    # Left column: the Clan + Mastery rows sit just beneath the header's
    # platform/syndicate icon row (close to it) and stack downward. The
    # stack must also clear the avatar's bottom — when there's no subtitle
    # the icon row rides high enough that the clan row would otherwise
    # collide with the circular avatar's lower edge.
    icon_row_cy = header_h // 2 + (42 if subtitle else 34)
    avatar_bottom = (
        (header_h - _PROGRESS_AVATAR_SIZE) // 2 + _PROGRESS_AVATAR_SIZE
    )
    stack_top = max(icon_row_cy + 12, avatar_bottom + 4)
    left_bottom = header_h
    clan_pill_top = None
    if clan_row is not None:
        clan_pill_top = stack_top
        stack_top = clan_pill_top + clan_pill_h + 8
        left_bottom = clan_pill_top + clan_pill_h
    mr_pill_top = None
    if mastery_row is not None:
        mr_pill_top = stack_top
        left_bottom = mr_pill_top + mr_pill_h

    # Right column: the panel must be tall enough for the hero emblem.
    panel_needed_bottom = panel_top + 10 + title_group_h + 12
    content_bottom = max(left_bottom, panel_needed_bottom)
    card_h = content_bottom + 16

    W, H = sc(_PROGRESS_CARD_W), sc(card_h)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # Scenic slate backdrop (profile variant): the smooth gold/energy glows
    # plus a faint pagoda scene — moon disc, mountain ridges, the
    # multi-tiered pagoda and its water reflection — kept subtle behind the
    # content, framed by a vignette. Composited through the rounded-corner
    # mask so the corners stay clean.
    panel = _card_backdrop(W, H, scenic=True)
    panel_mask = _rounded_mask(W, H, sc(_PROGRESS_RADIUS))
    canvas.paste(panel, (0, 0), panel_mask)
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, W - 1, H - 1),
        radius=sc(_PROGRESS_RADIUS), outline=_PROGRESS_BORDER + (255,),
        width=sc(2),
    )

    avatar_px = sc(_PROGRESS_AVATAR_SIZE)
    avatar = _circular_avatar(avatar_bytes, avatar_px)
    avatar_y = sc((header_h - _PROGRESS_AVATAR_SIZE) // 2)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (
            sc(pad) + sc(6), avatar_y + avatar_px - sc(10),
            sc(pad) + avatar_px - sc(6), avatar_y + avatar_px + sc(14),
        ),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(sc(7)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(avatar, (sc(pad), avatar_y))

    text_x = sc(pad) + avatar_px + sc(22)
    draw = ImageDraw.Draw(canvas)

    eyebrow_font = _load_font(sc(14), bold=True)
    name_font = _load_font(sc(40), bold=True)

    # Header rows anchored around the avatar's midline: eyebrow, headline,
    # an optional small subtitle (server nick), then the platform/syndicate
    # icon row. The identity block's right edge is bounded by the Titles-
    # panel divider so nothing crowds into the right column.
    cy = sc(header_h) // 2
    eyebrow_cy = cy - sc(32 if subtitle else 28)
    name_cy = cy - sc(8 if subtitle else 2)
    subtitle_cy = cy + sc(15)
    plat_cy = cy + sc(42 if subtitle else 34)

    # Thin gold rule between the avatar and the identity text — a refined
    # divider spanning the eyebrow + name rows.
    rule_x = sc(pad) + avatar_px + sc(11)
    draw.rounded_rectangle(
        (rule_x, eyebrow_cy - sc(9), rule_x + sc(3), name_cy + sc(13)),
        radius=sc(2), fill=_PROGRESS_ACCENT + (235,),
    )

    # The identity block (name, subtitle, icon row) is bounded on the
    # right by the Titles-panel divider. Clan now lives in its own pill in
    # the left column beneath the header (drawn further below).
    name_right_bound = sc(col_divider_x) - sc(14)

    draw.text(
        (text_x, eyebrow_cy), "USER PROFILE", font=eyebrow_font,
        fill=_PROGRESS_ACCENT, anchor="lm",
    )

    name = headline
    max_name_w = name_right_bound - text_x
    name = _ellipsize(draw, name, name_font, max_name_w)
    draw.text(
        (text_x, name_cy), name, font=name_font,
        fill=_PROGRESS_TEXT, anchor="lm",
    )

    # Server nickname as a small muted subtitle beneath the in-game handle.
    if subtitle:
        sub_font = _load_font(sc(12), bold=True)
        max_sub_w = name_right_bound - text_x
        sub_txt = _ellipsize(draw, subtitle, sub_font, max_sub_w)
        draw.text(
            (text_x, subtitle_cy), sub_txt, font=sub_font,
            fill=_PROGRESS_MUTED, anchor="lm",
        )

    # Platform + syndicate flags share one row beneath the name. The
    # platform icon leads with a soft gold glow; a lone syndicate trails
    # it as icon + faction-coloured name, while two or more collapse to
    # icon-only flags (faction-coloured dot fallback when an emoji is
    # unset).
    row_cx = text_x
    if platform_row is not None and platform_row[2]:
        plat_icon_px = sc(26)
        glow_d = plat_icon_px * 2
        gcx = row_cx + plat_icon_px // 2
        glow = Image.new("RGBA", (glow_d, glow_d), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse(
            (glow_d * 0.2, glow_d * 0.2, glow_d * 0.8, glow_d * 0.8),
            fill=_PROGRESS_ACCENT + (55,),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(sc(7)))
        canvas.alpha_composite(
            glow, (gcx - glow_d // 2, plat_cy - glow_d // 2)
        )
        _paste_emoji_icon(
            canvas, platform_row[2], row_cx, plat_cy, plat_icon_px,
            label="Platform",
        )
        row_cx += plat_icon_px + sc(10)
    if len(syndicate_factions) == 1:
        sname, scolor, sbytes = syndicate_factions[0]
        syn_icon_px = sc(24)
        if sbytes and _paste_emoji_icon(
            canvas, sbytes, row_cx, plat_cy, syn_icon_px, label="Syndicate"
        ):
            row_cx += syn_icon_px + sc(9)
        else:
            r = syn_icon_px // 3
            ImageDraw.Draw(canvas).ellipse(
                (
                    row_cx + syn_icon_px // 2 - r, plat_cy - r,
                    row_cx + syn_icon_px // 2 + r, plat_cy + r,
                ),
                fill=(scolor or _PROGRESS_MUTED) + (255,),
            )
            row_cx += syn_icon_px + sc(9)
        syn_font = _load_font(sc(15), bold=True)
        max_w = name_right_bound - row_cx
        nm = _ellipsize(draw, sname or "", syn_font, max_w)
        draw.text(
            (row_cx, plat_cy), nm, font=syn_font,
            fill=scolor or _PROGRESS_TEXT, anchor="lm",
        )
    elif syndicate_factions:
        syn_icon_px = sc(24)
        syn_gap = sc(4)
        for _sname, scolor, sbytes in syndicate_factions:
            if row_cx + syn_icon_px > name_right_bound:
                break
            if not (sbytes and _paste_emoji_icon(
                canvas, sbytes, row_cx, plat_cy, syn_icon_px,
                label="Syndicate",
            )):
                r = syn_icon_px // 3
                ImageDraw.Draw(canvas).ellipse(
                    (
                        row_cx + syn_icon_px // 2 - r, plat_cy - r,
                        row_cx + syn_icon_px // 2 + r, plat_cy + r,
                    ),
                    fill=(scolor or _PROGRESS_MUTED) + (255,),
                )
            row_cx += syn_icon_px + syn_gap

    # Clan (left column, beneath the header): a plain text row — just the
    # clan icon + name (no "CLAN:" label) with the name in the clan role's
    # own colour, no pill. The icon's left edge aligns with the Mastery
    # badge's left edge below it so the under-avatar stack shares one clean
    # left margin.
    if clan_row is not None and clan_pill_top is not None:
        clan_value_font = _load_font(sc(11), bold=True)
        clan_icon_px = sc(15)
        clan_icon_gap = sc(7)
        clan_val = clan_row[1] or "\u2014"
        clan_emoji = clan_row[2]
        clan_name_fill = clan_color or _PROGRESS_ACCENT
        has_clan_icon = bool(clan_emoji)
        cx0 = sc(pad) + sc(8)
        max_clan_w = sc(col_divider_x) - cx0 - sc(14)
        icon_w = (clan_icon_px + clan_icon_gap) if has_clan_icon else 0
        max_val_w = max_clan_w - icon_w
        clan_val = _ellipsize(draw, clan_val, clan_value_font, max_val_w)
        ccy = sc(clan_pill_top) + sc(10)
        cx = cx0
        if has_clan_icon and _paste_emoji_icon(
            canvas, clan_emoji, cx, ccy, clan_icon_px, label="Clan"
        ):
            cx += clan_icon_px + clan_icon_gap
        draw.text(
            (cx, ccy), clan_val, font=clan_value_font,
            fill=clan_name_fill, anchor="lm",
        )

    # Mastery Rank capsule badge beneath the header (gold-tinted fill +
    # hairline gold border), sized to its content.
    if mastery_row is not None and mr_pill_top is not None:
        mr_label_font = _load_font(sc(13), bold=True)
        mr_value_font = _load_font(sc(14), bold=True)
        mr_icon_px = sc(18)
        mr_icon_gap = sc(8)
        badge_pad_x = sc(14)
        mr_label, mr_value = _mastery_label_value(mastery_row[1])
        label_txt = f"{mr_label}: "
        value_txt = mr_value or "\u2014"
        has_icon = bool(mastery_row[2])
        lbl_w = draw.textlength(label_txt, font=mr_label_font)
        val_w = draw.textlength(value_txt, font=mr_value_font)
        icon_w = (mr_icon_px + mr_icon_gap) if has_icon else 0
        pill_w = int(icon_w + lbl_w + val_w + badge_pad_x * 2)
        pill_h = sc(mr_pill_h)
        pill_x0 = sc(pad) + sc(8)
        pill_y0 = sc(mr_pill_top)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            (pill_x0, pill_y0, pill_x0 + pill_w, pill_y0 + pill_h),
            radius=pill_h // 2, fill=_PROGRESS_ACCENT + (14,),
            outline=_PROGRESS_ACCENT + (105,), width=max(1, sc(1)),
        )
        canvas.alpha_composite(overlay)
        mcy = pill_y0 + pill_h // 2
        mx = pill_x0 + badge_pad_x
        if has_icon and _paste_emoji_icon(
            canvas, mastery_row[2], mx, mcy, mr_icon_px, label="Mastery Rank"
        ):
            mx += mr_icon_px + mr_icon_gap
        draw.text(
            (mx, mcy), label_txt, font=mr_label_font,
            fill=_PROGRESS_ACCENT, anchor="lm",
        )
        draw.text(
            (mx + lbl_w, mcy), value_txt, font=mr_value_font,
            fill=_PROGRESS_FILL_GOLD_END, anchor="lm",
        )

    # ---- Right column: the titles crest ----------------------------------
    # An ornate crest near the top of the column: a gold "TITLE(S)" eyebrow,
    # a tapered gold flourish with a centre gem, then up to three titles in
    # gradient-gold at the clan-name text size, and a muted "+N more" when
    # the member holds others. Anchored high so the eyebrow sits level with
    # the identity header.
    cxc = sc((panel_x0 + panel_x1) // 2)
    title_eyebrow_font = _load_font(sc(13), bold=True)
    flourish_half_w = sc(right_panel_w // 2 - 26)

    def _title_flourish(cy: int) -> None:
        """A gold hairline peaking at the centre, capped by a gold gem."""
        w = max(2, flourish_half_w * 2)
        h = max(1, sc(_T_FL_H))
        xs = np.linspace(-1.0, 1.0, w, dtype=np.float32)
        a = np.clip(1.0 - xs * xs, 0.0, 1.0) ** 0.8
        strip = np.empty((h, w, 4), dtype=np.uint8)
        strip[..., 0] = _PROGRESS_ACCENT[0]
        strip[..., 1] = _PROGRESS_ACCENT[1]
        strip[..., 2] = _PROGRESS_ACCENT[2]
        strip[..., 3] = (a * 220.0).astype(np.uint8)[None, :]
        canvas.alpha_composite(
            Image.fromarray(strip, "RGBA"), (cxc - w // 2, cy - h // 2)
        )
        r = sc(4)
        draw.polygon(
            [(cxc, cy - r), (cxc + r, cy), (cxc, cy + r), (cxc - r, cy)],
            fill=_PROGRESS_FILL_GOLD_END + (255,),
        )

    # Anchor the emblem near the top of the right column so it sits high,
    # roughly level with the identity header rather than mid-card.
    yl = panel_top + 10

    eyebrow_label = "TITLE" if len(shown_titles) == 1 else "TITLES"
    draw.text(
        (cxc, sc(yl + _T_EB_H // 2)), eyebrow_label, font=title_eyebrow_font,
        fill=_PROGRESS_ACCENT, anchor="mm",
    )
    yl += _T_EB_H

    if title_lines:
        yl += _T_EB_GAP
        _title_flourish(sc(yl))
        yl += _T_FL_H + _T_FL_GAP

        name_font = _load_font(sc(_T_NAME_FS), bold=True)
        line_h = sc(_T_NAME_LH)

        # Each title in gradient-gold (warm highlight → rich gold), drawn
        # per line via a text mask so the gold flows vertically through it.
        for ln in title_lines:
            if ln:
                bbox = draw.textbbox((0, 0), ln, font=name_font, anchor="lt")
                tw = max(1, bbox[2] - bbox[0])
                th = max(1, bbox[3] - bbox[1])
                tmask = Image.new("L", (tw, th), 0)
                ImageDraw.Draw(tmask).text(
                    (-bbox[0], -bbox[1]), ln, font=name_font, fill=255,
                    anchor="lt",
                )
                grad = _vertical_gradient(
                    tw, th, _PROGRESS_FILL_GOLD_END, _PROGRESS_FILL_GOLD
                )
                gx = cxc - tw // 2
                gyy = sc(yl) + line_h // 2 - th // 2
                canvas.paste(grad, (gx, gyy), tmask)
            yl += _T_NAME_LH

        if more_count:
            yl += _T_MORE_GAP
            draw.text(
                (cxc, sc(yl + _T_MORE_H // 2)), f"+{more_count} more",
                font=_load_font(sc(10), bold=True),
                fill=_PROGRESS_MUTED, anchor="mm",
            )
            yl += _T_MORE_H
    else:
        yl += _T_EB_GAP
        draw.text(
            (cxc, sc(yl + 10)), "None yet",
            font=_load_font(sc(12), bold=True),
            fill=_PROGRESS_MUTED, anchor="mm",
        )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
