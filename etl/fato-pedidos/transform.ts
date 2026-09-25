import {
  normalizeStr,
  limpaCnpj,
  dateToInt,
  skuToMarca,
  normalizarNumero,
} from "../shared/normalize.js";
import { logger } from "../shared/logger.js";
import { loadPedidosCancelados } from "../shared/pedidos-cancelados.js";
import type { ExtractedData } from "./extract.js";
import {
  PLACEHOLDER_UUID,
  PLACEHOLDER_CNPJ,
  PLACEHOLDER_PROD,
  mapFiliais,
  mapMarcas,
  marcaOverride,
  mapTinyErpOrigem,
  skuFixMap,
  camposNumericos,
} from "./maps.js";

// Tipo de saída normalizada para carga no banco
/**
 * CNPJs de empresas DO PRÓPRIO GRUPO — venda para elas é transferência, não receita.
 *
 * ⚠️ Duplica a intenção de `CNPJS_INTERNOS` em `extract-tiny-apice.ts`, mas com alcance
 * diferente: lá o corte é na extração do Tiny (evita buscar detalhe caro de NF), aqui é na
 * convergência, onde Protheus TAMBÉM passa. As duas listas precisam andar juntas — se um CNPJ
 * do grupo entrar em uma e não na outra, ele volta pelo ERP que ficou de fora.
 */
const CNPJS_GRUPO = new Set([
  // MAGA COMERCIO (unidades 40001/40002/40005/40004 do grupo no WebGex)
  "38246589000185", "38246589000266", "38246589000347", "38246589000428",
  "58323315000150",                                   // RITU PARTNERS (40003)
]);

export interface FatoPedidoRow {
  pedido_id: string;
  item_sequencia: number;
  nf_numero: string | null;
  nf_chave: string | null;
  /** Só em devolução: nº da NF-e que esta nota estorna. Lido da `obs` no extract do Tiny. */
  nf_referenciada: string | null;
  erp_origem: string;
  data_pedido_id: number | null;
  data_faturamento_id: number | null;
  cliente_id: string;
  vendedor_id: string;
  produto_id: string;
  marca_id: string | null;
  filial_id: string;
  tipo_operacao: string | null;
  status: string | null;
  quantidade: number;
  valor_unitario: number;
  valor_total: number;
  valor_desconto: number;
  valor_frete: number;
  uf_id: string | null;
  pedido_erp_id: string | null;
  forma_pagamento: string | null;
  parcelas: string | null;
}

// ============================================================
// Helpers de lookup (construídos uma vez, usados nos dois ramos)
// ============================================================

function buildVendedorMap(
  dimVendedor: Record<string, unknown>[],
  aliases: Record<string, unknown>[]
): Map<string, string> {
  const m = new Map<string, string>();
  for (const row of dimVendedor) {
    const nome = normalizeStr(row.nome);
    if (nome) m.set(nome, String(row.id));
  }
  for (const row of aliases) {
    const alias = normalizeStr(row.alias);
    if (alias) m.set(alias, String(row.vendedor_id));
  }
  return m;
}

function buildClienteSet(dimCliente: Record<string, unknown>[]): Set<string> {
  const s = new Set<string>();
  for (const row of dimCliente) {
    const cnpj = String(row.cnpj || "").trim();
    if (cnpj) s.add(cnpj);
  }
  return s;
}

function buildProdutoSet(dimProduto: Record<string, unknown>[]): Set<string> {
  const s = new Set<string>();
  for (const row of dimProduto) {
    const sku = String(row.sku || "").trim();
    if (sku) s.add(sku);
  }
  return s;
}

// Mapa sku → marca_id do cadastro (dim_produto) — fonte autoritativa da marca do
// produto, usada como fallback quando a origem (ERP) não traz marca mapeável.
// Ex.: Yenzah (YE) vinha do Protheus sem marca e skuToMarca não reconhece o prefixo YE.
function buildProdutoMarcaMap(dimProduto: Record<string, unknown>[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const row of dimProduto) {
    const sku = String(row.sku || "").trim();
    const marca = String(row.marca_id || "").trim();
    if (sku && marca) m.set(sku, marca);
  }
  return m;
}

