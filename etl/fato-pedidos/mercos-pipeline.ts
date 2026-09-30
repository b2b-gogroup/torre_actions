/**
 * Pipeline de pedido Mercos — fonte suplementar de `protheusPedidos`.
 *
 * ── POR QUE EXISTE ───────────────────────────────────────────────────────────
 * O middleware (db 48 "Middleware Hop", schema `ecommerce`) parou de receber pedido novo
 * em **09/09/2026 19:09:10** no ES e **31/08** no RJ. Os cards 19610 (`protheusPedidos`)
 * e 19611 (`protheusTrat`) leem de lá, então o PIPELINE do Gerencial congelou — enquanto
 * o FATURADO, que vem do Protheus (db 47), seguiu em dia.
 *
 * Medido em 17/09/2026: a Torre tinha **2 pedidos** Protheus não-faturados na base inteira
 * desde 20/ago, contra **149 pedidos / R$ 1.350.285,38** vivos no Mercos.
 *
 * ⚠️ **NÃO é perda de receita.** O pedido continua chegando ao Protheus e faturando — 100%
 * das NFs Protheus de 10 a 16/09 têm número de pedido Mercos. O que se perdeu foi a janela
 * de ver o pedido ANTES de virar nota. Este módulo devolve essa janela.
 *
 * ── O DESENHO (pedido do chefe, áudio de 17/09/2026) ─────────────────────────
 * "Tira o que já tá faturado da [lista de] pedidos do Mercos: se já apareceu no Protheus
 *  como faturado, limpa; se tá em separação, separado etc., traz com aquele status."
 *
 * Três fontes vivas, cada uma respondendo uma pergunta:
 *   1. `mercos_vendas_detalhadas` (Supabase) → QUAL pedido existe, de quem, quanto
 *   2. card **19670** (db 47, `SC5010.C5_XNUMECM`) → JÁ FATUROU? (a "limpeza")
 *   3. `corpem_saida` (WMS Corpem/BraLog)  → EM QUE FASE está de verdade
 *
 * ⚠️ **ESTE MÓDULO É SÓ DO TRILHO MERCOS/PROTHEUS — a ÁPICE NÃO PASSA POR AQUI.**
 * Ápice é Tiny (`tiny_es`/`tiny_rj`), tem pipeline próprio e nunca entrou no Mercos. Marca
 * da Ápice aparecendo neste pipeline é **defeito**, não dado: foi assim que se descobriu, em
 * 17/09, que o cron `preencher-marca-produto` estava jogando R$ 859.489,32 de pedido em
 * aberto dentro da Ápice (ver `db/migrations/20260917c`).
 *
 * ⚠️ **A chave do cruzamento é (regiao, pedido_mercos), NUNCA só o número.** Cada filial
 * tem numeração própria no Mercos — o pedido 1081 é do RJ e o 1081 do ES é outro cliente.
 * É a mesma colisão das convenções #22 e #25 do CLAUDE.md.
 *
 * ⚠️ **O Protheus NÃO serve como fonte de pipeline**, e isso foi medido antes de desenhar:
 * o pedido só é criado no `SC5010` quando vai faturar — desde 01/08/2026 existem apenas
 * 8 pedidos SEM NF no ES. Por isso o Mercos continua sendo a fonte e o Protheus só limpa.
 *
 * ── A LIMITAÇÃO, DECLARADA ───────────────────────────────────────────────────
 * ⚠️ **Nenhuma das três fontes traz ITEM.** `corpem_saida.sku` é a CONTAGEM de SKUs do
 * pedido (`sku=10, qtde_total=200` = 10 SKUs, 200 unidades), não o código — nome de coluna
 * que engana, conferido na amostra. Então cada pedido entra como UMA linha, com o SKU
 * sentinela `MC00000` e **marca NULL**.
 *
 * Consequência, que é preciso saber ao ler a tela:
 *   ✅ funil, KPI de pipeline, cliente, vendedor, valor, UF  → corretos
 *   ❌ "Faturamento por Marca" (parte do pipeline)           → incompleto
 *   ❌ ranking de produto / matriz SKU                        → não afetados (filtram faturado)
 *
 * ── DESLIGA SOZINHO ──────────────────────────────────────────────────────────
 * ⚠️ Só entra pedido que o card 19610 **não trouxe**. No dia em que o middleware voltar,
 * o card volta a trazer os pedidos com item de verdade, esta fonte para de gerar linha
 * sozinha e o SKU sentinela some da base no primeiro run full — sem ninguém desligar nada.
 */

