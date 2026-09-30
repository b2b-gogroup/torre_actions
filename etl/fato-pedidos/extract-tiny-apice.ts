/**
 * Extração de dados Apice via API do Tiny ERP — substitui os cards Metabase:
 * - 18490 (tinyAPES faturado)  → fetchNFsFaturadas(TOKEN_ES)
 * - 18519 (tinyAPRJ faturado)  → fetchNFsFaturadas(TOKEN_RJ)
 * - 18641 (tinyAPESPedidos)    → fetchPedidosAbertos(TOKEN_ES)
 * - 18660 (tinyAPRJPedidos)    → fetchPedidosAbertos(TOKEN_RJ)
 *
 * Retorna dados no mesmo formato de Record<string, unknown>[] dos cards Metabase,
 * compatível com normalizeTiny() em transform.ts.
 */
import { logger } from "../shared/logger.js";
import {
  tinyPesquisarTodas,
  tinyObterNF,
  tinyObterPedido,
  tinyProcessarConcorrente,
} from "../shared/tiny-api-client.js";
import { getSupabaseAdmin } from "../shared/supabase-admin.js";

const CONCURRENCY = 2; // calls paralelas de detalhe — 2x1s = ~2 calls/sec, seguro para Tiny

// ── Constantes ────────────────────────────────────────────────────────────────

/** Naturezas que correspondem a Venda (filtrar no detalhe da NF) */
const NAT_VENDA = [
  "venda de mercadoria",
  "venda de mercadoria adq",
];

/** Naturezas a excluir (transferência, brinde, exposição, remessa sem venda) */
const NAT_EXCLUIR = [
  "transfere", "brinde", "exposic",
  "remessa", "demonstra",
];

/**
 * Situações de pedido que devem ser ignoradas — o corte é ANTES de buscar o detalhe.
 *
 * ⚠️ `'Enviado'` e `'Pronto para envio'` entraram em 02/set/2026 e a razão é ECONOMIA DE
 * CHAMADA, não mudança de regra: a lista (`pedidos.pesquisa.php`) NÃO devolve
 * `id_nota_fiscal` (campos: codigo_rastreamento, data_pedido, data_prevista, id,
 * id_vendedor, nome, nome_vendedor, numero, numero_ecommerce, situacao, url_rastreamento,
 * valor), então o único jeito de saber se o pedido já foi faturado era buscar o detalhe —
 * e o `if (det.id_nota_fiscal) return null` logo abaixo o descartava em seguida.
 *
 * MEDIDO na API do Tiny, JANELA DE 60 DIAS (a mesma que `ETL_ABERTOS_DIAS` usa por default),
 * nos DOIS ERPs, conferindo `id_nota_fiscal` em cada pedido candidato:
 *
 *   faixa      situação             com NF   sem NF
 *   0-30d      Enviado                 712        0   ES 641 + RJ 71
 *   0-30d      Pronto para envio        31        0   ES 31
 *   30-60d     Enviado               1.000        0   ES 855 + RJ 145
 *   30-60d     Pronto para envio         5        0   ES 4 + RJ 1
 *   TOTAL                            1.748        0   <- zero contraexemplo
 *
 * Como 1.748 de 1.748 já tinham NF, o corte é **no-op na saída**: as linhas que ele evita
 * eram exatamente as que o `return null` jogava fora. Economiza ~72% das chamadas de
 * detalhe (194 de 270 na janela de 7d do intraday, ~145 s por run).
 *
 * ⛔ NÃO ACRESCENTAR `'Preparando envio'` NEM `'Em aberto'` — medido, os dois têm pedido
 * REAL sem NF, e o comportamento deles é instável:
 *   'Preparando envio'  0-30d: 58 com NF e 8 SEM (88%)  ·  30-60d: 0 com NF e 2 SEM (0%)
 *   'Em aberto'         ES: 0 com NF e 13 SEM (0%)      ·  RJ: 19 com NF e 0 sem (100%)
 * ⚠️ `'Em aberto'` se comporta ao CONTRÁRIO entre ES e RJ — cortar por ele perderia 13
 * pedidos reais no ES. Situação do Tiny não é sinal confiável por si; só vale cortar onde
 * a medição deu 100% com margem, e só nas duas acima ela deu.
 *
 * ⚠️ A medição cobre o que o ETL LÊ (janela de `ETL_ABERTOS_DIAS`, default 60). Pedido
 * mais antigo que isso não é buscado, então não há faixa não medida — mas se
 * `ETL_ABERTOS_DIAS` subir acima de 60, REMEDIR antes de confiar neste corte. O caso
 * `#27430` (CONECTA, 'Enviado' há 42 dias e SEM NF, R$ 67.821,74 — duplicata logística
 * neutralizada pelo ledger em 31/jul) é de pedido de ~19/jun, fora de qualquer janela de
 * 60d de hoje; é o contraexemplo que existe e o motivo de a guarda acima estar escrita.
 *
 * ⚠️ `SIT_PED_MAP` mantém as entradas de `'Enviado'` e `'Pronto para envio'` de propósito:
 * o `SIT_PED_IGNORAR` é testado ANTES no predicado de filtro, então elas ficam
 * inalcançáveis por este caminho — mas apagá-las tornaria o corte irreversível sem
 * arqueologia.
 */
const SIT_PED_IGNORAR = new Set([
  "Cancelado", "Faturado", "Entregue",
  "Enviado", "Pronto para envio",   // 1.748/1.748 já tinham NF — ver bloco acima
]);

/** Mapa de situação Tiny → status fato_pedidos */
const SIT_PED_MAP: Record<string, string> = {
  "Em aberto":         "Em aberto",
  "Aprovado":          "Em aberto",       // pedido aprovado, aguardando separação
  "Preparando envio":  "Em separação",
  "Enviado":           "Em separação",
  "Pronto para envio": "Aguardando Faturamento",
};

/**
 * CNPJs de empresas internas — excluir dos pedidos.
 *
 * ⚠️ MAGA e RITU PARTNERS entraram em 18/ago/2026. A MAGA aparecia no ranking de clientes do
 * Gerencial (R$ 94k, 48,5% de share no recorte que o usuário viu; R$ 357.398,58 em 3 NFs no ano)
 * como se fosse venda — é TRANSFERÊNCIA INTERNA. Confirmado por dois lados: o CNPJ
 * 38.246.589/0002-66 é a **unidade 40002** do próprio grupo no WebGex, e a pessoa atribuída como
 * "vendedora" é de OPERAÇÕES, cuidando do trâmite interno (informação do usuário).
 *
 * Os quatro CNPJs da MAGA e o da RITU entram juntos: hoje só o 0002-66 fatura, mas corrigir um e
 * deixar os irmãos de fora é garantir que o problema volte pela filial seguinte.
 *
 * ⚠️ Isto só vale para cargas FUTURAS — o histórico sai pelo ledger
 * `fato_pedidos_cancelamento_manual`, senão as linhas já gravadas continuam no faturamento.
 */
const CNPJS_INTERNOS = new Set([
  "48290289000157","48290289000319","48290289000238",
  "54137817000216","54137817000305","54137817000488",
  "57344563000114","57344563000203","26301600000183",
  "54190174000355","46803642000120","58181480000114",
  "60453002000249","60453002000168","60453002000320",
  "26301600000264","26301600000345",
  // MAGA COMERCIO (unidades 40001/40002/40005/40004 do grupo) + RITU PARTNERS (40003)
  "38246589000185","38246589000266","38246589000347","38246589000428",
  "58323315000150",
]);

// ── Helpers ───────────────────────────────────────────────────────────────────

function limparCNPJ(v: unknown): string {
  return String(v ?? "").replace(/\D/g, "").padStart(14, "0").slice(0, 14);
}

function dataBrParaIso(v: unknown): string {
  const s = String(v ?? "").trim();
  if (!s) return "";
  const [d, m, y] = s.split("/");
  return y && m && d ? `${y}-${m.padStart(2,"0")}-${d.padStart(2,"0")}` : s;
}

function isNaturezaVenda(nat: string): boolean {
  const lower = nat.toLowerCase();
  if (NAT_EXCLUIR.some(e => lower.includes(e))) return false;
  return NAT_VENDA.some(v => lower.startsWith(v));
}

function isNaturezaDevolucao(nat: string): boolean {
  return nat.toLowerCase().includes("devolu");
}

function isNaturezaBonificacao(nat: string): boolean {
  return nat.toLowerCase().includes("bonific");
}

/**
 * Pedidos AINDA NÃO faturados não têm NF (natureza_operacao), então a
 * bonificação é sinalizada no Tiny via marcador/tag do pedido (ex.: "Bonificação",
 * visível na lista de pedidos). Mesma detecção textual do isNaturezaBonificacao,
 * aplicada aos marcadores em vez da natureza fiscal.
 */
