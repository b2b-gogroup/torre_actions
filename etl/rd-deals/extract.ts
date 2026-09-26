import { logger } from "../shared/logger.js";
import { withRetry } from "../shared/retry.js";

const RD_BASE = "https://crm.rdstation.com/api/v1";

/**
 * Funis que alimentam `rd_deals`.
 *
 * ⚠️ **A lista ser fixa já custou 2.918 negócios invisíveis.** Até 18/ago/2026 só havia
 * "Novos" e "ATIVOS" aqui, com o comentário "IDs confirmados via API jun/2026" — e nesse
 * meio tempo o time **recriou os funis** com os mesmos nomes prefixados por `***` e IDs
 * novos. As duas gerações coexistem, então nada quebrou: a aba de CRM simplesmente foi
 * ficando incompleta **em silêncio**. Medido em 18/ago: a Torre mostrava 3.426 de 8.475
 * negócios, e Jordana (487), Annie (438) e Laura (82) apareciam com ZERO — foi como o
 * problema chegou, reportado como "insere o nome delas no RD Conversas".
 *
 * Os 4 funis `***` entraram por decisão do usuário (18/ago/2026). Ficaram FORA, de
 * propósito, os de ocorrência/chamado — "Ocorrências Inside Sales" (2.037) e "OCORRÊNCIAS
 * RCA" (31) — porque não são venda e diluiriam a conversão do time. Os demais (KEY
 * ACCOUNTS, Recompras, CLIENTES PERDIDOS, OPERAÇÕES B2B, Rafaela - Amazon, CRÍTICOS,
 * INATIVOS, MARKETPLACES, CLÍNICAS, DISTRIBUIDORES) somam ~94 negócios e seguem fora até
 * alguém decidir que contam.
 *
 * Ao mexer aqui, lembre que `avisarFunisFora()` (abaixo) declara no log todo funil com
 * negócio que não está nesta lista — é o que impede a próxima recriação de passar batida.
 */
export const PIPELINES = [
  { id: "697278408c683d0017629be2", name: "Novos" },
  { id: "6983352ed076b10013254cc3", name: "ATIVOS" },
  // geração recriada (IDs 6a56…/6a68…), adicionada em 18/ago/2026
  { id: "6a68be1db63499002f3404ec", name: "***Novos" },
  { id: "6a5680375ed37f00243e040a", name: "***Ativos" },
  { id: "6a5680e95de0830020845563", name: "***Inativos" },
  { id: "6a56807e4af5a3002cecc6ae", name: "*** Criticos" },
] as const;

/** Funis conhecidos e deliberadamente fora da carga — não viram aviso. */
const FUNIS_IGNORADOS = new Set<string>([
  "6a04599d07e3580017d0e2f4", // Ocorrências Inside Sales — chamado, não venda
  "69c3d6857ffa510013dde691", // OCORRÊNCIAS RCA — idem
]);

// ID do custom field CNPJ nas organizações (confirmado via API jun/2026)
const CNPJ_CF_ID = "69b054c3675aa20015186d18";

// ── Tipos brutos da API ──────────────────────────────────────────────────────

export interface RdDealRaw {
  id: string;
  name: string;
  win: boolean | null;
  amount_montly: number;
  amount_unique: number;
  amount_total: number;
  prediction_date: string | null;
  rating: number;
  hold: boolean | null;
  interactions: number;
  deal_lost_reason_id: string | null;
  deal_lost_note: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  deal_pipeline: { id: string; name: string } | null;
  deal_stage: { id: string; name: string; nickname: string } | null;
  user: { id: string; name: string; email?: string } | null;
  organization: { id: string; name: string } | null;
  deal_custom_fields: Array<{
    value: string | string[];
    custom_field: { label: string };
  }>;
  deal_stage_histories: Array<{
    id: string;
    deal_stage_id: string;
    start_date: string;
    end_date: string | null;
  }>;
}

export interface RdOrgRaw {
  id: string;
  organization_custom_fields: Array<{
    custom_field_id: string;
    value: string;
  }>;
}

function token(): string {
  const t = process.env.RD_CRM_TOKEN;
  if (!t) throw new Error("RD_CRM_TOKEN não configurado");
  return t;
}

function rdUrl(path: string, params: Record<string, string> = {}): string {
  const qs = new URLSearchParams({ token: token(), ...params }).toString();
  return `${RD_BASE}${path}?${qs}`;
}

