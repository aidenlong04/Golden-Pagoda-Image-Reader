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
_PROGRESS_BG_EDGE = (18, 19, 22)       # dividers / inner shadow
_PROGRESS_BORDER = (58, 62, 70)        # crisp outer hairline
_PROGRESS_TRACK = (43, 45, 49)         # #2B2D31 bar track
_PROGRESS_TRACK_EDGE = (24, 25, 28)    # track rim for definition
_PROGRESS_FILL_START = (93, 208, 243)  # Warframe energy cyan
_PROGRESS_FILL_END = (134, 230, 168)   # mint — gradient mid-stop
_PROGRESS_FILL_GOLD = (208, 162, 80)   # Orokin gold for finished bars
_PROGRESS_FILL_GOLD_END = (240, 214, 140)  # warm gold highlight end
_PROGRESS_TEXT = (236, 238, 240)
_PROGRESS_MUTED = (163, 166, 170)
_PROGRESS_ACCENT = (212, 168, 87)      # gold accent (footer / pct)
_PROGRESS_MISSING = (236, 170, 92)     # amber — 'missing data' callout
# Warm sandstone tint for the faint Golden Pagoda watermark in the card
# backdrop — echoes the landmark without pulling the slate palette warm.
_PAGODA_TINT = (200, 156, 102)
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
    ``_render_progress_card_png`` (invoked 4-5 times per render).
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

    def roof(top: float, half_w: float, rh: float, ridge: float) -> None:
        hw, r = half_w * W, ridge * W
        t, h = top * H, rh * H
        d.polygon(
            [
                (cx - r, t), (cx + r, t),
                (cx + 0.60 * hw, t + 0.60 * h),
                (cx + 0.90 * hw, t + 0.80 * h),
                (cx + hw, t + 0.52 * h),          # right eave flicks up
                (cx + 0.84 * hw, t + h),
                (cx - 0.84 * hw, t + h),
                (cx - hw, t + 0.52 * h),          # left eave flicks up
                (cx - 0.90 * hw, t + 0.80 * h),
                (cx - 0.60 * hw, t + 0.60 * h),
            ],
            fill=255,
        )

    # Tiered tower bodies + stepped base (drawn first; roofs union over).
    body(0.205, 0.060, 0.060)
    body(0.375, 0.095, 0.075)
    body(0.590, 0.150, 0.115)
    body(0.700, 0.220, 0.105)   # base block
    body(0.800, 0.285, 0.085)   # wider step
    body(0.880, 0.330, 0.055)   # ground platform
    # Upturned-eave roofs, smallest at the crown.
    roof(0.110, 0.150, 0.100, 0.030)
    roof(0.255, 0.245, 0.125, 0.045)
    roof(0.440, 0.355, 0.155, 0.065)
    # Finial: slender mast, a ring, and a crowning sphere.
    mast = 0.018 * W
    d.rectangle([cx - mast, 0.045 * H, cx + mast, 0.130 * H], fill=255)
    ring = 0.040 * W
    d.ellipse([cx - ring, 0.070 * H, cx + ring, 0.070 * H + ring], fill=255)
    sph = 0.028 * W
    d.ellipse([cx - sph, 0.010 * H, cx + sph, 0.010 * H + 2 * sph], fill=255)

    fw, fh = max(1, width), max(1, height)
    mask = mask.resize((fw, fh), Image.LANCZOS)
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))
    mask = mask.point(lambda v: int(v * alpha / 255))
    out = Image.new("RGBA", (fw, fh), (color[0], color[1], color[2], 0))
    out.putalpha(mask)
    return out