import { logger } from "../shared/logger.js";
import { fetchMetabaseCard } from "../shared/metabase-client.js";
import { fetchSupabaseTable } from "../shared/supabase-admin.js";
import { janelaProtheus } from "./janela-protheus.js";
import { FASE_WMS_STATUS, FASE_WMS_JA_FATURADO, type LinhaWms } from "./fase-wms.js";

/** SKU sentinela — ver `db/migrations/20260917_produto_pedido_sem_detalhe.sql`. */
export const SKU_SEM_DETALHE = "MC00000";

/** Card que responde "este pedido Mercos já virou NF no Protheus?" (db 47, C5_XNUMECM). */
const CARD_MERCOS_X_PROTHEUS = 19670;

/** Região do Mercos → filial da Torre. SP não usa Mercos (1 pedido em toda a base). */
const REGIAO_FILIAL: Record<string, string> = { ES: "CD ES", RJ: "CD RJ" };

/**
 * Status do Mercos que significam "pedido firme, ainda não faturado".
 *
 * ⚠️ `Nota Integrada` e `Faturado` ficam FORA: são o estado normal pós-NF, e o faturado
 * já entra pelo card do Protheus. `Cancelado` idem.
 *
 * ⚠️ **`Erro na Integração` fica FORA, e isso NÃO é escolha minha — é o que o card 19610
 * sempre fez.** O `WHERE` dele exclui explicitamente os status `1301-84937 / 1302-84946 /
 * 1303-84955`, que são "Erro na Integração". Incluir aqui mudaria a regra do funil por
 * efeito colateral de um conserto de fonte.
 *
 * E a medição mostra por que a regra existe: dos 28 pedidos nesse estado, **21 têm mais de
 * 30 dias** (os mais velhos com 90), somando R$ 110.915,00 que nunca integraram e não vão
 * virar receita. Já `Em Separação` e `Aguardando Faturamento` têm **ZERO** acima de 30 dias
 * — é pipeline vivo. Eles ficam de fora do funil e visíveis no Mercos, como sempre foi.
 */
const STATUS_PIPELINE = new Set([
  // "Captado" (30/set/2026, pedido do usuário): o pedido acabou de ser concluído no Mercos e o
  // WMS talvez ainda não o conheça — entra como Preparando para envio (ver STATUS_MERCOS_TORRE).
  // Com fase no WMS, a fase ganha; sem ela, vale o Mercos.
  "Captado",
  "Em Separação",
  "Separado",
  "Aguardando Faturamento",
]);

/**
 * Tipos de pedido que viram linha. ⚠️ `Sem saldo`, `Transferência de Estoque` e `Reenvio`
 * ficam fora — não são venda nem bonificação e nunca entraram no funil.
 */
const TIPOS_ACEITOS = new Set(["Venda", "Bonificação"]);

/**
 * Fase do WMS → etapa da Torre: mapeamento em `fase-wms.ts` (fonte única, compartilhada com
 * o card antigo 19610). ⚠️ `N.F. Conf.`/`Emb. Conf.`/`CkoVol.*` = nota já saiu: o pedido é
 * DESCARTADO (o faturado entra pelo trilho do Protheus, com os itens reais).
 */

/** Status do Mercos → status da Torre, quando o WMS não conhece o pedido. */
const STATUS_MERCOS_TORRE: Record<string, string> = {
  "Captado": "Em separação",
  "Em Separação": "Em separação",
  "Separado": "Aguardando Faturamento",
  "Aguardando Faturamento": "Aguardando Faturamento",
};

interface LinhaMercos {
  numero_pedido: number | string | null;
  regiao: string | null;
  data_emissao: string | null;
  razao_social: string | null;
  cnpj_cpf: string | null;
  cidade: string | null;
  estado: string | null;
  vendedor: string | null;
  tipo_pedido: string | null;
  status_pedido: string | null;
  status_personalizado: string | null;
  condicao_pagamento: string | null;
  total_pedido: number | string | null;
}

interface LinhaProtheusCross {
  pedido_mercos: string | null;
  regiao: string | null;
  numero_nota: string | null;
  situacao_protheus: string | null;
}

interface LinhaItem {
  regiao: string | null;
  numero_pedido: number | string | null;
  item_seq: number | string | null;
  sku: string | null;
  produto: string | null;
  quantidade: number | string | null;
  preco_liquido: number | string | null;
  subtotal: number | string | null;
}

