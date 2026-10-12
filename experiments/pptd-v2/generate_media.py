"""Create the deterministic crop calibration image used by this throwaway probe."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

directory = Path(__file__).parent / "fixture" / "media"
directory.mkdir(parents=True, exist_ok=True)
image = Image.new("RGB", (800, 400), "#e5ebe3")
draw = ImageDraw.Draw(image)
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
for index, color in enumerate(["#D79032", "#E8D4AD", "#BBD3C3", "#166854"]):
    draw.rectangle((index * 200, 0, (index + 1) * 200 - 1, 399), fill=color)
    draw.text((index * 200 + 82, 176), chr(65 + index), font=font, fill="#17342D" if index < 3 else "#FFFFFF")
for x in range(0, 801, 100):
    draw.line((x, 0, x, 400), fill="#FFFFFF", width=2)
for y in range(0, 401, 100):
    draw.line((0, y, 800, y), fill="#FFFFFF", width=2)
draw.rectangle((100, 4, 699, 395), outline="#17342D", width=4)
image.save(directory / "fixture.png")
print(directory / "fixture.png")
