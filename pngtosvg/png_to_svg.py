# ========================================
# PNG to SVG Converter
# Uses VTracer (Fast and Good Quality)
# ========================================

import argparse
import os
import sys

# Check if VTracer is installed
try:
    import vtracer
    VTRACER_AVAILABLE = True
except ImportError:
    VTRACER_AVAILABLE = False
    print("⚠️  VTracer is not installed. Install it using: pip install vtracer")


# ─────────────────────────────────────────────────────────────────────────────
# Quality presets — maps quality level (0-3) to VTracer parameters
# Higher quality = finer detail, smaller speckle filter, tighter curves
# ─────────────────────────────────────────────────────────────────────────────
QUALITY_PRESETS = {
    0: {  # Simple — fast, minimal detail
        "filter_speckle": 20,
        "corner_threshold": 90,
        "length_threshold": 6.0,
        "max_iterations": 5,
        "splice_threshold": 75,
        "color_precision": 4,
        "layer_difference": 32,
    },
    1: {  # Medium — balanced
        "filter_speckle": 10,
        "corner_threshold": 60,
        "length_threshold": 4.0,
        "max_iterations": 8,
        "splice_threshold": 60,
        "color_precision": 6,
        "layer_difference": 20,
    },
    2: {  # High — good detail (default)
        "filter_speckle": 4,
        "corner_threshold": 60,
        "length_threshold": 3.5,
        "max_iterations": 10,
        "splice_threshold": 45,
        "color_precision": 6,
        "layer_difference": 16,
    },
    3: {  # Ultra — maximum detail, larger SVGs
        "filter_speckle": 2,
        "corner_threshold": 45,
        "length_threshold": 2.0,
        "max_iterations": 15,
        "splice_threshold": 30,
        "color_precision": 8,
        "layer_difference": 8,
    },
}


def convert_png_to_svg(input_path, output_path=None, color_mode="color", quality=2):
    """
    Convert a PNG/JPEG/WEBP image to SVG vector format using VTracer.

    Parameters:
        input_path  (str): Path to input image file
        output_path (str): Path to save output SVG (optional, defaults to same name .svg)
        color_mode  (str): 'color' (full color), 'binary' (black & white), or 'gray'
        quality     (int): Detail level 0 (simple) to 3 (ultra detail)

    Returns:
        True on success, False on failure.
    """
    if not os.path.exists(input_path):
        print(f"❌ Error: File not found - {input_path}")
        return False

    if not VTRACER_AVAILABLE:
        print("❌ VTracer is not installed.")
        print("   Install it with:  pip install vtracer")
        return False

    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".svg"

    # Clamp quality to valid range
    quality = max(0, min(3, quality))
    preset = QUALITY_PRESETS[quality]

    print(f"Converting: {input_path}")
    print(f"Output:     {output_path}")
    print(f"Mode: {color_mode} | Quality: {quality}")

    try:
        vtracer.convert_image_to_svg_py(
            input_path,
            output_path,
            colormode=color_mode,
            hierarchical="stacked",
            mode="spline",
            filter_speckle=preset["filter_speckle"],
            color_precision=preset["color_precision"],
            layer_difference=preset["layer_difference"],
            corner_threshold=preset["corner_threshold"],
            length_threshold=preset["length_threshold"],
            max_iterations=preset["max_iterations"],
            splice_threshold=preset["splice_threshold"],
        )

        svg_size = os.path.getsize(output_path)
        print(f"✅ Success! SVG created ({svg_size:,} bytes)")
        return True

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PNG to SVG Converter — powered by VTracer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python png_to_svg.py logo.png
  python png_to_svg.py logo.png -o output.svg -m color -q 3
  python png_to_svg.py photo.jpg -m binary -q 0
        """,
    )
    parser.add_argument("input", help="Input image file path (PNG, JPEG, WEBP)")
    parser.add_argument("-o", "--output", help="Output SVG file path (default: <input>.svg)")
    parser.add_argument(
        "-m", "--mode",
        choices=["color", "binary", "gray"],
        default="color",
        help="Color mode (default: color)",
    )
    parser.add_argument(
        "-q", "--quality",
        type=int,
        choices=[0, 1, 2, 3],
        default=2,
        help="Quality level 0=simple, 1=medium, 2=high, 3=ultra (default: 2)",
    )

    args = parser.parse_args()
    success = convert_png_to_svg(args.input, args.output, args.mode, args.quality)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()