@functools.lru_cache(maxsize=8)
def _card_backdrop_cached(width: int, height: int) -> Image.Image:
    """Build the finished card backdrop sized ``(width, height)``.

    Memoised because the backdrop is deterministic per ``(width, height)``
    yet expensive — it allocates several full-size numpy layers and draws
    the supersampled pagoda silhouette. Card dimensions repeat across
    renders (the progress card is a fixed size; profile heights cluster on
    a few values), so a small LRU keeps the heavy build off the hot path
    on the 512MB box. ``_card_backdrop`` hands callers a defensive copy so
    this shared instance is never mutated.
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
        inner_alpha=22, falloff=1.7,
    ))
    # Cool energy-cyan bloom from the upper-right balances the warm glow
    # with a hint of Warframe energy.
    panel.alpha_composite(_radial_gradient(
        width, height, center=(width * 0.97, height * 0.12),
        radius=longest * 0.72, color=_PROGRESS_FILL_START,
        inner_alpha=13, falloff=2.0,
    ))
    # Subtle Golden Pagoda watermark — the landmark behind the name,
    # anchored low and right-of-centre, layered over the glows but under
    # the vignette so its edges settle into the frame. Composited through
    # a full-size transparent layer (paste clips safely for any card
    # height) and kept very faint so it never disrupts the avatar, name,
    # or profile fields drawn on top.
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


def _card_backdrop(width: int, height: int) -> Image.Image:
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
    so callers may safely composite onto it.
    """
    return _card_backdrop_cached(width, height).copy()


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


