"""Creates scan.jpg: a noisy, faded 'photo of a document' used for the stress test."""
from PIL import Image, ImageDraw, ImageChops

W, H = 3200, 2000
page = Image.new("RGB", (W, H), (222, 218, 205))
d = ImageDraw.Draw(page)
for i in range(60):                                   # fake lines of text
    y = 90 + i * 30
    d.rectangle([160, y, 160 + (i * 97) % 2400 + 400, y + 10], fill=(120, 118, 112))
noise = Image.effect_noise((W, H), 70).convert("RGB")  # camera grain
page = ImageChops.blend(page, noise, 0.25)
page.save("scan.jpg", quality=35)
import os
print("scan.jpg", W, "x", H, round(os.path.getsize("scan.jpg") / 1024), "KB")
