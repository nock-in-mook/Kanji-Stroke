#!/usr/bin/env python3
"""
ストロークデータの視覚検証ツール
フォント参照 + 骨格 + ストロークを重ねてPNG出力
"""
import os, sys, json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

FONT_PATH = Path(__file__).parent / "fonts" / "KleeOne-SemiBold.ttf"
STROKE_DATA_DIR = Path(__file__).parent / "stroke_data"
OUTPUT_DIR = Path(__file__).parent / "viz_output"

W, H = 300, 300
SCALE = 3  # 出力画像を3倍に拡大（900x900）

STROKE_COLORS = [
    (229, 57, 53),    # 赤
    (30, 136, 229),   # 青
    (67, 160, 71),    # 緑
    (251, 140, 0),    # オレンジ
    (142, 36, 170),   # 紫
    (0, 172, 193),    # シアン
    (109, 76, 65),    # 茶
    (84, 110, 122),   # グレー
]


def render_font_gray(kanji, w, h):
    """フォントをグレースケールで描画"""
    font = ImageFont.truetype(str(FONT_PATH), 250)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    draw.text((w // 2, 259), kanji, fill=255, font=font, anchor="ms")
    return img


def render_skeleton(kanji, w, h):
    """骨格ビットマップを生成"""
    import auto_strokes as a
    bmp = a.render_font(kanji)
    skel = bmp.copy()
    a.zhang_suen_thin(skel)
    return skel


def parse_path_points(path_str):
    """パス文字列からポイントリストを抽出"""
    points = []
    parts = path_str.replace('M ', '').replace('L ', '').split()
    for part in parts:
        if ',' in part:
            x, y = part.split(',')
            points.append((float(x), float(y)))
    return points


def draw_stroke_line(draw, points, color, width=10):
    """ストロークを太い線として描画"""
    if len(points) < 2:
        return
    for i in range(len(points) - 1):
        x1, y1 = points[i][0] * SCALE, points[i][1] * SCALE
        x2, y2 = points[i+1][0] * SCALE, points[i+1][1] * SCALE
        draw.line([(x1, y1), (x2, y2)], fill=color + (200,), width=width * SCALE // 3)
    # 丸い端点
    for p in points:
        r = width * SCALE // 6
        cx, cy = p[0] * SCALE, p[1] * SCALE
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (200,))


def visualize_kanji(kanji, save=True):
    """1文字の漢字のストロークを可視化"""
    code = f"{ord(kanji):05x}"
    json_path = STROKE_DATA_DIR / f"{code}.json"

    if not json_path.exists():
        print(f"  {kanji}: JSONなし、スキップ")
        return None

    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 1. フォント参照（薄いグレー）
    font_img = render_font_gray(kanji, W, H)

    # 2. 骨格
    skel = render_skeleton(kanji, W, H)

    # 3. 拡大画像に合成
    out_w, out_h = W * SCALE, H * SCALE
    img = Image.new("RGBA", (out_w, out_h), (255, 255, 255, 255))

    # フォント参照を薄く
    font_scaled = font_img.resize((out_w, out_h), Image.NEAREST)
    font_rgba = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    for y in range(out_h):
        for x in range(out_w):
            v = font_scaled.getpixel((x, y))
            if v > 0:
                font_rgba.putpixel((x, y), (200, 200, 200, min(100, v)))
    # 高速化: numpy使用
    font_arr = np.array(font_scaled)
    font_rgba_arr = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    mask = font_arr > 0
    font_rgba_arr[mask] = [200, 200, 200, 100]
    font_rgba = Image.fromarray(font_rgba_arr, 'RGBA')
    img = Image.alpha_composite(img, font_rgba)

    # 骨格を点線で
    skel_rgba = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    for y in range(H):
        for x in range(W):
            if skel[y, x]:
                # 拡大座標
                for dy in range(SCALE):
                    for dx in range(SCALE):
                        sx, sy = x * SCALE + dx, y * SCALE + dy
                        if sx < out_w and sy < out_h:
                            skel_rgba[sy, sx] = [255, 140, 83, 100]
    skel_img = Image.fromarray(skel_rgba, 'RGBA')
    img = Image.alpha_composite(img, skel_img)

    # 4. ストローク描画
    stroke_layer = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stroke_layer)

    for i, stroke in enumerate(data['strokes']):
        color = STROKE_COLORS[i % len(STROKE_COLORS)]
        points = parse_path_points(stroke['path'])
        draw_stroke_line(draw, points, color)

    img = Image.alpha_composite(img, stroke_layer)

    # ガイド十字線
    guide_layer = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    guide_draw = ImageDraw.Draw(guide_layer)
    mid_x, mid_y = out_w // 2, out_h // 2
    for i in range(0, out_w, 12):
        guide_draw.line([(i, mid_y), (min(i+6, out_w), mid_y)], fill=(200, 200, 200, 80), width=1)
        guide_draw.line([(mid_x, i), (mid_x, min(i+6, out_h))], fill=(200, 200, 200, 80), width=1)
    img = Image.alpha_composite(img, guide_layer)

    if save:
        OUTPUT_DIR.mkdir(exist_ok=True)
        out_path = OUTPUT_DIR / f"{code}_{kanji}.png"
        img.save(str(out_path))
        print(f"  {kanji} → {out_path}")

    return img


def main():
    chars = sys.argv[1:] if len(sys.argv) > 1 else []

    if not chars:
        # stroke_data/ にある全JSONを処理
        for f in sorted(STROKE_DATA_DIR.glob("*.json")):
            with open(f, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            chars.append(data['kanji'])

    print(f"可視化: {len(chars)}文字")
    for kanji in chars:
        if len(kanji) == 1:
            visualize_kanji(kanji)
    print("完了")


if __name__ == '__main__':
    main()
