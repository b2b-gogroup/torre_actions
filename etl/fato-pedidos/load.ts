import pg from "pg";
import { logger } from "../shared/logger.js";
import { withRetry } from "../shared/retry.js";
import type { FatoPedidoRow } from "./transform.js";
import { janelaProtheus } from "./janela-protheus.js";

const BATCH_SIZE = 500;
const DELAY_BETWEEN_BATCHES_MS = 500;

const UPSERT_SQL = `
INSERT INTO fato_pedidos (
  pedido_id, item_sequencia, nf_numero, nf_chave, nf_referenciada, erp_origem,
  data_pedido_id, data_faturamento_id,
  cliente_id, vendedor_id, produto_id, marca_id, filial_id,
  tipo_operacao, status, quantidade, valor_unitario, valor_total,
  valor_desconto, valor_frete, uf_id, pedido_erp_id,
  forma_pagamento, parcelas
)
SELECT
  pedido_id, item_sequencia, nf_numero, nf_chave, nf_referenciada, erp_origem,
  data_pedido_id, data_faturamento_id,
  cliente_id, vendedor_id::uuid, produto_id, marca_id, filial_id::uuid,
  tipo_operacao, status, quantidade, valor_unitario, valor_total,
  valor_desconto, valor_frete, uf_id, pedido_erp_id,
  forma_pagamento, parcelas
FROM json_populate_recordset(
  null::record,
  $1::json
) AS x(
  pedido_id text, item_sequencia int, nf_numero text, nf_chave text, nf_referenciada text, erp_origem text,
  data_pedido_id int, data_faturamento_id int,
  cliente_id text, vendedor_id text, produto_id text, marca_id text, filial_id text,
  tipo_operacao text, status text, quantidade numeric, valor_unitario numeric, valor_total numeric,
  valor_desconto numeric, valor_frete numeric, uf_id text, pedido_erp_id text,
  forma_pagamento text, parcelas text
)
ON CONFLICT (erp_origem, filial_id, pedido_id, item_sequencia) DO UPDATE SET
  nf_numero           = EXCLUDED.nf_numero,
  nf_chave            = EXCLUDED.nf_chave,
  nf_referenciada     = EXCLUDED.nf_referenciada,
  data_faturamento_id = EXCLUDED.data_faturamento_id,
  cliente_id          = EXCLUDED.cliente_id,
  vendedor_id         = EXCLUDED.vendedor_id,
  produto_id          = EXCLUDED.produto_id,
  marca_id            = EXCLUDED.marca_id,
  filial_id           = EXCLUDED.filial_id,
  status              = EXCLUDED.status,
  quantidade          = EXCLUDED.quantidade,
  valor_unitario      = EXCLUDED.valor_unitario,
  valor_total         = EXCLUDED.valor_total,
  valor_desconto      = EXCLUDED.valor_desconto,
  valor_frete         = EXCLUDED.valor_frete,
  forma_pagamento     = EXCLUDED.forma_pagamento,
  parcelas            = EXCLUDED.parcelas,
  updated_at          = now()
-- Guarda anti-churn: só reescreve a linha se ALGUM campo realmente mudou.
-- Sem isso, todo run reescrevia ~38k linhas idênticas (dead tuples + WAL à toa).
-- ⚠️ CAMPO NOVO TEM DE ENTRAR NAS SEIS LISTAS deste SQL — colunas do INSERT, SELECT, tipos do
-- json_populate_recordset, DO UPDATE SET e as DUAS tuplas daqui. Faltar só nas tuplas é o
-- pior caso: o campo entra em linha nova e NUNCA é preenchido nas que já existem, porque a
-- guarda considera a linha "idêntica" e pula o update — falha silenciosa, sem erro nenhum.
WHERE (
  fato_pedidos.nf_numero, fato_pedidos.nf_chave, fato_pedidos.nf_referenciada,
  fato_pedidos.data_faturamento_id,
  fato_pedidos.cliente_id, fato_pedidos.vendedor_id, fato_pedidos.produto_id,
  fato_pedidos.marca_id, fato_pedidos.filial_id, fato_pedidos.status,
  fato_pedidos.quantidade, fato_pedidos.valor_unitario, fato_pedidos.valor_total,
  fato_pedidos.valor_desconto, fato_pedidos.valor_frete,
  fato_pedidos.forma_pagamento, fato_pedidos.parcelas
) IS DISTINCT FROM (
  EXCLUDED.nf_numero, EXCLUDED.nf_chave, EXCLUDED.nf_referenciada,
  EXCLUDED.data_faturamento_id,
  EXCLUDED.cliente_id, EXCLUDED.vendedor_id, EXCLUDED.produto_id,
  EXCLUDED.marca_id, EXCLUDED.filial_id, EXCLUDED.status,
  EXCLUDED.quantidade, EXCLUDED.valor_unitario, EXCLUDED.valor_total,
  EXCLUDED.valor_desconto, EXCLUDED.valor_frete,
  EXCLUDED.forma_pagamento, EXCLUDED.parcelas
);
`;

const STATUS_PRIORITY: Record<string, number> = {
  Faturado: 0,
  "Em separação": 1,
  "Aguardando Faturamento": 2,
  "Em aberto": 3,
};

/** Chave de dedup: filial + (pedido_id ou nf_numero) + item_sequencia */
function dedupKey(item: FatoPedidoRow): string {
  const pedido = item.pedido_id || item.nf_numero || "";
  return `${item.filial_id}||${pedido}||${item.item_sequencia}`;
}