export interface ResultadoPipelineMercos {
  linhas: Record<string, unknown>[];
  diagnostico: {
    mercos_lidos: number;
    firmes: number;
    ja_no_card: number;
    removidos_protheus: number;
    removidos_wms: number;
    gerados: number;
    valor_gerado: number;
    com_fase_wms: number;
    qtd_real_do_wms: number;
    qtd_assumida_1: number;
    com_item_detalhado: number;
    sem_item_detalhado: number;
    linhas_de_item: number;
    por_status: Record<string, number>;
  };
}

const chave = (regiao: unknown, pedido: unknown) =>
  `${String(regiao ?? "").trim().toUpperCase().replace("_APICE", "")}|${String(pedido ?? "").trim().replace(/^0+/, "") || "0"}`;

const num = (v: unknown): number => {
  if (v === null || v === undefined || v === "") return 0;
  const n = Number(String(v).replace(",", "."));
  return Number.isFinite(n) ? n : 0;
};

/** `YYYY-MM-DD` → `YYYY-MM-DDT00:00:00` (o card 19610 devolve `data_pedido` com hora). */
const dataPedido = (iso: unknown): string | null => {
  const s = String(iso ?? "").slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? `${s}T00:00:00` : null;
};

/**
 * Monta as linhas de pipeline que o card 19610 deixou de trazer.
 *
 * @param pedidosDoCard linhas já vindas do card 19610 — o que está aqui NÃO é regerado.
 * @param diasJanela    quantos dias de emissão olhar para trás (default 90).
 */
