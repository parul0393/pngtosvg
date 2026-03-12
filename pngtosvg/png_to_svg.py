#!/usr/bin/env python3
"""
Convert PNG images to SVG using potrace (pypotrace or potracer).
Traces dark areas of the image as vector paths.
Uses potracer (pure Python) by default; works with pypotrace if C libs are installed.
"""

import sys
from pathlib import Path
from typing import Optional, List, Tuple

import numpy as np
from PIL import Image

try:
    import potrace
except ImportError:
    raise ImportError("Install potracer: pip install potracer (or pypotrace with C libs)")

# When input is a PIL Image we use the PIL path (Bitmap(img, blacklevel)); when numpy we use numpy path.


def _ensure_rgb(img: Image.Image, background_rgb: Tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """Convert to RGB, compositing RGBA onto the given background (default white) so transparent pixels don't become black."""
    if img.mode == "RGB":
        return img
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, background_rgb)
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB")


def _rgba_foreground_mask_and_color(img: Image.Image) -> Optional[Tuple[Image.Image, str]]:
    """
    For RGBA images: trace only visible (alpha >= 128) pixels and use their dominant color.
    Returns (bilevel_image, fill_hex) or None if not RGBA. Use this to avoid tracing black/transparent background.
    """
    if img.mode != "RGBA":
        return None
    arr = np.array(img)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    visible = a >= 128
    if not np.any(visible):
        return None
    # Check if there are non-black visible pixels (colored logo on transparent bg)
    not_black = (r > 40) | (g > 40) | (b > 40)
    fg_colored = visible & not_black
    if np.any(fg_colored):
        # Colored logo: trace only colored pixels, use their color
        fg_pixels = arr[fg_colored][:, :3]
        cr = int(np.median(fg_pixels[:, 0]))
        cg = int(np.median(fg_pixels[:, 1]))
        cb = int(np.median(fg_pixels[:, 2]))
        fill = f"#{cr:02x}{cg:02x}{cb:02x}"
        bilevel = np.where(fg_colored, 0, 255).astype(np.uint8)
    else:
        # Black logo on transparent bg: trace all visible pixels, fill black
        fill = "#000000"
        bilevel = np.where(visible, 0, 255).astype(np.uint8)
    return (Image.fromarray(bilevel), fill)


