/**
 * 漢字書き順エディタ API Worker
 * KVに保存データを格納、KanjiVGデータをキャッシュ
 */

interface Env {
  KV: KVNamespace;
}

// 1年生80字の漢字リスト
const KANJI_BY_GRADE: Record<string, string[]> = {
  "1": [
    "一","右","雨","円","王","音","下","火","花","貝",
    "学","気","九","休","玉","金","空","月","犬","見",
    "五","口","校","左","三","山","子","四","糸","字",
    "耳","七","車","手","十","出","女","小","上","森",
    "人","水","正","生","青","夕","石","赤","千","川",
    "先","早","草","足","村","大","男","竹","中","虫",
    "町","天","田","土","二","日","入","年","白","八",
    "百","文","木","本","名","目","立","力","林","六"
  ]
};

// Unicode変換ヘルパー
function kanjiToCode(ch: string): string {
  return ch.codePointAt(0)!.toString(16).padStart(5, '0');
}

// CORSヘッダー
const CORS_HEADERS: Record<string, string> = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      ...CORS_HEADERS,
    },
  });
}

// --- API ハンドラ ---

async function handleList(env: Env): Promise<Response> {
  const result: unknown[] = [];
  for (const [gradeStr, chars] of Object.entries(KANJI_BY_GRADE)) {
    const grade = parseInt(gradeStr);
    for (const ch of chars) {
      const code = kanjiToCode(ch);
      // KVに保存データがあるかチェック
      const stored = await env.KV.get(`stroke:${code}`);
      let hasData = false;
      let strokeCount = 0;
      if (stored) {
        hasData = true;
        try {
          const d = JSON.parse(stored);
          strokeCount = d.stroke_count || (d.strokes?.length ?? 0);
        } catch {}
      }
      result.push({ kanji: ch, unicode: code, grade, has_data: hasData, stroke_count: strokeCount });
    }
  }
  return jsonResponse(result);
}

async function handleLoad(code: string, env: Env): Promise<Response> {
  const data = await env.KV.get(`stroke:${code}`);
  if (!data) {
    return jsonResponse({ error: 'Not found' }, 404);
  }
  return new Response(data, {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      ...CORS_HEADERS,
    },
  });
}

async function handleSave(code: string, request: Request, env: Env): Promise<Response> {
  try {
    const body = await request.text();
    // JSONとして検証
    JSON.parse(body);
    await env.KV.put(`stroke:${code}`, body);
    return jsonResponse({ ok: true });
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : 'Unknown error';
    return jsonResponse({ error: message }, 400);
  }
}

async function handleKvg(code: string, env: Env): Promise<Response> {
  // KVキャッシュ確認
  const cached = await env.KV.get(`kvg:${code}`);
  if (cached) {
    return new Response(cached, {
      headers: {
        'Content-Type': 'image/svg+xml; charset=utf-8',
        ...CORS_HEADERS,
      },
    });
  }

  // GitHubから取得
  const url = `https://raw.githubusercontent.com/KanjiVG/kanjivg/master/kanji/${code}.svg`;
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(10000) });
    if (!resp.ok) {
      return jsonResponse({ error: `KanjiVG: ${resp.statusText}` }, resp.status);
    }
    const svgText = await resp.text();
    // KVにキャッシュ（30日間）
    await env.KV.put(`kvg:${code}`, svgText, { expirationTtl: 60 * 60 * 24 * 30 });
    return new Response(svgText, {
      headers: {
        'Content-Type': 'image/svg+xml; charset=utf-8',
        ...CORS_HEADERS,
      },
    });
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : 'Fetch failed';
    return jsonResponse({ error: message }, 502);
  }
}

// --- メインルーター ---

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // API ルーティング
    if (path === '/api/list' && request.method === 'GET') {
      return handleList(env);
    }

    const loadMatch = path.match(/^\/api\/load\/([0-9a-f]{5})$/);
    if (loadMatch && request.method === 'GET') {
      return handleLoad(loadMatch[1], env);
    }

    const saveMatch = path.match(/^\/api\/save\/([0-9a-f]{5})$/);
    if (saveMatch && request.method === 'POST') {
      return handleSave(saveMatch[1], request, env);
    }

    const kvgMatch = path.match(/^\/api\/kvg\/([0-9a-f]{5})$/);
    if (kvgMatch && request.method === 'GET') {
      return handleKvg(kvgMatch[1], env);
    }

    return jsonResponse({ error: 'Not found' }, 404);
  },
} satisfies ExportedHandler<Env>;
