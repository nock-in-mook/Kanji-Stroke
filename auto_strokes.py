#!/usr/bin/env python3
"""
骨格グラフ方式による自動ストローク登録

処理フロー:
  1. Klee One フォントを300×300に描画 (PIL)
  2. Zhang-Suen 細線化 → 骨格ビットマップ
  3. 骨格からグラフ構築 (端点 + 分岐点 → ノード、間のピクセル列 → エッジ)
  4. KanjiVG SVGを取得 → 各画の始点・終点を抽出
  5. 始点・終点をグラフノードにマッチング → 最短経路探索
  6. RDP間引き → 制御点 → JSON出力
"""

import sys
import os
import json
import re
import heapq
from pathlib import Path

# Windows cp932エンコーディング問題を回避
os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import requests

# === 定数 ===
FONT_PATH = Path(__file__).parent / "fonts" / "KleeOne-SemiBold.ttf"
OUTPUT_DIR = Path(__file__).parent / "stroke_data"
W, H = 300, 300
FONT_SIZE = 250
BASELINE_Y = 259  # Klee One メトリクス: ascent=1160, descent=288, unitsPerEm=1000
KVG_SCALE = 300 / 109  # KanjiVG座標 → 300×300

# 8近傍方向
DIRS = [(-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)]


# ============================================================
# 1. フォント描画
# ============================================================