// Mapa cnpj → co-titulares (UUIDs) de `carteira_compartilhada`. Co-titular NÃO é dono:
// o dono continua em `carteira_rca`. O que este mapa habilita é a INVERSÃO de precedência
// abaixo — num cliente compartilhado, quem o ERP diz que vendeu ganha do dono da carteira.
//
// Fora de um cliente compartilhado o mapa está vazio e nada muda. É essa a razão de a
// exceção morar numa tabela e não num `if` por CNPJ: liga e desliga sem deploy.
function buildCompartilhadoMap(rows: Record<string, string>[]): Map<string, Set<string>> {
  const m = new Map<string, Set<string>>();
  for (const row of rows || []) {
    // fetchSupabaseTable devolve `ativo` como boolean; string "false" também não passa.
    if (String(row["ativo"] ?? "true").toLowerCase() === "false") continue;
    const cnpj = limpaCnpj(row["cnpj_cliente"]);
    const uuid = (row["vendedor_id"] || "").trim();
    if (!cnpj || !uuid) continue;
    if (!m.has(cnpj)) m.set(cnpj, new Set());
    m.get(cnpj)!.add(uuid);
  }
  return m;
}

/**
 * Vendedor que o ERP diz ter feito a venda, SE ele for co-titular deste cliente.
 *
 * É o coração da "dupla validação" (pedido do Italo, 11/set/2026): numa rede atendida por
 * mais de um RCA, a carteira diz quem VÊ, e o pedido diz quem VENDEU. Sem isto, os dois
 * ramos abaixo dariam 100% do faturamento ao dono do CNPJ — que é o que acontecia com as
 * 328 NFs da Bel, todas no Pedro Igor, inclusive as que não foram dele.
 *
 * De onde vem o nome: Tiny/Apice manda `nome_vendedor` na NF e no pedido (é a PJ do RCA,
 * ex. "PIAB REPRESENTACAO COMERCIAL LTDA"); Protheus/Mercos mandam o nome do vendedor.
 * Os dois passam pelo mesmo `vendedorMap` (dim_vendedor + dim_vendedor_alias), então um
 * RCA novo na rede só precisa do alias cadastrado — nada aqui muda.
 *
 * Devolve "" quando não se aplica, e aí a precedência antiga (carteira primeiro) segue igual.
 */
function vendedorCoTitular(
  nomeVendedorERP: unknown,
  cnpj: string,
  vendedorMap: Map<string, string>,
  compartilhadoMap: Map<string, Set<string>>
): string {
  const coTitulares = compartilhadoMap.get(cnpj);
  if (!coTitulares || coTitulares.size === 0) return "";
  const vid = vendedorMap.get(normalizeStr(nomeVendedorERP));
  return vid && coTitulares.has(vid) ? vid : "";
}

function buildCarteiraMap(carteiraRCAs: Record<string, string>[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const row of carteiraRCAs) {
    const cnpj = limpaCnpj(row["cnpj_cliente"] || row["cnpj_corrigido"] || row["CNPJ/CPF"]);
    // DB: vendedor_id é UUID direto; Google Sheets legado: NOME_VENDEDOR
    const uuid = (row["vendedor_id"] || "").trim();
    if (cnpj && uuid) m.set(cnpj, uuid);
  }
  return m;
}

function buildSkuMap(
  cadastroProdutos: Record<string, string>[]
): Map<string, string> {
  const m = new Map<string, string>();
  for (const row of cadastroProdutos) {
    const marca = normalizeStr(row["Marca"] || row["marca"]);
    const skuAntigo = normalizeStr(row["Cód Antigo"] || row["cod_antigo"] || "");
    const skuNovo = (row["SKU"] || row["sku"] || "").trim();
    if (marca && skuAntigo && skuNovo) {
      m.set(`${marca}-${skuAntigo}`, skuNovo);
    }
  }
  return m;
}

// ============================================================
// 0. mergeEnrich — equivale ao Merge1 do N8N (mode: combine, enrichInput2)
// ============================================================
// Enriquece input2 (fat) com campos de input1 (trat) matched por chave