function temMarcadorBonificacao(marcadores: unknown): boolean {
  if (!Array.isArray(marcadores)) return false;
  return marcadores.some(m => {
    const wrap = (m as Record<string, unknown>)?.marcador ?? m;
    const descricao = String((wrap as Record<string, unknown>)?.descricao ?? "");
    return descricao.toLowerCase().includes("bonific");
  });
}

/**
 * Nº do pedido de origem, embutido em texto livre na `obs` da NF (ex.: "Nº Pedido:
 * 28870") — a API do Tiny não expõe esse vínculo em campo estruturado no retorno
 * de nota.fiscal.obter.php. Sem isso, a linha faturada usava o nº da própria NF
 * como pedido_id — identidade diferente da do pedido "aberto" original, então o
 * pedido ficava contado 2x no pipeline (aberto + faturado) até correção manual
 * via ledger. Caso 28870/NF 027413 (31/jul/2026) + mais 5 achados na mesma janela.
 */
function extrairNumeroPedidoDaObs(obs: unknown): string | null {
  const m = String(obs ?? "").match(/N[ºo°]\.?\s*Pedido:?\s*(\d+)/i);
  return m ? m[1] : null;
}

/**
 * Nº da NF-e que esta nota ESTORNA — só existe em nota de devolução, e vem no mesmo texto livre
 * da `obs` que já é lido acima:
 *
 *   "Número da NF-e referenciada: 23990
 *    Data de emissão da NF-e referenciada: 31/03/2026
 *    Chave de acesso da NF-e referenciada: 3226032630160000018355003000"
 *
 * ⚠️ É A ÚNICA CHAVE HONESTA para parear venda com devolução. Sem ela o pareamento vira
 * (cliente + valor + janela de data), e valor NÃO é chave: no mesmo dia em que esta função
 * nasceu, um casamento por valor devolveu 5.452 pares para 543 notas, com a mesma fatura
 * casando com 3 NFs da Amazon.
 *
 * Cobertura medida em 25/ago (20 NFDs do tiny_es): 17 trazem a referência. Onde não vier,
 * devolve null — ausência declarada, jamais preenchida por adivinhação.
 *
 * ⚠️ Não confundir com `extrairNumeroPedidoDaObs`: as duas leem a MESMA `obs` e as duas casam
 * um número, mas uma aponta para o PEDIDO que originou esta nota e a outra para a NOTA que esta
 * nota estorna. O regex exige a palavra "referenciada" justamente para não colidir.
 */
function extrairNfReferenciadaDaObs(obs: unknown): string | null {
  const m = String(obs ?? "").match(/NF-?e?\s*referenciada:?\s*(\d+)/i);
  return m ? m[1].replace(/^0+/, "") || m[1] : null;
}