/** Deduplica por filial_id + pedido_id (ou nf_numero) + item_sequencia, priorizando status Faturado */
function dedup(items: FatoPedidoRow[]): FatoPedidoRow[] {
  const map = new Map<string, FatoPedidoRow>();
  for (const item of items) {
    const key = dedupKey(item);
    const existing = map.get(key);
    if (!existing) {
      map.set(key, item);
    } else {
      const existingPrio = STATUS_PRIORITY[existing.status ?? ""] ?? 99;
      const newPrio = STATUS_PRIORITY[item.status ?? ""] ?? 99;
      if (newPrio < existingPrio) map.set(key, item);
    }
  }
  return Array.from(map.values());
}

/** Divide array em chunks de tamanho fixo */
function chunk<T>(arr: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size));
  }
  return chunks;
}

export interface LoadResult {
  totalItems: number;
  batches: number;
  batchesFailed: number;
}

export interface LoadOptions {
  canceladasES?: string[];  // NF números canceladas Apice ES
  canceladasRJ?: string[];  // NF números canceladas Apice RJ
  /**
   * Hora de início deste run do ETL — vira o carimbo de chegada em `fato_pedido_carga`.
   * ⚠️ É UM valor para o lote inteiro, de propósito. Usar `now()` por batch daria segundos
   * diferentes a pedidos que chegaram na MESMA carga e inventaria uma ordem que não existe:
   * a resolução real é a da carga (9 runs/dia), não a do pedido no ERP.
   */
  cargaEm?: Date;
  /** Run do GitHub Actions que trouxe esta carga (rastreabilidade: "qual carga trouxe"). */
  cargaRun?: string;
}