function mergeEnrich(
  input1: Record<string, unknown>[],
  input2: Record<string, unknown>[],
  matchField: string
): Record<string, unknown>[] {
  const lookup = new Map<unknown, Record<string, unknown>>();
  for (const row of input1) {
    lookup.set(row[matchField], row);
  }

  return input2.map((row) => {
    const match = lookup.get(row[matchField]);
    if (match) {
      // Enriquece: campos do input1 preenchem lacunas do input2
      // input2 só tem prioridade quando o valor é preenchido (não null/undefined/"")
      const merged: Record<string, unknown> = { ...match };
      for (const [key, val] of Object.entries(row)) {
        if (val !== null && val !== undefined && val !== "") {
          merged[key] = val;
        }
      }
      return merged;
    }
    return row;
  });
}

// ============================================================
// 1a. filterCancelledPedidos — remove pedidos da lista de cancelados
// ============================================================
// Deve ser aplicado ANTES de qualquer outro tratamento em protheusPedidos

function filterCancelledPedidos(
  pedidos: Record<string, unknown>[],
  cancelados: Set<string>
): Record<string, unknown>[] {
  const filtered = pedidos.filter(
    (r) => !cancelados.has(String(r.pedido_mercos ?? "").trim())
  );
  logger.debug(
    `filterCancelledPedidos: ${pedidos.length} pedidos → ${filtered.length} após remover cancelados`
  );
  return filtered;
}

// ============================================================
// 1. filterAlreadyInvoiced — equivale ao node trat_pedidos_NFE
// ============================================================
// Remove pedidos Protheus que já têm NF no dataset de faturados

function filterAlreadyInvoiced(
  pedidos: Record<string, unknown>[],
  faturados: Record<string, unknown>[]
): Record<string, unknown>[] {
  const faturadosSet = new Set(faturados.map((r) => r.filial_pedido));
  const filtered = pedidos.filter((r) => !faturadosSet.has(r.filial_pedido));
  logger.debug(
    `filterAlreadyInvoiced: ${pedidos.length} pedidos → ${filtered.length} após remover faturados`
  );
  return filtered;
}

// Equivalente para Tiny: remove itens de pedidos (em aberto) cujo pedido_mercos+filial
// já existe nos datasets de faturados — evita duplicatas com status misto no banco.
function filterTinyAlreadyInvoiced(
  pedidos: Record<string, unknown>[],
  faturados: Record<string, unknown>[]
): Record<string, unknown>[] {
  const faturadosSet = new Set(
    faturados.map((r) => `${r.pedido_mercos}|${normalizeStr(r.filial)}`)
  );
  const filtered = pedidos.filter(
    (r) => !faturadosSet.has(`${r.pedido_mercos}|${normalizeStr(r.filial)}`)
  );
  logger.debug(
    `filterTinyAlreadyInvoiced: ${pedidos.length} itens → ${filtered.length} após remover já faturados`
  );
  return filtered;
}

// ============================================================
// 2. enrichVendedor — equivale ao node trat_vendedor
// ============================================================
// Enriquece vendas Protheus com vendedor_id via CNPJ → carteira RCA (UUID direto)

function enrichVendedor(
  vendas: Record<string, unknown>[],
  carteiraMap: Map<string, string>,
  vendedorMap: Map<string, string>,
  compartilhadoMap: Map<string, Set<string>>
): Record<string, unknown>[] {
  return vendas.map((row) => {
    const cnpj = limpaCnpj(row.cnpj_cliente);
    const out = { ...row };
    const uuid = carteiraMap.get(cnpj);
    if (uuid) out.vendedor_id_carteira = uuid;
    const coTitular = vendedorCoTitular(row.nome_vendedor, cnpj, vendedorMap, compartilhadoMap);
    if (coTitular) out.vendedor_id_cotitular = coTitular;
    return out;
  });
}

// ============================================================
// 3. normalizeProtheus — equivale ao node trat_protheus
// ============================================================