export async function montarPipelineMercos(
  pedidosDoCard: Record<string, unknown>[],
  // ⚠️ 60 e não 90: é o mesmo corte do diff-delete no intraday. Medido em 17/09, 100% dos
  // pedidos gerados têm 0-7 dias de emissão (o buraco do middleware começou em 09/09), então
  // 60 dias cobre o caso com folga de 7x — e manter a MESMA janela no full e no intraday
  // elimina a faixa 61-90 d, que só o full geraria e só o full apagaria.
  diasJanela = 60,
): Promise<ResultadoPipelineMercos> {
  // ⚠️ A JANELA NUNCA PODE PASSAR DA DO DIFF-DELETE, e isso é o que evita linha órfã.
  // O `load.ts` só apaga o que sumiu da origem DENTRO do corte do Protheus
  // (`COALESCE(data_faturamento_id, data_pedido_id) >= corteInt`, 60 d no intraday).
  // Gerando um pedido de 90 dias, no dia em que ele deixasse de vir (porque faturou) a
  // linha ficaria FORA do alcance do delete e sobreviveria para sempre como "Em separação"
  // — somando ao pipeline um valor que já virou receita, que é exatamente a duplicação
  // que esta fonte existe para não causar. O `load.ts` já avisa disso do outro lado:
  // "as duas contas TÊM que ser a mesma".
  const jp = janelaProtheus();
  const diasEfetivo = jp.ativa ? Math.min(diasJanela, jp.dias) : diasJanela;
  if (diasEfetivo !== diasJanela) {
    logger.info(`Pipeline Mercos: janela ${diasJanela}d → ${diasEfetivo}d (limite do diff-delete do Protheus)`);
  }
  const corte = new Date(Date.now() - diasEfetivo * 86_400_000).toISOString().slice(0, 10);

  const [mercos, cross, wms, itensCapturados] = await Promise.all([
    fetchSupabaseTable("mercos_vendas_detalhadas") as unknown as Promise<LinhaMercos[]>,
    fetchMetabaseCard(CARD_MERCOS_X_PROTHEUS, { data_inicial: corte }) as unknown as Promise<LinhaProtheusCross[]>,
    fetchSupabaseTable("corpem_saida") as unknown as Promise<LinhaWms[]>,
    // ⚠️ Itens capturados à mão pelo RPA `automacoes/mercos-pedidos-itens`. É OPCIONAL:
    // tabela vazia (ninguém rodou ainda) devolve [] e o módulo volta ao comportamento de
    // 1 linha por pedido. Nunca derruba a carga por causa disto.
    (fetchSupabaseTable("mercos_pedido_item") as unknown as Promise<LinhaItem[]>)
      .catch((e) => { logger.warn(`mercos_pedido_item indisponível (segue sem itens): ${e}`); return [] as LinhaItem[]; }),
  ]);

  // Lado Protheus: o que JÁ virou NF. Esta é a "limpeza" do desenho.
  const faturadoNoProtheus = new Set(
    cross.filter((c) => c.situacao_protheus === "faturado")
         .map((c) => chave(c.regiao, c.pedido_mercos)),
  );
  // Itens por pedido, quando o RPA já capturou.
  const itensPorPedido = new Map<string, LinhaItem[]>();
  for (const it of itensCapturados) {
    const k = chave(it.regiao, it.numero_pedido);
    (itensPorPedido.get(k) ?? itensPorPedido.set(k, []).get(k)!).push(it);
  }

  // Lado WMS: fase real da separação.
  const faseWms = new Map<string, LinhaWms>();
  for (const w of wms) faseWms.set(chave(w.regiao, w.numero_pedido_cliente), w);

  // O que o card já trouxe não é regerado — é isto que desliga o módulo sozinho
  // quando o middleware voltar.
  const jaNoCard = new Set(
    pedidosDoCard.map((p) => chave(
      String(p.filial ?? "").includes("RJ") ? "RJ" : String(p.filial ?? "").includes("SP") ? "SP" : "ES",
      p.pedido_mercos,
    )),
  );

  const diag = {
    mercos_lidos: mercos.length,
    firmes: 0,
    ja_no_card: 0,
    removidos_protheus: 0,
    removidos_wms: 0,
    gerados: 0,
    valor_gerado: 0,
    com_fase_wms: 0,
    qtd_real_do_wms: 0,
    qtd_assumida_1: 0,
    com_item_detalhado: 0,
    sem_item_detalhado: 0,
    linhas_de_item: 0,
    por_status: {} as Record<string, number>,
  };

  const linhas: Record<string, unknown>[] = [];

  for (const m of mercos) {
    const regiao = String(m.regiao ?? "").trim().toUpperCase();
    const filial = REGIAO_FILIAL[regiao];
    if (!filial) continue;                                         // SP não usa Mercos
    if (String(m.data_emissao ?? "") < corte) continue;
    if (String(m.status_pedido ?? "") !== "Concluído") continue;    // orçamento/cancelado fora
    if (!STATUS_PIPELINE.has(String(m.status_personalizado ?? ""))) continue;
    if (!TIPOS_ACEITOS.has(String(m.tipo_pedido ?? ""))) continue;

    diag.firmes++;
    const k = chave(regiao, m.numero_pedido);

    if (jaNoCard.has(k)) { diag.ja_no_card++; continue; }
    if (faturadoNoProtheus.has(k)) { diag.removidos_protheus++; continue; }

    const w = faseWms.get(k);
    if (w) {
      diag.com_fase_wms++;
      // Nota já saiu pelo WMS: o faturado entra pelo Protheus, com itens de verdade.
      if (FASE_WMS_JA_FATURADO.has(String(w.situacao_fase ?? "")) ||
          String(w.numero_nf ?? "").trim() !== "") {
        diag.removidos_wms++;
        continue;
      }
    }

    const status =
      (w && FASE_WMS_STATUS[String(w.situacao_fase ?? "")]) ||
      STATUS_MERCOS_TORRE[String(m.status_personalizado ?? "")] ||
      "Em aberto";

    // ⚠️ `quantidade` NÃO pode ser 0: `fato_pedidos` tem
    // `CHECK (quantidade <> 0)` e o transform descarta a linha ANTES de chegar ao banco.
    // Foi assim que a 1ª versão perdeu as 105 linhas inteiras no dry run — o extract
    // somava certo (2.366 + 105 = 2.471) e o transform entregava ZERO, sem erro nenhum.
    //
    // O WMS dá a quantidade REAL do pedido (`qtde_total`, unidades; medido: 6.394 de
    // 6.394 linhas preenchidas e > 0). Onde o WMS não conhece o pedido, assume-se **1**,
    // que é o menor valor que o CHECK aceita e não infla nada — e o diagnóstico conta
    // quantos ficaram assim, para ninguém somar unidade achando que é medição.
    const qtdWms = w ? num(w.qtde_total) : 0;
    const quantidade = qtdWms > 0 ? qtdWms : 1;
    if (qtdWms > 0) diag.qtd_real_do_wms++; else diag.qtd_assumida_1++;

    const valor = num(m.total_pedido);
    diag.gerados++;
    diag.valor_gerado += valor;
    diag.por_status[status] = (diag.por_status[status] ?? 0) + 1;

    // ── Itens de verdade, quando o RPA já capturou o pedido ──────────────────
    // ⚠️ Com item, a linha deixa de ser sintética: SKU real, quantidade real e a marca
    // resolvida pelo caminho normal do transform (prefixo do SKU / dim_produto). É o que
    // devolve o pedido ao "Faturamento por Marca". Sem item, cai no bloco de baixo.
    const itens = itensPorPedido.get(k) ?? [];
    if (itens.length > 0) {
      diag.com_item_detalhado++;
      diag.linhas_de_item += itens.length;
      let seq = 0;
      for (const it of itens) {
        seq++;
        linhas.push({
          ...comumDoPedido(m, filial, status),
          // marca fica NULA aqui e o transform a resolve pelo SKU — não adianta adivinhar
          // aqui e depois o transform sobrescrever: seria duas regras para o mesmo campo.
          marca: null,
          codigo_produto: String(it.sku ?? "").trim(),
          descricao_produto: it.produto,
          quantidade: num(it.quantidade) || 1,
          valor_unitario: num(it.preco_liquido),
          valor_total_item: num(it.subtotal),
          item_seq_origem: seq,
        });
      }
      continue;
    }
    diag.sem_item_detalhado++;

    // Sem item capturado: o pedido entra como UMA linha, com o SKU sentinela.
    // ⚠️ Mesmo cabeçalho do caminho COM item (`comumDoPedido`) — duas cópias do mesmo
    // objeto divergem em semanas e ninguém descobre até um número sair errado.
    linhas.push({
      ...comumDoPedido(m, filial, status),
      marca: null,                    // desconhecida: não há item
      codigo_produto: SKU_SEM_DETALHE,
      descricao_produto: "Pedido sem detalhamento de item",
      quantidade,
      // sem item, não existe preço unitário — `CHECK (valor_unitario >= 0)` aceita nulo
      valor_unitario: null,
      valor_total_item: valor,
      // marcador interno: `transform.ts` zera a marca desta linha (ver a migration do SKU).
      __sem_detalhe: true,
    });
  }

  logger.info(
    `Pipeline Mercos (supl.): ${diag.gerados} pedidos / R$ ${diag.valor_gerado.toFixed(2)} ` +
    `| firmes ${diag.firmes} · já no card ${diag.ja_no_card} · ` +
    `removidos: Protheus ${diag.removidos_protheus}, WMS ${diag.removidos_wms} ` +
    `| fase WMS em ${diag.com_fase_wms} · qtd real ${diag.qtd_real_do_wms}, assumida=1 em ${diag.qtd_assumida_1} ` +
    `| COM item ${diag.com_item_detalhado} (${diag.linhas_de_item} linhas), SEM item ${diag.sem_item_detalhado}`,
  );

  return { linhas, diagnostico: diag };
}

