#!/usr/bin/env python3
"""生成 DSH Cookie Cleaner 的 Windows 图标 app.ico。

设计：深蓝色（#1E3A8A）圆角矩形背景，中间白色粗体 "DSH"。
依赖 Pillow（仅开发/打包阶段使用，主程序运行时不依赖 Pillow）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


CANVAS_SIZE = 1024
BACKGROUND = "#1E3A8A"
FOREGROUND = "#FFFFFF"
ICON_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先使用 Windows 自带的粗体字体，找不到时退回 Pillow 默认字体。"""

    candidates = (
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue

    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str) -> ImageFont.ImageFont:
    """在固定安全区内找到尽可能大的字号。"""

    max_width = int(CANVAS_SIZE * 0.74)
    max_height = int(CANVAS_SIZE * 0.34)
    for size in range(460, 80, -5):
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= max_width and height <= max_height:
            return font
    return _load_font(80)


def make_icon(output: Path = Path("app.ico")) -> Path:
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 留一点透明外边距；圆角半径约为边长的 21%，保持扁平、简洁。
    inset = 18
    draw.rounded_rectangle(
        (inset, inset, CANVAS_SIZE - inset, CANVAS_SIZE - inset),
        radius=218,
        fill=BACKGROUND,
    )

    text = "DSH"
    font = _fit_font(draw, text)
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = (CANVAS_SIZE - width) / 2 - bbox[0]
    y = (CANVAS_SIZE - height) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=FOREGROUND)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="ICO", sizes=ICON_SIZES, bitmap_format="bmp")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DSH Cookie Cleaner 的 app.ico")
    parser.add_argument(
        "-o",
        "--output",
        default="app.ico",
        help="输出文件路径（默认：app.ico）",
    )
    args = parser.parse_args()
    path = make_icon(Path(args.output))
    print(f"图标已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