/** Carrega os dados na fato_pedidos via batches com upsert */
export async function loadBatches(items: FatoPedidoRow[], opts: LoadOptions = {}): Promise<LoadResult> {
  logger.group("Carga");

  const unique = dedup(items);
  logger.info(`Dedup: ${items.length} → ${unique.length} itens únicos`);

  const batches = chunk(unique, BATCH_SIZE);
  logger.info(`${batches.length} batches de até ${BATCH_SIZE} itens`);

  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("DATABASE_URL é obrigatória para a carga");
  }

  // ⚠️ TIMEOUTS OBRIGATORIOS. Sem eles, `pool.connect()` espera PARA SEMPRE quando o pool
  // nao entrega conexao, e `client.query()` nunca resolve nem rejeita — a mesma classe de bug
  // que travava o boleto do Itau "processando eternamente" (31/jul/2026, `fetch` sem timeout).
  // Aqui doi mais: os 44 batches de UPSERT logam em `logger.debug`, que e SUPRIMIDO, entao um
  // travamento no meio do laco e 100% silencioso — o run fica `in_progress` sem uma linha de log
  // e sem nada em `pg_stat_activity`, e nao ha como distinguir "lento" de "morto".
  //   connectionTimeoutMillis  falha em 30 s em vez de esperar o pool para sempre
  //   statement_timeout        o UPSERT normal leva ~1 s; 120 s e folga de 100x
  //   query_timeout            rede/pgbouncer podem engolir a resposta sem o servidor abortar
  // O `withRetry` do laco (maxAttempts 2) reaproveita isso: agora ele TEM o que retentar.
  const pool = new pg.Pool({
    connectionString: databaseUrl,
    max: 2,
    connectionTimeoutMillis: 30_000,
    statement_timeout: 120_000,
    query_timeout: 150_000,
  });
  let batchesFailed = 0;

  try {

    // Guarda de sanidade: aborta ANTES do delete se o novo lote de Protheus/SP/BB
    // faturado vier muito menor que o que já existe no banco. Protege contra fonte
    // Metabase que falha SEM lançar erro (200 vazio/truncado por timeout no lado deles) —
    // o critical-list em extract.ts já cobre a falha explícita, isto é a segunda camada.
    // Incidente 23/jul/2026: protheusFat com "fetch failed", ETL seguiu com 0 faturados,
    // full apagou ~R$4M de Protheus de julho sem repor. Ver docs/etl/etl-fato-pedidos.md §6.
    // ATENÇÃO: a comparação tem que ser maçã com maçã. No intraday o extract busca o
    // faturado Protheus só da janela (`janelaProtheus()`, default 60d), então contar o
    // banco INTEIRO faria a guarda abortar sempre: 14.425 na janela vs 47.419 no total
    // = 30,4%, colado no limite de 30% (medido 28/jul/2026). Por isso, quando a janela
    // está ativa, o COUNT do banco é recortado pela mesma janela (por data de emissão,
    // que é o campo filtrado na origem). No full segue global.
    {
      const janela = janelaProtheus();
      const novosFaturadosNaoTiny = unique.filter(
        (i) => !["tiny_es", "tiny_rj"].includes(i.erp_origem) && (i.status ?? "").toLowerCase() === "faturado"
      ).length;
      const guardClient = await pool.connect();
      let existentesFaturados = 0;
      try {
        const r = janela.ativa
          ? await guardClient.query(
              `SELECT COUNT(*) AS n FROM fato_pedidos
                WHERE erp_origem NOT IN ('tiny_es','tiny_rj','tiny_es_fix','tiny_rj_fix', 'tiny_sp', 'tiny_sp_fix','denavita_fix')
                  AND lower(status)='faturado'
                  AND data_faturamento_id >= $1`,
              [janela.corteInt]
            )
          : await guardClient.query(
              `SELECT COUNT(*) AS n FROM fato_pedidos WHERE erp_origem NOT IN ('tiny_es','tiny_rj','tiny_es_fix','tiny_rj_fix', 'tiny_sp', 'tiny_sp_fix','denavita_fix') AND lower(status)='faturado'`
            );
        existentesFaturados = parseInt(r.rows[0]?.n ?? "0", 10);
      } finally {
        guardClient.release();
      }
      const MIN_BASELINE = 50;   // abaixo disso o banco já era pequeno — não compara
      const THRESHOLD = 0.3;     // novo lote não pode vir com menos de 30% do que já existe
      if (existentesFaturados > MIN_BASELINE && novosFaturadosNaoTiny < existentesFaturados * THRESHOLD) {
        throw new Error(
          `Guarda de sanidade Protheus/SP/BB: novo lote tem ${novosFaturadosNaoTiny} itens faturados, ` +
          `banco tem ${existentesFaturados}${janela.ativa ? ` (janela ${janela.dias}d, emissão >= ${janela.corteInt})` : " (total)"} ` +
          `— queda > 70%. Abortando ANTES do delete ` +
          `(fonte provavelmente falhou parcialmente). Ver docs/etl/etl-fato-pedidos.md §6.`
        );
      }
    }

    // DELETE principal: apaga Protheus (reimportado via Metabase).
    // ⚠️ `tiny_sp` está CONGELADO desde 29/set/2026 e entra nas listas de exclusão junto com
    // a família `_fix`: o Data Mart arquivou as tabelas das contas Tiny Ápice/Barbours
    // atacado SP (`raw.*` → `archive.*`, sem permissão para o Metabase), então os cards
    // 19612/19613/19614 deixaram de existir como fonte. Sem estar aqui, o próximo full
    // apagaria o `tiny_sp` de 2026 inteiro (R$ 5,0 mi) e não reporia nada.
    // As 5 listas (guarda ×2, keepKeys, diff-delete, delete) têm que andar JUNTAS.
    //  • Modo FULL (skip off): apaga desde 20260101 — espelho exato da origem
    //    (linhas deletadas na origem somem aqui).
    //  • Modo SKIP (intraday): apaga só os últimos 60 dias — linhas antigas ficam e
    //    são atualizadas via ON CONFLICT (com guarda anti-churn: linha idêntica não
    //    é reescrita). Deleção na origem >60d só reflete no full da madrugada
    //    (ghost de no máx. ~24h — aceitável).
    const skipModeProtheus = process.env.ETL_SKIP_NF_CARREGADAS === "true";
    // ETL_DIFF_DELETE (default OFF): troca o "apaga tudo >= corte + reinsere" por um
    // diff-delete (apaga só o que sumiu na origem). O delete-tudo descarta+reescreve
    // ~90k linhas/run e deixa a fato_pedidos com ~45MB de "ar"/bloat permanente
    // (heap ~2x o dado vivo) — a tabela (150MB) passa a não caber no shared_buffers
    // (224MB) e vira a raiz dos incidentes de saturação de I/O. Com o diff-delete as
    // linhas inalteradas nem são tocadas (o UPSERT abaixo, com guarda IS DISTINCT FROM,
    // já as ignora) → zero dead tuples → sem bloat. Ver docs/banco/otimizacao-db-performance.md §7.
    // Flag OFF por padrão: deploy é no-op até validar num run manual (comparar contagens)
    // e ligar no workflow. Mesma proteção de partial-load da guarda de sanidade acima.
    const diffDelete = process.env.ETL_DIFF_DELETE === "true";
    const delClient = await pool.connect();
    try {
      // Corte vem de janela-protheus.ts — o MESMO usado pelo extract pra filtrar a origem.
      // Antes era calculado aqui (60d hardcoded); com o filtro de data na extração as duas
      // contas TÊM que ser a mesma, senão o diff-delete apaga linha que existe na origem
      // mas ficou fora do lote buscado.
      const protheusCorteInt = janelaProtheus().corteInt;

      if (diffDelete) {
        // Chaves (identidade cross-ERP) que vieram neste run — o que NÃO estiver aqui
        // e estiver no escopo (>= corte, não-tiny) sumiu na origem → apagar.
        const keepKeys = unique
          .filter((i) => !["tiny_es", "tiny_rj", "tiny_es_fix", "tiny_rj_fix", "tiny_sp", "tiny_sp_fix", "denavita_fix"].includes(i.erp_origem))
          .map((i) => ({
            erp_origem: i.erp_origem,
            filial_id: i.filial_id,
            pedido_id: i.pedido_id,
            item_sequencia: i.item_sequencia,
          }));
        await delClient.query("BEGIN");
        try {
          await delClient.query(
            `CREATE TEMP TABLE _inc_keys (erp_origem text, filial_id uuid, pedido_id text, item_sequencia int) ON COMMIT DROP`
          );
          for (const kb of chunk(keepKeys, 5000)) {
            await delClient.query(
              `INSERT INTO _inc_keys
                 SELECT erp_origem, filial_id::uuid, pedido_id, item_sequencia
                 FROM json_populate_recordset(null::record, $1::json)
                   AS x(erp_origem text, filial_id text, pedido_id text, item_sequencia int)`,
              [JSON.stringify(kb)]
            );
          }
          await delClient.query(`CREATE INDEX ON _inc_keys (erp_origem, filial_id, pedido_id, item_sequencia)`);
          const delRes = await delClient.query(
            `DELETE FROM fato_pedidos f
               WHERE COALESCE(f.data_faturamento_id, f.data_pedido_id) >= $1
                 AND f.erp_origem NOT IN ('tiny_es','tiny_rj','tiny_es_fix','tiny_rj_fix', 'tiny_sp', 'tiny_sp_fix','denavita_fix')
                 AND NOT EXISTS (
                   SELECT 1 FROM _inc_keys k
                   WHERE k.erp_origem = f.erp_origem
                     AND k.filial_id = f.filial_id
                     AND k.pedido_id = f.pedido_id
                     AND k.item_sequencia = f.item_sequencia
                 )`,
            [protheusCorteInt]
          );
          await delClient.query("COMMIT");
          logger.info(
            `DIFF-DELETE fato_pedidos (Protheus/SP/BB) >= ${protheusCorteInt}: ${delRes.rowCount ?? 0} linhas removidas (ausentes na origem), ${keepKeys.length} chaves preservadas${skipModeProtheus ? ` (janela ${janelaProtheus().dias}d — intraday)` : " (full)"}`
          );
        } catch (e) {
          await delClient.query("ROLLBACK");
          throw e;
        }
      } else {
        // FIX 03/ago/2026: recorte por COALESCE(data_faturamento_id, data_pedido_id), não só
        // data_pedido_id. NF faturada do Protheus tem data_pedido_id NULL (o card faz
        // `NULL AS data_pedido`), então `data_pedido_id >= corte` a EXCLUÍA do delete → a linha
        // antiga nunca era apagada; como o item_sequencia oscila entre runs, cada carga inseria
        // uma cópia nova → faturado duplicava/acumulava (jul/2026: 39 NFs, ~R$1,38M fantasma,
        // ex. NF 4512 VEMAC 3×). A emissão (=data_faturamento) é a MESMA coluna que o card usa
        // pra filtrar (janela-protheus.ts), então a janela do delete casa com a do extract.
        await delClient.query(`
          DELETE FROM fato_pedidos
          WHERE COALESCE(data_faturamento_id, data_pedido_id) >= $1
            AND erp_origem NOT IN ('tiny_es', 'tiny_rj', 'tiny_es_fix', 'tiny_rj_fix', 'tiny_sp', 'tiny_sp_fix', 'denavita_fix')
        `, [protheusCorteInt]);
        logger.info(`DELETE fato_pedidos (Protheus/SP/BB) >= ${protheusCorteInt} concluído${skipModeProtheus ? ` (janela ${janelaProtheus().dias}d — intraday)` : " (full)"}`);
      }
    } finally {
      delClient.release();
    }

    // DELETE tiny_es/rj — substitui dados antigos pelos dados corretos da API Tiny.
    //  • Modo FULL (skip off): apaga TODA a janela (limpa órfãos) e reinsere tudo.
    //  • Modo SKIP (skip on): apaga SÓ as NFs que estão sendo reinseridas neste run
    //    (delete-by-nf_numero). NFs já carregadas que o skip NÃO re-extraiu nunca são
    //    tocadas → sem perda de dados. Cancelamento de NF antiga é tratado pelo
    //    UPDATE de canceladas (não pelo delete). Ver docs/etl-fato-pedidos.md §6.
    const skipMode = process.env.ETL_SKIP_NF_CARREGADAS === "true";
    const delTinyClient = await pool.connect();
    try {
      if (skipMode) {
        const nfDe = (erp: string) => Array.from(new Set(
          unique.filter(i => i.erp_origem === erp && i.nf_numero).map(i => String(i.nf_numero))
        ));
        const nfEs = nfDe("tiny_es");
        const nfRj = nfDe("tiny_rj");
        if (nfEs.length > 0) {
          await delTinyClient.query(
            `DELETE FROM fato_pedidos WHERE erp_origem='tiny_es' AND nf_numero = ANY($1)`, [nfEs]);
        }
        if (nfRj.length > 0) {
          await delTinyClient.query(
            `DELETE FROM fato_pedidos WHERE erp_origem='tiny_rj' AND nf_numero = ANY($1)`, [nfRj]);
        }
        logger.info(`DELETE tiny (SKIP) por nf_numero: ${nfEs.length} ES + ${nfRj.length} RJ NFs reinseridas`);
      } else {
        const tinyInicioEnv = process.env.TINY_API_INICIO ?? "";
        const tinyFim = new Date().toISOString().slice(0, 10).replace(/-/g, "");
        // ⚠️ A janela do DELETE tem que ser a MESMA que o extract usa para buscar NFs
        // (extract.ts::calcTinyInicio = 1º dia do MÊS ANTERIOR, ou TINY_API_INICIO).
        //
        // Até 01/ago/2026 aqui eram "últimos 15 dias" fixos, e o descasamento gerava
        // órfão: o extract re-detalhava NF de 32 dias atrás e reinseria, mas o DELETE só
        // limpava 15 — a linha antiga sobrevivia. Enquanto a identidade não mudava isso era
        // invisível; quando o c15cc3c trocou o pedido_id da linha faturada (nº da NF → nº
        // real do pedido), a linha velha virou uma SEGUNDA cópia da mesma NF.
        // Efeito medido: 190 NFs duplicadas, R$ 1.480.676,69 contados em dobro — e todas
        // com faturamento entre 01/07 e 16/07, exatamente o trecho que ficava fora dos 15
        // dias. De 17/07 em diante (dentro da janela) nenhuma duplicou.
        // Doc: docs/processos/correcao-duplicacao-apice-20260801.md
        const tinyInicioDate = tinyInicioEnv || (() => {
          const h = new Date();
          let ano = h.getUTCFullYear();
          let mes = h.getUTCMonth() - 1;            // mês anterior (0-11)
          if (mes < 0) { mes = 11; ano -= 1; }
          return `${ano}-${String(mes + 1).padStart(2, "0")}-01`;
        })();
        const tinyInicioInt = parseInt(tinyInicioDate.replace(/-/g, ""), 10);

        // Guarda de sanidade do Tiny — mesma ideia da guarda de Protheus/SP/BB acima.
        // Passou a ser necessária quando a janela do DELETE cresceu de 15d para o mês
        // anterior inteiro: apagar mais dias significa perder mais se a extração falhar
        // no meio (rate limit erro 6, timeout). A API do Tiny falha em silêncio — o
        // fetch de detalhe engole o erro e devolve null (extract-tiny-apice.ts), então
        // um lote truncado chega aqui parecendo legítimo.
        {
          const novosTinyJanela = unique.filter(
            (i) => ["tiny_es", "tiny_rj"].includes(i.erp_origem) &&
                   (i.status ?? "").toLowerCase() === "faturado"
          ).length;
          const gTiny = await delTinyClient.query(
            `SELECT COUNT(*) AS n FROM fato_pedidos
              WHERE erp_origem IN ('tiny_es','tiny_rj') AND lower(status)='faturado'
                AND COALESCE(data_faturamento_id, data_pedido_id) BETWEEN $1 AND $2`,
            [tinyInicioInt, parseInt(tinyFim, 10)]
          );
          const existentesTiny = parseInt(gTiny.rows[0]?.n ?? "0", 10);
          const MIN_BASELINE_TINY = 50;
          const THRESHOLD_TINY = 0.3;
          if (existentesTiny > MIN_BASELINE_TINY && novosTinyJanela < existentesTiny * THRESHOLD_TINY) {
            throw new Error(
              `Guarda de sanidade Tiny: novo lote tem ${novosTinyJanela} itens faturados, ` +
              `banco tem ${existentesTiny} na janela ${tinyInicioInt}→${tinyFim} — queda > 70%. ` +
              `Abortando ANTES do delete (extração provavelmente truncada por rate limit). ` +
              `Ver docs/processos/correcao-duplicacao-apice-20260801.md.`
            );
          }
        }

        await delTinyClient.query(`
          DELETE FROM fato_pedidos
          WHERE erp_origem IN ('tiny_es', 'tiny_rj')
            AND COALESCE(data_faturamento_id, data_pedido_id) >= $1
            AND COALESCE(data_faturamento_id, data_pedido_id) <= $2
        `, [tinyInicioInt, parseInt(tinyFim, 10)]);
        logger.info(`DELETE tiny_es/rj janela API ${tinyInicioInt}→${tinyFim} concluído`);
      }
    } finally {
      delTinyClient.release();
    }

    // DELETE pedidos em aberto ES/RJ — janela CASA com a do extract (ETL_ABERTOS_DIAS).
    //  • Full (sem env): 60 dias — DEVE casar com o default do extract (extract.ts também usa 60d).
    //    Usar 90d aqui causaria deleção de abertos que o extract não re-busca (gap 61-90d).
    //  • Intraday (ETL_ABERTOS_DIAS=7): só 7 dias — refresca os recentes (pega transições)
    //    SEM tocar nos abertos de 8-60d carregados pelo full da manhã (senão sumiriam).
    {
      const abertosDiasEnv = process.env.ETL_ABERTOS_DIAS;
      const delDias = abertosDiasEnv ? (parseInt(abertosDiasEnv, 10) || 60) : 60;
      const delAbertoClient = await pool.connect();
      try {
        const dataCorte = new Date();
        dataCorte.setDate(dataCorte.getDate() - delDias);
        const dataInt = parseInt(dataCorte.toISOString().slice(0, 10).replace(/-/g, ""), 10);
        await delAbertoClient.query(`
          DELETE FROM fato_pedidos
          WHERE erp_origem IN ('tiny_es', 'tiny_rj')
            AND lower(status) NOT IN ('faturado', 'cancelado')
            AND data_pedido_id >= $1
        `, [dataInt]);
        logger.info(`DELETE pedidos abertos tiny_es/rj >= ${dataInt} (janela ${delDias}d) concluído`);
      } finally {
        delAbertoClient.release();
      }
    }

    // ⚠️ PROGRESSO EM `info`, NAO EM `debug`. O laco levava 46 s no run de 02/set 14:48 e
    // ficou 8m51s sem UMA linha de log no run seguinte — com `logger.debug` suprimido, os 44
    // batches sao um buraco cego no meio da carga, e "lento" fica indistinguivel de "morto".
    // Loga a cada `LOG_CADA_N` batches (e sempre o ultimo) com o tempo acumulado, para que o
    // proximo caso destes seja localizavel no log em vez de exigir arqueologia em
    // pg_stat_activity — que, medido, NAO mostra a sessao do ETL neste cluster.
    const LOG_CADA_N = 10;
    const t0Batches = Date.now();
    let msNoBanco = 0;

    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      const batchLabel = `Batch ${i + 1}/${batches.length}`;

      try {
        const t0 = Date.now();
        await withRetry(
          async () => {
            const client = await pool.connect();
            try {
              await client.query(UPSERT_SQL, [JSON.stringify(batch)]);
            } finally {
              client.release();
            }
          },
          { maxAttempts: 2, baseDelayMs: 3000, label: batchLabel }
        );
        msNoBanco += Date.now() - t0;
      } catch (err) {
        batchesFailed++;
        const msg = err instanceof Error ? err.message : String(err);
        logger.error(`${batchLabel}: falhou definitivamente`, { error: msg });
      }

      const ultimo = i === batches.length - 1;
      if (ultimo || (i + 1) % LOG_CADA_N === 0) {
        const decorrido = ((Date.now() - t0Batches) / 1000).toFixed(1);
        const noBanco = (msNoBanco / 1000).toFixed(1);
        logger.info(
          `UPSERT ${i + 1}/${batches.length} batches — ${decorrido}s decorridos ` +
          `(${noBanco}s no banco, resto e o delay de ${DELAY_BETWEEN_BATCHES_MS}ms)` +
          (batchesFailed ? ` · ${batchesFailed} falharam` : "")
        );
      }

      // Delay entre batches (exceto o último)
      if (i < batches.length - 1) {
        await new Promise((r) => setTimeout(r, DELAY_BETWEEN_BATCHES_MS));
      }
    }


    // Marca como excluido pedidos tiny_es/rj "em separação/aberto" que ficaram fora da janela
    // de abertos do extract (stale: foram faturados em Tiny mas o registro não foi atualizado).
    // Condição: data_pedido_id < cutoff (fora do que o extract re-busca) + nf_numero IS NULL
    // (só pedidos, não NFs — NFs faturadas têm nf_numero preenchido e status=Faturado).
    // Safety: não toca em 'faturado' nem 'cancelado' — só em_sep/aberto sem NF.
    //
    // SÓ NO FULL (09/jul/2026): o full extrai abertos dos últimos 60d, então o cutoff 60d
    // casa com o que ele re-busca — só exclui o que está genuinamente fora da janela.
    // O intraday extrai abertos só de 7d (ETL_ABERTOS_DIAS=7); rodar o stale aqui excluía
    // TODO aberto de 8-60d (a faixa do carry-forward do funil, 60d) porque o intraday não
    // re-busca esses pedidos — premissa "não voltou = faturei" é falsa p/ aberto legítimo.
    // Efeito era flicker: full ressuscita → intraday nuka → full volta. As transições <7d
    // do intraday já são tratadas pelo DELETE #3 (abertos) + UPSERT. Ver docs §6.
    if (!skipMode) {
      const staleClient = await pool.connect();
      try {
        const abertosDiasEnv = process.env.ETL_ABERTOS_DIAS;
        const staleDias = abertosDiasEnv ? (parseInt(abertosDiasEnv, 10) || 60) : 60;
        const staleCorte = new Date();
        staleCorte.setDate(staleCorte.getDate() - staleDias);
        const staleInt = parseInt(staleCorte.toISOString().slice(0, 10).replace(/-/g, ""), 10);
        const staleRes = await staleClient.query(`
          UPDATE fato_pedidos
          SET excluido = true, updated_at = now()
          WHERE erp_origem IN ('tiny_es', 'tiny_rj')
            AND lower(status) NOT IN ('faturado', 'cancelado')
            AND excluido = false
            AND nf_numero IS NULL
            AND data_pedido_id < $1
        `, [staleInt]);
        if (staleRes.rowCount && staleRes.rowCount > 0) {
          logger.info(`Stale tiny pedidos marcados excluido: ${staleRes.rowCount} (data_pedido_id < ${staleInt})`);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("Limpeza stale tiny falhou (não crítico)", { error: msg });
      } finally {
        staleClient.release();
      }
    }

    // Marca NFs canceladas da API Tiny como status='Cancelado' no banco
    const canceladasES = opts.canceladasES ?? [];
    const canceladasRJ = opts.canceladasRJ ?? [];
    if (canceladasES.length > 0 || canceladasRJ.length > 0) {
      const cancelClient = await pool.connect();
      try {
        if (canceladasES.length > 0) {
          await cancelClient.query(
            `UPDATE fato_pedidos SET status='Cancelado'
             WHERE erp_origem='tiny_es' AND nf_numero = ANY($1) AND status != 'Cancelado'`,
            [canceladasES]
          );
          logger.info(`Canceladas ES marcadas: ${canceladasES.length} NFs`);
        }
        if (canceladasRJ.length > 0) {
          await cancelClient.query(
            `UPDATE fato_pedidos SET status='Cancelado'
             WHERE erp_origem='tiny_rj' AND nf_numero = ANY($1) AND status != 'Cancelado'`,
            [canceladasRJ]
          );
          logger.info(`Canceladas RJ marcadas: ${canceladasRJ.length} NFs`);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("Marcação de canceladas falhou (não crítico)", { error: msg });
      } finally {
        cancelClient.release();
      }
    }

    // Reaplica overrides de vendedor (fato_pedidos_override_vendedor) após o upsert
    const overrideClient = await pool.connect();
    try {
      await overrideClient.query("SELECT aplica_overrides_vendedor()");
      logger.info("aplica_overrides_vendedor() executado");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_overrides_vendedor() falhou (não crítico)", { error: msg });
    } finally {
      overrideClient.release();
    }

    // Reaplica overrides de cliente (fato_pedidos_override_cliente) após o upsert
    const overrideClienteClient = await pool.connect();
    try {
      const res = await overrideClienteClient.query("SELECT linhas_alteradas FROM aplica_overrides_cliente()");
      logger.info(`aplica_overrides_cliente() executado: ${res.rows[0]?.linhas_alteradas ?? 0} linhas`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_overrides_cliente() falhou (não crítico)", { error: msg });
    } finally {
      overrideClienteClient.release();
    }


    // Normaliza tipo_operacao (Bonificado→Bonificacao, Não Faturado→Venda) após o upsert
    const normClient = await pool.connect();
    try {
      const normRes = await normClient.query("SELECT nao_faturado_corrigidos, bonificado_corrigidos FROM aplica_normalizacoes()");
      const nr = normRes.rows[0];
      logger.info(`aplica_normalizacoes() executado: ${nr?.nao_faturado_corrigidos ?? 0} Não Faturado + ${nr?.bonificado_corrigidos ?? 0} Bonificado corrigidos`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_normalizacoes() falhou (não crítico)", { error: msg });
    } finally {
      normClient.release();
    }

    // Preenche marca_id a partir do cadastro (dim_produto.marca_id) onde ficou NULL.
    // Rede de segurança p/ marcas que a derivação do transform não resolveu (ex.: Yenzah/YE
    // via Protheus — skuToMarca não reconhecia o prefixo). Só preenche NULL (não sobrescreve
    // marcaOverride). Idempotente. Migration 20260721_marca_from_produto_yenzah.sql + cron 50 */2.
    const marcaClient = await pool.connect();
    try {
      const marcaRes = await marcaClient.query("SELECT fn_preencher_marca_produto() AS n");
      logger.info(`fn_preencher_marca_produto(): ${marcaRes.rows[0]?.n ?? 0} linhas com marca preenchida do cadastro`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("fn_preencher_marca_produto() falhou (não crítico)", { error: msg });
    } finally {
      marcaClient.release();
    }

    // Marca pedidos cancelados no Mercos como Cancelado no banco
    const cancelClient = await pool.connect();
    try {
      const cancelRes = await cancelClient.query("SELECT linhas_canceladas FROM aplica_cancelados_mercos()");
      logger.info(`aplica_cancelados_mercos() executado: ${cancelRes.rows[0]?.linhas_canceladas ?? 0} linhas`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_cancelados_mercos() falhou (não crítico)", { error: msg });
    } finally {
      cancelClient.release();
    }

    // Reaplica o ledger de pedido DUPLICADO sem NF (fato_pedidos_duplicado_sem_nf).
    // Obrigatório aqui, e não só no cron (56 */2): o DELETE de pedidos abertos acima
    // (janela ETL_ABERTOS_DIAS) é HARD e não filtra `excluido`, então a linha é
    // destruída e reinserida pelo UPSERT com o default `excluido=false`. Sem esta
    // chamada, todo run FULL (janela 60d) devolveria os duplicados ao pipeline e o
    // funil ficaria inflado até o cron seguinte (até 2h de janela, todo dia).
    // Com ela a janela é ZERO. Ver docs/etl/etl-fato-pedidos.md §3.2.1.
    const dupClient = await pool.connect();
    try {
      const dupRes = await dupClient.query("SELECT aplica_pedidos_duplicados_sem_nf() AS linhas");
      logger.info(`aplica_pedidos_duplicados_sem_nf() executado: ${dupRes.rows[0]?.linhas ?? 0} linhas`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_pedidos_duplicados_sem_nf() falhou (não crítico)", { error: msg });
    } finally {
      dupClient.release();
    }


    // Pedido com etiqueta "reprovado" no Tiny e ainda NÃO faturado sai da conta (excluido=true)
    // e volta sozinho se faturar ou se a etiqueta sair. Migration 20260929. Mesmo motivo do
    // duplicado acima: o DELETE de abertos + UPSERT recria a linha com excluido=false.
    const repClient = await pool.connect();
    try {
      const r = await repClient.query("SELECT marcadas, revertidas FROM fn_aplica_pedido_reprovado()");
      logger.info(
        `fn_aplica_pedido_reprovado(): ${r.rows[0]?.marcadas ?? 0} linhas fora da conta, ` +
        `${r.rows[0]?.revertidas ?? 0} devolvidas`
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("fn_aplica_pedido_reprovado() falhou (não crítico)", { error: msg });
    } finally {
      repClient.release();
    }

    // Reaplica devoluções permanentes (fato_pedidos_devolucoes) após o upsert
    const devolClient = await pool.connect();
    try {
      const devolRes = await devolClient.query("SELECT linhas_alteradas FROM aplica_devolucoes()");
      logger.info(`aplica_devolucoes() executado: ${devolRes.rows[0]?.linhas_alteradas ?? 0} linhas`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("aplica_devolucoes() falhou (não crítico)", { error: msg });
    } finally {
      devolClient.release();
    }

    // Cliente INTERNO (cliente_interno): CNPJ da própria operação, usado pela
    // logística. A devolução dele é movimentação interna, não devolução de venda,
    // então não pode abater faturamento -- marca excluido=true nessas linhas.
    // ⚠️ Roda DEPOIS de aplica_devolucoes(): é ela que define tipo_operacao='Devolucao',
    // e este passo filtra por esse tipo. Invertido, não marcaria nada na 1ª carga.
    // ⚠️ Tem de rodar A CADA carga: o UPSERT recria a linha como ativa (convenção #14).
    const internoClient = await pool.connect();
    try {
      const r = await internoClient.query(
        "SELECT marcadas, revertidas FROM fn_aplica_cliente_interno()"
      );
      logger.info(
        `fn_aplica_cliente_interno(): ${r.rows[0]?.marcadas ?? 0} linhas fora do faturamento, ` +
        `${r.rows[0]?.revertidas ?? 0} devolvidas`
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("fn_aplica_cliente_interno() falhou (não crítico)", { error: msg });
    } finally {
      internoClient.release();
    }

    // Cancelados no Mercos que o Protheus ainda mostra ativos (ex: pedido cancelado só
    // no Mercos). Marca excluido=true + status='Cancelado' no fato cruzando
    // mercos_vendas_detalhadas.status_personalizado='Cancelado' (número do pedido + CNPJ).
    // Roda toda carga porque o DELETE+INSERT do ETL reinsere as linhas como ativas.
    const cancClient = await pool.connect();
    try {
      const r = await cancClient.query("SELECT fn_marcar_cancelados_mercos() AS n");
      logger.info(`fn_marcar_cancelados_mercos(): ${r.rows[0]?.n ?? 0} linhas marcadas como canceladas`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("fn_marcar_cancelados_mercos() falhou (não crítico)", { error: msg });
    } finally {
      cancClient.release();
    }

    // Carimba em `fato_pedido_carga` a hora desta carga: quando cada pedido chegou na Torre
    // e quando entrou na etapa em que está. É o que permite ordenar "os últimos que entraram"
    // na /tv e no drill-down do funil — `fato_pedidos.created_at` NÃO serve, porque o
    // DELETE+INSERT da carga o reseta (o Tiny inteiro todo dia; medido 15/09/2026: 1 único
    // dia distinto em 7.250 linhas de setembro). Ver migration 20260915f.
    //
    // ⚠️ POSIÇÃO NÃO É COSMÉTICA: tem de vir DEPOIS de todos os passos que reescrevem `status`
    // (canceladas Tiny, aplica_cancelados_mercos, fn_marcar_cancelados_mercos, duplicados,
    // devoluções). Carimbar antes registraria uma etapa que o pedido já não tem quando a
    // tela abre, e a próxima carga gravaria a "transição" que na verdade foi erro de ordem.
    // Custo medido: 456 ms, tudo em cache. Não crítico — falhar aqui não pode derrubar carga.
    const cargaClient = await pool.connect();
    try {
      const cargaEm = opts.cargaEm ?? new Date();
      const cargaRun = opts.cargaRun ?? null;
      const cargaRes = await cargaClient.query(
        "SELECT novos, transicoes FROM fn_registra_carga_pedidos($1::timestamptz, $2)",
        [cargaEm.toISOString(), cargaRun]
      );
      const cr = cargaRes.rows[0];
      logger.info(
        `fn_registra_carga_pedidos(): ${cr?.novos ?? 0} pedidos novos na Torre, ` +
        `${cr?.transicoes ?? 0} mudaram de etapa (carimbo ${cargaEm.toISOString()}` +
        `${cargaRun ? `, run ${cargaRun}` : ""})`
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("fn_registra_carga_pedidos() falhou (não crítico)", { error: msg });
    } finally {
      cargaClient.release();
    }

    // ANALYZE pós-carga — DELETE+INSERT deixa stats do planner velhas; sem isso o
    // planner erra plano (seq scan na fato 70k) → spike de CPU/IO no nano. Barato.
    const analyzeClient = await pool.connect();
    try {
      await analyzeClient.query("ANALYZE fato_pedidos");
      logger.info("ANALYZE fato_pedidos concluído");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn("ANALYZE fato_pedidos falhou (não crítico)", { error: msg });
    } finally {
      analyzeClient.release();
    }

    // Refresh das MVs do dashboard — garante que KPIs e rankings refletem os dados do ETL.
    // Só no run FULL: nos intraday (skip) o pg_cron refresh-mv-gerencial (a cada 2h)
    // cobre a atualização — evita 2 REFRESH CONCURRENTLY pesados 8x/dia.
    if (!skipModeProtheus) {
      const mvClient = await pool.connect();
      try {
        await mvClient.query("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_gerencial_dia");
        logger.info("REFRESH mv_gerencial_dia concluído");
        await mvClient.query("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_gerencial_dia_pedidos");
        logger.info("REFRESH mv_gerencial_dia_pedidos concluído");
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        logger.warn("REFRESH MVs falhou (não crítico)", { error: msg });
      } finally {
        mvClient.release();
      }
    } else {
      logger.info("REFRESH MVs pulado (intraday — cron refresh-mv-gerencial a cada 2h cobre)");
    }
  } finally {
    await pool.end();
  }

  const result: LoadResult = {
    totalItems: unique.length,
    batches: batches.length,
    batchesFailed,
  };

  if (batchesFailed > 0) {
    logger.warn(`Carga: ${batchesFailed}/${batches.length} batches falharam`, { ...result });
  } else {
    logger.info(`Carga completa: ${unique.length} itens em ${batches.length} batches`, { ...result });
  }

  logger.groupEnd();
  return result;
}