// ── Loop 1 e 2: deals por funil ──────────────────────────────────────────────

async function fetchDealsPipeline(
  pipelineId: string,
  pipelineName: string
): Promise<RdDealRaw[]> {
  return withRetry(async () => {
    const deals: RdDealRaw[] = [];
    let page = 1;
    let declaredTotal = Infinity;

    logger.group(`Deals "${pipelineName}"`);

    while (true) {
      // ⚠️ Paginação por número de página, NÃO pelo cursor `next_page`.
      // O comentário antigo aqui dizia que "o cursor ignora deal_pipeline_id e paginaria
      // além do funil". Medido em 19/ago/2026 e é o contrário: com o filtro de funil, o
      // cursor devolve **exatamente a mesma página 1** (200 de 200 ids repetidos), então o
      // loop relia a primeira página até `deals.length >= declaredTotal` e o upsert
      // deduplicava — sobrava ~1 página por funil. Efeito medido: `***Novos` 1.141
      // declarados → 200 na Torre; `***Ativos` 1.119 → 200; `***Inativos` 526 → 200. O
      // único funil correto era `*** Criticos`, porque 183 cabe numa página.
      // `page=2` numérico devolve 200 ids NOVOS, mantém `total=1119` e os deals conferidos
      // pertencem ao funil pedido — o filtro é respeitado.
      const params: Record<string, string> = {
        deal_pipeline_id: pipelineId,
        limit: "200",
        page: String(page),
      };

      const res = await fetch(rdUrl("/deals", params), {
        signal: AbortSignal.timeout(30_000),
      });
      if (!res.ok) throw new Error(`RD /deals HTTP ${res.status} (página ${page})`);

      const data = await res.json() as {
        total: number;
        deals: RdDealRaw[];
        has_more: boolean;
        next_page: string | null;
      };

      // total vem só na primeira página — guarda como teto de parada e como conferência.
      if (page === 1) declaredTotal = data.total;

      // Injeta pipeline no deal — list endpoint não retorna deal_pipeline
      for (const d of data.deals) {
        if (!d.deal_pipeline) {
          (d as RdDealRaw & { deal_pipeline: { id: string; name: string } }).deal_pipeline =
            { id: pipelineId, name: pipelineName };
        }
      }
      deals.push(...data.deals);
      logger.info(`  Página ${page}: ${data.deals.length} deals (acumulado: ${deals.length}/${declaredTotal})`);

      if (data.deals.length === 0) break;           // página vazia = fim
      if (deals.length >= declaredTotal) break;
      if (!data.has_more) break;
      page++;

      if (page > 100) {
        logger.warn(`Backstop: mais de 100 páginas no funil "${pipelineName}"`);
        break;
      }

      await new Promise(r => setTimeout(r, 150));
    }

    logger.groupEnd();

    // Conferência por funil: ids ÚNICOS contra o total declarado pela própria API.
    // Sem isso, paginação que repete página passa como sucesso — foi exatamente o que
    // aconteceu por 2 meses (o log dizia "2000 deals" e o banco ficava com 200 únicos).
    const unicos = new Set(deals.map(d => d.id)).size;
    if (unicos < declaredTotal) {
      logger.warn(
        `⚠️ Funil "${pipelineName}": ${unicos} deals únicos de ${declaredTotal} declarados ` +
        `pela API (faltam ${declaredTotal - unicos}). Paginação pode ter parado antes do fim.`
      );
    } else {
      logger.info(`Funil "${pipelineName}": ${unicos} deals únicos (declarados: ${declaredTotal}) ✓`);
    }
    if (unicos < deals.length) {
      logger.info(`  (${deals.length - unicos} repetidos entre páginas, deduplicados no upsert)`);
    }
    return deals;
  }, { maxAttempts: 3, baseDelayMs: 3000, label: `Deals "${pipelineName}"` });
}

// ── Loop 3: organizações (paginação por número de página) ───────────────────