function normalizeProtheus(
  data: Record<string, unknown>[],
  vendedorMap: Map<string, string>,
  clienteSet: Set<string>,
  produtoSet: Set<string>,
  produtoMarcaMap: Map<string, string>
): FatoPedidoRow[] {
  const sequenciaCounters = new Map<string, number>();
  const clienteSemMatch = new Map<string, number>(); // cnpj → contagem
  const produtoSemMatch = new Map<string, number>(); // sku → contagem

  const rows = data.map((d) => {
    const pedidoKey = String(d.pedido_mercos || "");
    const seq = (sequenciaCounters.get(pedidoKey) || 0) + 1;
    sequenciaCounters.set(pedidoKey, seq);

    const cnpj = String(d.cnpj_cliente || "").trim();
    const sku = String(d.codigo_produto || "").trim();
    // ⚠️ Pedido sem detalhamento de item (SKU sentinela `MC00000`, fonte suplementar do
    // Mercos enquanto o middleware está parado): a marca é DESCONHECIDA por construção,
    // e tem de sair NULL. `dim_produto.marca_id` é NOT NULL com FK, então o sentinela
    // carrega um 'AP' inerte na dimensão — sem esta guarda, o último `COALESCE` abaixo
    // (`produtoMarcaMap`) o pegaria e jogaria o pipeline inteiro dentro da Ápice no
    // "Faturamento por Marca". Ver `db/migrations/20260917_produto_pedido_sem_detalhe.sql`,
    // conferência 4.
    const marca_id = sku === "MC00000"
      ? null
      : (marcaOverride[sku] || mapMarcas[normalizeStr(d.marca)] || skuToMarca(sku) || produtoMarcaMap.get(sku) || null);

    const pedidoIdRaw = String(d.pedido_mercos || "").trim();
    const pedidoIdFinal = pedidoIdRaw
      || String(d.pedido_protheus || "").trim()
      || (String(d.numero_nota || "").trim() ? `protheus:NF-${String(d.numero_nota).trim()}` : "");

    return {
      pedido_id: pedidoIdFinal,
      item_sequencia: seq,
      nf_numero: str(d.numero_nota),
      nf_chave: str(d.chave_nfe),
      // Protheus não expõe a nota referenciada; só o trilho Tiny preenche.
      nf_referenciada: null,
      erp_origem: "protheus",
      data_pedido_id: dateToInt(d.data_pedido),
      data_faturamento_id: dateToInt(d.data_emissao),
      cliente_id: (() => {
        if (clienteSet.has(cnpj)) return cnpj;
        if (cnpj) clienteSemMatch.set(cnpj, (clienteSemMatch.get(cnpj) || 0) + 1);
        return PLACEHOLDER_CNPJ;
      })(),
      vendedor_id:
        // 0) cliente compartilhado: quem o ERP diz que vendeu ganha do dono da carteira.
        //    Só preenche quando o vendedor do ERP é co-titular deste CNPJ (ver
        //    `vendedorCoTitular`), então fora de rede compartilhada isto é sempre "".
        str(d.vendedor_id_cotitular) ||
        str(d.vendedor_id_carteira) ||
        vendedorMap.get(normalizeStr(d.nome_vendedor)) ||
        PLACEHOLDER_UUID,
      produto_id: (() => {
        if (sku && produtoSet.has(sku)) return sku;
        if (sku) produtoSemMatch.set(sku, (produtoSemMatch.get(sku) || 0) + 1);
        return PLACEHOLDER_PROD;
      })(),
      marca_id,
      filial_id: mapFiliais[normalizeStr(d.filial)] || PLACEHOLDER_UUID,
      tipo_operacao: normalizeTipoOperacao(str(d.tipo_operacao), normalizarNumero(d.valor_total_item), str(d.status), str(d.forma_pagamento)),
      status: str(d.status),
      quantidade: normalizarNumero(d.quantidade),
      valor_unitario: normalizarNumero(d.valor_unitario),
      valor_total: normalizarNumero(d.valor_total_item),
      valor_desconto: normalizarNumero(d.valor_desconto),
      valor_frete: normalizarNumero(d.valor_frete),
      uf_id: str(d.uf_cliente),
      pedido_erp_id: str(d.pedido_protheus),
      forma_pagamento: str(d.forma_pagamento),
      parcelas: str(d.parcelas),
    };
  });

  if (clienteSemMatch.size > 0) {
    logger.warn("Protheus — cliente_id sem match em dim_cliente:", Object.fromEntries(
      [...clienteSemMatch.entries()].sort((a, b) => b[1] - a[1])
    ));
  }
  if (produtoSemMatch.size > 0) {
    logger.warn("Protheus — produto_id sem match em dim_produto:", Object.fromEntries(
      [...produtoSemMatch.entries()].sort((a, b) => b[1] - a[1])
    ));
  }

  return rows;
}

