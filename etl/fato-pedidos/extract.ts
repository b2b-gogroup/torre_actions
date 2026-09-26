import { fetchMetabaseCard } from "../shared/metabase-client.js";
import { fetchGoogleSheet } from "../shared/google-sheets.js";
import { fetchSupabaseTable } from "../shared/supabase-admin.js";
import { logger } from "../shared/logger.js";
import { janelaProtheus, filtrarPorDataPedido } from "./janela-protheus.js";
import {
  fetchNFsFaturadas,
  fetchPedidosAbertos,
  fetchNFsCanceladas,
  type TinyApiceConfig,
} from "./extract-tiny-apice.js";

// IDs dos cards do Metabase — coleção 462 "ETL torre" (dentro da 230 B2B)
//
// ⚠️ ESTES 6 CARDS SÃO CÓPIAS DE MANUTENÇÃO, criadas em 02/09/2026 para o ETL parar de
// depender de cards da coleção 324 "DW B2B", onde qualquer pessoa edita o que outros
// consomem — e este ETL ABORTA quando uma fonte crítica muda de shape (lista `critical`
// abaixo; a guarda existe desde 23/jul/2026, quando o card de faturado falhou, o full
// apagou todo o Protheus de 2026 e não repôs).
//
// A origem de cada um segue viva e INTOCADA para os demais consumidores do Metabase.
// Ao editar qualquer um destes, conferir contra a origem por (a) contagem, (b) md5
// ORDEM-INDEPENDENTE e (c) soma de TODA coluna numérica — card que devolve a mesma
// quantidade de linhas com valor diferente é o modo de falha que passa despercebido.
//
// Conferido em 02/09/2026, 6/6 batendo nos três critérios:
//   19609 ← 19605  15.905 linhas  md5 8e381da843d8   (140s → 21s)
//   19610 ← 18661   2.486 linhas  md5 f0b5250ab43f
//   19611 ← 18515   7.250 linhas  md5 b6b90ca0d0ac
//   19612 ← 18481   1.426 linhas  md5 c909e64ff230
//   19613 ← 18520   4.002 linhas  md5 44232de166ba
//   19614 ← 18659      34 linhas  md5 1f7db7dc2800
//
// ⚠️ md5 para comparar DUAS QUERIES tem que ser ORDEM-INDEPENDENTE. `string_agg` sem
// `ORDER BY` concatena na ordem que o plano entrega, e plano diferente → hash diferente
// COM O DADO IDÊNTICO (visto aqui: 49.832 = 49.832 linhas e hash divergente). Isso acusa
// falso positivo exatamente quando a otimização funciona, porque é aí que o plano muda.
// Use `md5(string_agg(h,'' ORDER BY h))`, ou hasheie e ordene do lado do cliente.
const CARDS = {
  protheusTrat:    19611,  // ← 18515 · db 48 Middleware Hop (ecommerce.*), query idêntica
  // ⚠️ ÚNICO dos 6 que fala com o ERP ao vivo (db 47 Protheus): junta SF2010 + SD2010 (57 GB)
  // + SC5010 + SC6010 (45 GB) + SA1010 + SF3010. Os outros 5 leem espelhos leves e respondem
  // em 1-6 s. Herdou do 18516/19605 a correção do cliente recadastrado (LATERAL que prefere o
  // cadastro vivo e aceita o deletado — sem ela a NOTA INTEIRA desaparecia: 19 NFs /
  // R$ 256.313,26 em ago/2026, todas CD ES) e ganhou DUAS correções de plano em 02/09/2026:
  //
  //   1) `a.A1_FILIAL = ''` no LATERAL do SA1010 + `SELECT a.*` (249 colunas) → as 4 usadas.
  //      Os 17 índices de SA1010 que cobrem (a1_cod, a1_loja) são TODOS liderados por
  //      a1_filial; sem ela o btree fica no plano mas é percorrido INTEIRO. Medido no mesmo
  //      índice sa10103: custo 105.462 sem a filial contra 8,45 com — 12.500×.
  //   2) A chave do join com SC5010 era `C5_NUM = (SELECT MIN(D2_PEDIDO) FROM SD2010 ...)`,
  //      subquery escalar CORRELACIONADA, executada uma vez POR NOTA (15.905 execuções)
  //      contra a SD2010. Virou a CTE agrupada `nf_pedido`: um agrupamento único.
  //
  // Plano pós-correção (EXPLAIN, janela 60d): custo total 44.976, ZERO Seq Scan, as 6 tabelas
  // em Index Scan. Dominante é a CTE sobre sd2010 (38.329, via sd2010c), que já é o índice certo.
  //
  // ⚠️ A DATA NÃO PODE SER FIXADA NO SQL. Este MESMO card serve o run FULL (2026 inteiro) e o
  // INTRADAY (janela curta); o corte vem do parâmetro `data_inicial`, preenchido por
  // janela-protheus.ts a partir de ETL_PROTHEUS_DIAS. Fixar data no SQL — ou pôr `default` no
  // template-tag, que faz o Metabase aplicar o filtro mesmo sem parâmetro — quebra o full em
  // silêncio. `{{data_inicial}}` é referenciado 2× de propósito (F2 e a CTE).
  //
  // ⚠️ NÃO PUBLICAR GANHO DE TEMPO deste card. O db 47 é um ERP compartilhado atrás de um
  // Metabase compartilhado: `pg_stat_activity` mostrou 9 conexões ATIVAS, todas do Metabase,
  // todas em wait IO:DataFileRead, de 7 userIDs diferentes, a mais antiga há 476 s. O MESMO
  // card deu 12,5 s e 497 s no mesmo dia, e o caso FULL deu 125,8 s e 6,9 s minutos depois.
  // A query em si custa ~2,4 s no banco. Antes de cronometrar qualquer coisa no db 47:
  //   SELECT count(*) FROM pg_stat_activity
  //    WHERE datname=current_database() AND state='active' AND pid<>pg_backend_pid();
  // 6+ e o número não vale. Use EXPLAIN, que é imune à fila.
  protheusFat:     19609,  // ← 19605 (que é cópia do 18516) · db 47 Protheus
  protheusPedidos: 19610,  // ← 18661 · db 48 Middleware Hop (ecommerce.*), query idêntica
  // Apice ES/RJ substituídos por API direta do Tiny (ver TINY_TOKEN_ES / TINY_TOKEN_RJ)
  tinyAPSP:        19612,  // ← 18481 · db 43 Data Mart (raw.*) — Apice SP, descontinuado
  tinyBBSP:        19613,  // ← 18520 · db 43 Data Mart (raw.*) — Barbours SP, descontinuado
  tinyAPSPPedidos: 19614,  // ← 18659 · db 43 Data Mart (raw.*) — Apice SP pedidos, descontinuado
} as const;

