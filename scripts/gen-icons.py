#!/usr/bin/env python3
"""SK-38 п.2: генерация иконок расширения (16/32/48/96) из вектора.

Дизайн: скруглённый фиолетовый тайл с диагональным градиентом (палитра
--accent/--btn-accent theme.css: #8b5cf6 → #6d28d9) + белая молния — тот же
силуэт, что ic-skills в inline-SVG спрайте сайдбара. Цветной тайл с белым
глифом узнаваем и читаем в обеих темах тулбара Firefox (и light, и dark —
иконка не зависит от currentColor, поэтому «обе темы» закрыты одним комплектом).

Рендер: 4x суперсэмплинг + LANCZOS-даунскейл — гладкие края без сглаживающих
библиотек. Зависимость: только Pillow (системный /usr/bin/python3).

Запуск из корня репо:  /usr/bin/python3 scripts/gen-icons.py
"""
import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "extension" / "icons"
SIZES = (16, 32, 48, 96)
SS = 4                      # суперсэмплинг
G = 96                      # дизайн-сетка (координаты рисунка)
C0 = (139, 92, 246)         # #8b5cf6 — верх-лево
C1 = (109, 40, 217)         # #6d28d9 — низ-право
# Молния — силуэт ic-skills (viewBox 24): M13 2 3 14h9l-1 8 10-12h-9l1-8z
BOLT24 = [(13, 2), (3, 14), (12, 14), (11, 22), (21, 10), (12, 10)]


def bolt_poly(scale: float, cx: float, cy: float):
    """Молния из 24-сетки -> центр (cx,cy), масштаб scale (по высоте 20 ед.)."""
    return [((x - 12.0) * scale + cx, (y - 12.0) * scale + cy) for x, y in BOLT24]


def render(size: int) -> Image.Image:
    s = size * SS
    k = s / G
    # диагональный градиент C0→C1 (t = (x+y)/2G), по пикселям — разово и точно
    px = bytearray(s * s * 4)
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2.0 * s)
            i = (y * s + x) * 4
            px[i] = int(C0[0] + (C1[0] - C0[0]) * t)
            px[i + 1] = int(C0[1] + (C1[1] - C0[1]) * t)
            px[i + 2] = int(C0[2] + (C1[2] - C0[2]) * t)
            px[i + 3] = 255
    img = Image.frombytes("RGBA", (s, s), bytes(px))
    # маска тайла: скругление Proton-пропорций (r=22/96)
    tile = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle([4 * k, 4 * k, (G - 4) * k, (G - 4) * k],
                        radius=22 * k, fill=255)
    img.putalpha(tile)
    # молния: та же геометрия на всех размерах (узнаваемость = масштаб, не форма)
    ddraw = ImageDraw.Draw(img)
    poly = [(x * k, y * k) for x, y in bolt_poly(2.9, 48.0, 49.0)]
    ddraw.polygon(poly, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        p = OUT / f"icon-{size}.png"
        render(size).save(p, "PNG")
        print(f"{p.relative_to(ROOT)}  {size}x{size}")


if __name__ == "__main__":
    main()