// ============================================================
// 4. enrichTiny — equivale ao node trat_1
// ============================================================
// Traduz SKU antigo → novo e enriquece vendedor via CNPJ

function enrichTiny(
  tinyData: Record<string, unknown>[],
  skuMap: Map<string, string>,
  carteiraMap: Map<string, string>,
  vendedorMap: Map<string, string>,
  compartilhadoMap: Map<string, Set<string>>
): Record<string, unknown>[] {
  return tinyData.map((row) => {
    const d = { ...row };

    // Traduz SKU
    const marcaNorm = normalizeStr(d.marca);
    const skuAntigoNorm = normalizeStr(d.codigo_produto);
    const chave = `${marcaNorm}-${skuAntigoNorm}`;
    const skuNovo = skuMap.get(chave);
    if (skuNovo) d.codigo_produto = skuNovo;

    // Enriquece vendedor via carteira RCA (UUID direto)
    const cnpj = limpaCnpj(d.cnpj_cliente);
    const uuid = carteiraMap.get(cnpj);
    if (uuid) d.vendedor_id_carteira = uuid;

    // Cliente compartilhado: guarda quem o Tiny diz que vendeu, se for co-titular.
    const coTitular = vendedorCoTitular(d.nome_vendedor, cnpj, vendedorMap, compartilhadoMap);
    if (coTitular) d.vendedor_id_cotitular = coTitular;

    return d;
  });
}

// ============================================================
// 5. normalizeTiny — equivale ao node trat_tiny
// ============================================================