def _segmented_bar(
    width: int, height: int, count: int, target: int, *,
    complete: bool, scale: int = 1,
) -> Image.Image:
    """Render a segmented progress bar: one rounded segment per
    verification category.

    The first ``count`` segments carry the glassy energy gradient —
    Warframe energy cyan flowing through mint into Orokin gold, the gold
    growing more pronounced toward the filled edge (fully gold when
    complete) — cropped from a single full-width fill so the light flows
    continuously across the bar; the remaining segments are
    recessed track with a faint amber "pending" tint so unmet categories
    read as outstanding. Small gaps separate the segments, and the last
    filled segment gets a soft leading-edge glow while in progress.
    ``scale`` keeps strokes proportional under supersampling.
    """
    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    n = max(1, target)
    count = max(0, min(count, n))
    radius = height // 2
    line_w = max(1, int(round(1.5 * scale)))
    gap = max(2, int(round(4 * scale)))

    # Horizontal gradient stops. In progress the bar flows from the
    # Warframe energy cyan through mint and into Orokin gold, with the
    # gold weighted toward the filled (right) edge so it grows more
    # pronounced the further a member progresses — easing into the
    # all-gold complete state. Filled segments crop their slice from this
    # single full-width fill so the light flows continuously rather than
    # restarting per segment.
    if complete:
        stops = ((0.0, _PROGRESS_FILL_GOLD), (1.0, _PROGRESS_FILL_GOLD_END))
    else:
        stops = (
            (0.0, _PROGRESS_FILL_START),
            (0.40, _PROGRESS_FILL_END),
            (1.0, _PROGRESS_FILL_GOLD),
        )
    end = stops[-1][1]

    t = np.linspace(0.0, 1.0, max(1, width), dtype=np.float32)
    xp = np.array([p for p, _ in stops], dtype=np.float32)
    grad_row = np.stack(
        [
            np.interp(
                t, xp,
                np.array([c[ch] for _, c in stops], dtype=np.float32),
            )
            for ch in range(3)
        ],
        axis=1,
    ).astype(np.float32)
    grad_base = np.repeat(grad_row[None, :, :], height, axis=0)
    shade = np.linspace(1.16, 0.80, height, dtype=np.float32)[:, None, None]
    fill_arr = np.empty((height, width, 4), dtype=np.uint8)
    fill_arr[..., :3] = np.clip(grad_base * shade, 0, 255).astype(np.uint8)
    fill_arr[..., 3] = 255
    fill_img = Image.fromarray(fill_arr, mode="RGBA")
    gloss_a = np.linspace(118.0, 0.0, height, dtype=np.float32)
    gloss_a[int(height * 0.55):] = 0.0
    gloss = np.zeros((height, width, 4), dtype=np.uint8)
    gloss[..., :3] = 255
    gloss[..., 3] = np.clip(gloss_a[:, None], 0, 255).astype(np.uint8)
    fill_img.alpha_composite(Image.fromarray(gloss, mode="RGBA"))
    trace = tuple(min(255, int(c * 1.18)) for c in end[:3])

    # Empty 'pending' segments: track nudged toward amber (neutral when
    # complete) so outstanding categories catch the eye.
    if complete:
        empty_fill = _PROGRESS_TRACK
    else:
        empty_fill = tuple(
            int(round(tc * 0.82 + mc * 0.18))
            for tc, mc in zip(_PROGRESS_TRACK, _PROGRESS_MISSING, strict=True)
        )

    seg_w = (width - gap * (n - 1)) / n
    last_filled = count - 1
    for i in range(n):
        x0 = int(round(i * (seg_w + gap)))
        x1 = width if i == n - 1 else int(round(x0 + seg_w))
        sw = x1 - x0
        if sw <= 0:
            continue
        seg_mask = _rounded_mask(sw, height, radius)
        if i < count:
            bar.paste(
                fill_img.crop((x0, 0, x0 + sw, height)), (x0, 0), seg_mask
            )
            # Traced brighter outline around the filled segment.
            ImageDraw.Draw(bar).rounded_rectangle(
                (x0, 0, x1 - 1, height - 1), radius=radius,
                outline=trace + (220,), width=line_w,
            )
            # Leading-edge glow on the last filled segment while in progress.
            if not complete and i == last_filled and count < n:
                glow = Image.new("RGBA", (sw, height), (0, 0, 0, 0))
                gx = sw - max(2, int(round(3 * scale)))
                ImageDraw.Draw(glow).line(
                    (gx, line_w, gx, height - line_w - 1),
                    fill=(255, 255, 255, 150),
                    width=max(1, int(round(2 * scale))),
                )
                glow = glow.filter(
                    ImageFilter.GaussianBlur(max(1, int(round(1.4 * scale))))
                )
                ga = np.asarray(glow.getchannel("A"), dtype=np.float32) * (
                    np.asarray(seg_mask, dtype=np.float32) / 255.0
                )
                glow.putalpha(Image.fromarray(ga.astype(np.uint8), mode="L"))
                bar.alpha_composite(glow, (x0, 0))
        else:
            seg_layer = Image.new("RGBA", (sw, height), (0, 0, 0, 0))
            sd = ImageDraw.Draw(seg_layer)
            sd.rounded_rectangle(
                (0, 0, sw - 1, height - 1), radius=radius, fill=empty_fill
            )
            # Inset top shadow so the empty segment reads as a recessed groove.
            tm = np.asarray(seg_mask, dtype=np.float32) / 255.0
            inset_a = np.linspace(70.0, 0.0, height, dtype=np.float32)
            inset = np.zeros((height, sw, 4), dtype=np.uint8)
            inset[..., 3] = np.clip(
                inset_a[:, None] * tm, 0, 255
            ).astype(np.uint8)
            seg_layer.alpha_composite(Image.fromarray(inset, mode="RGBA"))
            sd.rounded_rectangle(
                (0, 0, sw - 1, height - 1), radius=radius,
                outline=_PROGRESS_TRACK_EDGE + (255,), width=line_w,
            )
            bar.alpha_composite(seg_layer, (x0, 0))

    return bar


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


