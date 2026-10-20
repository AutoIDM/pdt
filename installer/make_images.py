"""Draw the pdt icon and the WixUI bitmaps from the AutoIDM logo.

    uv run --no-project --with pillow==13.0.0 python installer/make_images.py

autoidm-logo.webp is the file that https://www.autoidm.com/autoidm-logo.webp
serves. The logo is black on a transparent background, so each image puts
it on white to keep it visible in the Windows dark theme.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
WHITE = (255, 255, 255)
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def place(logo: Image.Image, size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    left, top, width, height = box
    mark = logo.copy()
    mark.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGB", size, WHITE)
    canvas.paste(mark, (left + (width - mark.width) // 2, top + (height - mark.height) // 2), mark)
    return canvas


def main() -> None:
    logo = Image.open(HERE / "autoidm-logo.webp").convert("RGBA")
    place(logo, (256, 256), (8, 8, 240, 240)).save(HERE / "pdt.ico", sizes=ICON_SIZES)
    # WixUI writes each dialog title from the left edge of the banner, as its own bitmap does.
    place(logo, (493, 58), (409, 6, 76, 46)).save(HERE / "banner.bmp")
    # WixUI writes the dialog text to the right of x=180 on the welcome and exit dialogs.
    place(logo, (493, 312), (16, 0, 148, 312)).save(HERE / "dialog.bmp")


if __name__ == "__main__":
    main()