// Configuração da API Tiny para Apice ES e RJ
const TINY_API_FIM = new Date().toISOString().slice(0, 10);

/**
 * Janela de início da busca Tiny = 1º dia do MÊS ANTERIOR (até hoje).
 * A janela larga garante captura de status retroativo — NF do mês passado que é
 * CANCELADA ou DEVOLVIDA depois. A lista e a busca de canceladas varrem todo esse
 * range (baratas); o detalhe (caro) é só das NFs novas quando ETL_SKIP_NF_CARREGADAS=true.
 * Pode ser sobrescrito por TINY_API_INICIO (data fixa, p/ backfill manual).
 */
async function calcTinyInicio(): Promise<string> {
  if (process.env.TINY_API_INICIO) return process.env.TINY_API_INICIO;
  const h = new Date();
  let ano = h.getUTCFullYear();
  let mes = h.getUTCMonth() - 1; // mês anterior (0-11)
  if (mes < 0) { mes = 11; ano -= 1; }
  const inicio = `${ano}-${String(mes + 1).padStart(2, "0")}-01`;
  logger.info(`Tiny API janela: ${inicio} (1º dia do mês anterior) → hoje`);
  return inicio;
}

const CFG_ES: TinyApiceConfig = {
  token:  process.env.TINY_TOKEN_ES ?? "",
  filial: "CD ES",
  marca:  "Apice",
  erp:    "tiny_es",
};

