import { logger } from "./logger.js";
import { withRetry } from "./retry.js";

const METABASE_URL = process.env.METABASE_URL || "https://metabase.gobeaute.com.br";

/**
 * Retry das chamadas ao Metabase: 6 tentativas, esperas 3 s, 6 s, 12 s, 24 s, 30 s
 * (~75 s de janela). Era 3 tentativas / 2 s + 4 s (~6 s), curto demais para a
 * conexão Metabase → Protheus (db 47), que pisca por 15-30 s: em 28/out/2026 três
 * runs seguidos do ETL abortaram com "The connection attempt failed." no card
 * 19609 (fonte crítica) enquanto a mesma consulta voltava sozinha segundos depois.
 */
const RETRY_METABASE = { maxAttempts: 6, baseDelayMs: 3000, maxDelayMs: 30000 };

function getApiKey(): string {
  const key = process.env.METABASE_API_KEY;
  if (!key) {
    throw new Error("METABASE_API_KEY é obrigatória");
  }
  return key;
}

/**
 * Busca dados de um card do Metabase via API key.
 *
 * `params` preenche as **template tags** do card (`{{nome}}` no SQL nativo) —
 * ex.: `fetchMetabaseCard(18516, { data_inicial: "2026-05-29" })`. Só funciona em
 * tag que o card declara; tag inexistente é ignorada pelo Metabase (não dá erro,
 * a query volta sem filtro). Conferir com `GET /api/card/<id>` →
 * `dataset_query.native["template-tags"]` antes de confiar num nome novo.
 */
export async function fetchMetabaseCard(
  cardId: number,
  params?: Record<string, string>
): Promise<Record<string, unknown>[]> {
  // Metabase v0.64 (atualizado em 28/out/2026) passou a exigir `id` em cada parâmetro
  // (sem ele: 400 "Schema for a string that cannot be blank"). E o `id` tem de ser o
  // da TEMPLATE TAG do card: com um id qualquer o Metabase responde 200 e IGNORA o
  // filtro em silêncio (medido no card 19609: 72.889 linhas sem filtro × 24.192 com).
  // Por isso o id é lido do próprio card, e tag que não existe aborta em vez de seguir.
  const ids = params && Object.keys(params).length ? await idsDasTags(cardId) : {};
  const parameters = Object.entries(params ?? {}).map(([slug, value]) => {
    const id = ids[slug];
    if (!id) {
      throw new Error(`Metabase card ${cardId}: template tag "${slug}" não existe no card`);
    }
    return {
      id,
      type: "string/=",
      value: [value],
      target: ["variable", ["template-tag", slug]],
    };
  });

  return withRetry(
    async () => {
      const res = await fetch(`${METABASE_URL}/api/card/${cardId}/query/json`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": getApiKey(),
        },
        body: JSON.stringify({ parameters }),
      });

      if (!res.ok) {
        throw new Error(`Metabase card ${cardId}: ${res.status} ${res.statusText}`);
      }

      const parsed = (await res.json()) as unknown;

      // O Metabase responde 200 mesmo quando a query falha do lado dele — o corpo
      // vira um OBJETO de erro ({status:"failed", error:...}) em vez do array de
      // linhas. Sem esta checagem o retorno inválido virava "sucesso": o log dizia
      // "undefined registros", o withRetry não tentava de novo, a lista de fontes
      // críticas (extract.ts) não abortava — e o ETL só quebrava 14 min depois no
      // transform com "pedidos.filter is not a function" (incidente 28/jul/2026,
      // card 18661/protheusPedidos; o mesmo card respondeu normal minutos depois).
      if (!Array.isArray(parsed)) {
        const snippet = JSON.stringify(parsed ?? null).slice(0, 300);
        throw new Error(
          `Metabase card ${cardId}: resposta 200 mas nao e array (${typeof parsed}) — ${snippet}`
        );
      }

      const data = parsed as Record<string, unknown>[];
      const filtro = parameters.length
        ? ` (filtro: ${Object.entries(params ?? {}).map(([k, v]) => `${k}=${v}`).join(", ")})`
        : "";
      logger.info(`Metabase card ${cardId}: ${data.length} registros${filtro}`);
      return data;
    },
    { ...RETRY_METABASE, label: `Metabase card ${cardId}` }
  );
}

/**
 * Nome → id das template tags de um card. Aceita os dois formatos do Metabase:
 * o antigo (`dataset_query.native["template-tags"]`, objeto por nome) e o v0.64
 * (`dataset_query.stages[].["template-tags"]`, lista com `name`).
 */
async function idsDasTags(cardId: number): Promise<Record<string, string>> {
  return withRetry(
    async () => {
      const res = await fetch(`${METABASE_URL}/api/card/${cardId}`, {
        headers: { "x-api-key": getApiKey() },
      });
      if (!res.ok) {
        throw new Error(`Metabase card ${cardId} (metadados): ${res.status} ${res.statusText}`);
      }
      const card = (await res.json()) as { dataset_query?: Record<string, unknown> };
      const dq = (card.dataset_query ?? {}) as Record<string, unknown>;
      const out: Record<string, string> = {};
      const coleta = (tags: unknown) => {
        if (Array.isArray(tags)) {
          for (const t of tags as { name?: string; id?: string }[]) {
            if (t?.name && t?.id) out[t.name] = t.id;
          }
        } else if (tags && typeof tags === "object") {
          for (const [nome, t] of Object.entries(tags as Record<string, { id?: string }>)) {
            if (t?.id) out[nome] = t.id;
          }
        }
      };
      coleta((dq.native as Record<string, unknown> | undefined)?.["template-tags"]);
      for (const st of (dq.stages as Record<string, unknown>[] | undefined) ?? []) {
        coleta(st?.["template-tags"]);
      }
      return out;
    },
    { ...RETRY_METABASE, label: `Metabase card ${cardId} (metadados)` }
  );
}

interface MetabaseDataset {
  data?: { rows?: unknown[][]; cols?: { name: string }[] };
  [k: string]: unknown;
}

/** Query SQL nativa ad-hoc contra um database do Metabase (ex.: Data Mart, database 43). */
export function nativeQuery(database: number, sql: string): Record<string, unknown> {
  return { database, type: "native", native: { query: sql, "template-tags": {} }, parameters: [] };
}

/** Executa a query (MBQL ou nativa) via /api/dataset e devolve as linhas já como objetos. */
export async function datasetRows(payload: Record<string, unknown>): Promise<Record<string, unknown>[]> {
  const json = await withRetry(
    async () => {
      const res = await fetch(`${METABASE_URL}/api/dataset`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-api-key": getApiKey() },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`Metabase dataset: HTTP ${res.status} ${await res.text().catch(() => "")}`.trim());
      return res.json() as Promise<MetabaseDataset>;
    },
    { ...RETRY_METABASE, label: "Metabase dataset" },
  );
  const cols = json.data?.cols?.map((c) => c.name) ?? [];
  const rows = json.data?.rows ?? [];
  return rows.map((row) => {
    const obj: Record<string, unknown> = {};
    cols.forEach((name, i) => { obj[name] = (row as unknown[])[i]; });
    return obj;
  });
}