async function fetchAllOrgs(): Promise<Map<string, string | null>> {
  return withRetry(async () => {
    // Retorna Map<org_id, cnpj_14_digitos | null>
    const map = new Map<string, string | null>();
    let page = 1;

    logger.group("Organizações");

    while (true) {
      const res = await fetch(rdUrl("/organizations", { limit: "200", page: String(page) }), {
        signal: AbortSignal.timeout(30_000),
      });
      if (!res.ok) throw new Error(`RD /organizations HTTP ${res.status} (página ${page})`);

      const data = await res.json() as {
        organizations: RdOrgRaw[];
        has_more: boolean;
      };

      for (const org of data.organizations) {
        const cf = org.organization_custom_fields?.find(
          f => f.custom_field_id === CNPJ_CF_ID
        );
        const cnpj = cf?.value
          ? String(cf.value).replace(/\D/g, "").padStart(14, "0").slice(0, 14)
          : null;
        map.set(org.id, cnpj || null);
      }

      logger.info(`  Página ${page}: ${data.organizations.length} orgs (acumulado: ${map.size})`);

      if (!data.has_more || data.organizations.length === 0) break;
      page++;

      if (page > 100) {
        logger.warn("Backstop: mais de 100 páginas de organizações");
        break;
      }

      await new Promise(r => setTimeout(r, 150));
    }

    logger.groupEnd();
    logger.info(`Organizações: ${map.size} com CNPJ mapeado`);
    return map;
  }, { maxAttempts: 3, baseDelayMs: 3000, label: "Organizações RD" });
}

// ── Entry point ──────────────────────────────────────────────────────────────

export interface ExtractResult {
  deals: RdDealRaw[];
  orgsMap: Map<string, string | null>; // org_id → cnpj
}

/**
 * Declara no log todo funil que tem negócio e NÃO está sendo carregado.
 *
 * Existe porque a falha anterior não deu erro nenhum: funil recriado, ETL verde, aba
 * incompleta por semanas. Aqui não decide nada e não ingere nada — só torna visível.
 * Saudável = "nenhum funil fora da carga com negócio".
 */
async function avisarFunisFora(): Promise<void> {
  try {
    const res = await fetch(rdUrl("/deal_pipelines"), { headers: { Accept: "application/json" } });
    if (!res.ok) { logger.warn(`Conferência de funis: HTTP ${res.status} — seguindo sem ela`); return; }
    const json = await res.json() as { deal_pipelines?: { id: string; name: string }[] } | { id: string; name: string }[];
    const funis = Array.isArray(json) ? json : (json.deal_pipelines ?? []);
    // Set<string> explícito: PIPELINES é `as const`, então o Set inferido teria os IDs
    // como tipos literais e `.has(f.id)` (string vinda da API) não compilaria.
    const carregados = new Set<string>(PIPELINES.map(p => p.id));

    const fora: string[] = [];
    for (const f of funis) {
      if (carregados.has(f.id) || FUNIS_IGNORADOS.has(f.id)) continue;
      // limit=1 só pra ler o `total` — não baixa os deals
      const r = await fetch(rdUrl("/deals", { deal_pipeline_id: f.id, limit: "1" }));
      if (!r.ok) continue;
      const total = ((await r.json()) as { total?: number }).total ?? 0;
      if (total > 0) fora.push(`${f.name} (${total})`);
    }
    if (fora.length) {
      logger.warn(
        `⚠️ ${fora.length} funil(is) com negócio FORA da carga: ${fora.join(", ")}. ` +
        `Se algum deles passou a ser usado pelo time, adicione o ID em PIPELINES (extract.ts).`
      );
    } else {
      logger.info("Conferência de funis: nenhum funil fora da carga com negócio");
    }
  } catch (e) {
    logger.warn(`Conferência de funis falhou (${e instanceof Error ? e.message : e}) — não é crítica`);
  }
}

export async function extractAll(): Promise<ExtractResult> {
  // Loop 1..N: deals de todos os funis da lista, em paralelo
  logger.info(`Iniciando extração dos deals (${PIPELINES.length} funis: ${PIPELINES.map(p => p.name).join(", ")})...`);
  const porFunil = await Promise.all(
    PIPELINES.map(p => fetchDealsPipeline(p.id, p.name))
  );
  const deals = porFunil.flat();
  logger.info(`Total de deals extraídos: ${deals.length}`);

  // não bloqueia a carga — só declara o que ficou fora
  await avisarFunisFora();

  // Loop 3: organizações
  logger.info("Iniciando extração das organizações...");
  const orgsMap = await fetchAllOrgs();

  return { deals, orgsMap };
}