def _draw_info_grid(
    canvas: Image.Image,
    *,
    norm_rows: list[tuple[str, str, bytes | None]],
    width: int,
    divider_y: int,
    pad: int,
    row_h: int,
    scale: int,
) -> None:
    """Draw the two-column reference grid shared by the progress and
    profile cards.

    Renders a hairline divider at ``divider_y`` then fills each
    ``(label, value, emoji_bytes)`` row into a cell as "[icon]  Label:
    Value" (icon aspect-preserved, bullet fallback), pulling the data
    outward across the full card width. Items fill left-to-right,
    top-to-bottom. ``divider_y`` and ``width`` are in supersampled pixels;
    ``pad`` / ``row_h`` are logical units scaled by ``scale``.
    """
    s = scale

    def sc(v: float) -> int:
        return int(round(v * s))

    draw = ImageDraw.Draw(canvas)
    W = width
    info_label_font = _load_font(sc(16), bold=True)
    info_value_font = _load_font(sc(18), bold=True)
    draw.line(
        [(sc(pad + 8), divider_y), (W - sc(pad + 8), divider_y)],
        fill=_PROGRESS_BG_EDGE,
        width=max(1, sc(1)),
    )

    leading_pad = sc(8)
    icon_gap = sc(8)

    def _draw_cell(
        x0: int, cy: int, label: str, value: str,
        emoji_bytes: bytes | None, lf: ImageFont.FreeTypeFont,
        vf: ImageFont.FreeTypeFont, icon_px: int, right_edge: int,
    ) -> None:
        # Draw "[icon]  Label: Value" anchored at x0, vertically
        # centred on cy and clipped/ellipsized to right_edge.
        if _paste_emoji_icon(
            canvas, emoji_bytes, x0, cy, icon_px, label=label
        ):
            text_x0 = x0 + icon_px + icon_gap
        else:
            bullet = "\u2022 "
            draw.text(
                (x0, cy), bullet, font=lf,
                fill=_PROGRESS_MUTED, anchor="lm",
            )
            text_x0 = x0 + int(draw.textlength(bullet, font=lf))

        label_text = f"{label}: "
        draw.text(
            (text_x0, cy), label_text, font=lf,
            fill=_PROGRESS_MUTED, anchor="lm",
        )
        value_x = text_x0 + draw.textlength(label_text, font=lf)
        max_value_w = right_edge - value_x
        v = _ellipsize(draw, value or "", vf, max_value_w)
        draw.text(
            (value_x, cy), v, font=vf,
            fill=_PROGRESS_TEXT, anchor="lm",
        )

    # Two-column grid: pull the reference data outward to use the full
    # card width. Items fill left-to-right, top-to-bottom.
    left_x = sc(pad) + leading_pad
    inner_right = W - sc(pad) - leading_pad
    col_gap = sc(24)
    mid_x = (left_x + inner_right) // 2
    left_col_right = mid_x - col_gap // 2
    right_col_x = mid_x + col_gap // 2
    icon_px = sc(22)
    row_y = divider_y + sc(14)
    for i in range(0, len(norm_rows), 2):
        cy = row_y + sc(row_h) // 2
        lbl, val, emj = norm_rows[i]
        _draw_cell(
            left_x, cy, lbl, val, emj, info_label_font,
            info_value_font, icon_px, left_col_right,
        )
        if i + 1 < len(norm_rows):
            lbl2, val2, emj2 = norm_rows[i + 1]
            _draw_cell(
                right_col_x, cy, lbl2, val2, emj2, info_label_font,
                info_value_font, icon_px, inner_right,
            )
        row_y += sc(row_h)