const CFG_RJ: TinyApiceConfig = {
  token:  process.env.TINY_TOKEN_RJ ?? "",
  filial: "CD RJ",
  marca:  "Apice",
  erp:    "tiny_rj",
};

// Google Sheets — apenas cadastro de produtos (carteira RCA migrada para tabela DB)
const SHEETS = {
  cadastroProdutos: {
    docId: "1kZiVF8nEQVzMG1lRNK3ToStUT57LEbg63Yk9CIwl5UE",
    sheet: "base",
  },
} as const;

export interface ExtractedData {
  // Protheus
  protheusTrat: Record<string, unknown>[];
  protheusFat: Record<string, unknown>[];
  protheusPedidos: Record<string, unknown>[];
  // Tiny vendas (ES/RJ via API Tiny; SP/BB via Metabase)
  tinyAPRJ: Record<string, unknown>[];
  tinyAPES: Record<string, unknown>[];
  // NFs canceladas ES/RJ — para marcar status=Cancelado no banco
  tinyAPESCanceladas: string[];
  tinyAPRJCanceladas: string[];
  tinyAPSP: Record<string, unknown>[];
  tinyBBSP: Record<string, unknown>[];
  // Tiny pedidos
  tinyAPESPedidos: Record<string, unknown>[];
  tinyAPRJPedidos: Record<string, unknown>[];
  tinyAPSPPedidos: Record<string, unknown>[];
  // Lookups
  carteiraRCAs: Record<string, string>[];
  // Visão compartilhada por cliente (11/set/2026). NÃO é dono — é co-titular: habilita
  // o pedido a ser atribuído a quem o ERP diz que vendeu. Ver transform.ts §vendedor_id.
  carteiraCompartilhada: Record<string, string>[];
  cadastroProdutos: Record<string, string>[];
  dimVendedor: Record<string, unknown>[];
  dimVendedorAlias: Record<string, unknown>[];
  dimCliente: Record<string, unknown>[];
  dimProduto: Record<string, unknown>[];
}

type SourceName = keyof ExtractedData;