def _dominant_color(img: Image.Image, background_threshold: int = 220) -> str:
    """Get dominant non-background color from image as hex #rrggbb for SVG fill."""
    img = _ensure_rgb(img)
    pixels = list(img.getdata())
    gray_vals = [0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] for p in pixels]
    mean_gray = sum(gray_vals) / len(gray_vals) if pixels else 0
    if mean_gray < 80:
        non_bg = [p for p, g in zip(pixels, gray_vals) if g > 40]
        if not non_bg:
            non_bg = [p for p in pixels if max(p) > 0]
    else:
        non_bg = [p for p in pixels if max(p) < background_threshold or min(p) < 256 - background_threshold]
    if not non_bg:
        return "#000000"
    r = sorted(p[0] for p in non_bg)[len(non_bg) // 2]
    g = sorted(p[1] for p in non_bg)[len(non_bg) // 2]
    b = sorted(p[2] for p in non_bg)[len(non_bg) // 2]
    return f"#{r:02x}{g:02x}{b:02x}"


def _grayscale_to_bilevel(gray: np.ndarray) -> np.ndarray:
    """
    Return bilevel image for tracing: 0 = foreground (logo), 255 = background.
    Handles both light-bg (dark logo) and dark-bg (light logo) images.
    """
    mean = float(gray.mean())
    if mean < 80:
        # Mostly dark image: foreground is the lighter part (logo)
        thresh = 40
        return np.where(gray > thresh, 0, 255).astype(np.uint8)
    if mean > 170:
        # Mostly light image: foreground is the darker part (logo)
        thresh = 200
        return np.where(gray < thresh, 0, 255).astype(np.uint8)
    # Middle: use 128
    return np.where(gray < 128, 0, 255).astype(np.uint8)


def _foreground_color_layers(img: Image.Image, bg_threshold: int = 220) -> List[Tuple[Image.Image, str]]:
    """
    Split RGB image into per-color layers for multi-color tracing.
    Returns list of (bilevel_PIL_image, fill_hex). Bilevel: 0 = this color (traced), 255 = other/background.
    """
    img = _ensure_rgb(img)
    arr = np.array(img)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mean_bright = float(r.mean() + g.mean() + b.mean()) / 3
    # Background: light pixels (light bg) or very dark (dark bg)
    if mean_bright < 80:
        bg = (r < 25) & (g < 25) & (b < 25)
    else:
        bg = (r >= bg_threshold) & (g >= bg_threshold) & (b >= bg_threshold)
    # Magenta/pink: high R and B, low G (ring logo etc.) - check before red so we get accurate fill
    is_magenta = (~bg) & (r > 80) & (b > 80) & (g < r * 0.7) & (g < b * 0.7)
    # Red: dominant red channel (red > blue)
    is_red = (~bg) & (r > 100) & (r >= g * 1.1) & (r >= b * 1.1) & (~is_magenta)
    # Blue: dominant blue channel (e.g. influns logo #0D6DBF)
    is_blue = (~bg) & (b > 60) & (b >= r * 1.05) & (b >= g * 1.05) & (~is_red) & (~is_magenta)
    # Black/dark: low RGB (exclude red/blue/magenta)
    is_black = (~bg) & (r < 120) & (g < 120) & (b < 120) & (~is_red) & (~is_blue) & (~is_magenta)
    layers = []
    if np.any(is_magenta):
        mag_pixels = arr[is_magenta]
        cr, cg, cb = int(np.median(mag_pixels[:, 0])), int(np.median(mag_pixels[:, 1])), int(np.median(mag_pixels[:, 2]))
        fill_mag = f"#{cr:02x}{cg:02x}{cb:02x}"
        gray_mag = np.where(is_magenta, 0, 255).astype(np.uint8)
        layers.append((Image.fromarray(gray_mag), fill_mag))
    if np.any(is_red):
        red_pixels = arr[is_red]
        cr, cg, cb = int(np.median(red_pixels[:, 0])), int(np.median(red_pixels[:, 1])), int(np.median(red_pixels[:, 2]))
        fill_red = f"#{cr:02x}{cg:02x}{cb:02x}"
        gray_red = np.where(is_red, 0, 255).astype(np.uint8)
        layers.append((Image.fromarray(gray_red), fill_red))
    if np.any(is_blue):
        blue_pixels = arr[is_blue]
        cr, cg, cb = int(np.median(blue_pixels[:, 0])), int(np.median(blue_pixels[:, 1])), int(np.median(blue_pixels[:, 2]))
        fill_blue = f"#{cr:02x}{cg:02x}{cb:02x}"
        gray_blue = np.where(is_blue, 0, 255).astype(np.uint8)
        layers.append((Image.fromarray(gray_blue), fill_blue))
    if np.any(is_black):
        fg_count = np.sum(~bg)
        black_count = np.sum(is_black)
        # Skip black layer if it's the vast majority of foreground (likely background painted black)
        if black_count < fg_count * 0.95:
            black_pixels = arr[is_black]
            cr, cg, cb = int(np.median(black_pixels[:, 0])), int(np.median(black_pixels[:, 1])), int(np.median(black_pixels[:, 2]))
            fill_black = f"#{cr:02x}{cg:02x}{cb:02x}"
            gray_black = np.where(is_black, 0, 255).astype(np.uint8)
            layers.append((Image.fromarray(gray_black), fill_black))
    return layers


def _point_xy(p) -> tuple:
    """Get (x, y) from a point (tuple or object with .x .y)."""
    if hasattr(p, "x") and hasattr(p, "y"):
        return (p.x, p.y)
    return (p[0], p[1])


def curve_to_path_d(curve, width: int, height: int, flip_y: bool = False) -> str:
    """Convert a potrace Curve to an SVG path 'd' string. flip_y=False (default) matches original: triangle up, text below."""
    parts = []

    def pt(p):
        x, y = _point_xy(p)
        if flip_y:
            return (x, height - 1 - y)
        return (x, y)

    def add_curve(c):
        segs = getattr(c, "segments", [])
        if not segs:
            return
        sx, sy = pt(c.start_point)
        parts.append(f"M{sx:.2f},{sy:.2f}")
        for seg in segs:
            ex, ey = pt(seg.end_point)
            if seg.is_corner:
                cx, cy = pt(seg.c)
                parts.append(f"L{cx:.2f},{cy:.2f}L{ex:.2f},{ey:.2f}")
            else:
                c1x, c1y = pt(seg.c1)
                c2x, c2y = pt(seg.c2)
                parts.append(f"C{c1x:.2f},{c1y:.2f} {c2x:.2f},{c2y:.2f} {ex:.2f},{ey:.2f}")
        parts.append("Z")
        for child in (getattr(c, "children", None) or []):
            add_curve(child)

    add_curve(curve)
    return " ".join(parts)


def trace_to_svg_pypotrace(data, width: int, height: int, **trace_kw) -> str:
    """Trace using pypotrace (numpy bitmap)."""
    import numpy as np
    bmp = potrace.Bitmap(data)
    path = bmp.trace(**trace_kw)
    curves = getattr(path, "curves_tree", None) or getattr(path, "curves", [])
    path_d_parts = [curve_to_path_d(c, width, height, trace_kw.get("flip_y", False)) for c in curves]
    return " ".join(path_d_parts)


def trace_to_svg_potracer(img: Image.Image, **trace_kw) -> str:
    """Trace using potracer (PIL Image)."""
    bm = potrace.Bitmap(img, blacklevel=0.5)
    path = bm.trace(
        turdsize=trace_kw.pop("turdsize", 2),
        turnpolicy=trace_kw.pop("turnpolicy", getattr(potrace, "POTRACE_TURNPOLICY_MINORITY", getattr(potrace, "TURNPOLICY_MINORITY", None))),
        alphamax=trace_kw.pop("alphamax", 1),
        opticurve=trace_kw.pop("opticurve", True),
        opttolerance=trace_kw.pop("opttolerance", 0.2),
    )
    width, height = img.size
    curves = getattr(path, "curves_tree", None) or getattr(path, "curves", [])
    flip_y = trace_kw.get("flip_y", False)
    path_d_parts = [curve_to_path_d(c, width, height, flip_y) for c in curves]
    return " ".join(path_d_parts)


def trace_to_svg(data_or_image, width: int = None, height: int = None, fill_color: str = "#000000", **trace_kw) -> str:
    """Trace bitmap and return SVG document string. Dispatches to pypotrace or potracer."""
    has_image = hasattr(data_or_image, "size") and hasattr(data_or_image, "convert")
    if has_image:
        img = data_or_image
        if img.mode != "L":
            img = img.convert("L")
        width, height = img.size
        gray = np.array(img)
        bilevel = _grayscale_to_bilevel(gray)
        path_d = trace_to_svg_potracer(Image.fromarray(bilevel), **trace_kw)
    else:
        data = data_or_image if isinstance(data_or_image, np.ndarray) else np.array(Image.open(data_or_image).convert("L"))
        if data.ndim == 2:
            height, width = data.shape
            # Ensure bilevel: 0 = background, 1 = foreground (traced)
            if data.dtype != np.uint32:
                data = (data <= (data.max() + data.min()) / 2).astype(np.uint32)
        else:
            raise ValueError("Expected 2D array or PIL Image")
        path_d = trace_to_svg_pypotrace(data, width, height, **trace_kw)
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <path fill="{fill_color}" fill-rule="evenodd" d="{path_d}"/>
</svg>'''
    return svg


def trace_to_svg_multicolor(rgb_img: Image.Image, **trace_kw) -> str:
    """Trace by color layer so red and black (etc.) get correct fills. Returns full SVG string."""
    width, height = rgb_img.size
    # For RGBA: trace only visible (alpha>=128) pixels with their color, so we don't trace black background
    rgba_layer = _rgba_foreground_mask_and_color(rgb_img)
    if rgba_layer is not None:
        bilevel_img, fill_color = rgba_layer
        path_d = trace_to_svg_potracer(bilevel_img, **trace_kw)
        if path_d.strip():
            return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <path fill="{fill_color}" fill-rule="evenodd" d="{path_d}"/>
</svg>'''
    img = _ensure_rgb(rgb_img)
    layers = _foreground_color_layers(img)
    if len(layers) <= 1:
        # Single color or none: fall back to grayscale trace + dominant color
        fill = _dominant_color(rgb_img)
        gray = np.array(rgb_img.convert("L"))
        bilevel = _grayscale_to_bilevel(gray)
        path_d = trace_to_svg_potracer(Image.fromarray(bilevel), **trace_kw)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <path fill="{fill}" fill-rule="evenodd" d="{path_d}"/>
</svg>'''
    path_lines = []
    for bilevel_img, fill_color in layers:
        path_d = trace_to_svg_potracer(bilevel_img, **trace_kw)
        if path_d.strip():
            path_lines.append(f'  <path fill="{fill_color}" fill-rule="evenodd" d="{path_d}"/>')
    if not path_lines:
        fill = _dominant_color(rgb_img)
        gray = np.array(rgb_img.convert("L"))
        bilevel = _grayscale_to_bilevel(gray)
        path_d = trace_to_svg_potracer(Image.fromarray(bilevel), **trace_kw)
        path_lines = [f'  <path fill="{fill}" fill-rule="evenodd" d="{path_d}"/>']
    paths_xml = "\n".join(path_lines)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
{paths_xml}
</svg>'''


def png_to_svg(
    png_path: Path,
    svg_path: Optional[Path] = None,
    threshold: int = 128,
    fill_color: Optional[str] = None,
    flip_y: bool = False,
    **trace_kw,
) -> Path:
    """Convert a PNG file to SVG. Returns path to the written SVG file.
    fill_color: SVG path fill (default: dominant color from image).
    flip_y: If False (default), SVG matches original (triangle up, text below). Set True only if result is upside down.
    """
    png_path = Path(png_path)
    if svg_path is None:
        svg_path = png_path.with_suffix(".svg")

    img = Image.open(png_path)
    trace_kw.setdefault("flip_y", flip_y)
    # Use multi-color tracing so red triangle + black text get correct fills; fallback to single fill if one color
    if fill_color is not None:
        img_rgb = _ensure_rgb(img)
        svg_content = trace_to_svg(img_rgb.convert("L"), fill_color=fill_color, **trace_kw)
    else:
        # Pass original so RGBA can use alpha mask (e.g. magenta ring on transparent/black)
        svg_content = trace_to_svg_multicolor(img, **trace_kw)
    svg_path.write_text(svg_content, encoding="utf-8")
    return svg_path


def main():
    script_dir = Path(__file__).resolve().parent

    # Default → scan script directory
    pngs = list(script_dir.glob("*.png"))

    # If user provided input
    if len(sys.argv) > 1:
        input_path = Path(sys.argv[1])

        if not input_path.exists():
            print(f"Path not found: {input_path}", file=sys.stderr)
            sys.exit(1)

        if input_path.is_dir():
            pngs = list(input_path.glob("*.png"))

            if not pngs:
                print("No PNG files found in given folder.", file=sys.stderr)
                sys.exit(1)
        else:
            pngs = [input_path]

    if not pngs:
        print("No PNG files found.", file=sys.stderr)
        sys.exit(1)

    for png in sorted(pngs):
        out = png.with_suffix(".svg")

        try:
            png_to_svg(png, svg_path=out)
            print(f"OK: {png.name} -> {out.name}")
        except Exception as e:
            print(f"Error converting {png.name}: {e}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()















    