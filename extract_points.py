"""
フォントグリフから漢字の頂点・制御点を抽出するスクリプト
CSSの font-size / flexbox center と同じ座標変換を使い、
ブラウザ表示と完全一致する座標を出力する
"""
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen
import json
import sys


def extract_glyph_points(font_path, char, container_size=300, font_size=250):
    """フォントから指定文字のグリフ座標を抽出し、CSSレンダリングと同じ変換で出力"""
    font = TTFont(font_path)
    cmap = font.getBestCmap()
    code = ord(char)

    if code not in cmap:
        print(f"エラー: '{char}' (U+{code:04X}) がフォントに見つかりません")
        return None

    glyph_name = cmap[code]
    glyph_set = font.getGlyphSet()
    pen = RecordingPen()
    glyph_set[glyph_name].draw(pen)

    # フォントメトリクス
    units_per_em = font['head'].unitsPerEm
    ascent = font['hhea'].ascent      # 1160 (Klee One)
    descent = font['hhea'].descent    # -288
    advance_width = font['hmtx'][glyph_name][0]

    # === CSSレンダリングと同じ座標変換 ===
    scale = font_size / units_per_em  # 250 / 1000 = 0.25

    # 水平: advance幅をコンテナ中央に配置
    rendered_width = advance_width * scale
    x_offset = (container_size - rendered_width) / 2  # (300-250)/2 = 25

    # 垂直: line-height:1 でのベースライン位置
    content_height = (ascent - descent) * scale
    half_leading = (font_size - content_height) / 2
    line_box_top = (container_size - font_size) / 2   # (300-250)/2 = 25
    baseline_y = line_box_top + half_leading + ascent * scale

    def transform(x, y):
        """フォント座標 → CSS描画と同じスクリーン座標"""
        nx = x_offset + x * scale
        ny = baseline_y - y * scale  # Y軸反転
        return (round(nx, 1), round(ny, 1))

    # パス操作を変換して出力
    contours = []
    current_contour = []

    for op, args in pen.value:
        if op == 'moveTo':
            if current_contour:
                contours.append(current_contour)
            current_contour = [{'type': 'move', 'point': transform(*args[0])}]
        elif op == 'lineTo':
            current_contour.append({'type': 'line', 'point': transform(*args[0])})
        elif op == 'qCurveTo':
            pts = [transform(*p) for p in args]
            current_contour.append({'type': 'qcurve', 'points': pts})
        elif op == 'curveTo':
            pts = [transform(*p) for p in args]
            current_contour.append({'type': 'curve', 'points': pts})
        elif op == 'closePath' or op == 'endPath':
            if current_contour:
                contours.append(current_contour)
                current_contour = []

    if current_contour:
        contours.append(current_contour)

    # 全頂点（重複排除）を抽出
    unique_points = {}
    point_id = 0
    for contour in contours:
        for seg in contour:
            if seg['type'] in ('move', 'line'):
                pt = seg['point']
                key = f"{pt[0]},{pt[1]}"
                if key not in unique_points:
                    unique_points[key] = {
                        'id': chr(65 + point_id) if point_id < 26 else f"P{point_id}",
                        'x': pt[0],
                        'y': pt[1]
                    }
                    point_id += 1
            elif seg['type'] in ('qcurve', 'curve'):
                pt = seg['points'][-1]
                key = f"{pt[0]},{pt[1]}"
                if key not in unique_points:
                    unique_points[key] = {
                        'id': chr(65 + point_id) if point_id < 26 else f"P{point_id}",
                        'x': pt[0],
                        'y': pt[1]
                    }
                    point_id += 1

    return {
        'kanji': char,
        'container_size': container_size,
        'font_size': font_size,
        'contours_count': len(contours),
        'total_points': len(unique_points),
        'points': list(unique_points.values()),
        'contours': contours
    }


if __name__ == '__main__':
    font_path = "fonts/KleeOne-SemiBold.ttf"
    char = sys.argv[1] if len(sys.argv) > 1 else '山'

    result = extract_glyph_points(font_path, char)
    if result:
        print(f"=== 「{result['kanji']}」のグリフ解析 ===")
        print(f"輪郭数: {result['contours_count']}")
        print(f"頂点数: {result['total_points']}")
        print()

        print("--- 頂点一覧 ---")
        for p in result['points']:
            print(f"  {p['id']}: ({p['x']}, {p['y']})")

        # アウトライン座標をJSON形式で出力
        outline = []
        for contour in result['contours']:
            for seg in contour:
                if seg['type'] in ('move', 'line'):
                    outline.append(list(seg['point']))
                elif seg['type'] in ('qcurve', 'curve'):
                    outline.append(list(seg['points'][-1]))

        print()
        print("--- outline JSON ---")
        print(json.dumps(outline, ensure_ascii=False))