def render_font(kanji: str) -> np.ndarray:
    """Klee One でフォントを300×300に描画、二値化して返す"""
    font = ImageFont.truetype(str(FONT_PATH), FONT_SIZE)
    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)
    # anchor='ms' = middle-baseline
    draw.text((W // 2, BASELINE_Y), kanji, fill=255, font=font, anchor="ms")
    bmp = np.array(img)
    return (bmp > 128).astype(np.uint8)


# ============================================================
# 2. Zhang-Suen 細線化
# ============================================================

def zhang_suen_thin(bmp: np.ndarray) -> np.ndarray:
    """Zhang-Suen 細線化。入力を直接変更する"""
    h, w = bmp.shape
    changed = True
    while changed:
        changed = False
        for step in range(2):
            # パディング付きで近傍を取得
            padded = np.pad(bmp, 1, mode='constant')
            # 8近傍 (P2〜P9、時計回り: 上,右上,右,右下,下,左下,左,左上)
            P2 = padded[0:h, 1:w+1]   # 上
            P3 = padded[0:h, 2:w+2]   # 右上
            P4 = padded[1:h+1, 2:w+2] # 右
            P5 = padded[2:h+2, 2:w+2] # 右下
            P6 = padded[2:h+2, 1:w+1] # 下
            P7 = padded[2:h+2, 0:w]   # 左下
            P8 = padded[1:h+1, 0:w]   # 左
            P9 = padded[0:h, 0:w]     # 左上

            neighbors = [P2, P3, P4, P5, P6, P7, P8, P9]
            B = sum(neighbors)  # 黒ピクセルの数

            # 遷移数A: 0→1の遷移回数
            A = np.zeros_like(bmp, dtype=np.int32)
            for i in range(8):
                A += ((neighbors[i] == 0) & (neighbors[(i+1) % 8] == 1)).astype(np.int32)

            # 共通条件
            cond = (bmp == 1) & (B >= 2) & (B <= 6) & (A == 1)

            if step == 0:
                cond &= (P2 * P4 * P6 == 0)
                cond &= (P4 * P6 * P8 == 0)
            else:
                cond &= (P2 * P4 * P8 == 0)
                cond &= (P2 * P6 * P8 == 0)

            if np.any(cond):
                bmp[cond] = 0
                changed = True

    return bmp


def bridge_skeleton_gaps(skel: np.ndarray, font_bmp: np.ndarray, max_gap: int = 20) -> int:
    """
    骨格の切断されたコンポーネント間をブリッジ。
    フォントビットマップ内で最短ペアを見つけ、直線でつなぐ。
    returns: ブリッジした回数
    """
    from collections import deque
    h, w = skel.shape

    # 連結成分を求める
    def find_components():
        visited = np.zeros_like(skel, dtype=bool)
        components = []
        for y in range(h):
            for x in range(w):
                if skel[y, x] and not visited[y, x]:
                    comp = []
                    queue = deque([(x, y)])
                    visited[y, x] = True
                    while queue:
                        cx, cy = queue.popleft()
                        comp.append((cx, cy))
                        for dx, dy in DIRS:
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < w and 0 <= ny < h and skel[ny, nx] and not visited[ny, nx]:
                                visited[ny, nx] = True
                                queue.append((nx, ny))
                    components.append(comp)
        return components

    bridges = 0
    components = find_components()
    if len(components) <= 1:
        return 0

    # ピクセル数が多い順にソート
    components.sort(key=len, reverse=True)
    main_comp_set = set(components[0])

    # 小コンポーネントを主コンポーネントに接続
    for comp in components[1:]:
        if len(comp) < 3:
            continue  # 極小コンポーネントは無視

        # 主コンポーネントとの最短距離ペアを見つける
        best_dist = max_gap + 1
        best_pair = None
        comp_set = set(comp)

        # サンプリングで高速化（全ペアは遅い）
        step_main = max(1, len(components[0]) // 200)
        step_comp = max(1, len(comp) // 50)
        for i in range(0, len(components[0]), step_main):
            mx, my = components[0][i]
            for j in range(0, len(comp), step_comp):
                cx, cy = comp[j]
                d = ((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_pair = ((mx, my), (cx, cy))

        if best_pair and best_dist <= max_gap:
            # 直線でブリッジ
            (x1, y1), (x2, y2) = best_pair
            steps = max(abs(x2 - x1), abs(y2 - y1))
            if steps > 0:
                for s in range(steps + 1):
                    t = s / steps
                    bx = round(x1 + (x2 - x1) * t)
                    by = round(y1 + (y2 - y1) * t)
                    if 0 <= bx < w and 0 <= by < h:
                        skel[by, bx] = 1
                bridges += 1
                # 主コンポーネントにマージ
                main_comp_set.update(comp_set)
                components[0] = list(main_comp_set)

    return bridges


# ============================================================
# 3. 骨格グラフ構築
# ============================================================

def neighbor_count(skel: np.ndarray, x: int, y: int) -> int:
    """(x,y)の8近傍の骨格ピクセル数"""
    h, w = skel.shape
    count = 0
    for dx, dy in DIRS:
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h:
            count += skel[ny, nx]
    return count


def build_graph(skel: np.ndarray):
    """
    骨格ビットマップからグラフを構築。
    returns: (nodes, edges, adjacency)
      nodes: [{id, x, y, type}]  type='end'|'branch'
      edges: [{from_id, to_id, pixels:[(x,y),...], length}]
      adjacency: {node_id: [(neighbor_id, edge_idx), ...]}
    """
    h, w = skel.shape

    # 全骨格ピクセルの近傍数を計算
    nc = np.zeros_like(skel, dtype=np.int32)
    for y in range(h):
        for x in range(w):
            if skel[y, x]:
                nc[y, x] = neighbor_count(skel, x, y)

    # ノード候補: 端点(nc==1) + 分岐点(nc>=3)
    raw_nodes = []
    for y in range(h):
        for x in range(w):
            if skel[y, x] and (nc[y, x] == 1 or nc[y, x] >= 3):
                raw_nodes.append((x, y, 'end' if nc[y, x] == 1 else 'branch'))

    # 近接ノード(5px以内)をマージ → スーパーノード
    MERGE_DIST = 5
    merged = []
    used = [False] * len(raw_nodes)
    for i in range(len(raw_nodes)):
        if used[i]:
            continue
        cluster = [i]
        used[i] = True
        for j in range(i + 1, len(raw_nodes)):
            if used[j]:
                continue
            dx = raw_nodes[i][0] - raw_nodes[j][0]
            dy = raw_nodes[i][1] - raw_nodes[j][1]
            if dx * dx + dy * dy <= MERGE_DIST * MERGE_DIST:
                cluster.append(j)
                used[j] = True
        # クラスタの重心
        cx = sum(raw_nodes[k][0] for k in cluster) / len(cluster)
        cy = sum(raw_nodes[k][1] for k in cluster) / len(cluster)
        # タイプ: 分岐点が1つでもあれば分岐点
        ntype = 'branch' if any(raw_nodes[k][2] == 'branch' for k in cluster) else 'end'
        merged.append({
            'id': len(merged),
            'x': round(cx),
            'y': round(cy),
            'type': ntype,
            'pixels': set((raw_nodes[k][0], raw_nodes[k][1]) for k in cluster)
        })

    # ノードマップ: (x,y) → node_id
    node_map = {}
    for node in merged:
        for px, py in node['pixels']:
            node_map[(px, py)] = node['id']
        # ノード座標自体もマップに（マージで重心がズレた場合のため近傍も登録）
        node_map[(node['x'], node['y'])] = node['id']

    # エッジ: ノードからトレースして次のノードまで
    edges = []
    adjacency = {n['id']: [] for n in merged}
    visited_edges = set()  # (min_id, max_id) で重複防止

    for node in merged:
        for px, py in node['pixels']:
            for dx, dy in DIRS:
                nx, ny = px + dx, py + dy
                if not (0 <= nx < w and 0 <= ny < h) or not skel[ny, nx]:
                    continue
                if (nx, ny) in node['pixels']:
                    continue  # 同じスーパーノード内

                # トレース開始
                trace = [(px, py), (nx, ny)]
                visited = {(px, py), (nx, ny)}
                cx, cy = nx, ny

                while True:
                    # 別のノードに到達したか？
                    if (cx, cy) in node_map and node_map[(cx, cy)] != node['id']:
                        break

                    # 次のピクセルを探す
                    found = False
                    for ddx, ddy in DIRS:
                        nnx, nny = cx + ddx, cy + ddy
                        if not (0 <= nnx < w and 0 <= nny < h) or not skel[nny, nnx]:
                            continue
                        if (nnx, nny) in visited:
                            continue
                        visited.add((nnx, nny))
                        trace.append((nnx, nny))
                        cx, cy = nnx, nny
                        found = True
                        break
                    if not found:
                        break

                # 終端がノードか確認
                end_node_id = node_map.get((cx, cy))
                if end_node_id is not None and end_node_id != node['id']:
                    edge_key = (min(node['id'], end_node_id), max(node['id'], end_node_id))
                    if edge_key not in visited_edges:
                        visited_edges.add(edge_key)
                        edge_idx = len(edges)
                        edges.append({
                            'from_id': node['id'],
                            'to_id': end_node_id,
                            'pixels': trace,
                            'length': len(trace)
                        })
                        adjacency[node['id']].append((end_node_id, edge_idx))
                        adjacency[end_node_id].append((node['id'], edge_idx))

    # ノード情報からpixelsセットを除外（JSON化のため）
    nodes = [{'id': n['id'], 'x': n['x'], 'y': n['y'], 'type': n['type']} for n in merged]
    return nodes, edges, adjacency


# ============================================================
# 4. KanjiVG取得・パース
# ============================================================

def fetch_kanjivg(kanji: str) -> str:
    """KanjiVGのSVGデータをGitHubから取得"""
    code = f"{ord(kanji):05x}"
    url = f"https://raw.githubusercontent.com/KanjiVG/kanjivg/master/kanji/{code}.svg"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"KanjiVGにデータなし: {kanji} (HTTP {resp.status_code})")
    return resp.text


def _cubic_bezier_sample(p0, p1, p2, p3, n_samples=10):
    """3次Bezier曲線をn_samples分割でサンプリング（始点は含まない）"""
    pts = []
    for i in range(1, n_samples + 1):
        t = i / n_samples
        u = 1 - t
        x = u*u*u*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t*t*t*p3[0]
        y = u*u*u*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t*t*t*p3[1]
        pts.append((x, y))
    return pts


def parse_svg_path(d_attr: str):
    """
    SVGパスのd属性をパースして座標リストを返す。
    M, m, C, c, S, s, L, l, H, h, V, v, Z コマンドに対応。
    Bezier曲線は10分割でサンプリングし、曲線上の中間点も含む。
    returns: [(x, y), ...] 全ポイント（始点、曲線サンプル点、終点）
    """
    BEZIER_SAMPLES = 10  # Bezier曲線の分割数

    # トークン化: コマンド文字 + 数値列
    tokens = re.findall(r'[MmCcSsLlHhVvZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d_attr)

    points = []
    cx, cy = 0.0, 0.0  # 現在位置
    start_x, start_y = 0.0, 0.0  # パス開始位置
    last_cp2 = None  # S/s コマンド用: 前のBezierの第2制御点
    i = 0

    def next_num():
        nonlocal i
        i += 1
        return float(tokens[i])

    while i < len(tokens):
        cmd = tokens[i]

        if cmd == 'M':
            cx, cy = next_num(), next_num()
            start_x, start_y = cx, cy
            points.append((cx, cy))
            last_cp2 = None
            # M の後の暗黙の L
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                cx, cy = next_num(), next_num()
                points.append((cx, cy))

        elif cmd == 'm':
            cx += next_num()
            cy += next_num()
            start_x, start_y = cx, cy
            points.append((cx, cy))
            last_cp2 = None

        elif cmd == 'C':
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                cp1x, cp1y = next_num(), next_num()
                cp2x, cp2y = next_num(), next_num()
                ex, ey = next_num(), next_num()
                # Bezier曲線をサンプリング
                samples = _cubic_bezier_sample(
                    (cx, cy), (cp1x, cp1y), (cp2x, cp2y), (ex, ey), BEZIER_SAMPLES
                )
                points.extend(samples)
                last_cp2 = (cp2x, cp2y)
                cx, cy = ex, ey

        elif cmd == 'c':
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                d1x, d1y = next_num(), next_num()
                d2x, d2y = next_num(), next_num()
                dx, dy = next_num(), next_num()
                cp1x, cp1y = cx + d1x, cy + d1y
                cp2x, cp2y = cx + d2x, cy + d2y
                ex, ey = cx + dx, cy + dy
                samples = _cubic_bezier_sample(
                    (cx, cy), (cp1x, cp1y), (cp2x, cp2y), (ex, ey), BEZIER_SAMPLES
                )
                points.extend(samples)
                last_cp2 = (cp2x, cp2y)
                cx, cy = ex, ey

        elif cmd == 'S':
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                cp2x, cp2y = next_num(), next_num()
                ex, ey = next_num(), next_num()
                # cp1 = 前のcp2の反転（なければ現在位置）
                if last_cp2:
                    cp1x = 2 * cx - last_cp2[0]
                    cp1y = 2 * cy - last_cp2[1]
                else:
                    cp1x, cp1y = cx, cy
                samples = _cubic_bezier_sample(
                    (cx, cy), (cp1x, cp1y), (cp2x, cp2y), (ex, ey), BEZIER_SAMPLES
                )
                points.extend(samples)
                last_cp2 = (cp2x, cp2y)
                cx, cy = ex, ey

        elif cmd == 's':
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                d2x, d2y = next_num(), next_num()
                dx, dy = next_num(), next_num()
                cp2x, cp2y = cx + d2x, cy + d2y
                ex, ey = cx + dx, cy + dy
                if last_cp2:
                    cp1x = 2 * cx - last_cp2[0]
                    cp1y = 2 * cy - last_cp2[1]
                else:
                    cp1x, cp1y = cx, cy
                samples = _cubic_bezier_sample(
                    (cx, cy), (cp1x, cp1y), (cp2x, cp2y), (ex, ey), BEZIER_SAMPLES
                )
                points.extend(samples)
                last_cp2 = (cp2x, cp2y)
                cx, cy = ex, ey

        elif cmd == 'L':
            last_cp2 = None
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                cx, cy = next_num(), next_num()
                points.append((cx, cy))

        elif cmd == 'l':
            last_cp2 = None
            while i + 1 < len(tokens) and tokens[i + 1] not in 'MmCcSsLlHhVvZz':
                dx, dy = next_num(), next_num()
                cx += dx
                cy += dy
                points.append((cx, cy))

        elif cmd == 'H':
            last_cp2 = None
            cx = next_num()
            points.append((cx, cy))

        elif cmd == 'h':
            last_cp2 = None
            cx += next_num()
            points.append((cx, cy))

        elif cmd == 'V':
            last_cp2 = None
            cy = next_num()
            points.append((cx, cy))

        elif cmd == 'v':
            last_cp2 = None
            cy += next_num()
            points.append((cx, cy))

        elif cmd in ('Z', 'z'):
            last_cp2 = None
            cx, cy = start_x, start_y

        i += 1

    return points


def parse_kvg_strokes(svg_text: str):
    """
    KanjiVG SVGをパースして各画の情報を返す。
    returns: [{ d, start, end, mid_points, type, id }]
    """
    # path要素を抽出（id付きのもの）
    path_pattern = re.compile(
        r'<path[^>]*\bid="([^"]+)"[^>]*\bd="([^"]+)"[^>]*/?>',
        re.DOTALL
    )
    type_pattern = re.compile(r'kvg:type="([^"]*)"')

    results = []
    for match in path_pattern.finditer(svg_text):
        path_id = match.group(1)
        d_attr = match.group(2)

        # ストロークIDでソートするため番号を取得
        stroke_num_match = re.search(r's(\d+)$', path_id)
        if not stroke_num_match:
            continue
        stroke_num = int(stroke_num_match.group(1))

        # kvg:typeを取得（path要素のテキスト範囲で検索）
        full_tag = match.group(0)
        type_match = type_pattern.search(full_tag)
        kvg_type = type_match.group(1) if type_match else ''

        # パスの座標を解析
        points = parse_svg_path(d_attr)
        if len(points) < 2:
            continue

        # 始点・終点（109×109座標系）
        start = points[0]
        end = points[-1]

        # ウェイポイント用（疎: 最大8点、グラフノードマッチング用）
        waypoints = []
        if len(points) > 2:
            n_wp = min(8, len(points) - 2)
            step = max(1, (len(points) - 1) // (n_wp + 1))
            for idx in range(step, len(points) - 1, step):
                waypoints.append(points[idx])

        # ガイドポイント用（密: Bezierサンプル全点、エッジペナルティ計算用）
        all_points = list(points)  # 全ポイント（始点含む）

        results.append({
            'num': stroke_num,
            'd': d_attr,
            'start': start,
            'end': end,
            'mid_points': waypoints,
            'all_points': all_points,
            'type': kvg_type,
            'id': path_id
        })

    results.sort(key=lambda x: x['num'])
    return results


# ============================================================
# 5. グラフマッチング + 経路探索
# ============================================================

def build_skeleton_label_map(skel, kvg_strokes, kvg_scale):
    """
    骨格ピクセルにKVGストローク番号をラベル付け。
    各骨格ピクセルは最も近いKVGストロークのパスに帰属する。
    ストロークルーティング時に「他ストロークの骨格」を避けるために使用。
    returns: label_map[y, x] = stroke_index (0-based) or -1 (ラベルなし)
    """
    h, w = skel.shape
    label_map = np.full((h, w), -1, dtype=np.int16)
    dist_map = np.full((h, w), 999.0, dtype=np.float32)
    LABEL_RADIUS = 12  # ラベリング半径（ピクセル）

    for si, kvg in enumerate(kvg_strokes):
        all_pts = kvg.get('all_points', [])
        if not all_pts:
            continue
        # KVG座標をスケーリング
        scaled = [(round(px * kvg_scale), round(py * kvg_scale)) for px, py in all_pts]
        # サンプリング（全点だと遅いので適度に間引き）
        step = max(1, len(scaled) // 30)
        sampled = scaled[::step]
        if scaled[-1] not in sampled:
            sampled.append(scaled[-1])

        for kx, ky in sampled:
            for dy in range(-LABEL_RADIUS, LABEL_RADIUS + 1):
                ny = ky + dy
                if ny < 0 or ny >= h:
                    continue
                for dx in range(-LABEL_RADIUS, LABEL_RADIUS + 1):
                    nx = kx + dx
                    if nx < 0 or nx >= w:
                        continue
                    if not skel[ny, nx]:
                        continue
                    d = (dx * dx + dy * dy) ** 0.5
                    if d < dist_map[ny, nx]:
                        dist_map[ny, nx] = d
                        label_map[ny, nx] = si

    labeled_count = np.sum(label_map >= 0)
    skel_count = np.sum(skel)
    print(f"  骨格ラベリング: {labeled_count}/{skel_count}px ({labeled_count*100//max(1,skel_count)}%)")
    return label_map


def find_nearest_skeleton_pixel(skel, target_x, target_y, search_radius=80):
    """骨格ビットマップ上で座標に最も近い骨格ピクセルを返す"""
    h, w = skel.shape
    tx, ty = round(target_x), round(target_y)
    best = None
    best_dist = search_radius
    r = search_radius
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            nx, ny = tx + dx, ty + dy
            if 0 <= nx < w and 0 <= ny < h and skel[ny, nx]:
                d = (dx * dx + dy * dy) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (nx, ny)
    return best


def _find_nearest_font_pixel(font_bmp, target_x, target_y, search_radius=15):
    """font_bmpビットマップ上で座標に最も近いフォントピクセルを返す"""
    h, w = font_bmp.shape
    tx, ty = round(target_x), round(target_y)
    # まず座標自体がフォント内か
    if 0 <= tx < w and 0 <= ty < h and font_bmp[ty, tx]:
        return (tx, ty)
    best = None
    best_dist = search_radius
    r = search_radius
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            nx, ny = tx + dx, ty + dy
            if 0 <= nx < w and 0 <= ny < h and font_bmp[ny, nx]:
                d = (dx * dx + dy * dy) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (nx, ny)
    return best


def skeleton_pixel_path(skel, start_xy, end_xy, guide_points=None, penalty_weight=0.3,
                         font_bmp=None, used_pixels=None, off_skeleton_cost=8.0,
                         label_map=None, stroke_index=-1):
    """
    骨格ピクセルレベルのDijkstra経路探索。
    グラフ構造に依存せず、骨格ピクセルを直接通る。
    guide_pointsからの距離でペナルティを付け、KVGパスに沿った経路を優先。
    font_bmpが指定されている場合、骨格ギャップをフォント内ピクセルで橋渡し。
    off_skeleton_cost: 骨格外フォント内ピクセルのコスト倍率（低いとフォントピクセル経由を許容）。
    used_pixels: 前のストロークが使用したピクセル集合 → 追加ペナルティ。
    """
    h, w = skel.shape

    # 最寄りの骨格ピクセルを見つける（見つからなければfont_bmpフォールバック）
    start_skel = find_nearest_skeleton_pixel(skel, start_xy[0], start_xy[1], search_radius=30)
    if not start_skel and font_bmp is not None:
        start_skel = _find_nearest_font_pixel(font_bmp, start_xy[0], start_xy[1], search_radius=30)
    if not start_skel:
        return None

    # 終点: まず通常の最寄りピクセルを探す（見つからなければfont_bmpフォールバック）
    end_skel = find_nearest_skeleton_pixel(skel, end_xy[0], end_xy[1], search_radius=30)
    if not end_skel and font_bmp is not None:
        end_skel = _find_nearest_font_pixel(font_bmp, end_xy[0], end_xy[1], search_radius=30)
    if not end_skel:
        return None
    if start_skel == end_skel:
        # 同一ピクセルにスナップしたが、元の座標が離れている場合はfont_bmpで経路探索
        orig_dist = ((start_xy[0] - end_xy[0]) ** 2 + (start_xy[1] - end_xy[1]) ** 2) ** 0.5
        if orig_dist > 10 and font_bmp is not None:
            # font_bmp内の最寄りピクセルをstart/endに設定して再探索
            start_font = _find_nearest_font_pixel(font_bmp, start_xy[0], start_xy[1])
            end_font = _find_nearest_font_pixel(font_bmp, end_xy[0], end_xy[1])
            if start_font and end_font and start_font != end_font:
                start_skel = start_font
                end_skel = end_font
                # font_bmpのみの経路探索に切り替え（骨格不要）
            else:
                return [start_skel]
        else:
            return [start_skel]

    # 始点コンポーネント内で終点に最も近いピクセルを優先（切断対策）
    # ただし始点が骨格上にない場合（font_bmpフォールバック）はスキップ
    start_on_skel = bool(skel[start_skel[1], start_skel[0]])
    if start_on_skel:
        from collections import deque as _deque
        _visited = set()
        _queue = _deque([start_skel])
        _visited.add(start_skel)
        while _queue:
            _cx, _cy = _queue.popleft()
            for _dx, _dy in DIRS:
                _nx, _ny = _cx + _dx, _cy + _dy
                if 0 <= _nx < w and 0 <= _ny < h and skel[_ny, _nx] and (_nx, _ny) not in _visited:
                    _visited.add((_nx, _ny))
                    _queue.append((_nx, _ny))

        if end_skel not in _visited:
            # 終点が別コンポーネント → 始点コンポーネント内で最寄りを探す
            best_end = start_skel
            best_d = float('inf')
            tx, ty = end_xy
            for px, py in _visited:
                d = ((px - tx) ** 2 + (py - ty) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best_end = (px, py)
            end_skel = best_end

    # ガイドポイントからの最小距離キャッシュ
    guide_cache = {}
    def guide_penalty(px, py):
        if not guide_points:
            return 0
        key = (px, py)
        if key not in guide_cache:
            guide_cache[key] = min(
                ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5
                for gx, gy in guide_points
            )
        return guide_cache[key]

    # Dijkstra on skeleton pixels（フォントビットマップで橋渡し可能）
    OFF_SKELETON_COST = off_skeleton_cost  # 骨格外フォント内ピクセルのコスト倍率
    USED_PIXEL_COST = 2.0    # 使用済みピクセルのコスト倍率
    WRONG_LABEL_COST = 3.0   # 他ストロークのラベル付きピクセルのコスト倍率
    dist = {start_skel: 0.0}
    prev = {start_skel: None}
    pq = [(0.0, start_skel)]

    while pq:
        d, pos = heapq.heappop(pq)
        cx, cy = pos
        if d > dist.get(pos, float('inf')):
            continue

        if pos == end_skel:
            # 経路復元
            path = []
            current = end_skel
            while current is not None:
                path.append(current)
                current = prev[current]
            return list(reversed(path))

        for dx, dy in DIRS:
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue

            on_skeleton = bool(skel[ny, nx])
            on_font = font_bmp is not None and bool(font_bmp[ny, nx])

            if not on_skeleton and not on_font:
                continue

            # 移動コスト（斜めは√2）
            base_cost = 1.414 if (dx != 0 and dy != 0) else 1.0
            if not on_skeleton:
                base_cost *= OFF_SKELETON_COST  # 骨格外だがフォント内 → 高コスト
            # 他ストロークラベルペナルティ
            if label_map is not None and stroke_index >= 0:
                pixel_label = label_map[ny, nx]
                if pixel_label >= 0 and pixel_label != stroke_index:
                    base_cost *= WRONG_LABEL_COST
            # 使用済みピクセルペナルティ
            npos = (nx, ny)
            if used_pixels and npos in used_pixels:
                base_cost *= USED_PIXEL_COST
            penalty = guide_penalty(nx, ny) * penalty_weight
            nd = d + base_cost + penalty

            if nd < dist.get(npos, float('inf')):
                dist[npos] = nd
                prev[npos] = pos
                heapq.heappush(pq, (nd, npos))

    return None


def pixel_path_with_waypoints(skel, start_xy, end_xy, waypoints, guide_points=None,
                               penalty_weight=3.0, font_bmp=None, used_pixels=None,
                               off_skeleton_cost=3.0, label_map=None, stroke_index=-1):
    """
    ウェイポイント経由のピクセルレベル経路探索。
    各セグメントを個別にDijkstraでルーティングし、結合する。
    waypointsが正しい中間点を通過するよう強制するため、
    グラフレベルでは間違った枝を選ぶケースでも正確な経路が得られる。
    off_skeleton_cost=3.0: 骨格外フォントピクセルのコストを低くし、
    ガイドに沿ったフォントピクセル経由のルートを許容する。
    """
    if not waypoints:
        return skeleton_pixel_path(skel, start_xy, end_xy, guide_points=guide_points,
                                    penalty_weight=penalty_weight, font_bmp=font_bmp,
                                    used_pixels=used_pixels, off_skeleton_cost=off_skeleton_cost,
                                    label_map=label_map, stroke_index=stroke_index)

    # ルートポイント: 始点 → WP1 → WP2 → ... → 終点
    route_pts = [start_xy] + [(round(wx), round(wy)) for wx, wy in waypoints] + [end_xy]

    all_pixels = []
    for i in range(len(route_pts) - 1):
        seg = skeleton_pixel_path(
            skel, route_pts[i], route_pts[i + 1],
            guide_points=guide_points, penalty_weight=penalty_weight,
            font_bmp=font_bmp, used_pixels=used_pixels,
            off_skeleton_cost=off_skeleton_cost,
            label_map=label_map, stroke_index=stroke_index
        )
        if seg is None or len(seg) < 1:
            return None
        # 重複接続点を除去
        if all_pixels and seg:
            if (all_pixels[-1] == seg[0] or
                (abs(all_pixels[-1][0] - seg[0][0]) <= 1 and
                 abs(all_pixels[-1][1] - seg[0][1]) <= 1)):
                seg = seg[1:]
        all_pixels.extend(seg)

    return all_pixels if len(all_pixels) >= 2 else None


def find_nearest_node(nodes, target_x, target_y, max_dist=50):
    """座標に最も近いグラフノードを返す"""
    best = None
    best_dist = max_dist
    for node in nodes:
        d = ((node['x'] - target_x) ** 2 + (node['y'] - target_y) ** 2) ** 0.5
        if d < best_dist:
            best_dist = d
            best = node
    return best


def find_extension_end_node(nodes, adjacency, edges, branch_node, kvg_direction, kvg_endpoint, exclude_ids=set()):
    """
    branchノードからKVG方向に沿ったend nodeを探す。
    kvg_direction: (dx, dy) 正規化ベクトル。KVGの端点方向。
    kvg_endpoint: (x, y) KVGの端点座標（延長が適切かの距離チェック用）。
    exclude_ids: 探索から除外するノードID集合。
    returns: end_node or None
    """
    branch_id = branch_node['id']
    # branchノードからKVG端点への距離
    branch_to_kvg = ((branch_node['x'] - kvg_endpoint[0]) ** 2 +
                     (branch_node['y'] - kvg_endpoint[1]) ** 2) ** 0.5
    best_node = None
    best_score = -1  # 方向一致度（cos類似度）

    for neighbor_id, edge_idx in adjacency.get(branch_id, []):
        if neighbor_id in exclude_ids:
            continue
        neighbor = nodes[neighbor_id]
        # branchノードから隣接ノードへの方向
        dx = neighbor['x'] - branch_node['x']
        dy = neighbor['y'] - branch_node['y']
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 1:
            continue
        # KVG方向との内積（cos類似度）
        cos_sim = (dx * kvg_direction[0] + dy * kvg_direction[1]) / dist
        # 延長先がKVG端点より遠くないかチェック
        node_to_kvg = ((neighbor['x'] - kvg_endpoint[0]) ** 2 +
                       (neighbor['y'] - kvg_endpoint[1]) ** 2) ** 0.5
        # 延長先がKVG端点に近づくか、少なくとも遠すぎない場合のみ
        if node_to_kvg > branch_to_kvg + 30:
            continue

        if cos_sim > best_score and neighbor['type'] == 'end':
            best_score = cos_sim
            best_node = neighbor
        # end nodeでなくても、方向が合っていればその先のend nodeを探す
        elif cos_sim > 0.3 and neighbor['type'] == 'branch':
            for nn_id, nn_eidx in adjacency.get(neighbor_id, []):
                if nn_id == branch_id or nn_id in exclude_ids:
                    continue
                nn = nodes[nn_id]
                if nn['type'] == 'end':
                    nn_to_kvg = ((nn['x'] - kvg_endpoint[0]) ** 2 +
                                 (nn['y'] - kvg_endpoint[1]) ** 2) ** 0.5
                    if nn_to_kvg > branch_to_kvg + 30:
                        continue
                    ddx = nn['x'] - branch_node['x']
                    ddy = nn['y'] - branch_node['y']
                    dd = (ddx * ddx + ddy * ddy) ** 0.5
                    if dd > 1:
                        c = (ddx * kvg_direction[0] + ddy * kvg_direction[1]) / dd
                        if c > best_score:
                            best_score = c
                            best_node = nn

    # 方向一致度が低すぎる場合は延長しない
    if best_score < 0.3:
        return None
    return best_node


def shortest_path(adjacency, edges, start_id, end_id):
    """
    Dijkstra で最短経路を探索。
    returns: エッジのピクセル列を結合した [(x, y), ...]
    """
    if start_id == end_id:
        return []

    # Dijkstra
    dist = {start_id: 0}
    prev = {}
    pq = [(0, start_id)]

    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float('inf')):
            continue
        if u == end_id:
            break
        for neighbor, edge_idx in adjacency.get(u, []):
            edge = edges[edge_idx]
            nd = d + edge['length']
            if nd < dist.get(neighbor, float('inf')):
                dist[neighbor] = nd
                prev[neighbor] = (u, edge_idx)
                heapq.heappush(pq, (nd, neighbor))

    if end_id not in prev:
        return None

    # 経路を逆順に再構築
    path_edges = []
    current = end_id
    while current in prev:
        p, eidx = prev[current]
        path_edges.append((p, current, eidx))
        current = p
    path_edges.reverse()

    # ピクセル列を結合
    all_pixels = []
    for from_id, to_id, eidx in path_edges:
        edge = edges[eidx]
        pixels = edge['pixels']
        # エッジの方向を合わせる
        if edge['from_id'] == from_id:
            segment = pixels
        else:
            segment = list(reversed(pixels))
        # 重複する接続点を除去
        if all_pixels and segment:
            start_px = segment[0]
            if all_pixels[-1] == start_px or (
                abs(all_pixels[-1][0] - start_px[0]) <= 1 and
                abs(all_pixels[-1][1] - start_px[1]) <= 1
            ):
                segment = segment[1:]
        all_pixels.extend(segment)

    return all_pixels


def shortest_path_directed(adjacency, edges, start_id, end_id, guide_points=None, penalty_weight=5.0,
                           used_edges=None, label_map=None, stroke_index=-1):
    """
    KVGガイドポイント考慮のDijkstra最短経路探索。
    guide_points: [(x, y), ...] KVGパスに沿ったガイドポイント。
    used_edges: {edge_idx: count} 前のストロークが使用したエッジ → 追加ペナルティ。
    label_map: 骨格ラベルマップ（ストローク所有権）。
    stroke_index: 現在のストローク番号（label_mapと組み合わせて使用）。
    エッジのコスト = 長さ + ガイドペナルティ + 使用済みペナルティ + ラベルペナルティ
    """
    if start_id == end_id:
        return []

    USED_EDGE_PENALTY = 50.0  # 使用済みエッジのペナルティ
    WRONG_LABEL_EDGE_PENALTY = 30.0  # 他ストロークラベルのエッジペナルティ

    # エッジごとのペナルティを事前計算
    edge_penalties = {}
    edge_label_penalties = {}
    if guide_points and len(guide_points) >= 2:
        for eidx, edge in enumerate(edges):
            pixels = edge['pixels']
            n = len(pixels)
            # 代表ピクセルをサンプリング（短いエッジは1点、長いエッジは3点）
            if n < 5:
                samples = [n // 2]
            else:
                samples = [n // 4, n // 2, 3 * n // 4]

            total_min_dist = 0
            for si in samples:
                px, py = pixels[si]
                min_dist = min(
                    ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5
                    for gx, gy in guide_points
                )
                total_min_dist += min_dist
            edge_penalties[eidx] = total_min_dist / len(samples)

    # ラベルペナルティ事前計算
    if label_map is not None and stroke_index >= 0:
        for eidx, edge in enumerate(edges):
            pixels = edge['pixels']
            n = len(pixels)
            if n == 0:
                continue
            # サンプリングで他ストロークラベル比率を計算
            step = max(1, n // 5)
            wrong_count = 0
            total_sampled = 0
            for si in range(0, n, step):
                px, py = pixels[si]
                lbl = label_map[py, px]
                total_sampled += 1
                if lbl >= 0 and lbl != stroke_index:
                    wrong_count += 1
            if total_sampled > 0 and wrong_count > 0:
                wrong_ratio = wrong_count / total_sampled
                edge_label_penalties[eidx] = wrong_ratio * WRONG_LABEL_EDGE_PENALTY

    # Dijkstra
    dist = {start_id: 0}
    prev = {}
    pq = [(0, start_id)]

    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float('inf')):
            continue
        if u == end_id:
            break
        for neighbor, edge_idx in adjacency.get(u, []):
            edge = edges[edge_idx]
            base_cost = edge['length']
            penalty = edge_penalties.get(edge_idx, 0) * penalty_weight
            # 使用済みエッジペナルティ
            if used_edges and edge_idx in used_edges:
                penalty += USED_EDGE_PENALTY * used_edges[edge_idx]
            # ラベルペナルティ（他ストロークの骨格を通るコスト）
            if edge_idx in edge_label_penalties:
                penalty += edge_label_penalties[edge_idx]
            nd = d + base_cost + penalty
            if nd < dist.get(neighbor, float('inf')):
                dist[neighbor] = nd
                prev[neighbor] = (u, edge_idx)
                heapq.heappush(pq, (nd, neighbor))

    if end_id not in prev:
        return None

    # 経路を逆順に再構築
    path_edges = []
    current = end_id
    while current in prev:
        p, eidx = prev[current]
        path_edges.append((p, current, eidx))
        current = p
    path_edges.reverse()

    # ピクセル列を結合
    all_pixels = []
    for from_id, to_id, eidx in path_edges:
        edge = edges[eidx]
        pixels = edge['pixels']
        if edge['from_id'] == from_id:
            segment = pixels
        else:
            segment = list(reversed(pixels))
        if all_pixels and segment:
            start_px = segment[0]
            if all_pixels[-1] == start_px or (
                abs(all_pixels[-1][0] - start_px[0]) <= 1 and
                abs(all_pixels[-1][1] - start_px[1]) <= 1
            ):
                segment = segment[1:]
        all_pixels.extend(segment)

    return all_pixels


def find_path_with_waypoints(nodes, adjacency, edges, start_node, end_node, waypoints,
                             guide_points=None, used_edges=None, label_map=None, stroke_index=-1):
    """
    中間ウェイポイントを経由する経路を探索。
    waypointsは(x, y)リスト。最寄りのノードに変換して経由する。
    guide_pointsが指定されている場合、各セグメントでガイド考慮Dijkstraを使用。
    used_edges: 前のストロークが使用したエッジ辞書。
    label_map: 骨格ラベルマップ。
    stroke_index: 現在のストローク番号。
    """
    # ウェイポイントをノードに変換
    wp_nodes = []
    for wx, wy in waypoints:
        wpn = find_nearest_node(nodes, wx, wy, max_dist=40)
        if wpn and wpn['id'] != start_node['id'] and wpn['id'] != end_node['id']:
            wp_nodes.append(wpn)

    # 重複除去
    seen = set()
    unique_wp = []
    for wpn in wp_nodes:
        if wpn['id'] not in seen:
            seen.add(wpn['id'])
            unique_wp.append(wpn)

    # 経路: start → wp1 → wp2 → ... → end
    route_nodes = [start_node] + unique_wp + [end_node]
    all_pixels = []

    for i in range(len(route_nodes) - 1):
        if guide_points:
            segment = shortest_path_directed(
                adjacency, edges, route_nodes[i]['id'], route_nodes[i + 1]['id'],
                guide_points=guide_points, penalty_weight=5.0,
                used_edges=used_edges, label_map=label_map, stroke_index=stroke_index
            )
        else:
            segment = shortest_path(adjacency, edges, route_nodes[i]['id'], route_nodes[i + 1]['id'])
        if segment is None:
            # 経路が見つからない場合、直接接続を試みる
            return None
        if all_pixels and segment:
            # 重複接続点を除去
            if len(all_pixels) > 0 and len(segment) > 0:
                if (abs(all_pixels[-1][0] - segment[0][0]) <= 2 and
                    abs(all_pixels[-1][1] - segment[0][1]) <= 2):
                    segment = segment[1:]
        all_pixels.extend(segment)

    return all_pixels


def validate_path_against_kvg(path_pixels, kvg_mid_points, threshold=25):
    """
    経路がKVGの中間点の近くを通るかチェック。
    returns: (is_valid, missed_waypoints)
    """
    if not kvg_mid_points or not path_pixels:
        return True, []

    missed = []
    for mx, my in kvg_mid_points:
        min_dist = float('inf')
        for px, py in path_pixels:
            d = ((px - mx) ** 2 + (py - my) ** 2) ** 0.5
            if d < min_dist:
                min_dist = d
        if min_dist > threshold:
            missed.append((mx, my))

    return len(missed) == 0, missed


# ============================================================
# 6. RDP間引き
# ============================================================

def pt_to_line_dist(px, py, ax, ay, bx, by):
    """点(px,py)と線分(a→b)の距離"""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def interpolate_points(points, step=2.0):
    """
    ポイント列を密に補間。stepピクセル間隔で線形補間ポイントを挿入。
    ㇕/㇆の角を保持するために、KVGポイント間を細かく補間する。
    """
    if len(points) < 2:
        return list(points)
    result = [points[0]]
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        dx, dy = x2 - x1, y2 - y1
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= step:
            result.append((x2, y2))
            continue
        n_steps = int(dist / step)
        for j in range(1, n_steps + 1):
            t = j / (n_steps + 1)
            ix = round(x1 + dx * t)
            iy = round(y1 + dy * t)
            result.append((ix, iy))
        result.append((x2, y2))
    return result


def find_corner_point(points, min_angle_change=50):
    """
    ポイント列の中から最も鋭い角（方向変化が大きい点）を検出。
    始点→候補点と候補点→終点の方向差で判定（グローバル方向変化）。
    min_angle_change: 角とみなす最小角度変化（度）。
    returns: (corner_index, angle_degrees) or (None, 0)
    """
    if len(points) < 5:
        return None, 0

    import math
    best_idx = None
    best_angle = 0
    sx, sy = points[0]
    ex, ey = points[-1]
    # 検索範囲: 20%〜80%（端は除外）
    margin = max(2, len(points) // 5)
    for i in range(margin, len(points) - margin):
        px, py = points[i]
        # 始点→候補点の方向
        dx_in = px - sx
        dy_in = py - sy
        # 候補点→終点の方向
        dx_out = ex - px
        dy_out = ey - py
        len_in = (dx_in ** 2 + dy_in ** 2) ** 0.5
        len_out = (dx_out ** 2 + dy_out ** 2) ** 0.5
        if len_in < 3 or len_out < 3:
            continue
        cos_angle = (dx_in * dx_out + dy_in * dy_out) / (len_in * len_out)
        cos_angle = max(-1, min(1, cos_angle))
        angle_deg = math.degrees(math.acos(cos_angle))
        if angle_deg > best_angle:
            best_angle = angle_deg
            best_idx = i

    if best_angle >= min_angle_change:
        return best_idx, best_angle
    return None, 0


def kvg_skeleton_snap_path(all_kvg_pts, skel, font_bmp):
    """
    KVGポイントを骨格ピクセルにスナップした直接経路。
    骨格グラフのルーティングに依存せず、KVGの方向知識を直接活用。
    骨格が見つからない場合はフォント内最寄りピクセルにフォールバック。
    """
    if len(all_kvg_pts) < 2:
        return None

    snapped = []
    for kx, ky in all_kvg_pts:
        # まず近距離でスナップ、失敗したら広範囲で再試行
        sp = find_nearest_skeleton_pixel(skel, kx, ky, search_radius=15)
        if not sp:
            sp = find_nearest_skeleton_pixel(skel, kx, ky, search_radius=30)
        if sp:
            if not snapped or sp != snapped[-1]:
                snapped.append(sp)
        else:
            # 骨格が見つからない → フォント内最寄り（広範囲）
            fp = _find_nearest_font_pixel(font_bmp, kx, ky, search_radius=30)
            if fp and (not snapped or fp != snapped[-1]):
                snapped.append(fp)

    if len(snapped) < 2:
        return None

    # スナップポイント間を密に補間
    dense = interpolate_points(snapped, step=2.0)
    return dense


def clip_to_outline(points, font_bmp):
    """
    全ポイントをフォントアウトライン内にクリッピング。
    アウトライン外のポイントは最寄りの内側ピクセルに移動。
    """
    h, w = font_bmp.shape
    result = []
    for px, py in points:
        x, y = round(px), round(py)
        # 範囲内かつフォント内ならそのまま
        if 0 <= x < w and 0 <= y < h and font_bmp[y, x]:
            result.append((x, y))
            continue
        # アウトライン外 → 最寄りの内側ピクセルを探す
        best = None
        best_dist = float('inf')
        for r in range(1, 20):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if abs(dx) != r and abs(dy) != r:
                        continue  # 外周だけ探す
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and font_bmp[ny, nx]:
                        d = dx * dx + dy * dy
                        if d < best_dist:
                            best_dist = d
                            best = (nx, ny)
            if best:
                break
        result.append(best if best else (x, y))
    return result


def smooth_path(points, window=5):
    """
    移動平均でパスを平滑化。始点・終点は保持。
    """
    if len(points) <= window:
        return list(points)
    result = [points[0]]
    half = window // 2
    for i in range(1, len(points) - 1):
        start = max(0, i - half)
        end = min(len(points), i + half + 1)
        avg_x = sum(p[0] for p in points[start:end]) / (end - start)
        avg_y = sum(p[1] for p in points[start:end]) / (end - start)
        result.append((round(avg_x), round(avg_y)))
    result.append(points[-1])
    return result


def fit_line_if_straight(points, threshold=4.0):
    """
    ポイント列がほぼ直線なら2点（始点・終点）に置換。
    横棒/縦棒（角度<20度）の場合は閾値を2倍にして積極的に直線化。
    threshold: 直線からの最大許容偏差（px）。
    """
    if len(points) <= 2:
        return list(points)
    ax, ay = points[0]
    bx, by = points[-1]

    # 方向検出: 横棒/縦棒は閾値を段階的に引き上げ
    dx, dy = abs(bx - ax), abs(by - ay)
    total_len = (dx * dx + dy * dy) ** 0.5
    effective_threshold = threshold
    if total_len > 20 and max(dx, dy) > 0:
        angle_ratio = min(dx, dy) / max(dx, dy)
        if angle_ratio < 0.15:  # ~8.5度以内 → ほぼ完全な横棒/縦棒
            effective_threshold = threshold * 3
        elif angle_ratio < 0.35:  # ~19度以内 → 横棒/縦棒
            effective_threshold = threshold * 2

    max_dev = 0
    for px, py in points[1:-1]:
        d = pt_to_line_dist(px, py, ax, ay, bx, by)
        if d > max_dev:
            max_dev = d
    if max_dev <= effective_threshold:
        return [points[0], points[-1]]
    return list(points)


def remove_zigzags(points, angle_threshold=0.3):
    """
    連続3点がほぼ折り返し（鋭角）する場合、中間点を除去。
    angle_threshold: cosが-1に近いほど折り返し。0.3 ≈ 107度。
    cos < -angle_threshold で除去。
    """
    if len(points) <= 2:
        return list(points)

    result = [points[0]]
    i = 1
    while i < len(points) - 1:
        # 3点: result[-1], points[i], points[i+1]
        ax, ay = result[-1]
        bx, by = points[i]
        cx, cy = points[i + 1]
        # ベクトル ab, bc
        abx, aby = bx - ax, by - ay
        bcx, bcy = cx - bx, cy - by
        ab_len = (abx ** 2 + aby ** 2) ** 0.5
        bc_len = (bcx ** 2 + bcy ** 2) ** 0.5
        if ab_len > 0 and bc_len > 0:
            cos_angle = (abx * bcx + aby * bcy) / (ab_len * bc_len)
            # 鋭い折り返し（cos < -threshold）→ 中間点を削除
            if cos_angle < -angle_threshold:
                # さらに、折り返し先が近い場合のみ除去（遠い場合は意味のある曲がり）
                if min(ab_len, bc_len) < 30:
                    i += 1
                    continue
        result.append(points[i])
        i += 1
    result.append(points[-1])
    return result


def remove_lateral_bumps(points, threshold=8.0):
    """
    ストロークの主方向に対して横方向に大きく外れる中間点を除去。
    水平/垂直ストロークの小さなガタつきを解消する。
    大きな偏差(15px以上)は意図的な角として保護。
    """
    if len(points) <= 3:
        return list(points)

    result = [points[0]]
    for i in range(1, len(points) - 1):
        # 前後の点を結ぶ線からの垂直距離
        d = pt_to_line_dist(
            points[i][0], points[i][1],
            result[-1][0], result[-1][1],
            points[i + 1][0], points[i + 1][1]
        )
        # 前後の点間の距離
        seg_len = ((points[i+1][0] - result[-1][0]) ** 2 +
                   (points[i+1][1] - result[-1][1]) ** 2) ** 0.5
        if seg_len < 3:
            result.append(points[i])
            continue
        # 大きな偏差は角として保護（㇕/㇆等）
        if d >= 15:
            result.append(points[i])
            continue
        # 固定閾値以下は除去（ガタつき）
        if d < threshold:
            continue
        result.append(points[i])
    result.append(points[-1])
    return result


def rdp_simplify(points, epsilon=3.0):
    """Ramer-Douglas-Peucker 間引き"""
    if len(points) <= 2:
        return list(points)

    ax, ay = points[0]
    bx, by = points[-1]
    max_dist = 0
    max_idx = 0
    for i in range(1, len(points) - 1):
        d = pt_to_line_dist(points[i][0], points[i][1], ax, ay, bx, by)
        if d > max_dist:
            max_dist = d
            max_idx = i

    if max_dist > epsilon:
        left = rdp_simplify(points[:max_idx + 1], epsilon)
        right = rdp_simplify(points[max_idx:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


# ============================================================
# 7. テーパー判定
# ============================================================

TAPER_TYPES = {'㇒', '㇏', '㇀', '㇂', '㇃', '㇚', '㇇'}

def should_taper(kvg_type: str) -> bool:
    """kvg:typeからはね/はらいを判定"""
    base = re.sub(r'[a-z]+$', '', kvg_type)
    return base in TAPER_TYPES


# ============================================================
# 8. メイン統合
# ============================================================

def generate_strokes(kanji: str, debug=False) -> dict:
    """1文字の漢字に対してストロークデータを生成"""
    print(f"\n=== {kanji} (U+{ord(kanji):04X}) ===")

    # 1. フォント描画
    print("  フォント描画...")
    bmp = render_font(kanji)
    if debug:
        # デバッグ: フォント描画結果を保存
        Image.fromarray(bmp * 255).save(OUTPUT_DIR / f"debug_{ord(kanji):05x}_font.png")

    # 2. 細線化
    print("  Zhang-Suen 細線化...")
    skel = bmp.copy()
    zhang_suen_thin(skel)

    # 2b. 骨格ギャップ修復（切断されたコンポーネントをブリッジ）
    n_bridges = bridge_skeleton_gaps(skel, bmp, max_gap=35)
    skel_count = np.sum(skel)
    print(f"  骨格ピクセル: {skel_count}" + (f" (ブリッジ{n_bridges}箇所)" if n_bridges else ""))
    if debug:
        Image.fromarray(skel * 255).save(OUTPUT_DIR / f"debug_{ord(kanji):05x}_skel.png")

    # 3. グラフ構築
    print("  グラフ構築...")
    nodes, edges, adjacency = build_graph(skel)
    print(f"  ノード: {len(nodes)}個, エッジ: {len(edges)}本")
    for n in nodes:
        print(f"    [{n['id']}] ({n['x']},{n['y']}) {n['type']}")

    # 4. KanjiVG取得
    print("  KanjiVG取得...")
    svg_text = fetch_kanjivg(kanji)
    kvg_strokes = parse_kvg_strokes(svg_text)
    print(f"  KanjiVG: {len(kvg_strokes)}画")

    # 4b. 骨格ラベリング（各ストロークの骨格ピクセル所有権）
    label_map = build_skeleton_label_map(skel, kvg_strokes, KVG_SCALE)

    # 使用済みエッジ追跡（ストローク間の重複防止）
    used_edges = {}  # {edge_idx: 使用回数}
    used_pixels = set()  # ピクセルレベルパス用

    def record_used_edges(path_pixels):
        """経路のピクセルからどのエッジを通ったか特定して記録"""
        if not path_pixels:
            return
        # ピクセル→エッジの逆引き
        path_set = set(path_pixels)
        for eidx, edge in enumerate(edges):
            edge_pixels = set(edge['pixels'])
            # 経路がエッジのピクセルを3つ以上含んでいれば「使用」とみなす
            overlap = len(path_set & edge_pixels)
            if overlap >= min(3, len(edge_pixels)):
                used_edges[eidx] = used_edges.get(eidx, 0) + 1
        # ピクセルレベル用
        used_pixels.update(path_pixels)

    # 5-8. 各画のマッチング + 経路探索
    result_strokes = []
    for si, kvg in enumerate(kvg_strokes):
        stroke_idx = si  # ラベルマップ用のストロークインデックス（0-based）
        print(f"\n  画{kvg['num']} ({kvg['id']}): type={kvg['type']}")

        # KanjiVG座標を300×300にスケーリング
        sx, sy = kvg['start'][0] * KVG_SCALE, kvg['start'][1] * KVG_SCALE
        ex, ey = kvg['end'][0] * KVG_SCALE, kvg['end'][1] * KVG_SCALE
        mid_scaled = [(mx * KVG_SCALE, my * KVG_SCALE) for mx, my in kvg['mid_points']]
        print(f"    始点: ({sx:.1f}, {sy:.1f}) → 終点: ({ex:.1f}, {ey:.1f})")

        # 最寄りのグラフノードを特定
        start_node = find_nearest_node(nodes, sx, sy)
        end_node = find_nearest_node(nodes, ex, ey)

        # ノードが見つからない場合、最寄り骨格ピクセル経由で再探索
        if start_node is None:
            nearest_skel = find_nearest_skeleton_pixel(skel, sx, sy)
            if nearest_skel:
                start_node = find_nearest_node(nodes, nearest_skel[0], nearest_skel[1], max_dist=80)
                if start_node:
                    print(f"    始点: 骨格ピクセル({nearest_skel[0]},{nearest_skel[1]})経由でノード検出")
        if end_node is None:
            nearest_skel = find_nearest_skeleton_pixel(skel, ex, ey)
            if nearest_skel:
                end_node = find_nearest_node(nodes, nearest_skel[0], nearest_skel[1], max_dist=80)
                if end_node:
                    print(f"    終点: 骨格ピクセル({nearest_skel[0]},{nearest_skel[1]})経由でノード検出")

        if start_node is None or end_node is None:
            print(f"    ⚠ ノードにマッチできず → KVG座標フォールバック")
            # フォールバック: KVG座標から直接ポイント生成
            pts = [(round(sx), round(sy)), (round(ex), round(ey))]
            path_d = f"M {pts[0][0]},{pts[0][1]} L {pts[1][0]},{pts[1][1]}"
            result_strokes.append({
                'points': [{'x': p[0], 'y': p[1]} for p in pts],
                'path': path_d,
                'taper': should_taper(kvg['type'])
            })
            continue

        print(f"    始点ノード: [{start_node['id']}]({start_node['x']},{start_node['y']}) {start_node['type']}")
        print(f"    終点ノード: [{end_node['id']}]({end_node['x']},{end_node['y']}) {end_node['type']}")

        # 始点=終点の場合 → 即座にfont_bmpピクセル経路に切り替え（短い㇔点画で頻発）
        if start_node['id'] == end_node['id']:
            print(f"    始点=終点 → font_bmpピクセル経路を試行")
            pixel_path = skeleton_pixel_path(
                skel, (round(sx), round(sy)), (round(ex), round(ey)),
                guide_points=None, penalty_weight=0.3, font_bmp=bmp,
                used_pixels=used_pixels, label_map=label_map, stroke_index=stroke_idx
            )
            if pixel_path and len(pixel_path) >= 2:
                print(f"    font_bmpピクセル経路: {len(pixel_path)}px")
                record_used_edges(pixel_path)
                path_pixels_final = smooth_path(pixel_path, window=5)
                path_pixels_final = clip_to_outline(path_pixels_final, bmp)
                simplified = rdp_simplify(path_pixels_final, epsilon=2.0)
                simplified = remove_zigzags(simplified)
                simplified = fit_line_if_straight(simplified, threshold=4.0)
                simplified = clip_to_outline(simplified, bmp)
                print(f"    経路: {len(pixel_path)}px → {len(simplified)}点")
                pts = [{'x': p[0], 'y': p[1]} for p in simplified]
                path_d = f"M {simplified[0][0]},{simplified[0][1]}"
                for p in simplified[1:]:
                    path_d += f" L {p[0]},{p[1]}"
                result_strokes.append({
                    'points': pts,
                    'path': path_d,
                    'taper': should_taper(kvg['type'])
                })
                continue
            else:
                # font_bmpでも失敗 → KVG座標フォールバック
                print(f"    ⚠ font_bmpでも経路なし → KVG座標フォールバック")
                pts = [(round(sx), round(sy)), (round(ex), round(ey))]
                path_d = f"M {pts[0][0]},{pts[0][1]} L {pts[1][0]},{pts[1][1]}"
                result_strokes.append({
                    'points': [{'x': p[0], 'y': p[1]} for p in pts],
                    'path': path_d,
                    'taper': should_taper(kvg['type'])
                })
                continue

        # branch node → end node 延長
        # KVGの始点→終点方向ベクトル
        kvg_dx = ex - sx
        kvg_dy = ey - sy
        kvg_len = (kvg_dx ** 2 + kvg_dy ** 2) ** 0.5
        if kvg_len > 0:
            kvg_dir = (kvg_dx / kvg_len, kvg_dy / kvg_len)
        else:
            kvg_dir = (0, 0)

        # 始点がbranchの場合 → KVG始点方向（逆方向）にend nodeを探す
        if start_node['type'] == 'branch' and kvg_len > 0:
            rev_dir = (-kvg_dir[0], -kvg_dir[1])
            ext_node = find_extension_end_node(
                nodes, adjacency, edges, start_node, rev_dir,
                kvg_endpoint=(sx, sy),
                exclude_ids={end_node['id']}
            )
            if ext_node:
                print(f"    始点延長: [{start_node['id']}]branch → [{ext_node['id']}]end ({ext_node['x']},{ext_node['y']})")
                start_node = ext_node

        # 終点がbranchの場合 → KVG終点方向（順方向）にend nodeを探す
        if end_node['type'] == 'branch' and kvg_len > 0:
            ext_node = find_extension_end_node(
                nodes, adjacency, edges, end_node, kvg_dir,
                kvg_endpoint=(ex, ey),
                exclude_ids={start_node['id']}
            )
            if ext_node:
                print(f"    終点延長: [{end_node['id']}]branch → [{ext_node['id']}]end ({ext_node['x']},{ext_node['y']})")
                end_node = ext_node

        # KVG-骨格オフセット補正（始点/終点のマッチ結果から推定）
        kvg_offset_dx = ((start_node['x'] - sx) + (end_node['x'] - ex)) / 2
        kvg_offset_dy = ((start_node['y'] - sy) + (end_node['y'] - ey)) / 2
        # X方向は小さく不安定なので無視、Y方向のみ補正
        kvg_offset_y = kvg_offset_dy if abs(kvg_offset_dy) > 3 else 0

        # ガイドポイント構築（Bezier密サンプルの全点をスケーリング + オフセット補正）
        all_scaled = [(px * KVG_SCALE, py * KVG_SCALE) for px, py in kvg.get('all_points', [])]
        if all_scaled:
            guide_points = [(round(ax), round(ay + kvg_offset_y)) for ax, ay in all_scaled]
        else:
            guide_points = [(round(sx), round(sy))]
            guide_points += [(round(mx), round(my)) for mx, my in mid_scaled]
            guide_points += [(round(ex), round(ey))]
        if abs(kvg_offset_y) > 3:
            print(f"    KVG-骨格Y補正: {kvg_offset_y:+.0f}px")

        # ウェイポイント + ガイド考慮Dijkstraで経路探索
        path_pixels = None
        if mid_scaled:
            path_pixels = find_path_with_waypoints(
                nodes, adjacency, edges, start_node, end_node, mid_scaled,
                guide_points=guide_points, used_edges=used_edges,
                label_map=label_map, stroke_index=stroke_idx
            )
            if path_pixels and len(path_pixels) >= 2:
                print(f"    ウェイポイント経由: {len(path_pixels)}ピクセル ({len(mid_scaled)}WP)")
            else:
                path_pixels = None

        # フォールバック: ガイド考慮Dijkstra（ウェイポイントなし）
        if path_pixels is None or len(path_pixels) < 2:
            path_pixels = shortest_path_directed(
                adjacency, edges, start_node['id'], end_node['id'],
                guide_points=guide_points, used_edges=used_edges,
                label_map=label_map, stroke_index=stroke_idx
            )

        # 経路長バリデーション: 迂回しすぎならピクセルレベルで再試行
        kvg_dist = ((sx - ex) ** 2 + (sy - ey) ** 2) ** 0.5
        if path_pixels and kvg_dist > 15 and len(path_pixels) > kvg_dist * 2.5:
            ratio = len(path_pixels) / kvg_dist
            print(f"    ⚠ 経路迂回 ({len(path_pixels)}px vs {kvg_dist:.0f}px直線, 比率{ratio:.1f}x)")
            alt_path = skeleton_pixel_path(
                skel, (round(sx), round(sy)), (round(ex), round(ey)),
                guide_points=guide_points, penalty_weight=1.5, font_bmp=bmp,
                used_pixels=used_pixels, label_map=label_map, stroke_index=stroke_idx
            )
            if alt_path and len(alt_path) >= 2:
                alt_ratio = len(alt_path) / kvg_dist
                if alt_ratio < ratio * 0.85:  # 15%以上短縮されたら切り替え
                    print(f"    → ピクセル経路に切替 ({len(alt_path)}px, 比率{alt_ratio:.1f}x)")
                    path_pixels = alt_path

        # 常時比較: 現在経路 vs ピクセル経路 vs ウェイポイント付きピクセル経路
        if path_pixels and len(path_pixels) >= 2 and guide_points and len(guide_points) >= 2:
            def _avg_dev(ppath):
                step = max(1, len(ppath) // 10)
                samps = ppath[::step]
                return sum(min(((px-gx)**2+(py-gy)**2)**0.5 for gx,gy in guide_points) for px,py in samps) / len(samps)

            dev_current = _avg_dev(path_pixels)
            best_path = path_pixels
            best_dev = dev_current

            # 候補1: ピクセル経路（ウェイポイントなし）
            alt_plain = skeleton_pixel_path(
                skel, (round(sx), round(sy)), (round(ex), round(ey)),
                guide_points=guide_points, penalty_weight=2.5, font_bmp=bmp,
                used_pixels=used_pixels, label_map=label_map, stroke_index=stroke_idx
            )
            if alt_plain and len(alt_plain) >= 2:
                dev_plain = _avg_dev(alt_plain)
                if dev_plain < best_dev:
                    best_path = alt_plain
                    best_dev = dev_plain

            # 候補2: ウェイポイント経由ピクセル経路（KVG中間点を強制経由）
            # all_kvg_ptsにもY軸オフセット補正を適用
            all_kvg_pts = [(round(px * KVG_SCALE), round(py * KVG_SCALE + kvg_offset_y))
                           for px, py in kvg.get('all_points', [])]
            if len(all_kvg_pts) >= 3:
                # 適応的ウェイポイント数（複雑なストロークほど多く、最大8）
                n_wp = min(8, max(3, len(all_kvg_pts) // 4))
                wp_pixel = []
                for i in range(n_wp):
                    frac = (i + 1) / (n_wp + 1)
                    idx = min(int(len(all_kvg_pts) * frac), len(all_kvg_pts) - 1)
                    wp_pixel.append(all_kvg_pts[idx])
                alt_wp = pixel_path_with_waypoints(
                    skel, (round(sx), round(sy)), (round(ex), round(ey)),
                    wp_pixel, guide_points=guide_points, penalty_weight=8.0,
                    font_bmp=bmp, used_pixels=used_pixels,
                    label_map=label_map, stroke_index=stroke_idx
                )
                if alt_wp and len(alt_wp) >= 2:
                    dev_wp = _avg_dev(alt_wp)
                    if dev_wp < best_dev:
                        best_path = alt_wp
                        best_dev = dev_wp

            # 候補3: KVGパス直接クリッピング（密補間付き）
            # ㇕/㇆タイプは角があるため常にKVGクリッピングを試行
            stroke_type = kvg.get('type', '')
            is_corner_type = any(c in stroke_type for c in ['㇕', '㇆', '㇖'])
            # 密集漢字ではKVGクリッピングのしきい値を下げて積極的に試行
            n_nodes = len(nodes)
            if is_corner_type:
                kvg_clip_threshold = 8
            elif n_nodes >= 40:
                kvg_clip_threshold = 5  # 密集漢字: 偏差5以上でKVGクリッピング試行
            elif n_nodes >= 25:
                kvg_clip_threshold = 7
            else:
                kvg_clip_threshold = 10
            if best_dev > kvg_clip_threshold and all_kvg_pts and len(all_kvg_pts) >= 2:
                # KVGポイント間を密に補間してから角の形状を保持
                kvg_dense = interpolate_points(all_kvg_pts, step=2.0)
                kvg_clipped = clip_to_outline(kvg_dense, bmp)
                # クリッピング後の強い平滑化（フォント輪郭の上端/下端ジグザグを解消）
                kvg_clipped = smooth_path(kvg_clipped, window=15)
                if len(kvg_clipped) >= 2:
                    # 骨格距離ペナルティ軽減（1に削減。KVGルーティングの恩恵を活かす）
                    def _skel_dist(ppath):
                        step = max(1, len(ppath) // 10)
                        samps = ppath[::step]
                        total = 0
                        for px, py in samps:
                            if 0 <= px < W and 0 <= py < H and skel[py, px]:
                                total += 0
                            else:
                                total += 1  # 骨格ペナルティ軽減（2→1）
                        return total / len(samps)
                    dev_kvg_clip = _avg_dev(kvg_clipped)
                    skel_penalty = _skel_dist(kvg_clipped)
                    # 長さ比ペナルティ: 迂回路検出（始点-終点直線距離 vs 実パス長）
                    p0 = kvg_clipped[0]
                    p1 = kvg_clipped[-1]
                    straight_dist = ((p1[0]-p0[0])**2 + (p1[1]-p0[1])**2)**0.5
                    path_len = sum(((kvg_clipped[i+1][0]-kvg_clipped[i][0])**2 +
                                    (kvg_clipped[i+1][1]-kvg_clipped[i][1])**2)**0.5
                                   for i in range(len(kvg_clipped)-1))
                    length_ratio = path_len / max(straight_dist, 1.0)
                    # 迂回比2.5以上は三角パス等の異常 → 候補を完全に却下
                    if length_ratio > 2.5:
                        print(f"    KVGクリッピング却下: 迂回比{length_ratio:.2f}x (>2.5)")
                        kvg_clipped = None
                    else:
                        length_penalty = max(0, (length_ratio - 1.8)) * 8.0
                        effective_dev = dev_kvg_clip + skel_penalty + length_penalty
                        if length_penalty > 0:
                            print(f"    KVGクリッピング迂回比: {length_ratio:.2f}x → ペナルティ{length_penalty:.1f}")
                    if kvg_clipped is not None and effective_dev < best_dev:
                        best_path = kvg_clipped
                        best_dev = effective_dev
                        print(f"    KVGクリッピング候補: 偏差{dev_kvg_clip:.1f}+骨格{skel_penalty:.1f}={effective_dev:.1f}px")

            # 候補4: KVG骨格スナップ経路（KVGポイントを骨格中心にスナップ）
            # Dijkstraルーティングに依存しないため、密集漢字で有効
            if all_kvg_pts and len(all_kvg_pts) >= 2:
                kvg_skel = kvg_skeleton_snap_path(all_kvg_pts, skel, bmp)
                if kvg_skel and len(kvg_skel) >= 2:
                    # 平滑化してから評価
                    kvg_skel_smooth = smooth_path(kvg_skel, window=7)
                    dev_kvg_skel = _avg_dev(kvg_skel_smooth)
                    # KVGスナップはルーティング不要で密集漢字に強いため
                    # ノード数に応じてボーナスを動的調整（密集→大きいボーナス）
                    n_nodes = len(nodes)
                    if is_corner_type:
                        base_bonus = 1.2
                    elif n_nodes >= 40:
                        base_bonus = 1.4  # 密集漢字: 40%ボーナス
                    elif n_nodes >= 25:
                        base_bonus = 1.25  # 中密度: 25%ボーナス
                    else:
                        base_bonus = 1.1
                    accept_threshold = best_dev * base_bonus
                    if dev_kvg_skel < accept_threshold:
                        best_path = kvg_skel_smooth
                        best_dev = dev_kvg_skel
                        print(f"    KVG骨格スナップ候補: 偏差{dev_kvg_skel:.1f}px")

            # 候補5: ㇕/㇆角分割ルーティング（角点で2セグメントに分割）
            if is_corner_type and all_kvg_pts and len(all_kvg_pts) >= 6:
                corner_idx, corner_angle = find_corner_point(all_kvg_pts)
                if corner_idx is not None:
                    corner_pt = all_kvg_pts[corner_idx]
                    # 角点を骨格にスナップ
                    # KVG-骨格Yオフセット(最大50px)を考慮した広い検索半径
                    corner_skel = find_nearest_skeleton_pixel(skel, corner_pt[0], corner_pt[1], search_radius=55)
                    if corner_skel is None:
                        corner_skel = _find_nearest_font_pixel(bmp, corner_pt[0], corner_pt[1], search_radius=55)
                    if corner_skel is None:
                        corner_skel = corner_pt
                    # セグメント1: 始点→角点
                    seg1_guide = [(round(p[0]), round(p[1])) for p in all_kvg_pts[:corner_idx+1]]
                    seg1 = skeleton_pixel_path(
                        skel, (round(sx), round(sy)), corner_skel,
                        guide_points=seg1_guide, penalty_weight=5.0,
                        font_bmp=bmp, used_pixels=used_pixels,
                        label_map=label_map, stroke_index=stroke_idx
                    )
                    # セグメント2: 角点→終点
                    seg2_guide = [(round(p[0]), round(p[1])) for p in all_kvg_pts[corner_idx:]]
                    seg2 = skeleton_pixel_path(
                        skel, corner_skel, (round(ex), round(ey)),
                        guide_points=seg2_guide, penalty_weight=5.0,
                        font_bmp=bmp, used_pixels=used_pixels,
                        label_map=label_map, stroke_index=stroke_idx
                    )
                    if seg1 and seg2 and len(seg1) >= 2 and len(seg2) >= 2:
                        # 接続点の重複除去して結合
                        combined = list(seg1)
                        if combined[-1] == seg2[0]:
                            combined.extend(seg2[1:])
                        else:
                            combined.extend(seg2)
                        dev_seg = _avg_dev(combined)
                        if dev_seg < best_dev:
                            best_path = combined
                            best_dev = dev_seg
                            print(f"    角分割ルーティング候補: 角度{corner_angle:.0f}° 偏差{dev_seg:.1f}px")

            # 改善があれば切替
            # 密集漢字（40+ノード）は偏差4以上で任意改善を許可
            n_nodes = len(nodes)
            high_dev_threshold = 4 if n_nodes >= 40 else (5 if n_nodes >= 25 else 6)
            if best_dev < dev_current * 0.90:
                print(f"    最適経路選択: 偏差{dev_current:.1f}→{best_dev:.1f}px")
                path_pixels = best_path
            elif dev_current > high_dev_threshold and best_dev < dev_current:
                print(f"    高偏差改善: 偏差{dev_current:.1f}→{best_dev:.1f}px")
                path_pixels = best_path

        # フォールバック2: ピクセルレベルDijkstra（グラフ切断時）
        if path_pixels is None or len(path_pixels) < 2:
            print(f"    グラフ経路なし → ピクセルレベル探索...")
            pixel_path = skeleton_pixel_path(
                skel, (round(sx), round(sy)), (round(ex), round(ey)),
                guide_points=guide_points, font_bmp=bmp,
                used_pixels=used_pixels, label_map=label_map, stroke_index=stroke_idx
            )
            if pixel_path and len(pixel_path) >= 2:
                print(f"    ピクセルレベル経路: {len(pixel_path)}px")
                path_pixels = pixel_path
            else:
                print(f"    ピクセルレベル経路も失敗")

        # フォールバック3: KVG座標直接（最終手段）
        if path_pixels is None or len(path_pixels) < 2:
            print(f"    ⚠ 経路なし → KVG座標フォールバック")
            pts = [(round(sx), round(sy)), (round(ex), round(ey))]
            path_d = f"M {pts[0][0]},{pts[0][1]} L {pts[1][0]},{pts[1][1]}"
            result_strokes.append({
                'points': [{'x': p[0], 'y': p[1]} for p in pts],
                'path': path_d,
                'taper': should_taper(kvg['type'])
            })
            continue

        # 使用済みエッジを記録（後続ストロークの重複回避用）
        record_used_edges(path_pixels)

        # 経路バリデーション（ウェイポイント経由でも外れる場合の検出）
        is_valid, missed = validate_path_against_kvg(path_pixels, mid_scaled)
        if not is_valid:
            print(f"    ⚠ 中間点{len(missed)}個が経路から外れている")

        # 後処理パイプライン
        # 1. 平滑化（骨格のピクセルノイズを除去）
        path_pixels = smooth_path(path_pixels, window=7)
        # 2. アウトラインクリッピング（フォント外にはみ出したポイントを内側に戻す）
        path_pixels = clip_to_outline(path_pixels, bmp)
        # 3. RDP間引き
        simplified = rdp_simplify(path_pixels, epsilon=3.0)
        # 4. ジグザグ除去（鋭い折り返し）
        simplified = remove_zigzags(simplified)
        # 5. 横方向バンプ除去（ガタつき解消）
        simplified = remove_lateral_bumps(simplified, threshold=6.0)
        # 6. 直線フィット（ほぼ直線なら2点に、閾値を緩和して横棒のガタつき解消）
        simplified = fit_line_if_straight(simplified, threshold=8.0)
        # 6b. ㇕/㇆セグメント別直線化（KVG角点で分割して各辺を直線化）
        stroke_type = kvg.get('type', '')
        if any(c in stroke_type for c in ['㇕', '㇆']) and len(simplified) >= 3:
            # KVGのall_pointsから角点を検出
            _all_kvg = [(round(px * KVG_SCALE), round(py * KVG_SCALE))
                        for px, py in kvg.get('all_points', [])]
            kvg_corner_idx, kvg_corner_angle = find_corner_point(_all_kvg, min_angle_change=40)
            if kvg_corner_idx is not None:
                kvg_corner = _all_kvg[kvg_corner_idx]
                # simplified内でKVG角点に最も近い点を探す
                best_si = None
                best_d = float('inf')
                for si in range(1, len(simplified) - 1):
                    d = ((simplified[si][0] - kvg_corner[0]) ** 2 +
                         (simplified[si][1] - kvg_corner[1]) ** 2) ** 0.5
                    if d < best_d:
                        best_d = d
                        best_si = si
                if best_si is not None and best_d < 30:
                    seg1 = simplified[:best_si + 1]
                    seg2 = simplified[best_si:]
                    seg1_straight = fit_line_if_straight(seg1, threshold=18.0)
                    seg2_straight = fit_line_if_straight(seg2, threshold=18.0)
                    if seg2_straight and seg2_straight[0] == seg1_straight[-1]:
                        simplified = list(seg1_straight) + list(seg2_straight[1:])
                    else:
                        simplified = list(seg1_straight) + list(seg2_straight)
        # 6c. 純横棒/純縦棒の強制直線化
        # ㇐/㇑タイプで3点以上残る場合、始点-終点の直線に強制変換
        # （密集文字で間違った分岐を通るケースの救済）
        if len(simplified) > 2:
            pure_type = kvg.get('type', '').rstrip('a')
            if pure_type in ('㇐', '㇑'):
                ax, ay = simplified[0]
                bx, by = simplified[-1]
                dx, dy = abs(bx - ax), abs(by - ay)
                total = (dx * dx + dy * dy) ** 0.5
                if total > 20:
                    angle_ratio = min(dx, dy) / max(dx, dy) if max(dx, dy) > 0 else 0
                    # 始点-終点の方向がH/Vに近い場合のみ強制直線化
                    if angle_ratio < 0.4:
                        simplified = [simplified[0], simplified[-1]]
        # 6d. ジグザグ除去
        # 両隣セグメントから大きく逸脱する短いセグメントを除去
        # （交差点付近で間違った分岐に入って戻るパターンの救済）
        import math
        if len(simplified) > 3:
            changed = True
            while changed and len(simplified) > 2:
                changed = False
                new_pts = [simplified[0]]
                skip_next = False
                for i in range(1, len(simplified) - 1):
                    if skip_next:
                        skip_next = False
                        continue
                    ax, ay = simplified[i - 1]
                    bx, by = simplified[i]
                    cx, cy = simplified[i + 1]
                    # B→Cセグメントの方向と長さ
                    seg_bc_angle = math.atan2(cy - by, cx - bx)
                    seg_bc_len = math.hypot(cx - bx, cy - by)
                    # A→Bセグメントの方向
                    seg_ab_angle = math.atan2(by - ay, bx - ax)
                    # A→Bとの方向差
                    diff_ab = abs(math.degrees(seg_bc_angle - seg_ab_angle))
                    if diff_ab > 180:
                        diff_ab = 360 - diff_ab
                    # C→D方向（存在すれば）
                    if i + 2 < len(simplified):
                        dx2, dy2 = simplified[i + 2]
                        seg_cd_angle = math.atan2(dy2 - cy, dx2 - cx)
                        diff_cd = abs(math.degrees(seg_cd_angle - seg_bc_angle))
                        if diff_cd > 180:
                            diff_cd = 360 - diff_cd
                    else:
                        diff_cd = diff_ab  # 末尾近くは前方向差で代用
                    # 両隣から45°以上逸脱 かつ 短いセグメント → ジグザグ
                    if diff_ab > 45 and diff_cd > 45 and seg_bc_len < 35:
                        changed = True
                        skip_next = False  # Bを除去、Cは残す
                        continue
                    new_pts.append(simplified[i])
                new_pts.append(simplified[-1])
                simplified = new_pts
        # 6e. 波打ち（wobble）除去
        # dx符号が交互に反転する短いセグメント列 → 中間点を除去
        # （骨格が左右に振れるケースの救済）
        if len(simplified) > 4:
            changed = True
            while changed and len(simplified) > 3:
                changed = False
                new_pts = [simplified[0]]
                i = 1
                while i < len(simplified) - 1:
                    ax, ay = simplified[i - 1]
                    bx, by = simplified[i]
                    cx, cy = simplified[i + 1]
                    dx_ab = bx - ax
                    dx_bc = cx - bx
                    dy_ab = by - ay
                    dy_bc = cy - by
                    seg_ab_len = math.hypot(dx_ab, dy_ab)
                    seg_bc_len = math.hypot(dx_bc, dy_bc)
                    # dx符号反転 かつ 両セグメントが短い かつ dy同符号（同方向に進行中）
                    x_reversal = (dx_ab * dx_bc < 0 and
                                  abs(dx_ab) < abs(dy_ab) * 0.8 and
                                  abs(dx_bc) < abs(dy_bc) * 0.8 and
                                  dy_ab * dy_bc > 0)
                    # dy符号反転も同様（横方向進行中の上下振れ）
                    y_reversal = (dy_ab * dy_bc < 0 and
                                  abs(dy_ab) < abs(dx_ab) * 0.8 and
                                  abs(dy_bc) < abs(dx_bc) * 0.8 and
                                  dx_ab * dx_bc > 0)
                    if (x_reversal or y_reversal) and min(seg_ab_len, seg_bc_len) < 30:
                        # Bを除去（波打ちの頂点）
                        changed = True
                        i += 1
                        continue
                    new_pts.append(simplified[i])
                    i += 1
                new_pts.append(simplified[-1])
                simplified = new_pts
        # 6f. 始点/終点フック除去
        # 最初/最後のセグメントが短くて全体方向から大きく逸脱 → 除去
        # （フォント装飾のセリフが骨格に残るケースの救済）
        if len(simplified) >= 3:
            dx_t = simplified[-1][0] - simplified[0][0]
            dy_t = simplified[-1][1] - simplified[0][1]
            total_len = math.hypot(dx_t, dy_t)
            if total_len > 20:
                angle_total = math.atan2(dy_t, dx_t)
                # 始点フック
                dx1 = simplified[1][0] - simplified[0][0]
                dy1 = simplified[1][1] - simplified[0][1]
                seg1_len = math.hypot(dx1, dy1)
                if seg1_len > 3:
                    angle1 = math.atan2(dy1, dx1)
                    diff1 = abs(math.degrees(angle1 - angle_total))
                    if diff1 > 180:
                        diff1 = 360 - diff1
                    if seg1_len < 25 and diff1 > 45:
                        simplified = [simplified[0]] + simplified[2:]
                # 終点フック
                if len(simplified) >= 3:
                    dxn = simplified[-1][0] - simplified[-2][0]
                    dyn = simplified[-1][1] - simplified[-2][1]
                    segn_len = math.hypot(dxn, dyn)
                    if segn_len > 3:
                        anglen = math.atan2(dyn, dxn)
                        diffn = abs(math.degrees(anglen - angle_total))
                        if diffn > 180:
                            diffn = 360 - diffn
                        if segn_len < 25 and diffn > 45:
                            simplified = simplified[:-2] + [simplified[-1]]
        # 6g. ㇕/㇆ 直角強制
        # 台形化を防止: 角点を調整して完全な直角を作る
        # ㇕/㇆ は漢字では常に直角（水平→垂直 or 垂直→水平）
        if any(c in stroke_type for c in ['㇕', '㇆']) and len(simplified) >= 3:
            # 角点を見つける（H/V遷移を形成する点を優先）
            # start→ci方向とci→end方向でH/Vが切り替わる点が真の角
            best_ci = None
            best_angle_change = 0
            for ci in range(1, len(simplified) - 1):
                # start→ciのグローバル方向
                dx_s = simplified[ci][0] - simplified[0][0]
                dy_s = simplified[ci][1] - simplified[0][1]
                # ci→endのグローバル方向
                dx_e = simplified[-1][0] - simplified[ci][0]
                dy_e = simplified[-1][1] - simplified[ci][1]
                s_is_h = abs(dx_s) > abs(dy_s)
                e_is_h = abs(dx_e) > abs(dy_e)
                if s_is_h != e_is_h:  # H/V遷移がある点のみ候補
                    prev = simplified[ci - 1]
                    curr = simplified[ci]
                    nxt = simplified[ci + 1]
                    angle_in = math.atan2(curr[1] - prev[1], curr[0] - prev[0])
                    angle_out = math.atan2(nxt[1] - curr[1], nxt[0] - curr[0])
                    change = abs(math.degrees(angle_out - angle_in))
                    if change > 180:
                        change = 360 - change
                    if change > best_angle_change:
                        best_angle_change = change
                        best_ci = ci
            if best_ci is not None and best_angle_change > 20:
                start = simplified[0]
                corner = simplified[best_ci]
                end = simplified[-1]
                # セグメント方向判定（start→corner, corner→end）
                dx1 = corner[0] - start[0]
                dy1 = corner[1] - start[1]
                dx2 = end[0] - corner[0]
                dy2 = end[1] - corner[1]
                seg1_is_h = abs(dx1) > abs(dy1)
                seg2_is_h = abs(dx2) > abs(dy2)
                # H→V パターン（㇕の典型: 横→右下へ曲がる）
                if seg1_is_h and not seg2_is_h:
                    new_corner = (corner[0], start[1])
                    new_end = (corner[0], end[1])
                    simplified = [start, new_corner, new_end]
                    print(f"    6g: H→V直角強制 corner({corner[0]},{corner[1]})→({new_corner[0]},{new_corner[1]})")
                # V→H パターン（㇆変形: 縦→横へ曲がる）
                elif not seg1_is_h and seg2_is_h:
                    new_corner = (start[0], corner[1])
                    new_end = (end[0], corner[1])
                    simplified = [start, new_corner, new_end]
                    print(f"    6g: V→H直角強制 corner({corner[0]},{corner[1]})→({new_corner[0]},{new_corner[1]})")
        # 6h. ㇑/㇐ 軸揃え（2点ストロークの微小ズレ修正）
        # ㇑は縦棒なのでx座標を揃えて完全垂直にする
        # ㇐は横棒なのでy座標を揃えて完全水平にする
        if len(simplified) == 2:
            pure_type = stroke_type.rstrip('ab')
            ax, ay = simplified[0]
            bx, by = simplified[1]
            dx_abs = abs(bx - ax)
            dy_abs = abs(by - ay)
            if pure_type == '㇑' and dy_abs > 20 and dx_abs < dy_abs * 0.25:
                avg_x = (ax + bx) // 2
                simplified = [(avg_x, ay), (avg_x, by)]
                if ax != avg_x or bx != avg_x:
                    print(f"    6h: ㇑垂直揃え x={ax},{bx}→{avg_x}")
            elif pure_type == '㇐' and dx_abs > 20 and dy_abs < dx_abs * 0.15:
                avg_y = (ay + by) // 2
                simplified = [(ax, avg_y), (bx, avg_y)]
                if ay != avg_y or by != avg_y:
                    print(f"    6h: ㇐水平揃え y={ay},{by}→{avg_y}")
        # 6i. 折れ曲がりストローク（㇄/㇃等）の追加平滑化
        # KVGクリッピング経路は骨格の分岐点付近でジグザグが残りやすい
        # 5点以上の場合、始点-終点を固定して中間を追加RDPで簡略化
        turn_types = ['㇄', '㇃', '㇈']
        if any(c in stroke_type for c in turn_types) and len(simplified) >= 5:
            before = len(simplified)
            simplified = rdp_simplify(simplified, epsilon=12.0)
            if len(simplified) < before:
                print(f"    6i: ㇄平滑化 {before}→{len(simplified)}点")
        # 7. 最終クリッピング（RDP後のポイントもアウトライン内に）
        simplified = clip_to_outline(simplified, bmp)
        print(f"    経路: {len(path_pixels)}px → {len(simplified)}点")

        # パス文字列生成
        pts = [{'x': p[0], 'y': p[1]} for p in simplified]
        path_d = f"M {simplified[0][0]},{simplified[0][1]}"
        for p in simplified[1:]:
            path_d += f" L {p[0]},{p[1]}"

        taper = should_taper(kvg['type'])
        print(f"    taper: {taper}")

        result_strokes.append({
            'points': pts,
            'path': path_d,
            'taper': taper
        })

    return {
        'kanji': kanji,
        'unicode': f"{ord(kanji):04X}",
        'stroke_count': len(result_strokes),
        'strokes': result_strokes
    }


def save_strokes(data: dict):
    """ストロークデータをJSONファイルに保存"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    code = f"{ord(data['kanji']):05x}"
    filename = OUTPUT_DIR / f"{code}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n  → {filename} に保存")
    return filename


# ============================================================
# CLI
# ============================================================

# 小学1年生配当漢字80字
GRADE1_KANJI = (
    "一二三四五六七八九十百千"
    "上下左右中大小月日年早"
    "木林森山川土石田天気雨"
    "花草虫犬人名女男子王玉"
    "目耳口手足力正生先休立"
    "入出本文字学校村町青赤"
    "白金円空火水夕車音糸竹貝見"
)


def main():
    debug = '--debug' in sys.argv
    grade1 = '--grade1' in sys.argv
    args = [c for c in sys.argv[1:] if not c.startswith('-')]

    if not args and not grade1:
        print("使い方: python auto_strokes.py 山 九")
        print("        python auto_strokes.py --grade1  (1年生80字)")
        sys.exit(1)

    # --grade1 フラグで1年生80字を一括生成
    if grade1:
        chars = list(GRADE1_KANJI)
    else:
        chars = args

    # 重複除去
    seen = set()
    unique = []
    for c in chars:
        if len(c) == 1 and c not in seen:
            seen.add(c)
            unique.append(c)
    chars = unique

    success = 0
    fail = 0
    for kanji in chars:
        try:
            data = generate_strokes(kanji, debug=debug)
            save_strokes(data)
            success += 1
        except Exception as e:
            print(f"✗ {kanji}: {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    print(f"\n完了: {success}成功 / {fail}失敗 / {len(chars)}文字")


if __name__ == '__main__':
    main()