function normalizeTiny(
  data: Record<string, unknown>[],
  vendedorMap: Map<string, string>,
  clienteSet: Set<string>,
  produtoSet: Set<string>,
  produtoMarcaMap: Map<string, string>
): FatoPedidoRow[] {
  const sequenciaCounters = new Map<string, number>();
  const semMatch = new Map<string, number>(); // nome_vendedor → contagem
  const clienteSemMatch = new Map<string, number>(); // cnpj → contagem
  const produtoSemMatch = new Map<string, number>(); // sku (já traduzido) → contagem

  const rows = data.map((d) => {
    const pedidoKey = String(d.pedido_mercos || "");
    const seq = (sequenciaCounters.get(pedidoKey) || 0) + 1;
    sequenciaCounters.set(pedidoKey, seq);

    const cnpj = String(d.cnpj_cliente || "").trim();
    let sku = String(d.codigo_produto || "").trim();

    let   marca_id   = mapMarcas[normalizeStr(d.marca)] || null;
    const filialNorm = normalizeStr(d.filial);
    const erp_origem = mapTinyErpOrigem[filialNorm] ?? "tiny";

    // De-para Tiny → SKU canônico, indexado por (codigo, marca_id).
    // Mesmo código numérico Tiny aparece em marcas diferentes (ex: 20764
    // = COND MANTEIGA na AP, DEO BD SPL na BB), então lookup global causa
    // mistura cross-marca.
    if (marca_id && skuFixMap[sku]?.[marca_id]) {
      sku = skuFixMap[sku][marca_id];
    }
    // Override de marca por SKU canônico (corrige cadastro errado na origem Tiny).
    // Aplicado APÓS o de-para acima, que precisa da marca original p/ mapear o SKU.
    if (marcaOverride[sku]) marca_id = marcaOverride[sku];
    // Fallback final: marca do cadastro do produto (dim_produto.marca_id) quando a
    // origem não trouxe marca mapeável — não sobrescreve marca já resolvida acima.
    if (!marca_id) marca_id = produtoMarcaMap.get(sku) ?? null;

    const itemValue     = normalizarNumero(d.valor_total_item);
    const valorNota     = normalizarNumero(d.valor_nota);     // NF total (post-discount)
    const descontoTotal = normalizarNumero(d.valor_desconto); // order-level, vinha duplicado por item
    // valor_nota = bruto - desconto → reconstruct bruto to use as proportional denominator.
    // Using valor_nota directly blows up when discount > 50% (e.g. valor_nota=149, desconto=5921).
    const totalProdutos = valorNota + descontoTotal;
    const statusLower   = str(d.status)?.toLowerCase() ?? "";

    // Desconto proporcional a este item (corrige duplicação order-level × n_itens)
    const descontoProporcional =
      totalProdutos > 0 && descontoTotal > 0
        ? (itemValue / totalProdutos) * descontoTotal
        : 0;

    // Para pedidos faturados: aplica desconto real. Pipeline: mantém valor de tabela.
    const valorTotalFinal =
      statusLower === "faturado" && descontoProporcional > 0
        ? Math.max(0, itemValue - descontoProporcional)
        : itemValue;

    const pedidoIdRaw = String(d.pedido_mercos || "").trim();
    const pedidoIdFinal = pedidoIdRaw
      || String(d.pedido_tiny || "").trim()
      || (String(d.numero_nota || "").trim() ? `${erp_origem}:NF-${String(d.numero_nota).trim()}` : "");

    return {
      pedido_id: pedidoIdFinal,
      item_sequencia: seq,
      nf_numero: str(d.numero_nota),
      nf_chave: str(d.chave_nfe),
      nf_referenciada: str(d.nf_referenciada),
      erp_origem,
      data_pedido_id: dateToInt(d.data_pedido),
      data_faturamento_id: dateToInt(d.data_emissao),
      cliente_id: (() => {
        if (clienteSet.has(cnpj)) return cnpj;
        if (cnpj) clienteSemMatch.set(cnpj, (clienteSemMatch.get(cnpj) || 0) + 1);
        return PLACEHOLDER_CNPJ;
      })(),
      vendedor_id: (() => {
        // 0) CLIENTE COMPARTILHADO — precedência INVERTIDA (11/set/2026).
        //    A carteira diz quem VÊ o cliente; a NF diz quem VENDEU. Numa rede atendida
        //    por dois RCAs (Bel: Pedro Igor + Fernanda Vidal) é a NF que manda, senão o
        //    dono do CNPJ levaria o faturamento do parceiro. Vazio fora desse caso.
        const coTitular = str(d.vendedor_id_cotitular);
        if (coTitular) return coTitular;
        // 1) carteira_rca UUID direto (lookup por CNPJ do cliente)
        const fromCarteira = str(d.vendedor_id_carteira);
        if (fromCarteira) return fromCarteira;
        // 2) nome_vendedor do ERP → dim_vendedor_alias → UUID
        const vid = vendedorMap.get(normalizeStr(d.nome_vendedor));
        if (!vid) {
          const nome = str(d.nome_vendedor) || "(vazio)";
          semMatch.set(nome, (semMatch.get(nome) || 0) + 1);
        }
        return vid || PLACEHOLDER_UUID;
      })(),
      produto_id: (() => {
        if (sku && produtoSet.has(sku)) return sku;
        if (sku) produtoSemMatch.set(sku, (produtoSemMatch.get(sku) || 0) + 1);
        return PLACEHOLDER_PROD;
      })(),
      marca_id,
      filial_id: mapFiliais[normalizeStr(d.filial)] || PLACEHOLDER_UUID,
      tipo_operacao: normalizeTipoOperacao(str(d.tipo_operacao), valorTotalFinal, str(d.status), str(d.forma_pagamento)),
      status: str(d.status),
      quantidade: normalizarNumero(d.quantidade),
      valor_unitario: normalizarNumero(d.valor_unitario),
      valor_total: valorTotalFinal,
      valor_desconto: descontoProporcional,
      valor_frete: normalizarNumero(d.valor_frete),
      uf_id: str(d.uf_cliente),
      pedido_erp_id: str(d.pedido_tiny),
      forma_pagamento: str(d.forma_pagamento),
      parcelas: str(d.parcelas),
    };
  });

  if (semMatch.size > 0) {
    logger.warn("Tiny — nome_vendedor sem match no dim_vendedor:", Object.fromEntries(
      [...semMatch.entries()].sort((a, b) => b[1] - a[1])
    ));
  }
  if (clienteSemMatch.size > 0) {
    logger.warn("Tiny — cliente_id sem match em dim_cliente:", Object.fromEntries(
      [...clienteSemMatch.entries()].sort((a, b) => b[1] - a[1])
    ));
  }
  if (produtoSemMatch.size > 0) {
    logger.warn("Tiny — produto_id sem match em dim_produto:", Object.fromEntries(
      [...produtoSemMatch.entries()].sort((a, b) => b[1] - a[1])
    ));
  }

  return rows;
}