def _render_progress_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    count: int,
    target: int,
    info_lines: list[tuple] | None = None,
) -> bytes:
    """Render the progress card and return PNG bytes.

    Composition: circular avatar (left) + gradient bar with overlay text
    on the right. When ``info_lines`` is provided, the card grows
    vertically and renders each ``(label, value)`` pair as a bulleted row
    beneath the bar (used to inline profile / clan / mastery / missing
    info on the verification PASS embed).

    ``info_lines`` entries may be 2-tuples ``(label, value)`` or 3-tuples
    ``(label, value, emoji_png_bytes)``. When emoji bytes are supplied,
    they are composited at the start of the row in place of the bullet.
    The whole card is laid out in logical units and rendered at
    ``_PROGRESS_SS``x so text and icons stay sharp on HiDPI clients.
    """
    progress = max(0.0, min(1.0, count / target)) if target > 0 else 0.0
    complete = target > 0 and count >= target
    s = _PROGRESS_SS

    def sc(v: float) -> int:
        return int(round(v * s))

    # Normalize entries to (label, value, emoji_bytes|None). The
    # "Missing Data" line is pulled out so it can render as an amber
    # callout pill directly beneath the bar (its contextual home) rather
    # than as a row in the reference grid below.
    norm_rows: list[tuple[str, str, bytes | None]] = []
    missing_row: tuple[str, str, bytes | None] | None = None
    for entry in info_lines or []:
        label = entry[0]
        value = entry[1]
        emoji = entry[2] if len(entry) >= 3 else None
        if str(label).strip().lower().startswith("missing"):
            missing_row = (label, value, emoji)
        else:
            norm_rows.append((label, value, emoji))

    # Logical layout sizes (1x); everything below is scaled via sc().
    pad = 22
    row_h = 30
    # Fixed header zone (avatar + name + percent + bar). The avatar is
    # centred in this zone so it never drifts as the footer/grid grow. A
    # status line (the gold "complete" note or the amber missing-data
    # pill) sits just beneath the bar, so reserve extra footer height
    # only when one is shown.
    header_h = 130
    has_status = complete or bool(missing_row)
    base_h = header_h + (42 if has_status else 14)
    info_block_h = 0
    # Regular rows render in two columns so the reference data spreads
    # across the full width instead of bunching on the left; the block
    # height therefore scales with the number of GRID rows (ceil(n / 2)).
    n_grid_rows = (len(norm_rows) + 1) // 2
    if norm_rows:
        info_block_h = 14 + n_grid_rows * row_h + 14
    card_h = base_h + info_block_h

    W, H = sc(_PROGRESS_CARD_W), sc(card_h)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # Shared slate backdrop (see _card_backdrop): a smooth gradient lifted
    # by soft gold/energy glows and a framing vignette, clipped to the
    # rounded, transparent corners so the card blends into Discord's
    # message background.
    panel = _card_backdrop(W, H)
    panel_mask = _rounded_mask(W, H, sc(_PROGRESS_RADIUS))
    canvas.paste(panel, (0, 0), panel_mask)
    # Crisp hairline border tracing the rounded panel edge.
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, W - 1, H - 1),
        radius=sc(_PROGRESS_RADIUS), outline=_PROGRESS_BORDER + (255,),
        width=sc(2),
    )

    avatar_px = sc(_PROGRESS_AVATAR_SIZE)
    avatar = _circular_avatar(avatar_bytes, avatar_px)
    avatar_y = sc((header_h - _PROGRESS_AVATAR_SIZE) // 2)

    # Soft drop shadow under the avatar.
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
    right_x = W - sc(pad)
    draw = ImageDraw.Draw(canvas)

    name_font = _load_font(sc(28), bold=True)
    label_font = _load_font(sc(16), bold=True)
    count_font = _load_font(sc(22), bold=True)
    footer_font = _load_font(sc(14))

    name = display_name or "Member"
    max_name_w = right_x - text_x - sc(10)
    name = _ellipsize(draw, name, name_font, max_name_w)
    draw.text((text_x, sc(18)), name, font=name_font, fill=_PROGRESS_TEXT)

    count_text = f"{count} / {target}"
    count_w = draw.textlength(count_text, font=count_font)
    draw.text(
        (right_x - count_w, sc(60)),
        count_text,
        font=count_font,
        fill=_PROGRESS_TEXT,
    )

    pct_label = f"{int(round(progress * 100))}%"
    pct_prefix = "PROGRESS  \u2022  "
    draw.text((text_x, sc(64)), pct_prefix, font=label_font,
              fill=_PROGRESS_MUTED)
    prefix_w = draw.textlength(pct_prefix, font=label_font)
    draw.text(
        (text_x + prefix_w, sc(64)), pct_label, font=label_font,
        fill=_PROGRESS_ACCENT if complete else _PROGRESS_TEXT,
    )

    bar_h = sc(24)
    bar_w = right_x - text_x
    bar_y = sc(98)
    bar = _segmented_bar(
        bar_w, bar_h, count, target, complete=complete, scale=s
    )
    # Soft drop shadow under the bar for depth.
    bshadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(bshadow).rounded_rectangle(
        (text_x, bar_y + sc(3), text_x + bar_w - 1, bar_y + bar_h - 1 + sc(3)),
        radius=bar_h // 2, fill=(0, 0, 0, 90),
    )
    bshadow = bshadow.filter(ImageFilter.GaussianBlur(sc(4)))
    canvas.alpha_composite(bshadow)
    canvas.alpha_composite(bar, (text_x, bar_y))

    # Status line directly beneath the bar — the contextual home for the
    # "what's left" message. Complete shows the gold success note; an
    # incomplete pass with leftover categories shows an amber "Missing"
    # pill (relocated here from a previously orphaned bottom grid row so
    # it reads as the call to action tied to the progress bar).
    status_y = bar_y + bar_h + sc(9)
    if complete:
        draw.text(
            (text_x, status_y),
            "\u2605  Operator, all roles have been registered!",
            font=footer_font, fill=_PROGRESS_ACCENT,
        )
    elif missing_row:
        amber = _PROGRESS_MISSING
        badge_font = _load_font(sc(15), bold=True)
        badge_text = f"Missing: {missing_row[1]}"
        b_icon_px = sc(17)
        b_gap = sc(7)
        b_pad_x = sc(11)
        b_pad_y = sc(5)
        b_asc, b_desc = badge_font.getmetrics()
        b_has_icon = bool(missing_row[2])
        b_content_h = max(b_icon_px if b_has_icon else 0, b_asc + b_desc)
        b_text_w = int(draw.textlength(badge_text, font=badge_font))
        b_content_w = (b_icon_px + b_gap if b_has_icon else 0) + b_text_w
        badge_w = b_content_w + b_pad_x * 2
        badge_h = b_content_h + b_pad_y * 2
        badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
        ImageDraw.Draw(badge).rounded_rectangle(
            (0, 0, badge_w - 1, badge_h - 1), radius=badge_h // 2,
            fill=amber + (38,), outline=amber + (175,), width=max(1, sc(1)),
        )
        canvas.alpha_composite(badge, (text_x, status_y))
        cx = text_x + b_pad_x
        cy = status_y + badge_h // 2
        if _paste_emoji_icon(
            canvas, missing_row[2], cx, cy, b_icon_px, label="Missing Data"
        ):
            cx += b_icon_px + b_gap
        draw.text(
            (cx, cy), badge_text, font=badge_font, fill=amber, anchor="lm",
        )

    # Render the labeled reference rows beneath the divider in a
    # two-column grid (clan / mastery / profile / platform). Each cell is
    # "[icon]  Label: Value" with the icon aspect-preserved (no stretch)
    # and vertically centred. The missing-data callout lives above, under
    # the bar, so the grid is purely the data that was read.
    if norm_rows:
        _draw_info_grid(
            canvas, norm_rows=norm_rows, width=W, divider_y=sc(base_h),
            pad=pad, row_h=row_h, scale=s,
        )

    buf = io.BytesIO()
    # Keep the alpha channel so the rounded corners stay transparent and
    # blend into Discord's message background instead of showing as black.
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_profile_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    info_lines: list[tuple] | None = None,
    in_game_name: str | None = None,
) -> bytes:
    """Render the "user profile" card and return PNG bytes.

    A sibling of :func:`_render_progress_card_png` with the progress bar
    removed, laid out as two columns split by a gold divider. The left
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
    # Shared slate backdrop: a smooth gradient lifted by soft gold/energy
    # glows and a framing vignette (see _card_backdrop) — no scattered
    # glyphs or line-art emblems, so the panel stays clean behind the
    # content. Composited through the rounded-corner mask so the corners
    # stay clean.
    panel = _card_backdrop(W, H)
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
    name_font = _load_font(sc(28), bold=True)

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
