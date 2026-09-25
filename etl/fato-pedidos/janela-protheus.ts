/**
 * Janela de datas do Protheus — FONTE ÚNICA para extract e load.
 *
 * Por que existe: o `load.ts` apaga/diff-deleta Protheus a partir de um corte
 * (`data_pedido_id >= corte`) e o `extract.ts` busca a partir de outro. Se as duas
 * janelas divergirem, o diff-delete apaga linha que existe na origem mas não foi
 * buscada naquele run. Manter a conta em um só lugar torna a divergência impossível.
 *
 * Modo intraday (`ETL_SKIP_NF_CARREGADAS=true`): janela de `ETL_PROTHEUS_DIAS`
 * (default 60) dias. Modo full: sem janela — busca e espelha 2026 inteiro.
 *
 * Motivo do filtro no intraday (28/jul/2026): o card de faturado do Protheus
 * (era o 18516; desde 31/ago/2026 é o **19605**, mesma consulta com o join de SA1010
 * corrigido — ver `extract.ts`; o custo por linha é idêntico, a janela continua
 * necessária pelo mesmo motivo) (`tbFaturamento - Protheus`,
 * ~39.9k linhas, todo 2026) leva ~5 min e o transporte na frente do Metabase corta em
 * ~300s — 3 tentativas de 300s falharam num run. Com janela de 60d são ~14,2k linhas
 * em ~190s, dentro do corte com folga. Ver docs/etl/etl-fato-pedidos.md §13.
 */

export interface JanelaProtheus {
  /** true = intraday com janela; false = full (sem filtro de data na origem) */
  ativa: boolean;
  /** dias da janela (só relevante quando ativa) */
  dias: number;
  /** 'YYYY-MM-DD' para o parâmetro `data_inicial` do card; null no full */
  dataInicial: string | null;
  /** corte YYYYMMDD usado pelo DELETE/diff-delete do load */
  corteInt: number;
}

/** Corte do full: espelho de 2026 inteiro (mesmo valor histórico do load.ts). */
const CORTE_FULL = 20260101;

export function janelaProtheus(): JanelaProtheus {
  const intraday = process.env.ETL_SKIP_NF_CARREGADAS === "true";
  if (!intraday) {
    return { ativa: false, dias: 0, dataInicial: null, corteInt: CORTE_FULL };
  }

  const dias = parseInt(process.env.ETL_PROTHEUS_DIAS ?? "60", 10) || 60;
  const d = new Date();
  d.setDate(d.getDate() - dias);
  const iso = d.toISOString().slice(0, 10);

  return {
    ativa: true,
    dias,
    dataInicial: iso,
    corteInt: parseInt(iso.replace(/-/g, ""), 10),
  };
}

/**
 * O card de faturado filtra por EMISSÃO da NF; o load recorta por `data_pedido_id`.
 * Como emissão >= data do pedido, tudo que o load pode apagar (pedido >= corte) tem
 * emissão >= corte e portanto FOI buscado — o diff-delete continua seguro. Este helper
 * aplica o mesmo corte, em código, nas fontes que NÃO têm parâmetro de data no Metabase
 * (cards 18515/18661, database 48): sem isso, pedido faturado há mais de `dias`
 * sobreviveria ao `filterAlreadyInvoiced` (que só conhece o faturado buscado) e entraria
 * no banco como "Em aberto", inflando o funil.
 */
export function filtrarPorDataPedido(
  rows: Record<string, unknown>[],
  janela: JanelaProtheus
): Record<string, unknown>[] {
  if (!janela.ativa || !janela.dataInicial) return rows;
  const corte = janela.dataInicial;
  return rows.filter((r) => {
    const raw = r.data_pedido ?? r.data_emissao;
    if (!raw) return true; // sem data → mantém (não dá pra decidir; upsert resolve)
    return String(raw).slice(0, 10) >= corte;
  });
}