// ============================================================
// 6. normalizeNumericValues — equivale ao node trat_valores
// ============================================================

function normalizeNumericValues(items: FatoPedidoRow[]): FatoPedidoRow[] {
  return items.map((item) => {
    const row = { ...item };
    for (const campo of camposNumericos) {
      (row as Record<string, unknown>)[campo] = normalizarNumero(
        (row as Record<string, unknown>)[campo]
      );
    }
    return row;
  });
}

// ============================================================
// Pipeline completa
// ============================================================

/** Executa toda a pipeline de transformação sobre os dados extraídos */
export async function transformAll(sources: ExtractedData): Promise<FatoPedidoRow[]> {
  logger.group("Transformação");

  // Construir lookups (uma vez)
  const vendedorMap = buildVendedorMap(sources.dimVendedor, sources.dimVendedorAlias);
  const clienteSet = buildClienteSet(sources.dimCliente);
  const produtoSet = buildProdutoSet(sources.dimProduto);
  const produtoMarcaMap = buildProdutoMarcaMap(sources.dimProduto);
  const carteiraMap = buildCarteiraMap(sources.carteiraRCAs);
  const compartilhadoMap = buildCompartilhadoMap(sources.carteiraCompartilhada);
  const skuMap = buildSkuMap(sources.cadastroProdutos);
  const canceladosSet = await loadPedidosCancelados();

  logger.info("Lookups construídos", {
    vendedores: vendedorMap.size,
    clientes: clienteSet.size,
    produtos: produtoSet.size,
    carteira: carteiraMap.size,
    clientes_compartilhados: compartilhadoMap.size,
    skus: skuMap.size,
    cancelados: canceladosSet.size,
  });

  // --- Ramo Protheus ---
  // Remove cancelados do protheusTrat (Em Separação, Aguardando Fat.) antes do merge
  const protheusTratFiltered = filterCancelledPedidos(
    sources.protheusTrat,
    canceladosSet
  );
  // Merge1 (N8N): enrichInput2 — enriquece protheusFat com campos de protheusTrat por filial_pedido
  const enrichedFat = mergeEnrich(
    protheusTratFiltered,
    sources.protheusFat,
    "filial_pedido"
  );
  // Limpeza: remove pedidos cancelados de protheusPedidos (Em aberto)
  const pedidosSemCancelados = filterCancelledPedidos(
    sources.protheusPedidos,
    canceladosSet
  );
  // trat_pedidos_NFE: remove pedidos que já foram faturados
  const pedidosFiltrados = filterAlreadyInvoiced(
    pedidosSemCancelados,
    sources.protheusFat
  );
  // GET_protheus_dados: append faturados enriquecidos + pedidos filtrados
  const protheusMerged = [...enrichedFat, ...pedidosFiltrados];
  logger.info(`Protheus merge: ${enrichedFat.length} faturados + ${pedidosFiltrados.length} pedidos (${sources.protheusPedidos.length - pedidosSemCancelados.length} cancelados removidos)`);

  const protheusEnriched = enrichVendedor(protheusMerged, carteiraMap, vendedorMap, compartilhadoMap);
  const protheusRows = normalizeProtheus(protheusEnriched, vendedorMap, clienteSet, produtoSet, produtoMarcaMap);
  logger.info(`Protheus: ${protheusRows.length} itens normalizados`);

  // --- Ramo Tiny ---
  // GET_tiny_dados: merge de 7 inputs (4 vendas + 3 pedidos)
  // Faturados primeiro; depois filtrar pedidos em aberto que já foram faturados
  // (mesmo pedido_mercos+filial em ambos os cards = ETL duplicado, não faturamento parcial)
  const tinyFaturados = [
    ...sources.tinyAPRJ,
    ...sources.tinyAPES,
    ...sources.tinyAPSP,
    ...sources.tinyBBSP,
  ];
  const tinyPedidosFiltrados = filterTinyAlreadyInvoiced(
    [
      ...sources.tinyAPESPedidos,
      ...sources.tinyAPRJPedidos,
      ...sources.tinyAPSPPedidos,
    ],
    tinyFaturados
  );
  const tinyMerged = [...tinyFaturados, ...tinyPedidosFiltrados];
  const tinyEnriched = enrichTiny(tinyMerged, skuMap, carteiraMap, vendedorMap, compartilhadoMap);
  const tinyRows = normalizeTiny(tinyEnriched, vendedorMap, clienteSet, produtoSet, produtoMarcaMap);
  logger.info(`Tiny: ${tinyRows.length} itens normalizados`);

  // --- Convergência ---
  const allRows = normalizeNumericValues([...protheusRows, ...tinyRows]);

  /*
    VENDA PARA EMPRESA DO PRÓPRIO GRUPO NÃO É VENDA (18/ago/2026).

    ⚠️ O filtro existia SÓ no `extract-tiny-apice.ts` (`CNPJS_INTERNOS`) — o Protheus nunca teve.
    A MAGA apareceu no ranking de clientes do Gerencial com 48,5% de share do recorte, e das 3 NFs
    dela **2 são Protheus** (R$ 94.274,88 + R$ 42.855,78): filtrar só no Tiny teria deixado passar
    justamente a que estava na tela.

    Por isso o corte vem aqui, na CONVERGÊNCIA — é o único ponto por onde os dois ERPs passam.
    O do extract continua, porque lá evita o custo de buscar o detalhe da NF.

    Confirmado por dois lados antes de cortar: o CNPJ é uma unidade do grupo no WebGex
    (38.246.589/0002-66 = unidade 40002) e a pessoa atribuída como vendedora é de OPERAÇÕES,
    cuidando do trâmite interno.
  */
  const semInterno = allRows.filter((r) => !CNPJS_GRUPO.has(String(r.cliente_id ?? "")));
  const internas = allRows.length - semInterno.length;
  if (internas > 0) {
    logger.warn(`Removidas ${internas} linhas de transferência interna (empresa do grupo)`);
  }

  // Filtrar linhas com quantidade = 0 (violam CHECK constraint do banco)
  const valid = semInterno.filter((r) => r.quantidade !== 0);
  const dropped = allRows.length - valid.length;
  if (dropped > 0) {
    logger.warn(`Removidas ${dropped} linhas com quantidade = 0`);
  }

  logger.info(`Total após normalização e filtro: ${valid.length} itens`);

  logger.groupEnd();
  return valid;
}