/** Executa todas as extrações (Metabase + Tiny API) */
export async function extractAll(): Promise<ExtractedData> {
  logger.group("Extração");

  // Calcula janela incremental para a API Tiny
  const TINY_API_INICIO = await calcTinyInicio();
  logger.info(`Tiny API janela: ${TINY_API_INICIO} → ${TINY_API_FIM}`);

  // ── Metabase + Supabase: rodam em paralelo (sem rate limit) ───────────────
  // Janela do Protheus (fonte única, compartilhada com o load.ts — ver janela-protheus.ts).
  // No intraday o card de faturado é filtrado por data de EMISSÃO no próprio Metabase:
  // sem isso são ~39,9k linhas / ~5min e o transporte corta em ~300s (3 tentativas
  // falharam em 28/jul). Com 60d são ~14,2k / ~190s.
  const janela = janelaProtheus();
  if (janela.ativa) {
    logger.info(
      `Protheus: janela de ${janela.dias} dias (emissão >= ${janela.dataInicial}, corte ${janela.corteInt}) — intraday`
    );
  } else {
    logger.info("Protheus: sem janela de data (full — espelha 2026 inteiro)");
  }
  const paramsFat = janela.dataInicial ? { data_inicial: janela.dataInicial } : undefined;

  const jobs: { name: SourceName; fn: () => Promise<unknown[]> }[] = [
    { name: "protheusTrat", fn: () => fetchMetabaseCard(CARDS.protheusTrat) },
    { name: "protheusFat",  fn: () => fetchMetabaseCard(CARDS.protheusFat, paramsFat) },
    { name: "protheusPedidos", fn: () => fetchMetabaseCard(CARDS.protheusPedidos) },
    // Descontinuados: mantém via Metabase
    { name: "tinyAPSP",        fn: () => fetchMetabaseCard(CARDS.tinyAPSP) },
    { name: "tinyBBSP",        fn: () => fetchMetabaseCard(CARDS.tinyBBSP) },
    { name: "tinyAPSPPedidos", fn: () => fetchMetabaseCard(CARDS.tinyAPSPPedidos) },
    // Carteira RCA: lida direto do banco (cnpj_cliente + vendedor_id UUID)
    {
      name: "carteiraRCAs",
      fn: () => fetchSupabaseTable("carteira_rca"),
    },
    // Carteira compartilhada: quem MAIS enxerga/atende o CNPJ além do dono.
    // ⚠️ NÃO é crítica de propósito — se falhar, o transform cai no comportamento
    // antigo (carteira manda sempre), que é o estado seguro. Falhar o ETL inteiro
    // por causa de uma tabela de exceção seria pior que a exceção não aplicar.
    {
      name: "carteiraCompartilhada",
      fn: () => fetchSupabaseTable("carteira_compartilhada"),
    },
    // Google Sheets — apenas cadastro de produtos
    {
      name: "cadastroProdutos",
      fn: () => fetchGoogleSheet(SHEETS.cadastroProdutos.docId, SHEETS.cadastroProdutos.sheet),
    },
    // Supabase dims
    { name: "dimVendedor",      fn: () => fetchSupabaseTable("dim_vendedor") },
    { name: "dimVendedorAlias", fn: () => fetchSupabaseTable("dim_vendedor_alias") },
    { name: "dimCliente",       fn: () => fetchSupabaseTable("dim_cliente") },
    { name: "dimProduto",       fn: () => fetchSupabaseTable("dim_produto") },
  ];

  const results = await Promise.allSettled(jobs.map((j) => j.fn()));

  const data = {} as Record<string, unknown[]>;
  const errors: string[] = [];

  results.forEach((result, i) => {
    const { name } = jobs[i];
    if (result.status === "fulfilled") {
      // Defesa em profundidade: fonte que devolve algo que não é array (ex.: corpo
      // de erro do Metabase com HTTP 200) entra como falha, não como "0 registros".
      // Sem isso o valor inválido seguia até o transform e quebrava lá
      // ("pedidos.filter is not a function"), depois de 14 min de execução — e sem
      // passar pela checagem de fontes críticas logo abaixo.
      if (Array.isArray(result.value)) {
        data[name] = result.value;
      } else {
        errors.push(`${name}: retorno invalido (${typeof result.value}, esperado array)`);
        data[name] = [];
      }
    } else {
      const msg = result.reason instanceof Error ? result.reason.message : String(result.reason);
      errors.push(`${name}: ${msg}`);
      data[name] = [];
    }
  });

  // Corte em código das fontes Protheus SEM parâmetro de data no Metabase
  // (cards 19611/19610 — cópias de 18515/18661 —, database 48; não declaram template tags).
  // Precisa casar com a
  // janela do faturado: `filterAlreadyInvoiced` (transform.ts) só sabe que um pedido foi
  // faturado se a NF veio no lote de faturado. Com faturado de 60d e pedidos desde
  // fevereiro, pedido faturado há mais tempo sobreviveria ao filtro e entraria como
  // "Em aberto", inflando o funil — eram 1.706 de 2.477 linhas (69%) do card 18661 em
  // 28/jul (card 18661, hoje 19610). Essas linhas antigas ficam fora do corte do DELETE (o load usa o MESMO corte),
  // então as que já estão no banco seguem intactas até o full da manhã.
  if (janela.ativa) {
    for (const nome of ["protheusPedidos", "protheusTrat"] as const) {
      const antes = data[nome]?.length ?? 0;
      data[nome] = filtrarPorDataPedido(data[nome] as Record<string, unknown>[], janela);
      const depois = data[nome].length;
      if (antes !== depois) {
        logger.info(`${nome}: ${antes} → ${depois} linhas após corte de ${janela.dias}d (janela do Protheus)`);
      }
    }
  }

  // ── Pipeline do Mercos: completa o que o middleware deixou de trazer ──────
  //
  // O card `protheusPedidos` (19610) lê `ecommerce.so_header` (db 48), que parou de
  // receber pedido novo em 09/09/2026 19:09:10 (ES) e 31/08 (RJ). O FATURADO não foi
  // afetado — vem do db 47 —, então o sintoma é o PIPELINE do Gerencial congelar sem
  // nenhum erro: o card segue devolvendo as mesmas ~2,4k linhas antigas, só para de
  // crescer. É a mesma assinatura da planilha de transporte em 31/ago (convenção #26):
  // a guarda de contagem do load não pega, porque o lote NÃO encolhe.
  //
  // ⚠️ Roda DEPOIS do corte de janela acima, de propósito: o módulo só gera pedido que
  // o card não trouxe, e o card já está recortado aqui. Rodar antes faria ele regerar o
  // que o corte tirou.
  //
  // ⚠️ Desliga sozinho. Quando o middleware voltar, o card volta a trazer os pedidos
  // (com item de verdade), o módulo gera 0 linha e o SKU sentinela some no primeiro run
  // full. `ETL_MERCOS_PIPELINE=false` desliga à mão se precisar.
  // Fontes do db 48 que o Mercos conseguiu repor NESTE run. Alimenta a checagem de
  // fontes críticas logo abaixo — ver a nota longa lá.
  const supridoPorMercos = new Set<SourceName>();

  if (process.env.ETL_MERCOS_PIPELINE !== "false") {
    try {
      const { montarPipelineMercos } = await import("./mercos-pipeline.js");
      const supl = await montarPipelineMercos(
        data.protheusPedidos as Record<string, unknown>[],
        Number(process.env.ETL_MERCOS_PIPELINE_DIAS ?? 60),
      );
      if (supl.linhas.length > 0) {
        data.protheusPedidos = [...(data.protheusPedidos ?? []), ...supl.linhas];
        supridoPorMercos.add("protheusPedidos");
      }
    } catch (e) {
      // NÃO é crítica: falhar aqui devolve o comportamento anterior (pipeline vazio),
      // que é ruim mas é o estado conhecido. Abortar o ETL inteiro por causa do
      // suplemento seria trocar "pipeline incompleto" por "carga não roda".
      logger.warn(`Pipeline Mercos suplementar falhou (não-crítico): ${e}`);
    }
  }

  // ── Tratativa do Mercos: repõe o ENRIQUECIMENTO que o card 19611 trazia ───
  //
  // Irmão do bloco acima. O 19611 (`protheusTrat`) é o único que traz `nome_vendedor`,
  // `forma_pagamento`, `parcelas` e `data_pedido` — medido: o `protheusFat` entrega esses
  // quatro a **ZERO**. Sem eles a carga não pode rodar (venda sem vendedor, bonificação
  // contada como venda), e é por isso que ele é `critical`.
  //
  // ⚠️ Roda DEPOIS do corte de janela, pela mesma razão do irmão: o módulo só gera a chave
  // que o card não trouxe, e o card já está recortado aqui.
  //
  // ⚠️ Desliga sozinho quando o middleware voltar (`ja_no_card` passa a cobrir tudo e ele
  // gera 0 linha). `ETL_MERCOS_TRAT=false` desliga à mão.
  if (process.env.ETL_MERCOS_TRAT !== "false") {
    try {
      const { montarTratMercos } = await import("./mercos-trat.js");
      const supl = await montarTratMercos(
        data.protheusTrat as Record<string, unknown>[],
        Number(process.env.ETL_MERCOS_TRAT_DIAS ?? process.env.ETL_MERCOS_PIPELINE_DIAS ?? 60),
      );
      if (supl.linhas.length > 0) {
        data.protheusTrat = [...(data.protheusTrat ?? []), ...supl.linhas];
        supridoPorMercos.add("protheusTrat");
      }
    } catch (e) {
      // Mesma regra do irmão: falhar aqui devolve o estado anterior (card vazio e ETL
      // abortando na checagem abaixo), que é ruim mas é o estado SEGURO. Abortar por
      // causa do suplemento seria trocar "carga não roda" por "carga não roda".
      logger.warn(`Trat Mercos suplementar falhou (não-crítico): ${e}`);
    }
  }

  if (errors.length > 0) {
    logger.error(`Extração Metabase: ${errors.length} fonte(s) falharam`, { errors });
    // dimVendedor/dimCliente/dimProduto: sem eles o transform quebra.
    // protheusTrat/protheusFat/protheusPedidos/tinyAPSP/tinyBBSP/tinyAPSPPedidos: fonte de
    // TODO o faturado/pedido Protheus+SP-legado. Se qualquer uma falhar, o load.ts (modo full)
    // faz DELETE incondicional de erp_origem NOT IN (tiny_es,tiny_rj) desde 01/jan e reinsere só
    // o que veio — fonte vazia = apaga histórico real sem repor nada (incidente 23/jul/2026:
    // protheusFat deu timeout, ETL seguiu com "0 faturados", full apagou ~R$4M de Protheus jul
    // sem devolver). Por isso essas 6 também são críticas — falha aqui aborta ANTES do load.
    const critical = [
      "dimVendedor", "dimCliente", "dimProduto",
      "protheusTrat", "protheusFat", "protheusPedidos",
      "tinyAPSP", "tinyBBSP", "tinyAPSPPedidos",
    ];
    // ⚠️ `protheusTrat`/`protheusPedidos` deixam de ser fatais **só quando o Mercos repôs
    // as linhas deles neste run** (blocos acima). É a diferença entre "tenho a informação
    // por outra fonte" e "não tenho a informação": nos dois casos o card falhou, mas só no
    // primeiro o transform recebe `nome_vendedor`/`forma_pagamento`. Sem essa distinção
    // seria afrouxar o `critical`, que é justamente o que apagou ~R$4M em 23/jul/2026.
    // As outras sete continuam fatais em qualquer cenário — nenhuma tem fonte alternativa.
    const fatais = errors.filter((e) =>
      critical.some((c) => e.startsWith(c) && !supridoPorMercos.has(c as SourceName))
    );
    if (fatais.length > 0) throw new Error(`Fontes críticas falharam: ${fatais.join("; ")}`);

    // Falha crítica coberta pelo Mercos nunca passa calada: é degradação declarada, e o
    // dia em que o middleware voltar essa linha some sozinha do log.
    const repostas = [...supridoPorMercos].filter((c) => errors.some((e) => e.startsWith(c)));
    if (repostas.length > 0) {
      logger.warn(
        `Fonte crítica falhou e foi REPOSTA pelo Mercos: ${repostas.join(", ")} — ` +
          `a carga segue, mas com o db 48 fora. Ver etl/fato-pedidos/mercos-trat.ts.`
      );

      // 🔴 O WARN acima NÃO BASTA, e essa é a lição desta rodada: o contorno deixa o run
      // VERDE e o `fato_pedidos` FRESCO, então ele apaga o sinal mais alto de que o
      // middleware está fora. Sobra log de GitHub Actions, que é exatamente onde ninguém
      // olha — foi assim que dois ETLs de transporte ficaram 12 dias parados em set/2026.
      //
      // ⚠️ Nenhuma das cinco regras da auditoria pega este caso: a carga RODOU, não
      // ENCOLHEU, o dado é de HOJE e não é pg_cron. A pergunta nova é "de ONDE veio o
      // dado?", e só quem carregou sabe responder — daí o marcador explícito.
      //
      // ⚠️ Inferir pelo SKU sentinela foi medido e NÃO serve: 0 de 403 linhas do pipeline
      // o carregavam, porque o RPA de itens tinha os itens de verdade.
      //
      // ⚠️ Falhar aqui NUNCA derruba a carga — o marcador é *sobre* a carga, não parte
      // dela. Melhor perder o carimbo que perder a carga inteira por causa dele.
      try {
        const { getSupabaseAdmin } = await import("../shared/supabase-admin.js");
        await getSupabaseAdmin()
          .from("etl_fonte_reposta")
          .insert(
            repostas.map((fonte) => ({
              fonte,
              card: CARDS[fonte as keyof typeof CARDS] ?? null,
              reposta_por: fonte === "protheusTrat" ? "mercos-trat" : "mercos-pipeline",
              linhas: (data[fonte as SourceName] as unknown[] | undefined)?.length ?? null,
              run: process.env.GITHUB_RUN_ID ?? null,
            }))
          );
      } catch (e) {
        logger.warn(`Não consegui carimbar etl_fonte_reposta (não-crítico): ${e}`);
      }
    }
  }

  // ── Tiny API: ES e RJ em sequência (evitar rate limit erro 6) ─────────────
  // Pedidos abertos: janela = ETL_ABERTOS_DIAS (default 60; intraday usa 7 p/ ficar leve
  // mas ainda pegar as TRANSIÇÕES dos pedidos recentes). O load deleta abertos na MESMA
  // janela (não toca nos mais antigos, que vêm do full da manhã). Ver docs/etl-fato-pedidos.md §6.
  // 60d (não 30d) garante cobertura do mês inteiro + buffer — pedidos de início de mês
  // não ficam stale quando o cron intraday (7d) domina por vários dias sem full rodar.
  const ABERTOS_DIAS = parseInt(process.env.ETL_ABERTOS_DIAS ?? "60", 10) || 60;
  const TINY_PEDIDOS_INICIO = (() => { const d = new Date(); d.setDate(d.getDate() - ABERTOS_DIAS); return d.toISOString().slice(0, 10); })();
  logger.info(`Pedidos abertos: janela ${ABERTOS_DIAS} dias (${TINY_PEDIDOS_INICIO} → hoje)`);

  // ES: NFs → pedidos → canceladas (sequencial, 500ms entre cada call)
  if (CFG_ES.token) {
    logger.info("Tiny API ES: iniciando (sequencial para evitar rate limit)");
    try { data.tinyAPES = await fetchNFsFaturadas(CFG_ES, TINY_API_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPES falhou: ${e}`); data.tinyAPES = []; }
    try { data.tinyAPESPedidos = await fetchPedidosAbertos(CFG_ES, TINY_PEDIDOS_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPESPedidos falhou: ${e}`); data.tinyAPESPedidos = []; }
    try { data.tinyAPESCanceladas = await fetchNFsCanceladas(CFG_ES, TINY_API_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPESCanceladas falhou: ${e}`); data.tinyAPESCanceladas = []; }
  } else {
    data.tinyAPES = []; data.tinyAPESPedidos = []; data.tinyAPESCanceladas = [];
  }

  // RJ: NFs → pedidos → canceladas (sequencial)
  if (CFG_RJ.token) {
    logger.info("Tiny API RJ: iniciando (sequencial para evitar rate limit)");
    try { data.tinyAPRJ = await fetchNFsFaturadas(CFG_RJ, TINY_API_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPRJ falhou: ${e}`); data.tinyAPRJ = []; }
    try { data.tinyAPRJPedidos = await fetchPedidosAbertos(CFG_RJ, TINY_PEDIDOS_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPRJPedidos falhou: ${e}`); data.tinyAPRJPedidos = []; }
    try { data.tinyAPRJCanceladas = await fetchNFsCanceladas(CFG_RJ, TINY_API_INICIO, TINY_API_FIM); }
    catch (e) { logger.warn(`tinyAPRJCanceladas falhou: ${e}`); data.tinyAPRJCanceladas = []; }
  } else {
    data.tinyAPRJ = []; data.tinyAPRJPedidos = []; data.tinyAPRJCanceladas = [];
  }

  // Array.isArray na soma: uma fonte com retorno inválido fazia o total virar NaN
  // ("Extração completa: NaN registros totais") e escondia o problema no log.
  const totalRows = Object.values(data).reduce(
    (sum, arr) => sum + (Array.isArray(arr) ? arr.length : 0),
    0
  );
  logger.info(`Extração completa: ${totalRows} registros totais`);
  logger.groupEnd();

  return data as unknown as ExtractedData;
}
