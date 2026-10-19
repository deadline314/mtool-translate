"""產生 assets/app.ico（只有改圖示時才需要執行，需要 Pillow）：python tools/make_icon.py"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "app.ico")
FONTS = [r"C:\Windows\Fonts\msjhbd.ttc", r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"]


def draw(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, size - 8, size - 8), radius=52, fill=(26, 95, 180, 255))
    d.rounded_rectangle((8, size * 0.62, size - 8, size - 8), radius=52, fill=(18, 72, 140, 255))
    d.rectangle((8, size * 0.62, size - 8, size * 0.62 + 52), fill=(18, 72, 140, 255))
    font_path = next(p for p in FONTS if os.path.exists(p))
    font = ImageFont.truetype(font_path, int(size * 0.56))
    d.text((size / 2, size * 0.44), "譯", font=font, fill="white", anchor="mm")
    small = ImageFont.truetype(font_path, int(size * 0.15))
    d.text((size / 2, size * 0.81), "MTool", font=small, fill=(220, 232, 250, 255), anchor="mm")
    return img


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("saved", OUT)