// Normaliza tipo_operacao: Tiny usa 'Não Faturado' para pedidos em aberto e 'Bonificado'
// para bonificações. Ambos precisam ser mapeados para os valores canônicos do sistema.
// Regra extra: linha de VENDA FATURADA com valor <= 0 é brinde/cortesia (qtd > 0, R$ 0)
// → reclassifica para Bonificacao. Evita reabrir "venda faturada com valor zero" na auditoria
// a cada ETL. Aplicado só em pedido fechado (Faturado), nunca em pedido em aberto.
// Regra extra 2: os cards Protheus de pedidos ABERTOS às vezes não preenchem tipo_operacao
// (o tipo só nasce na NF). Bonificação chega vazia com forma_pagamento='BONIFICAÇÃO';
// venda aberta também pode chegar vazia (ex.: pedido 5915, PIX, 14/jul). Sem essa regra a
// linha vira pipeline sem tipo: conta no funil mas fica invisível p/ meta/KPIs (divergência).
// Pedido aberto sem tipo → Bonificacao (forma BONIFICA%) ou Venda (default, como o
// 'Não Faturado'→Venda do Tiny). Faturado sem tipo permanece null — não inventar faturamento.
function normalizeTipoOperacao(v: string | null, valorTotal?: number | null, status?: string | null, formaPagamento?: string | null): string | null {
  if (!v) {
    if ((formaPagamento ?? '').toUpperCase().startsWith('BONIFICA')) return 'Bonificacao';
    const st = (status ?? '').toLowerCase();
    if (st && st !== 'faturado' && st !== 'cancelado') return 'Venda';
    return v;
  }
  const lower = v.toLowerCase();
  let tipo = v;
  if (lower === 'não faturado' || lower === 'nao faturado') tipo = 'Venda';
  else if (lower === 'bonificado') tipo = 'Bonificacao';
  if (tipo.toLowerCase() === 'venda' && valorTotal != null && valorTotal <= 0
      && (status ?? '').toLowerCase() === 'faturado') {
    tipo = 'Bonificacao';
  }
  return tipo;
}

// Helper: trim + null para strings vazias
function str(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s || null;
}
