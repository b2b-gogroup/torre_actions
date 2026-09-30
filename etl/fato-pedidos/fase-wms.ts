/**
 * Fase do WMS (Corpem/BraLog) → etapa do funil da Torre. FONTE ÚNICA do mapeamento.
 *
 * Definido pela área de Operações em 30/set/2026. Antes disso o mapeamento morava só em
 * `mercos-pipeline.ts` (e o card antigo 19610 ignorava o WMS, usando o `status_personalizado`
 * do Mercos), então o MESMO estágio físico aparecia com dois rótulos diferentes.
 *
 * Estado ANTERIOR (para reverter) e passo a passo: docs/processos/funil-status-wms-fase.md
 *
 * `Em Sep.` e `Emb. Conf.` não estavam na lista de Operações; o usuário decidiu em 30/set/2026:
 * `Em Sep.` = Preparando para envio e `Emb. Conf.` = Faturado. Ajustes futuros: AQUI, num lugar só.
 */
import { fetchSupabaseTable } from "../shared/supabase-admin.js";
import { logger } from "../shared/logger.js";

export const ETAPA_PREPARANDO = "Em separação";
export const ETAPA_AGUARDANDO = "Aguardando Faturamento";

/** Fases que ainda são pipeline: fase do WMS → etapa. */
export const FASE_WMS_STATUS: Record<string, string> = {
  "Em Digit.": ETAPA_PREPARANDO,
  "A Sep.": ETAPA_PREPARANDO,
  "Em Sep.": ETAPA_PREPARANDO,      // decisão do usuário (não estava na lista de Operações)
  "Sep. Ok": ETAPA_PREPARANDO,
  "Em Cko.": ETAPA_PREPARANDO,
  "Cko. Ok": ETAPA_PREPARANDO,
  "Sep. Conf.": ETAPA_AGUARDANDO,
};

/**
 * Fases em que a nota já saiu: o pedido NÃO é pipeline (o faturado entra pelo Protheus, com
 * os itens reais). As três primeiras já trazem `numero_nf` preenchido no WMS (medido em
 * 30/set: CkoVol. Ok 55/55, Em CkoVol. 1/1, N.F. Conf. 30/30).
 */
export const FASE_WMS_JA_FATURADO = new Set([
  "CkoVol. Ok",
  "Em CkoVol.",
  "N.F. Conf.",
  "Emb. Conf.",                     // decisão do usuário (não estava na lista de Operações)
]);

export interface LinhaWms {
  numero_pedido_cliente: string | null;
  regiao: string | null;
  situacao_fase: string | null;
  numero_nf: string | null;
  /** Unidades do pedido inteiro. Medido: sempre preenchido e > 0. */
  qtde_total: number | string | null;
}

/** `ES_APICE` e `ES` são o mesmo CD; número de pedido sem zero à esquerda. */
export const chaveWms = (regiao: unknown, pedido: unknown) =>
  `${String(regiao ?? "").trim().toUpperCase().replace("_APICE", "")}|${String(pedido ?? "").trim().replace(/^0+/, "") || "0"}`;

function regiaoDaFilial(filial: unknown): string {
  const f = String(filial ?? "");
  return f.includes("RJ") ? "RJ" : f.includes("SP") ? "SP" : "ES";
}

/**
 * Reescreve a etapa dos pedidos Protheus NÃO faturados que vêm do card antigo 19610, usando
 * a fase real do WMS. Só troca entre Preparando ↔ Aguardando: não descarta linha, não mexe em
 * "Em aberto" (pedido que o WMS ainda não conhece) e não toca pedido sem registro no WMS.
 *
 * ⚠️ A chave é (região, nº do pedido): o número recicla entre CDs, e cruzar só por número
 * trouxe 14 pedidos "Emb. Conf." falsos na 1ª medição.
 *
 * Desliga com `ETL_FASE_WMS=false`. Falha aqui NÃO aborta a carga (estado anterior = status do Mercos).
 */
export async function aplicarFaseWms(
  linhas: Record<string, unknown>[],
): Promise<{ trocadas: number; sem_wms: number; ja_faturado_no_wms: number; por_etapa: Record<string, number> }> {
  const diag = { trocadas: 0, sem_wms: 0, ja_faturado_no_wms: 0, por_etapa: {} as Record<string, number> };
  if (process.env.ETL_FASE_WMS === "false") return diag;

  const wms = (await fetchSupabaseTable("corpem_saida")) as unknown as LinhaWms[];
  const porPedido = new Map<string, LinhaWms>();
  for (const w of wms) porPedido.set(chaveWms(w.regiao, w.numero_pedido_cliente), w);

  for (const r of linhas) {
    const atual = String(r.status ?? "");
    if (atual !== ETAPA_PREPARANDO && atual !== ETAPA_AGUARDANDO) continue;
    const w = porPedido.get(chaveWms(regiaoDaFilial(r.filial), r.pedido_mercos));
    if (!w) { diag.sem_wms++; continue; }
    const fase = String(w.situacao_fase ?? "");
    if (FASE_WMS_JA_FATURADO.has(fase)) { diag.ja_faturado_no_wms++; continue; }
    const nova = FASE_WMS_STATUS[fase];
    if (!nova || nova === atual) continue;
    r.status = nova;
    diag.trocadas++;
    diag.por_etapa[`${atual} → ${nova}`] = (diag.por_etapa[`${atual} → ${nova}`] ?? 0) + 1;
  }

  logger.info(
    `Fase WMS (card 19610): ${diag.trocadas} trocadas · ${diag.sem_wms} sem registro no WMS · ` +
    `${diag.ja_faturado_no_wms} com fase de nota já emitida (mantidas) ` +
    JSON.stringify(diag.por_etapa),
  );
  return diag;
}