/** Marcador do Tiny sem acento/caixa, p/ casar "não faturar" == "NAO FATURAR". */
function normalizarMarcador(m: unknown): string {
  const wrap = (m as Record<string, unknown>)?.marcador ?? m;
  return String((wrap as Record<string, unknown>)?.descricao ?? "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "");
}

/**
 * Pedido DUPLICADO que nunca vai gerar NF própria.
 *
 * Manobra real da operação (confirmada com a coordenadora do Tiny em 31/jul/2026):
 * quando um pedido já faturado apresenta problema, a vendedora NÃO cancela a NF —
 * ela DUPLICA o pedido. O pedido novo serve só pra o WMS separar/despachar, e a nota
 * que vale é a do pedido antigo. Esse pedido novo fica em "Em separação"/"Enviado"
 * pra sempre, indistinguível de um pedido normal — exceto pelo marcador.
 *
 * Sem esta detecção o funil conta a mesma mercadoria DUAS vezes: uma no Faturado
 * (NF do pedido antigo) e outra no pipeline (pedido duplicado). Medido em jul/2026
 * na marca Apice: 11 pedidos, R$ 394.076,16 = 13% do pipeline do mês.
 *
 * Dois sinais, ambos observados em produção:
 *   (a) intenção explícita — "não faturar", "não fazer nota fiscal", "nao fatura"
 *   (b) referência à NF de OUTRO pedido — "enviar pela nota 26505", "NF 26488"
 *
 * Validado contra os 161 pedidos do pipeline Apice de julho: pega 10 dos 11 casos
 * reais + 1 pedido já cancelado no Tiny ("não faturar / pedido errado"), ZERO
 * falso-positivo. O 11º (#27430) não tem marcador algum que o distinga e por isso
 * mora no ledger `fato_pedidos_duplicado_sem_nf` (migration 20260731c) — o ledger
 * é a rede pros casos que nenhum marcador alcança.
 */
/**
 * Etiqueta "reprovado" (29/set/2026, pedido da coordenadora de vendas). Pedido reprovado e
 * AINDA NÃO faturado sai da conta (funil/meta), mas continua na Torre com alerta no Gerencial;
 * quem aplica é `fn_aplica_pedido_reprovado()` (load.ts), a partir de `tiny_pedido_reprovado`.
 * ⚠️ "análise de crédito" NÃO é reprovado — segue contando (decisão do usuário). O casamento é
 * por PALAVRA inteira ("reprovado"/"reprovada"), para não pegar marcador que só a contenha.
 */
function temMarcadorReprovado(marcadores: unknown): boolean {
  if (!Array.isArray(marcadores)) return false;
  return marcadores.some(m => /(^|[^a-z])reprovad[oa]s?([^a-z]|$)/.test(normalizarMarcador(m)));
}

/** Estado da etiqueta por pedido ABERTO visto neste run (com ou sem ela). */
const reprovadoPorPedido = new Map<string, {
  erp_origem: string; pedido_id: string; reprovado: boolean; marcadores: string[];
  cnpj: string | null; cliente: string | null; valor_pedido: number | null; data_pedido: string | null;
}>();

function coletarReprovado(erp: string, det: Record<string, unknown>, cnpj: string, dataPed: string | null): void {
  const pedidoId = String(det.numero ?? "").trim();
  if (!pedidoId) return;
  const marc = Array.isArray(det.marcadores)
    ? (det.marcadores as unknown[])
        .map(m => String(((m as Record<string, unknown>)?.marcador as Record<string, unknown> ?? m as Record<string, unknown>)?.descricao ?? "").trim())
        .filter(Boolean)
    : [];
  reprovadoPorPedido.set(`${erp}|${pedidoId}`, {
    erp_origem: erp,
    pedido_id: pedidoId,
    reprovado: temMarcadorReprovado(det.marcadores),
    marcadores: marc,
    cnpj: cnpj || null,
    cliente: String((det.cliente as Record<string, unknown> | undefined)?.nome ?? "") || null,
    valor_pedido: parseFloat(String(det.total_pedido ?? "")) || null,
    data_pedido: dataPed || null,
  });
}

/**
 * Grava em `tiny_pedido_reprovado` o estado da etiqueta dos pedidos abertos vistos neste run.
 * Pedido visto SEM a etiqueta também é gravado (reprovado=false): é assim que a retirada da
 * etiqueta no Tiny devolve o pedido à conta. Chamada 1x em index.ts, ANTES do load (que roda
 * `fn_aplica_pedido_reprovado`). Não-crítica: falhar aqui só atrasa a regra, não a carga.
 */
export async function flushReprovadosTiny(): Promise<void> {
  if (reprovadoPorPedido.size === 0) return;
  const sb = getSupabaseAdmin();
  const linhas = Array.from(reprovadoPorPedido.values());
  const LOTE = 300;
  for (let i = 0; i < linhas.length; i += LOTE) {
    const chunk = linhas.slice(i, i + LOTE).map(l => ({ ...l, visto_em: new Date().toISOString() }));
    const { error } = await sb.from("tiny_pedido_reprovado").upsert(chunk, { onConflict: "erp_origem,pedido_id" });
    if (error) { logger.warn(`tiny_pedido_reprovado: gravação falhou (${error.message})`); return; }
  }
  const rep = linhas.filter(l => l.reprovado);
  logger.info(
    `Tiny API: etiqueta "reprovado" — ${rep.length} de ${linhas.length} pedidos abertos` +
      (rep.length ? ` (${rep.map(r => `${r.erp_origem}:${r.pedido_id}`).join(", ")})` : "")
  );
  reprovadoPorPedido.clear();
}

function temMarcadorNaoFaturar(marcadores: unknown): boolean {
  if (!Array.isArray(marcadores)) return false;
  const INTENCAO = /nao\s*fatur|nao\s*fazer\s*nota|sem\s*nota\s*fiscal/;
  const NF_ALHEIA = /enviar\s*(pela\s*)?(nota|nf)\s*\d|^nf\s*\d|\bnf\s*\d{4,}/;
  return marcadores.some(m => {
    const d = normalizarMarcador(m);
    return INTENCAO.test(d) || NF_ALHEIA.test(d);
  });
}

/**
 * Consulta quais números de NF já foram PROCESSADOS para este erp_origem
 * (tabela tiny_nf_processadas — superset que inclui NFs carregadas E NFs excluídas
 * por natureza, ex: transferência/brinde/interno). Permite pular o detalhe (caro)
 * tanto das já carregadas quanto das que sempre seriam descartadas. Itens/desconto
 * de NF faturada não mudam após emissão; NF excluída idem. Consulta em lotes pelos
 * números do período. Em erro, retorna conjunto vazio → fallback seguro: detalha todas.
 */
async function consultarNFsProcessadas(erp: string, numeros: string[]): Promise<Set<string>> {
  const set = new Set<string>();
  const unicos = Array.from(new Set(numeros.map(n => String(n ?? "").trim()).filter(Boolean)));
  if (unicos.length === 0) return set;
  const sb = getSupabaseAdmin();
  const LOTE = 300;
  for (let i = 0; i < unicos.length; i += LOTE) {
    const chunk = unicos.slice(i, i + LOTE);
    const { data, error } = await sb
      .from("tiny_nf_processadas").select("nf_numero")
      .eq("erp_origem", erp).in("nf_numero", chunk);
    if (error) {
      logger.warn(`${erp}: consulta de NFs processadas falhou (${error.message}) — vai detalhar todas`);
      return new Set();
    }
    for (const r of data ?? []) {
      const n = String((r as Record<string, unknown>).nf_numero ?? "").trim();
      if (n) set.add(n);
    }
  }
  return set;
}

// ── Data REAL do pedido por NF ────────────────────────────────────────────────
//
// ⚠️ O detalhe da NF (`nota.fiscal.obter.php`) NÃO tem `data_pedido` — conferido no payload ao
// vivo em 15/09/2026, que traz 48 chaves e nenhuma delas é essa. Até aqui o código fazia
// `String(det.data_pedido ?? dataEmis)`, então caía SEMPRE no fallback e gravava a data da
// EMISSÃO no lugar da data do pedido.
//
// Não era detalhe: medido na API numa amostra de 21 NFs de setembro, **18 tinham data de pedido
// diferente da emissão** — a NF 028339 foi pedida em 07/08 e emitida em 01/09 (25 dias), a
// 028340 em 05/08 contra 01/09 (27 dias). No banco isso dava 99,8% dos faturados tiny_es com
// `data_pedido_id = data_faturamento_id`, e o "Xd após o pedido" da tela sempre zerado.
//
// O vínculo existe: a NF traz **`id_venda`**, que é o id do pedido em `pedido.obter.php`.
//
// ⚠️ CACHE PERMANENTE, e a razão é custo: o run full re-detalha toda a janela (mês anterior +
// atual, ~990 NFs desde agosto), e sem cache seriam ~990 chamadas EXTRA por run. Com ele, cada
// NF é buscada uma vez na vida — a data do pedido não muda depois que a nota saiu. O volume
// novo é ~12 NFs/dia.
//
// ⚠️ Linha com `data_pedido` NULL significa "consultado, a origem não tem vínculo"; linha
// AUSENTE significa "ainda não consultado". Sem essa distinção, NF sem `id_venda` seria
// reconsultada em todo run, para sempre.

/** Cache em memória desta execução: `erp|nf` → data ISO, ou "" quando a origem não tem vínculo. */
const dataPedidoPorNf = new Map<string, string>();

/** Lê o cache persistido para as NFs deste lote. Falha aqui nunca aborta: só faz reconsultar. */
async function carregarCacheDataPedido(erp: string, numeros: string[]): Promise<void> {
  const unicos = Array.from(new Set(numeros.map(n => String(n ?? "").trim()).filter(Boolean)));
  if (unicos.length === 0) return;
  const sb = getSupabaseAdmin();
  const LOTE = 300;
  for (let i = 0; i < unicos.length; i += LOTE) {
    const chunk = unicos.slice(i, i + LOTE);
    const { data, error } = await sb
      .from("tiny_nf_data_pedido").select("nf_numero, data_pedido")
      .eq("erp_origem", erp).in("nf_numero", chunk);
    if (error) {
      logger.warn(`${erp}: cache de data do pedido indisponível (${error.message}) — vai reconsultar`);
      return;
    }
    for (const r of data ?? []) {
      const rec = r as Record<string, unknown>;
      const n = String(rec.nf_numero ?? "").trim();
      if (n) dataPedidoPorNf.set(`${erp}|${n}`, String(rec.data_pedido ?? ""));
    }
  }
}

/** Grava no cache o que foi resolvido nesta execução. */
async function gravarCacheDataPedido(
  erp: string,
  linhas: { nf_numero: string; id_venda: string | null; data_pedido: string | null }[]
): Promise<void> {
  if (linhas.length === 0) return;
  const sb = getSupabaseAdmin();
  const LOTE = 300;
  for (let i = 0; i < linhas.length; i += LOTE) {
    const { error } = await sb.from("tiny_nf_data_pedido").upsert(
      linhas.slice(i, i + LOTE).map(l => ({ erp_origem: erp, ...l })),
      { onConflict: "erp_origem,nf_numero" }
    );
    if (error) logger.warn(`${erp}: gravação do cache de data do pedido falhou (${error.message})`);
  }
}

/**
 * Data real do pedido desta NF, em ISO. Devolve `""` quando a origem não sabe — e aí quem chama
 * decide o fallback, em vez de esta função inventar a data da emissão como se fosse do pedido.
 */
async function resolverDataPedidoNf(
  cfg: TinyApiceConfig,
  nfNum: string,
  det: Record<string, unknown>,
  pendentes: { nf_numero: string; id_venda: string | null; data_pedido: string | null }[]
): Promise<string> {
  const chave = `${cfg.erp}|${nfNum}`;
  const cacheado = dataPedidoPorNf.get(chave);
  if (cacheado !== undefined) return cacheado;   // "" = consultado e sem vínculo

  const idVenda = String(det.id_venda ?? "").trim();
  if (!idVenda || idVenda === "0") {
    dataPedidoPorNf.set(chave, "");
    pendentes.push({ nf_numero: nfNum, id_venda: null, data_pedido: null });
    return "";
  }
  try {
    const ped = await tinyObterPedido(cfg.token, idVenda);
    // Mesmo payload, mais dois campos: o número do Uplaces. Não custa chamada nenhuma.
    coletarUplaces(cfg.erp, ped, nfNum);
    const iso = dataBrParaIso(ped.data_pedido);   // a API devolve DD/MM/AAAA
    dataPedidoPorNf.set(chave, iso);
    pendentes.push({ nf_numero: nfNum, id_venda: idVenda, data_pedido: iso || null });
    return iso;
  } catch (e) {
    // ⚠️ NÃO cacheia o erro: falha de rede é transitória, e gravar "" aqui congelaria a NF
    // como "sem vínculo" para sempre. Só volta vazio nesta execução.
    logger.warn(`${cfg.erp}: não consegui a data do pedido da NF ${nfNum} (id_venda ${idVenda}): ${e}`);
    return "";
  }
}

// ── Número do pedido no UPLACES ───────────────────────────────────────────────
//
// O representante não conhece o número do pedido no Tiny — só o do Uplaces (pedido do usuário,
// 21/set/2026). Na tela do Tiny ele aparece DUAS vezes, como "Nº pedido" e "Identificador do
// pedido e-commerce"; na API são `numero_ordem_compra` e `numero_ecommerce`.
//
// ⚠️ OS DOIS CAMPOS, NUNCA UM SÓ. Medido ao vivo em 21/09/2026, 25 pedidos de setembro por conta:
//
//     ES ... numero_ecommerce == numero_ordem_compra em 17/17 (8 pedidos sem nenhum dos dois)
//     RJ ... numero_ecommerce VAZIO em 25/25 · numero_ordem_compra preenchido em 25/25
//     divergência entre os dois campos: 0 de 42
//
// Ler só `numero_ecommerce` é o que fez a investigação de 02/set (`backfill-numero-ecommerce-
// tiny.py`) concluir que "o dado não existe na origem para o RJ". Existe, no campo ao lado.
//
// ⚠️ CUSTO ZERO DE CHAMADA nos dois caminhos, e é por isso que a captura mora aqui:
//   • faturadas — `resolverDataPedidoNf` JÁ busca `pedido.obter.php` por causa da data real
//     do pedido; só passamos a ler mais dois campos do payload que já chegou.
//   • abertos   — `fetchPedidosAbertos` JÁ busca o detalhe de cada pedido.
// Nenhuma call nova entra no ETL.
//
// ⚠️ CONSEQUÊNCIA DO CACHE: NF já registrada em `tiny_nf_data_pedido` não volta à API, então o
// ETL só captura o que é NOVO. O histórico entra uma vez por `etl/dev/backfill-uplaces-tiny.cjs`.
//
// Gravado em `tiny_pedido_uplaces` (migration 20260921e), fora do fluxo de `fato_pedidos` — o
// número é atributo do PEDIDO e a tabela fato é grão de ITEM; ver o porquê na migration.

type UplacesColetado = {
  pedido_numero: string;
  numero_uplaces: string;
  nf_numero: string | null;
  id_pedido_tiny: string | null;
  fonte: "ecommerce" | "ordem_compra";
};

/** `erp|pedido_numero` → linha a gravar. */
const uplacesPorPedido = new Map<string, UplacesColetado>();

/** Só dígitos, sem zeros à esquerda — a forma canônica que `lib/pedido-identificador.ts` compara. */
function numCanonicoEtl(v: unknown): string {
  return String(v ?? "").replace(/\D/g, "").replace(/^0+/, "");
}

/**
 * Lê o número do Uplaces do payload de um PEDIDO do Tiny (`pedido.obter.php` ou a linha da
 * lista de pedidos, que também traz `numero_ecommerce`). Silenciosa quando o pedido não tem —
 * venda direta do representante não passa pelo portal, e ausência aqui é o estado normal.
 */
function coletarUplaces(
  erp: string,
  ped: Record<string, unknown>,
  nfNum: string | null,
): void {
  const pedidoNumero = numCanonicoEtl(ped.numero);
  if (!pedidoNumero) return;

  const ecom  = numCanonicoEtl(ped.numero_ecommerce);
  const ordem = numCanonicoEtl(ped.numero_ordem_compra);
  const numero_uplaces = ecom || ordem;
  if (!numero_uplaces) return;

  const chave = `${erp}|${pedidoNumero}`;
  const anterior = uplacesPorPedido.get(chave);
  uplacesPorPedido.set(chave, {
    pedido_numero: pedidoNumero,
    numero_uplaces,
    // A NF só aparece no caminho das faturadas. Nunca apaga a que já foi coletada: o mesmo
    // pedido pode ser visto como aberto (sem NF) depois de já ter sido visto faturado no
    // mesmo run, e gravar NULL por cima perderia o vínculo.
    nf_numero: numCanonicoEtl(nfNum) || anterior?.nf_numero || null,
    id_pedido_tiny: String(ped.id ?? "").trim() || anterior?.id_pedido_tiny || null,
    fonte: ecom ? "ecommerce" : "ordem_compra",
  });
}

/**
 * Grava em `tiny_pedido_uplaces` o que foi coletado nesta execução. Chamada 1x ao final do ETL
 * (index.ts), DEPOIS de extractAll() e fora do load.ts — falha aqui é não-crítica e nunca
 * aborta a carga de pedidos (mesmo desenho de `flushEmailsClienteTiny`).
 */
export async function flushUplacesTiny(): Promise<void> {
  if (uplacesPorPedido.size === 0) return;
  const sb = getSupabaseAdmin();
  const linhas = Array.from(uplacesPorPedido.entries()).map(([chave, v]) => ({
    erp_origem: chave.split("|")[0],
    ...v,
  }));
  const LOTE = 300;
  let gravados = 0;
  for (let i = 0; i < linhas.length; i += LOTE) {
    const chunk = linhas.slice(i, i + LOTE);
    const { error } = await sb.from("tiny_pedido_uplaces").upsert(
      chunk.map(l => ({ ...l, atualizado_em: new Date().toISOString() })),
      { onConflict: "erp_origem,pedido_numero" }
    );
    if (error) { logger.warn(`tiny_pedido_uplaces: gravação falhou (${error.message})`); return; }
    gravados += chunk.length;
  }
  logger.info(`Tiny API: ${gravados} pedidos com número do Uplaces gravados em tiny_pedido_uplaces`);
  uplacesPorPedido.clear();
}

/**
 * CNPJ → e-mail coletado do payload de pedido/NF do Tiny nesta execução (ES+RJ juntos, faturados
 * e abertos) — a API já devolve `cliente.email` no MESMO detalhe que fetchNFsFaturadas/
 * fetchPedidosAbertos buscam pra montar fato_pedidos, então capturar isso não custa chamada extra.
 * Gravado em dim_cliente.email_tiny ao final da extração (flushEmailsClienteTiny), fora do fluxo de
 * fato_pedidos: nunca decide sucesso/falha do ETL principal (23/ago/2026, ver CLAUDE.md).
 */
const emailsClienteTiny = new Map<string, string>();

function coletarEmailCliente(cnpj: string, cliente: Record<string, unknown>): void {
  const email = String(cliente.email ?? "").trim();
  if (cnpj.length === 14 && email.includes("@")) emailsClienteTiny.set(cnpj, email);
}

/**
 * Grava em dim_cliente.email_tiny os e-mails coletados nesta execução — só onde a coluna está
 * NULL hoje (nunca sobrescreve; não é dono de email_manual/email_mercos, que vêm de outras
 * fontes). Chamada 1x ao final do ETL (index.ts), DEPOIS de extractAll() e fora do load.ts de
 * fato_pedidos — falha aqui é não-crítica e nunca aborta a carga de pedidos.
 */
export async function flushEmailsClienteTiny(): Promise<void> {
  if (emailsClienteTiny.size === 0) return;
  const sb = getSupabaseAdmin();
  const pares = Array.from(emailsClienteTiny.entries());
  const LOTE = 300;
  let gravados = 0;
  for (let i = 0; i < pares.length; i += LOTE) {
    const chunk = pares.slice(i, i + LOTE);
    const cnpjs = chunk.map(([cnpj]) => cnpj);
    const { data: existentes, error: errSel } = await sb
      .from("dim_cliente").select("cnpj").in("cnpj", cnpjs).is("email_tiny", null);
    if (errSel) { logger.warn(`email_tiny: consulta falhou (${errSel.message})`); continue; }
    const alvo = new Set((existentes ?? []).map((r) => (r as { cnpj: string }).cnpj));
    for (const [cnpj, email] of chunk) {
      if (!alvo.has(cnpj)) continue;
      const { error } = await sb.from("dim_cliente").update({ email_tiny: email }).eq("cnpj", cnpj);
      if (error) { logger.warn(`email_tiny: falha ao gravar ${cnpj} (${error.message})`); continue; }
      gravados++;
    }
  }
  logger.info(`email_tiny: ${gravados}/${emailsClienteTiny.size} clientes novos gravados`);
}

/**
 * Registra no cache (tiny_nf_processadas) os números de NF que ACABARAM de ser
 * detalhados com sucesso (inclui as excluídas por natureza) — assim não são
 * re-detalhados nos próximos runs. NFs com erro de fetch NÃO entram (re-tentam depois).
 * Upsert idempotente; falha aqui é não-crítica (só perde a otimização do próximo run).
 */
async function registrarNFsProcessadas(erp: string, numeros: string[]): Promise<void> {
  // ⚠️ Teste/dev NUNCA pode marcar nota como processada: o cache diz "esta nota já está no banco", e
  // um teste que só LÊ a API mas registra a nota faz o ETL real pulá-la para sempre (30/09/2026: 18
  // notas, ~R$ 119 mil, sumiram do faturado por causa de um teste local). Com
  // `ETL_NF_CACHE_SOMENTE_LEITURA=true` a leitura continua e a escrita é ignorada.
  if (process.env.ETL_NF_CACHE_SOMENTE_LEITURA === "true") return;
  const unicos = Array.from(new Set(numeros.map(n => String(n ?? "").trim()).filter(Boolean)));
  if (unicos.length === 0) return;
  const sb = getSupabaseAdmin();
  const LOTE = 500;
  for (let i = 0; i < unicos.length; i += LOTE) {
    const chunk = unicos.slice(i, i + LOTE).map(nf_numero => ({ erp_origem: erp, nf_numero }));
    const { error } = await sb
      .from("tiny_nf_processadas")
      .upsert(chunk, { onConflict: "erp_origem,nf_numero", ignoreDuplicates: true });
    if (error) { logger.warn(`${erp}: registro de NFs processadas falhou (${error.message})`); return; }
  }
}

function proporcionalItem(
  valorItem: number, somaItens: number, valorNota: number
): number {
  if (somaItens <= 0) return valorItem;
  return valorItem * (valorNota / somaItens);
}

/**
 * Rateia `total` (R$) entre os itens na proporção de `pesos`, em CENTAVOS, e garante que a
 * soma das partes seja exatamente `total` — o resto do arredondamento vai para os itens de
 * maior fração (maior resto). Sem isso cada item arredondava sozinho e o pedido ficava a
 * ±R$ 0,03 do total da NF no Tiny (medido em 30/set/2026: 30899 = 5.806,63 no Tiny contra
 * 5.806,63 somando itens+frete, mas com 5.655,67/150,96 em vez de 5.655,63/151,00).
 */
export function ratearCentavos(pesos: number[], total: number): number[] {
  const sinal  = total < 0 ? -1 : 1;
  const totalC = Math.round(Math.abs(total) * 100);
  const somaP  = pesos.reduce((a, p) => a + p, 0);
  if (pesos.length === 0) return [];
  if (somaP <= 0 || totalC === 0) return pesos.map(() => 0);
  const brutos = pesos.map((p) => (p / somaP) * totalC);
  const partes = brutos.map((b) => Math.floor(b));
  let resto = totalC - partes.reduce((a, p) => a + p, 0);
  const ordem = brutos
    .map((b, i) => ({ i, frac: b - Math.floor(b), peso: pesos[i] }))
    .sort((a, b) => b.frac - a.frac || b.peso - a.peso || a.i - b.i);
  for (let k = 0; resto > 0; k = (k + 1) % ordem.length) { partes[ordem[k].i] += 1; resto--; }
  return partes.map((c) => (sinal * c) / 100);
}

/**
 * Valores por item de um pedido/NF do Tiny, já rateados e fechando ao centavo com o Tiny:
 *   Σ bruto     = "Total produtos"
 *   Σ desconto  = "Desconto" do cabeçalho
 *   Σ líquido   = Total produtos − Desconto
 *   Σ frete     = "Frete pago pelo cliente"
 *   Σ líquido + Σ frete = "Total da venda"
 * O `transform` NÃO rateia de novo quando a linha vem com `desconto_rateado: true`.
 */
function ratearItensTiny(itens: unknown[], valorDesc: number, valorFrete: number) {
  const brutos = itens.map((i) => {
    const item = ((i as Record<string, unknown>).item ?? i) as Record<string, unknown>;
    const qtd  = parseFloat(String(item.quantidade ?? 0)) || 0;
    const unit = parseFloat(String(item.valor_unitario ?? 0)) || 0;
    return Math.round(qtd * unit * 100) / 100;
  });
  const descontos = ratearCentavos(brutos, valorDesc);
  const fretes    = ratearCentavos(brutos, valorFrete);
  return brutos.map((b, i) => ({
    bruto:    b,
    desconto: descontos[i],
    liquido:  Math.round((b - descontos[i]) * 100) / 100,
    frete:    fretes[i],
  }));
}

// ── Extração de NFs faturadas ─────────────────────────────────────────────────

export interface TinyApiceConfig {
  token:   string;
  filial:  string;    // ex: "CD ES"
  marca:   string;    // ex: "Apice"
  erp:     string;    // ex: "tiny_es"
}

/**
 * FOTOGRAFIA DAS NOTAS DESTE RUN, usada por `fetchPedidosAbertos` (30/09/2026).
 *
 * Por que existe: o pedido aberto que aponta para uma nota (`id_nota_fiscal` ≠ 0) era tratado como
 * "já faturado" e SAÍA do funil — mas o faturado só aceita nota AUTORIZADA (com chave de acesso)
 * que estava na lista de notas lida minutos antes. Quando a nota não cumpre isso, o pedido some
 * dos DOIS lados e o valor desaparece do total até alguém reemitir/autorizar (caso 30924, R$ 270 mil,
 * dois dias fora) ou até a próxima carga (caso 1900, R$ 24 mil: nota emitida entre a leitura das
 * notas e a dos pedidos). Regra nova: o pedido só sai do funil se a nota dele, NESTA leitura, é
 * autorizada (vai entrar como faturado) ou cancelada (comportamento de sempre). Senão continua contado.
 * `ETL_PEDIDO_COM_NOTA_SEM_FATURADO=false` volta ao comportamento anterior.
 */
const notasDoRun = new Map<string, { inicio: string; autorizadas: Set<string>; canceladas: Set<string> }>();

/** Só para teste (etl/dev/teste-regra-nota.ts): injeta a fotografia de notas. */
export function __definirNotasDoRunParaTeste(erp: string, inicio: string, autorizadas: string[], canceladas: string[]): void {
  notasDoRun.set(erp, { inicio, autorizadas: new Set(autorizadas), canceladas: new Set(canceladas) });
}

export function notaDoPedidoJaContaComoFaturado(erp: string, idNf: string, dataPedidoIso: string | null): boolean {
  if (process.env.ETL_PEDIDO_COM_NOTA_SEM_FATURADO === "false") return true;
  const snap = notasDoRun.get(erp);
  if (!snap) return true;                                        // sem leitura de notas neste run: comportamento antigo
  if (!dataPedidoIso || dataPedidoIso < snap.inicio) return true; // nota pode estar fora da janela lida: não arrisca dupla contagem
  return snap.autorizadas.has(idNf) || snap.canceladas.has(idNf);
}

/**
 * Busca NFs emitidas no período e retorna itens no formato dos cards Metabase.
 * Inclui: Vendas (tipo_operacao=Venda) e Devoluções (tipo_operacao=Devolucao).
 * Exclui: Canceladas (sit=3), Bonificações, Transferências.
 */
export async function fetchNFsFaturadas(
  cfg: TinyApiceConfig,
  dataInicio: string,  // YYYY-MM-DD
  dataFim: string,
): Promise<Record<string, unknown>[]> {
  logger.info(`Tiny API ${cfg.erp}: buscando NFs faturadas ${dataInicio} → ${dataFim}`);

  // Converte para DD/MM/YYYY (formato Tiny)
  const toTiny = (iso: string) => iso.split("-").reverse().join("/");

  // 1. Lista todas as NFs do período (qualquer situação)
  const nfsLista = await tinyPesquisarTodas(
    "notas.fiscais.pesquisa.php", cfg.token,
    { dataInicial: toTiny(dataInicio), dataFinal: toTiny(dataFim) },
    "notas_fiscais"
  );

  logger.info(`Tiny API ${cfg.erp}: ${nfsLista.length} NFs na lista — buscando detalhes...`);

  // Fotografia para `fetchPedidosAbertos` (ver `notasDoRun`): ids das notas de SAÍDA autorizadas
  // (com chave de acesso) e das canceladas, tirada aqui, ANTES de qualquer descarte.
  {
    const autorizadas = new Set<string>();
    const canceladas = new Set<string>();
    for (const n of nfsLista) {
      const h = ((n as Record<string, unknown>).nota_fiscal as Record<string, unknown>) ?? (n as Record<string, unknown>);
      if (String(h.tipo ?? "").toUpperCase() !== "S" || !h.id) continue;
      if (String(h.situacao ?? "") === "3") canceladas.add(String(h.id));
      else if (String(h.chave_acesso ?? "").trim()) autorizadas.add(String(h.id));
    }
    notasDoRun.set(cfg.erp, { inicio: dataInicio, autorizadas, canceladas });
  }

  const rows: Record<string, unknown>[] = [];
  let skipped = 0;

  // ─── Filtra da lista o que NÃO é nota fiscal válida, antes de buscar detalhes ───────────
  //
  // ⚠️ ATÉ 25/ago/2026 ESTE FILTRO SÓ PULAVA A CANCELADA (`sit === "3"`), e tudo o mais entrava
  // como `Faturado`. Nota em digitação e nota REJEITADA PELA SEFAZ contavam como receita e
  // apareciam na fila de cobrança do financeiro. Medido no tiny_es de 2026: 6 notas /
  // R$ 243.032,39 — a maior, R$ 89.472,00, ainda "Pendente" desde 31/jul. O achado veio de uma
  // planilha de vendedor onde UMA delas foi anotada como "NF denegada"; as outras cinco ninguém
  // tinha visto.
  //
  // O critério é a CHAVE DE ACESSO, não o código de situação. Nota fiscal só recebe chave quando
  // a SEFAZ autoriza — é definição, não heurística —, enquanto o significado de cada código
  // numérico precisa ser decorado e a doc do Tiny não amarra código↔nome um a um. Conferido na
  // API: a NF 27465 (Pendente) vem SEM o campo `chave_acesso` no header da listagem; a 27466
  // (normal) vem com. O header também traz `descricao_situacao` em texto ("Pendente"), que serve
  // para o log dizer o MOTIVO — mas quem decide é a chave.
  //
  // ⚠️ A REGRA VALE SÓ PARA NOTA DE SAÍDA (`tipo: "S"`). Nota de ENTRADA não carrega chave nossa
  // — a chave é de quem emitiu —, e é justamente como chega a DEVOLUÇÃO DE CLIENTE. Aplicar a
  // regra a todas descartaria as devoluções: medido em 25/ago, 417 das 1.787 NFs do período são
  // entrada sem chave, contra 3 saídas de fato inválidas. A 1ª versão deste filtro fazia isso e
  // teria abortado o ETL (24,8% de descarte no ES, 50,1% no RJ) — a guarda abaixo é o que pegou.
  //
  // ⚠️ GUARDA OBRIGATÓRIA: se o Tiny parar de mandar `chave_acesso`, este filtro descartaria o
  // faturamento inteiro em silêncio — a mesma falha que o card do Itaú produziu em 04/ago. A taxa
  // é conferida SOBRE AS SAÍDAS (o denominador tem de ser o universo que a regra julga, senão a
  // entrada dilui o sintoma) e o lote aborta acima do limiar. Medido em 25/ago no período de
  // jul–ago: 1.288 saídas válidas contra 3 inválidas — 0,2%. Descarte normal é quase zero.
  //
  // ⚠️ E o código numérico NÃO é confiável de cabeça: medido na origem, `7` = Emitida DANFE,
  // `8` = Registrada, `6` = Autorizada e **`5` = REJEITADA** — chutei `5` como "autorizada" ao
  // investigar e dei duas notas inválidas como boas. Por isso quem decide é a chave, e o texto
  // de `descricao_situacao` entra só no log.
  const MAX_DESCARTE_PCT = 20;
  const descartes = new Map<string, number>();
  let saidas = 0;
  const nfsFiltradas = nfsLista.filter(nfItem => {
    const nfHeader = (nfItem as Record<string, unknown>).nota_fiscal as Record<string, unknown>
      ?? nfItem as Record<string, unknown>;
    if (!nfHeader.id) { skipped++; return false; }
    const sit = String(nfHeader.situacao ?? "");
    const ehSaida = String(nfHeader.tipo ?? "").toUpperCase() === "S";
    if (ehSaida) saidas++;
    if (sit === "3") {
      descartes.set("cancelada", (descartes.get("cancelada") ?? 0) + 1);
      skipped++;
      return false;
    }
    if (ehSaida && !String(nfHeader.chave_acesso ?? "").trim()) {
      const motivo = `saida sem chave de acesso (${nfHeader.descricao_situacao ?? `situacao ${sit}`})`;
      descartes.set(motivo, (descartes.get(motivo) ?? 0) + 1);
      skipped++;
      return false;
    }
    return true;
  });
  if (descartes.size > 0) {
    const detalhe = Array.from(descartes.entries())
      .sort((x, y) => y[1] - x[1]).map(([m, n]) => `${n} ${m}`).join(" · ");
    logger.info(`Tiny API ${cfg.erp}: ${skipped} NFs fora do faturamento — ${detalhe}`);
  }
  const semChave = Array.from(descartes.entries())
    .filter(([m]) => m.startsWith("saida sem chave")).reduce((n, [, v]) => n + v, 0);
  const pctDescarte = saidas > 0 ? (semChave / saidas) * 100 : 0;
  if (pctDescarte > MAX_DESCARTE_PCT) {
    throw new Error(
      `Tiny API ${cfg.erp}: ${pctDescarte.toFixed(1)}% das notas de SAIDA vieram sem ` +
      `'chave_acesso' (${semChave} de ${saidas}), acima do limite de ${MAX_DESCARTE_PCT}%. ` +
      `Suspeita de a origem ter parado de enviar o campo — abortando em vez de carregar ` +
      `faturamento pela metade.`
    );
  }

  // SKIP do detalhe de NFs já carregadas (intraday rápido). Seguro a partir do fix
  // no load.ts: em modo skip, o load só apaga as NFs que estão sendo reinseridas
  // (delete-by-nf_numero), nunca as antigas. Itens/desconto de NF emitida são imutáveis;
  // cancelamento vem de fetchNFsCanceladas; devolução é NF nova (detalhada).
  // Limitação: número de NF reusado só é pego pelo full diário (lag ≤24h).
  // Ver docs/etl-fato-pedidos.md §6.
  const skipCarregadas = process.env.ETL_SKIP_NF_CARREGADAS === "true";
  const headerDe = (nfItem: unknown) =>
    ((nfItem as Record<string, unknown>).nota_fiscal as Record<string, unknown>) ?? (nfItem as Record<string, unknown>);
  let nfsParaDetalhar = nfsFiltradas;
  if (skipCarregadas) {
    const numeros = nfsFiltradas.map(n => String(headerDe(n).numero ?? ""));
    const processadas = await consultarNFsProcessadas(cfg.erp, numeros);
    nfsParaDetalhar = nfsFiltradas.filter(n => !processadas.has(String(headerDe(n).numero ?? "").trim()));
    skipped += nfsFiltradas.length - nfsParaDetalhar.length;
    logger.info(`Tiny API ${cfg.erp}: SKIP ativo — ${processadas.size} NFs já processadas, detalhando ${nfsParaDetalhar.length} novas`);
  } else {
    logger.info(`Tiny API ${cfg.erp}: detalhando todas as ${nfsFiltradas.length} NFs (skip desativado)`);
  }

  // Busca detalhes em paralelo (CONCURRENCY calls simultâneas)
  const detalhes = await tinyProcessarConcorrente(
    nfsParaDetalhar,
    async (nfItem) => {
      const nfHeader = (nfItem as Record<string, unknown>).nota_fiscal as Record<string, unknown>
        ?? nfItem as Record<string, unknown>;
      try {
        const det = await tinyObterNF(cfg.token, String(nfHeader.id));
        return { nfHeader, det };
      } catch (e) {
        logger.warn(`${cfg.erp}: erro ao obter NF ${nfHeader.numero}: ${e}`);
        return null;
      }
    },
    CONCURRENCY
  );

  // NFs cuja decisão já é DEFINITIVA — só essas entram no cache (não re-detalhar depois).
  //
  // ⚠️ Só pode cachear o que é imutável. Até 31/jul/2026 a NF era marcada aqui, logo após o
  // detalhe, ANTES de qualquer validação — então uma NF ainda sem itens (em digitação, não
  // autorizada) era pulada por `itens.length === 0` mas ficava marcada como processada, e
  // nenhum run futuro voltava nela. Quando a NF era emitida de fato, já era tarde: sumia da
  // Torre para sempre. Caso real: 8 NFs de 31/07 (R$ 480.401,72 em venda, incluindo GAM MAX
  // de R$ 306 mil) marcadas às 19:09 e ausentes do faturamento — achadas só porque um RCA
  // estranhou pedidos "parados" no funil.
  //
  // Agora o push acontece nos pontos de decisão final: excluída por natureza, CNPJ interno,
  // ou NF efetivamente carregada em situação terminal.
  const processadasOk: string[] = [];

  /** Situação de NF que não muda mais: emitida / cancelada / autorizada / DANFE / registrada. */
  const SIT_NF_TERMINAL = new Set(["2", "3", "6", "7", "8"]);
  const situacaoTerminal = (det: Record<string, unknown>, nfHeader: Record<string, unknown>) =>
    SIT_NF_TERMINAL.has(String(det.situacao ?? nfHeader.situacao ?? "").trim());

  // Cache da data real do pedido — carregado em bloco ANTES do laço para que a maioria das NFs
  // não gere chamada nenhuma (ver o bloco de comentários de `resolverDataPedidoNf`).
  const pendentesDataPedido: { nf_numero: string; id_venda: string | null; data_pedido: string | null }[] = [];
  await carregarCacheDataPedido(
    cfg.erp,
    detalhes.map(i => String((i?.nfHeader as Record<string, unknown> | undefined)?.numero ?? "")).filter(Boolean)
  );

  for (const item of detalhes) {
    if (!item) { skipped++; continue; }
    const { nfHeader, det } = item;
    const nfNum = String(nfHeader.numero ?? "");

    const natureza  = String(det.natureza_operacao ?? "");
    const isVenda   = isNaturezaVenda(natureza);
    const isDevol   = isNaturezaDevolucao(natureza);
    const isBonif   = isNaturezaBonificacao(natureza);

    // Natureza fiscal não muda depois de emitida → decisão final, pode cachear.
    if (!isVenda && !isDevol && !isBonif) { processadasOk.push(nfNum); skipped++; continue; }

    const tipo_operacao = isDevol ? "Devolucao" : isBonif ? "Bonificacao" : "Venda";

    // Dados do cabeçalho
    const cliente   = (det.cliente ?? {}) as Record<string, unknown>;
    const cnpj      = limparCNPJ(cliente.cpf_cnpj);
    // Pula empresas internas — o CNPJ do destinatário não muda → decisão final, pode cachear.
    if (CNPJS_INTERNOS.has(cnpj)) { processadasOk.push(nfNum); skipped++; continue; }
    coletarEmailCliente(cnpj, cliente);
    const dataEmis  = dataBrParaIso(det.data_emissao ?? nfHeader.data_emissao);
    // Data REAL do pedido (via `id_venda` → pedido.obter). Cai na emissão só quando a origem
    // não tem o vínculo — e aí é a melhor aproximação disponível, não uma escolha de estilo.
    // ⚠️ O `dataBrParaIso` está DENTRO de `resolverDataPedidoNf`: a API devolve DD/MM/AAAA, e o
    // código antigo fazia `String(...)` cru, então se o campo um dia aparecesse entraria no
    // formato errado sem ninguém notar.
    const dataPedReal = await resolverDataPedidoNf(cfg, nfNum, det, pendentesDataPedido);
    const dataPed     = dataPedReal || dataEmis;
    // Prioriza o nº do pedido real (obs) — cai pro nº da NF só se não conseguir parsear.
    const numeroPedidoReal = extrairNumeroPedidoDaObs(det.obs) ?? String(det.numero ?? nfHeader.numero ?? "");

    // Valores da NF
    const valorNota    = parseFloat(String(det.valor_total_nota ?? nfHeader.valor ?? 0)) || 0;
    const valorDesc    = parseFloat(String(det.valor_desconto ?? 0)) || 0;
    const valorFrete   = parseFloat(String(det.valor_frete    ?? 0)) || 0;

    const itens = ((det.itens ?? []) as unknown[]);
    // NF sem item é estado TRANSITÓRIO (em digitação / ainda não autorizada): NÃO cachear,
    // senão ela nunca mais é relida quando ganhar os itens. Foi exatamente esse o bug.
    if (itens.length === 0) {
      logger.warn(`${cfg.erp}: NF ${nfNum} sem itens (situacao ${det.situacao ?? nfHeader.situacao}) — nao cacheada, sera relida no proximo run`);
      skipped++; continue;
    }

    // Só cacheia NF já em situação terminal (emitida/cancelada/autorizada/DANFE/registrada).
    // Em situação transitória os itens/valores ainda podem mudar → relê no próximo run.
    if (situacaoTerminal(det, nfHeader)) processadasOk.push(nfNum);

    // Rateio de desconto e frete fechando AO CENTAVO com a NF (30/set/2026). Antes o
    // transform rateava o desconto e aqui o frete era rateado, cada item arredondando
    // sozinho → pedido a ±R$ 0,03 do Tiny. Agora rateia aqui, uma vez, e manda
    // `desconto_rateado: true` para o transform não refazer.
    const rateio = ratearItensTiny(itens, valorDesc, valorFrete);

    for (let seq = 0; seq < itens.length; seq++) {
      const itemWrap = itens[seq] as Record<string, unknown>;
      const item     = (itemWrap.item ?? itemWrap) as Record<string, unknown>;

      const qtd      = parseFloat(String(item.quantidade   ?? 0)) || 0;
      const unitario = parseFloat(String(item.valor_unitario ?? 0)) || 0;
      const r        = rateio[seq];

      rows.push({
        marca:               cfg.marca,
        filial:              cfg.filial,
        pedido_mercos:       numeroPedidoReal,
        filial_pedido:       `${cfg.filial}-${numeroPedidoReal}`,
        pedido_protheus:     null,
        natureza_operacao:   natureza,
        tipo_operacao,
        // Só é preenchido em devolução — a venda não referencia nota nenhuma.
        nf_referenciada:     extrairNfReferenciadaDaObs(det.obs),
        numero_nota:         String(det.numero ?? nfHeader.numero ?? ""),
        serie:               String(det.serie ?? nfHeader.serie ?? ""),
        data_pedido:         dataPed,
        data_emissao:        dataEmis,
        quantidade_volumes:  null,
        codigo_produto:      String(item.codigo ?? ""),
        descricao_produto:   String(item.descricao ?? ""),
        quantidade:          qtd,
        valor_unitario:      unitario,
        valor_total_item:    r.liquido,   // já com o desconto do cabeçalho rateado
        valor_nota:          Math.round((valorNota - valorFrete) * 100) / 100, // sem frete
        valor_desconto:      r.desconto,  // parte deste item do desconto da NF
        valor_frete:         r.frete,
        desconto_rateado:    true,
        // ⚠️ ORDEM INVERTIDA até 08/ago/2026: `meio_pagamento` vinha PRIMEIRO, e quando ele existe
        // vale `"Asaas"` — o nome do GATEWAY, não do método. Resultado: 3.256 NFs entraram com
        // forma `ASAAS` e caíram em "Indefinido" na aba, apagando a forma verdadeira que estava
        // no campo ao lado. Medido na API: o pedido traz `forma_pagamento` = pix|boleto|credito
        // e `meio_pagamento` = Asaas|null, lado a lado. O método vem primeiro; o gateway é fallback.
        forma_pagamento:     String(det.forma_pagamento || det.meio_pagamento || ""),
        parcelas:            String(det.condicao_pagamento ?? ""),
        chave_nfe:           String(det.chave_acesso ?? nfHeader.chave_acesso ?? ""),
        nome_cliente:        String(cliente.nome ?? ""),
        cnpj_cliente:        cnpj,
        cidade_cliente:      String(cliente.cidade ?? ""),
        uf_cliente:          String(cliente.uf ?? ""),
        id_vendedor:         String(det.id_vendedor ?? nfHeader.id_vendedor ?? ""),
        nome_vendedor:       String(det.nome_vendedor ?? nfHeader.nome_vendedor ?? ""),
        status:              "Faturado",
        _item_sequencia:     seq + 1,
      });
    }
  }

  // Grava no cache as NFs detalhadas neste run (não re-detalhar depois)
  await registrarNFsProcessadas(cfg.erp, processadasOk);
  await gravarCacheDataPedido(cfg.erp, pendentesDataPedido);
  if (pendentesDataPedido.length > 0) {
    const comData = pendentesDataPedido.filter(p => p.data_pedido).length;
    logger.info(
      `Tiny API ${cfg.erp}: data real do pedido resolvida para ${comData}/${pendentesDataPedido.length} NFs novas ` +
      `(as demais não têm vínculo na origem e ficam com a data de emissão)`
    );
  }

  logger.info(`Tiny API ${cfg.erp}: ${rows.length} itens extraídos (${skipped} NFs puladas)`);
  return rows;
}

// ── Extração de pedidos em aberto ─────────────────────────────────────────────

/**
 * Busca pedidos não-faturados (Em aberto, Em Separação, Aguardando Fat.)
 * no período e retorna itens no formato dos cards Metabase.
 */
export async function fetchPedidosAbertos(
  cfg: TinyApiceConfig,
  dataInicio: string,
  dataFim: string,
): Promise<Record<string, unknown>[]> {
  logger.info(`Tiny API ${cfg.erp}: buscando pedidos abertos ${dataInicio} → ${dataFim}`);

  const toTiny = (iso: string) => iso.split("-").reverse().join("/");

  // Busca todos os pedidos (situacao=0 = todos)
  const pedsLista = await tinyPesquisarTodas(
    "pedidos.pesquisa.php", cfg.token,
    { dataInicial: toTiny(dataInicio), dataFinal: toTiny(dataFim), situacao: 0 },
    "pedidos"
  );

  logger.info(`Tiny API ${cfg.erp}: ${pedsLista.length} pedidos na lista — buscando detalhes...`);

  const rows: Record<string, unknown>[] = [];
  let skipped = 0;
  const pedidosComNotaNaoFaturada: string[] = [];
  const numerosMantidosPorNota = new Set<string>();   // nº dos pedidos mantidos só pela regra da nota

  // Filtra pedidos irrelevantes antes de buscar detalhes
  const pedsFiltrados = pedsLista.filter(pedItem => {
    const pedHeader = (pedItem as Record<string, unknown>).pedido as Record<string, unknown>
      ?? pedItem as Record<string, unknown>;
    const situacao = String(pedHeader.situacao ?? "");
    if (SIT_PED_IGNORAR.has(situacao) || !SIT_PED_MAP[situacao] || !pedHeader.id) {
      skipped++; return false;
    }
    return true;
  });

  // Busca detalhes em paralelo (CONCURRENCY calls simultâneas)
  const detalhesPed = await tinyProcessarConcorrente(
    pedsFiltrados,
    async (pedItem) => {
      const pedHeader = (pedItem as Record<string, unknown>).pedido as Record<string, unknown>
        ?? pedItem as Record<string, unknown>;
      try {
        const det = await tinyObterPedido(cfg.token, String(pedHeader.id));
        if (det.id_nota_fiscal && String(det.id_nota_fiscal) !== "0") {
          const idNf = String(det.id_nota_fiscal);
          // Nota autorizada/cancelada nesta leitura: o pedido sai do funil (já é/será faturado).
          if (notaDoPedidoJaContaComoFaturado(cfg.erp, idNf, dataBrParaIso(det.data_pedido))) return null;
          // Nota pendente, rejeitada, apagada ou emitida depois da leitura das notas: o pedido
          // CONTINUA no funil — senão o valor some dos dois lados (ver `notasDoRun`).
          pedidosComNotaNaoFaturada.push(`#${pedHeader.numero} R$ ${(parseFloat(String(det.total_pedido ?? 0)) || 0).toFixed(2)}`);
          numerosMantidosPorNota.add(String(det.numero ?? pedHeader.numero ?? "").trim());
        }
        // Duplicata logística: a NF é a do pedido antigo. Contar aqui = contar 2x.
        if (temMarcadorNaoFaturar(det.marcadores)) {
          logger.info(`${cfg.erp}: pedido ${pedHeader.numero} ignorado — marcador de "não faturar"/NF de outro pedido (duplicata logística)`);
          return null;
        }
        return { pedHeader, det };
      } catch (e) {
        logger.warn(`${cfg.erp}: erro ao obter pedido ${pedHeader.numero}: ${e}`);
        return null;
      }
    },
    CONCURRENCY
  );

  // ── Última porta contra DUPLICAR ──────────────────────────────────────────────────────────
  // Pedido mantido só pela regra da nota (`notasDoRun`) NUNCA pode coexistir com linha FATURADA do
  // mesmo pedido no banco: se já existe faturado para (erp_origem, pedido), ele sai do funil.
  // Falha fechando: se a consulta não responder, TODOS os mantidos saem (comportamento antigo) —
  // preferimos o buraco de sempre a contar a mesma venda duas vezes.
  let descartarMantidos: Set<string> | "todos" = new Set<string>();
  if (numerosMantidosPorNota.size > 0) {
    try {
      const { data: fat, error } = await getSupabaseAdmin()
        .from("fato_pedidos")
        .select("pedido_id")
        .eq("erp_origem", cfg.erp)
        .in("pedido_id", Array.from(numerosMantidosPorNota))
        .ilike("status", "faturado")
        .eq("excluido", false)
        .range(0, 9999);
      if (error) throw new Error(error.message);
      descartarMantidos = new Set((fat ?? []).map(r => String((r as { pedido_id: string }).pedido_id).trim()));
    } catch (e) {
      descartarMantidos = "todos";
      logger.warn(`Tiny API ${cfg.erp}: não consegui checar faturado no banco (${e}) — pedidos mantidos pela regra da nota saem do funil (comportamento antigo)`);
    }
  }
  const detalhesPedFinal = detalhesPed.map(item => {
    if (!item) return item;
    const num = String(item.det.numero ?? item.pedHeader.numero ?? "").trim();
    if (!numerosMantidosPorNota.has(num)) return item;
    if (descartarMantidos === "todos" || descartarMantidos.has(num)) {
      logger.info(`Tiny API ${cfg.erp}: pedido ${num} já tem linha FATURADA no banco — não mantido no funil (evita contar 2x)`);
      return null;
    }
    return item;
  });

  if (pedidosComNotaNaoFaturada.length > 0) {
    logger.info(
      `Tiny API ${cfg.erp}: ${pedidosComNotaNaoFaturada.length} pedido(s) com nota ligada que NÃO é faturado ` +
      `(pendente/rejeitada/apagada/emitida agora) — candidatos a MANTER no funil: ${pedidosComNotaNaoFaturada.join(", ")}`
    );
  }

  for (const item of detalhesPedFinal) {
    if (!item) { skipped++; continue; }
    const { pedHeader, det } = item;

    const cliente   = (det.cliente ?? {}) as Record<string, unknown>;
    const cnpj      = limparCNPJ(cliente.cpf_cnpj);
    // Pula empresas internas
    if (CNPJS_INTERNOS.has(cnpj)) { skipped++; continue; }
    coletarEmailCliente(cnpj, cliente);
    // Pedido aberto ainda não tem NF — o vínculo com a nota entra depois, pelo caminho das
    // faturadas, no mesmo registro (a chave é o pedido).
    coletarUplaces(cfg.erp, det, null);
    const dataPed   = dataBrParaIso(det.data_pedido);
    // Etiqueta "reprovado": o pedido ENTRA no fato normalmente e `fn_aplica_pedido_reprovado`
    // o tira da conta depois — assim ele continua visível (alerta no Gerencial) e volta a
    // contar sozinho se faturar ou se a etiqueta sair.
    coletarReprovado(cfg.erp, det as Record<string, unknown>, cnpj, dataPed ? String(dataPed) : null);

    const valorTotal = parseFloat(String(det.total_pedido ?? 0)) || 0;
    const valorFrete = parseFloat(String(det.valor_frete  ?? 0)) || 0;
    const valorDesc  = parseFloat(String(det.valor_desconto ?? 0)) || 0;

    const itens = ((det.itens ?? []) as unknown[]);
    if (itens.length === 0) { skipped++; continue; }

    const isBonif = temMarcadorBonificacao(det.marcadores);

    // Mesmo rateio das NFs (fecha ao centavo com o Tiny). ⚠️ Antes o desconto era rateado
    // AQUI e o transform rateava de novo sobre o valor já rateado, então `valor_desconto`
    // do pipeline saía uma fração do real (set/2026: R$ 5,5 mil gravados contra ~R$ 48 mil).
    const rateio = ratearItensTiny(itens, valorDesc, valorFrete);

    for (let seq = 0; seq < itens.length; seq++) {
      const itemWrap = itens[seq] as Record<string, unknown>;
      const item     = (itemWrap.item ?? itemWrap) as Record<string, unknown>;

      const qtd      = parseFloat(String(item.quantidade   ?? 0)) || 0;
      const unitario = parseFloat(String(item.valor_unitario ?? 0)) || 0;
      const r        = rateio[seq];

      rows.push({
        marca:               cfg.marca,
        filial:              cfg.filial,
        pedido_mercos:       String(det.numero ?? pedHeader.numero ?? ""),
        filial_pedido:       `${cfg.filial}-${det.numero ?? pedHeader.numero}`,
        pedido_protheus:     null,
        natureza_operacao:   isBonif ? "Bonificação" : "Venda de Mercadoria à contribuinte",
        tipo_operacao:       isBonif ? "Bonificacao" : "Venda",
        numero_nota:         null,
        serie:               null,
        data_pedido:         dataPed,
        data_emissao:        null,
        quantidade_volumes:  null,
        codigo_produto:      String(item.codigo ?? ""),
        descricao_produto:   String(item.descricao ?? ""),
        quantidade:          qtd,
        valor_unitario:      unitario,
        valor_total_item:    r.liquido,
        valor_nota:          valorTotal,
        valor_desconto:      r.desconto,
        valor_frete:         r.frete,
        desconto_rateado:    true,
        // ⚠️ ORDEM INVERTIDA até 08/ago/2026: `meio_pagamento` vinha PRIMEIRO, e quando ele existe
        // vale `"Asaas"` — o nome do GATEWAY, não do método. Resultado: 3.256 NFs entraram com
        // forma `ASAAS` e caíram em "Indefinido" na aba, apagando a forma verdadeira que estava
        // no campo ao lado. Medido na API: o pedido traz `forma_pagamento` = pix|boleto|credito
        // e `meio_pagamento` = Asaas|null, lado a lado. O método vem primeiro; o gateway é fallback.
        forma_pagamento:     String(det.forma_pagamento || det.meio_pagamento || ""),
        parcelas:            String(det.condicao_pagamento ?? ""),
        chave_nfe:           null,
        nome_cliente:        String(cliente.nome ?? ""),
        cnpj_cliente:        cnpj,
        cidade_cliente:      String(cliente.cidade ?? ""),
        uf_cliente:          String(cliente.uf ?? ""),
        id_vendedor:         String(det.id_vendedor ?? pedHeader.id_vendedor ?? ""),
        nome_vendedor:       String(det.nome_vendedor ?? pedHeader.nome_vendedor ?? ""),
        status:              SIT_PED_MAP[String(pedHeader.situacao ?? "")] ?? "Em aberto",
        _item_sequencia:     seq + 1,
      });
    }
  }

  logger.info(`Tiny API ${cfg.erp}: ${rows.length} itens de pedidos abertos (${skipped} pulados)`);
  return rows;
}

// ── NFs canceladas ────────────────────────────────────────────────────────────

/**
 * Retorna lista de números de NFs canceladas (sit=3) no período.
 * Não busca detalhe — apenas o número para marcar como Cancelado no banco.
 * Muito mais rápido que fetchNFsFaturadas pois não faz calls de detalhe.
 */
export async function fetchNFsCanceladas(
  cfg: TinyApiceConfig,
  dataInicio: string,
  dataFim: string,
): Promise<string[]> {
  logger.info(`Tiny API ${cfg.erp}: buscando NFs canceladas ${dataInicio} → ${dataFim}`);

  const toTiny = (iso: string) => iso.split("-").reverse().join("/");

  const lista = await tinyPesquisarTodas(
    "notas.fiscais.pesquisa.php", cfg.token,
    { dataInicial: toTiny(dataInicio), dataFinal: toTiny(dataFim), situacao: 3 },
    "notas_fiscais"
  );

  const numeros = lista
    .map(n => {
      const nf = (n as Record<string, unknown>).nota_fiscal as Record<string, unknown> ?? n as Record<string, unknown>;
      return String(nf.numero ?? "").trim();
    })
    .filter(Boolean);

  logger.info(`Tiny API ${cfg.erp}: ${numeros.length} NFs canceladas encontradas`);
  return numeros;
}
