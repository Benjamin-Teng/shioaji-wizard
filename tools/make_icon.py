"""產生 src/shioaji_wizard/static/favicon.ico（自製，不沿用任何其他專案的 icon）。

設計：紙白圓角方塊＋靛藍邊框（對應 UI 的紙本「檢核單」語彙），中央一顆印泥紅的圓形印章，
章內一個白色勾——「蓋章通過」。16px 仍看得出紅圓＋白勾。

用法（Pillow 只在產 icon 時需要，不列入專案依賴）：
    uv run --with pillow python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw  # ty: ignore[unresolved-import] — 只在產 icon 時用 uv run --with pillow

OUT = Path(__file__).resolve().parents[1] / "src" / "shioaji_wizard" / "static" / "favicon.ico"
PAPER = (245, 246, 242, 255)
INK_BLUE = (36, 69, 107, 255)
SEAL = (184, 51, 42, 255)
WHITE = (255, 255, 255, 255)


def render(size: int) -> Image.Image:
    # 以 4× 超取樣畫再縮，邊緣才平滑
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 256  # 以 256 為設計座標
    # 紙白圓角方塊＋靛藍邊
    r = 56 * u
    d.rounded_rectangle(
        (6 * u, 6 * u, s - 6 * u, s - 6 * u), radius=r, fill=PAPER, outline=INK_BLUE, width=int(10 * u)
    )
    # 印章：紅圓＋白色內圈
    cx, cy, rad = 128 * u, 128 * u, 82 * u
    d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=SEAL)
    ring = 68 * u
    d.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=WHITE, width=int(6 * u))
    # 勾
    w = int(24 * u)
    pts = [(88 * u, 130 * u), (116 * u, 160 * u), (172 * u, 100 * u)]
    d.line(pts, fill=WHITE, width=w, joint="curve")
    for p in pts:
        d.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = [render(n) for n in sizes]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[1:])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes), sizes={sizes}")
    # 預覽用 PNG 放到 dist/（不進 static、不進 bundle）
    preview = OUT.parents[3] / "dist" / "favicon-preview.png"
    preview.parent.mkdir(exist_ok=True)
    frames[0].save(preview)
    print(f"preview {preview}")


if __name__ == "__main__":
    main()