/**
 * Campos de cabeçalho do pedido, iguais em toda linha dele.
 * ⚠️ Existe para o caminho COM item e o SEM item não divergirem: eram duas cópias do
 * mesmo objeto, e duas cópias divergem em semanas sem ninguém notar (convenção #23).
 */
function comumDoPedido(m: LinhaMercos, filial: string, status: string): Record<string, unknown> {
  const pedido = String(m.numero_pedido ?? "").trim();
  return {
    filial,
    pedido_mercos: pedido,
    filial_pedido: `${filial}-${pedido}`,
    pedido_protheus: null,
    "natureza_operação": m.tipo_pedido,
    tipo_operacao: m.tipo_pedido,
    numero_nota: null,
    serie: null,
    data_pedido: dataPedido(m.data_emissao),
    data_emissao: null,
    quantidade_volumes: null,
    valor_nota: num(m.total_pedido),
    valor_desconto: null,
    valor_frete: 0,
    forma_pagamento: condicaoParaForma(m.condicao_pagamento),
    parcelas: null,
    chave_nfe: null,
    nome_cliente: m.razao_social,
    cnpj_cliente: String(m.cnpj_cpf ?? "").replace(/\D/g, ""),
    cidade_cliente: m.cidade,
    uf_cliente: m.estado,
    so_criador_id: null,
    nome_vendedor: m.vendedor,
    status,
  };
}

/**
 * `067 - PIX` → `PIX`. Mesma regra dos cards 19610/19611, que fazem o split por ` - `
 * no próprio SQL — repetida aqui porque a fonte agora é o Mercos e não o middleware.
 */
function condicaoParaForma(condicao: unknown): string | null {
  const s = String(condicao ?? "").trim();
  if (!s.includes(" - ")) return s || null;
  const depois = s.split(" - ").slice(1).join(" - ").trim();
  return depois.split(" ")[0] || null;
}
