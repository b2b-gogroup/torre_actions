/**
 * ETL Fotos de Produto (Shopify) → dim_produto_foto
 *
 * Até 04/set/2026 a fonte era a planilha Google "Produtos_Consolidado" (mantida à mão pelo
 * time de marketing) — ver histórico no HEAD anterior deste arquivo. A partir de 05/set/2026,
 * por decisão do usuário, a fonte passou a ser o card do Metabase 28639 "Stg Shopify Products"
 * (https://metabase.gocase.com.br/question/28639-stg-shopify-products).
 *
 * ⚠️ **09/out/2026: trocado de "ler o CARD" pra "rodar o SQL dele direto no banco".** A chave de
 * API gerada pelo portal interno de self-service (que cria chave por Metabase + grupo de acesso)
 * só autoriza **SQL nativo nos bancos liberados pro grupo** — ela NUNCA tem permissão de
 * coleção/card, por desenho do próprio portal ("a chave só serve pra rodar SQL nativo pela API").
 * `GET /api/card/28639` com essa chave dá 403 mesmo só pra pegar metadado; `GET /api/collection`
 * devolve `[]` (zero coleção visível, nem a raiz) — não é bug de permissão faltando, é o portal
 * nunca concedendo esse eixo. `GET /api/database` nos mesmos bancos funciona normal. Então: em vez
 * de pedir o card salvo, rodamos a MESMA query dele via `/api/dataset` (SQL nativo), que essa
 * chave já pode fazer — peguei o SQL literal em "Exibir SQL" na UI do card (é um `SELECT *` puro
 * da tabela, sem filtro/join nenhum) e copiei aqui, sem LIMIT (o card tinha um LIMIT de export da
 * UI, 1048575, que não faz sentido pro ETL).
 *
 * `DATABASE_DATA_MART` = 63 no Metabase gocase — confirmado via `GET /api/database` com a chave
 * (`{"id":63,"name":"Data Mart"}`), não é o mesmo id usado no outro Metabase (gobeaute) pra um
 * banco de mesmo nome — ids são por instância, nunca assumir que repetem entre Metabases.
 *
 * ⚠️ **`/api/dataset` corta em 2.000 linhas, EM SILÊNCIO** (`/api/card/:id/query/json`, que a
 * gente usava antes, não tinha esse teto — é exatamente o que a troca perdeu). A tabela tem
 * **3.547 linhas** medidas via `count(*)`; sem paginar, cada run pegava uma fatia arbitrária de
 * 2.000 (ordem física do Postgres, não garantida entre chamadas) — foi isso que fez a guarda de
 * encolhimento disparar com números DIFERENTES em runs seguidos (838 numa vez, 543 noutra) sobre
 * a MESMA fonte: nenhum catálogo encolheu, a página sorteada é que mudava. `buscarTodasLinhas()`
 * pagina com `LIMIT 2000 OFFSET N` até a página vir menor que 2000 — confirmado ao vivo que
 * `LIMIT 2000 OFFSET 0` + `LIMIT 2000 OFFSET 2000` somam exatos os 3.547.
 *
 * O resultado é 1 linha por VARIANTE de produto (tem Product ID + Variant ID + Sku por linha),
 * não 1 linha por foto — diferente da planilha antiga, que já vinha com 1 linha por foto e a
 * ordem implícita na posição. Cada linha carrega até 2 fotos candidatas: `image_src` (imagem
 * principal do produto) e `variant_image_url` (imagem específica da variante, quando existe e é
 * diferente da principal) — as duas viram entradas em `dim_produto_foto`, ordem 1/2 nessa
 * prioridade.
 *
 * ⚠️ **Ápice não tem SKU nenhum nessa tabela — achado 18/set/2026, na 1ª carga real via Metabase.**
 * O catálogo Ápice inteiro no Shopify (754 linhas, `Brand: "apice"`) usa um SKU NUMÉRICO
 * próprio (ex. `20588`, `46210` — o código do Tiny/ERP da Ápice), sem nenhuma relação com o
 * `AP01xxx` que a Torre usa pra Ápice: **0 de 3.443 linhas têm SKU começando em "AP"**.
 * Não é bug de normalização — são dois sistemas de identificação diferentes pro mesmo produto
 * (mesmo problema já registrado do lado do ETL de pedidos, em `etl/dev/fuzzy-match-skus.ts`).
 * Fallback: quando o SKU não bate com `dim_produto`, tenta pelo **código de barras**
 * (`barcode`/EAN) contra `dim_produto.ean` — match EXATO, não fuzzy (foto errada é pior que
 * sem foto). Medido: 108 de 129 produtos Ápice ativos têm EAN cadastrado e batem exato com o
 * card. EAN duplicado entre 2+ SKUs em `dim_produto` NUNCA vira fallback (ambíguo — não dá pra
 * saber qual dos dois é o dono da foto).
 *
 * Full refresh diário (truncate + insert) — a tabela inteira é a fonte da verdade, não há nada pra
 * mesclar incrementalmente. Roda via GitHub Actions (.github/workflows/etl-produto-fotos.yml).
 *
 * Uso local:
 *   cd etl && npm ci
 *   METABASE_URL=https://metabase.gocase.com.br METABASE_API_KEY=... \
 *     SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... npx tsx produto-fotos/index.ts
 */
import { createClient } from "@supabase/supabase-js";
import { nativeQuery, datasetRows } from "../shared/metabase-client.js";

const DATABASE_DATA_MART = 63;
const SQL_FOTOS = `SELECT * FROM "silver"."stg_shopify_products"`;
/** Medido ao vivo em 09/out/2026 — ver aviso no cabeçalho do arquivo. */
const PAGE_SIZE = 2000;

/** Pagina `/api/dataset` com LIMIT/OFFSET até a página vir menor que PAGE_SIZE — é o jeito de
 *  contornar o teto de 2.000 linhas sem depender do endpoint de download (`/api/dataset/json`
 *  devolveu 400 com o mesmo payload, formato de upload é diferente; LIMIT/OFFSET é confirmado). */
async function buscarTodasLinhas(sql: string): Promise<Record<string, unknown>[]> {
  const todas: Record<string, unknown>[] = [];
  let offset = 0;
  while (true) {
    const pagina = await datasetRows(nativeQuery(DATABASE_DATA_MART, `${sql} LIMIT ${PAGE_SIZE} OFFSET ${offset}`));
    todas.push(...pagina);
    if (pagina.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }
  return todas;
}

/** Abaixo desta fracao do que JA esta gravado, a carga nova e tratada como fonte quebrada -- nao
 *  como queda real de catalogo. A guarda de baixo so pegava card VAZIO, e em 18/set/2026 uma carga
 *  de 678 linhas passou por cima de 3.144 sem reclamar, porque "nao e zero": a origem tinha trocado
 *  o SKU da Apice e esvaziado `variant_image_url`. Encolher nao e sinonimo de estar certo.
 *  ETL_FOTOS_FORCA=1 ignora (use so com a queda ja conferida); ETL_FOTOS_DRY=1 mede sem gravar. */
const PISO_ENCOLHIMENTO = 0.7;
const DRY = process.env.ETL_FOTOS_DRY === "1";
/** ETL_SO_CONTEUDO=1 grava só produto_conteudo (não mexe nas fotos) — para recarregar o texto. */
const SO_CONTEUDO = process.env.ETL_SO_CONTEUDO === "1";
const FORCA = process.env.ETL_FOTOS_FORCA === "1";

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error("[etl-produto-fotos] Faltam env vars: NEXT_PUBLIC_SUPABASE_URL/SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY");
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY, { auth: { persistSession: false } });

type FotoRow = { sku: string; ordem: number; url: string; atualizado_em: string };

/** `validos` = todo SKU que existe em `dim_produto` (match direto). `eanParaSku` = fallback
 *  por código de barras, só pra EAN que pertence a UM ÚNICO SKU em `dim_produto` — ver o
 *  porquê no cabeçalho do arquivo (achado Ápice, 18/set/2026). */
type Catalogo = { validos: Set<string>; eanParaSku: Map<string, string> };

/** O Metabase devolve a chave do jeito que a coluna foi salva na base (snake_case, esperado
 *  pra tabela de staging) — mas alguns cards já vêm com o nome "bonito" que a UI mostra. Tenta
 *  as duas grafias em vez de travar numa suposição só. */
function pick(row: Record<string, unknown>, ...chaves: string[]): unknown {
  for (const chave of chaves) {
    if (row[chave] !== undefined && row[chave] !== null) return row[chave];
  }
  return undefined;
}

function parseFotos(
  rows: Record<string, unknown>[],
  catalogo: Catalogo
): { rows: FotoRow[]; semMatch: string[] } {
  const agora = new Date().toISOString();
  const ordemPorSku = new Map<string, number>();
  // dim_produto_foto tem UNIQUE(sku, url) — mesma guarda de dedup que a planilha já precisava
  // (produto com mesma foto repetida em mais de 1 linha do card).
  const vistos = new Set<string>();
  const out: FotoRow[] = [];
  const semMatch = new Set<string>();

  for (const row of rows) {
    const skuBruto = String(pick(row, "sku", "Sku") ?? "").trim().toUpperCase();
    if (!skuBruto) continue;

    const barcode = String(pick(row, "barcode", "Barcode") ?? "").trim();
    const skuCanonico = catalogo.validos.has(skuBruto)
      ? skuBruto
      : (barcode && catalogo.eanParaSku.get(barcode)) || null;

    if (!skuCanonico) {
      semMatch.add(skuBruto);
      continue;
    }

    const imagemPrincipal = String(pick(row, "image_src", "Image Src") ?? "").trim();
    const imagemVariante = String(pick(row, "variant_image_url", "Variant Image URL") ?? "").trim();
    // Imagem principal primeiro (ordem 1); a da variante só entra se existir e for DIFERENTE da
    // principal — a maioria das linhas não tem imagem própria de variante (Shopify herda a do
    // produto), incluí-la sempre duplicaria a mesma foto como ordem 1 e 2 pra quase todo SKU.
    const candidatas = [imagemPrincipal, imagemVariante].filter((u, i, arr) => u !== "" && arr.indexOf(u) === i);

    for (const url of candidatas) {
      const chave = `${skuCanonico}|${url}`;
      if (vistos.has(chave)) continue;
      vistos.add(chave);

      const ordem = (ordemPorSku.get(skuCanonico) ?? 0) + 1;
      ordemPorSku.set(skuCanonico, ordem);

      out.push({ sku: skuCanonico, ordem, url, atualizado_em: agora });
    }
  }
  return { rows: out, semMatch: [...semMatch] };
}

/* ---------- Conteúdo de produto (descrição/modo de uso/composição) → produto_conteudo ----------
 * Passo NÃO crítico (02/out/2026): o texto alimenta o Agente de Vendas do WhatsApp, e falhar aqui
 * nunca pode impedir as fotos. Upsert por SKU (não trunca): SKU que some do card mantém o último
 * texto conhecido, que continua valendo para o produto. */
type ConteudoRow = {
  sku: string; marca: string | null; titulo: string | null; subtitulo: string | null; descricao: string | null;
  modo_de_uso: string | null; composicao: string | null; tipo: string | null; tags: string[] | null;
  url_loja: string | null; atualizado_em: string;
};

const ENTIDADES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };
/** HTML do Shopify → texto corrido (quebras viram linha, tags somem, entidades decodificadas). */
function textoLimpo(v: unknown, max = 4000): string | null {
  const bruto = String(v ?? "");
  if (!bruto.trim()) return null;
  const t = bruto
    .replace(/<\s*br\s*\/?>/gi, "\n").replace(/<\/\s*(p|li|h\d|div)\s*>/gi, "\n").replace(/<li[^>]*>/gi, "- ")
    .replace(/<[^>]+>/g, "")
    .replace(/&[a-z#0-9]+;/gi, (e) => ENTIDADES[e.toLowerCase()] ?? (/^&#\d+;$/.test(e) ? String.fromCharCode(Number(e.slice(2, -1))) : e))
    .replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  return t ? t.slice(0, max) : null;
}

function parseConteudo(rows: Record<string, unknown>[], catalogo: Catalogo): ConteudoRow[] {
  const agora = new Date().toISOString();
  const melhor = new Map<string, { row: ConteudoRow; peso: number }>();
  for (const row of rows) {
    const skuBruto = String(pick(row, "sku", "Sku") ?? "").trim().toUpperCase();
    if (!skuBruto) continue;
    const barcode = String(pick(row, "barcode", "Barcode") ?? "").trim();
    const sku = catalogo.validos.has(skuBruto) ? skuBruto : (barcode && catalogo.eanParaSku.get(barcode)) || null;
    if (!sku) continue;
    const descricao = textoLimpo(pick(row, "descricao", "Descricao")) ?? textoLimpo(pick(row, "body_html", "Body Html"));
    const tagsBrutas = pick(row, "tags", "Tags");
    const c: ConteudoRow = {
      sku,
      marca: textoLimpo(pick(row, "brand", "Brand"), 60),
      titulo: textoLimpo(pick(row, "product_title", "Product Title"), 300),
      subtitulo: textoLimpo(pick(row, "subtitulo", "Subtitulo"), 500),
      descricao,
      modo_de_uso: textoLimpo(pick(row, "modo_de_uso", "Modo De Uso"), 2000),
      composicao: textoLimpo(pick(row, "composicao", "Composicao"), 3000),
      tipo: textoLimpo(pick(row, "product_type", "Product Type"), 100),
      tags: Array.isArray(tagsBrutas) ? tagsBrutas.map(String).slice(0, 30)
        : typeof tagsBrutas === "string" && tagsBrutas.trim() ? tagsBrutas.split(",").map((x) => x.trim()).filter(Boolean).slice(0, 30) : null,
      url_loja: textoLimpo(pick(row, "online_store_url", "Online Store URL"), 500),
      atualizado_em: agora,
    };
    // Linha ATIVA ganha de rascunho/arquivada; entre iguais, a de mais texto.
    const ativo = String(pick(row, "status", "Status") ?? "").toLowerCase() === "active" ? 1_000_000 : 0;
    const peso = ativo + (c.descricao?.length ?? 0) + (c.modo_de_uso?.length ?? 0);
    const atual = melhor.get(sku);
    if (!atual || peso > atual.peso) melhor.set(sku, { row: c, peso });
  }
  return [...melhor.values()].map((m) => m.row).filter((r) => r.descricao || r.modo_de_uso || r.composicao);
}

async function gravarConteudo(rowsCard: Record<string, unknown>[], catalogo: Catalogo): Promise<void> {
  const rows = parseConteudo(rowsCard, catalogo);
  console.log(`[etl-produto-fotos] conteúdo: ${rows.length} SKUs com texto (descrição ${rows.filter((r) => r.descricao).length}, modo de uso ${rows.filter((r) => r.modo_de_uso).length})`);
  if (DRY || rows.length === 0) return;
  for (let i = 0; i < rows.length; i += 300) {
    const { error } = await supabase.from("produto_conteudo").upsert(rows.slice(i, i + 300), { onConflict: "sku" });
    if (error) throw new Error(`upsert produto_conteudo (chunk ${i}): ${error.message}`);
  }
}

async function truncate(): Promise<void> {
  const { error } = await supabase.from("dim_produto_foto").delete().neq("sku", "__nonexistent__");
  if (error) throw new Error(`Truncate dim_produto_foto: ${error.message}`);
}

async function bulkInsert(rows: FotoRow[]): Promise<void> {
  const BATCH = 500;
  for (let i = 0; i < rows.length; i += BATCH) {
    const chunk = rows.slice(i, i + BATCH);
    const { error } = await supabase.from("dim_produto_foto").insert(chunk);
    if (error) throw new Error(`Insert dim_produto_foto (chunk ${i}): ${error.message}`);
  }
}

/** dim_produto_foto.sku tem FK pra dim_produto.sku — o card traz SKU de kit/combo que não é
 *  produto cadastrado na Torre (ex.: código de kit tipo "KBS00038") e SKU numérico que não
 *  existe na Torre de jeito nenhum (Ápice — ver cabeçalho). Sem filtrar, 1 SKU inválido
 *  derruba o chunk de 500 inteiro. Carrega `ean` junto pro fallback por código de barras. */
async function carregarCatalogo(): Promise<Catalogo> {
  const validos = new Set<string>();
  const contagemPorEan = new Map<string, number>();
  const skuPorEan = new Map<string, string>();
  let from = 0;
  const PAGE = 1000;
  while (true) {
    const { data, error } = await supabase.from("dim_produto").select("sku, ean").range(from, from + PAGE - 1);
    if (error) throw new Error(`Consulta dim_produto: ${error.message}`);
    for (const r of (data ?? []) as { sku: string; ean: string | null }[]) {
      validos.add(r.sku);
      const ean = (r.ean ?? "").trim();
      if (!ean) continue;
      contagemPorEan.set(ean, (contagemPorEan.get(ean) ?? 0) + 1);
      skuPorEan.set(ean, r.sku);
    }
    if (!data || data.length < PAGE) break;
    from += PAGE;
  }

  // EAN que pertence a mais de 1 SKU em dim_produto não vira fallback — usá-lo às cegas
  // arriscaria colar a foto de um produto no cadastro de outro (achar a foto certa exige
  // saber QUAL dos SKUs é o dono do código de barras, e o dado não diz).
  const eanParaSku = new Map<string, string>();
  let ambiguos = 0;
  for (const [ean, sku] of skuPorEan) {
    if ((contagemPorEan.get(ean) ?? 0) > 1) { ambiguos++; continue; }
    eanParaSku.set(ean, sku);
  }
  if (ambiguos > 0) {
    console.warn(`[etl-produto-fotos] ${ambiguos} EAN duplicados em dim_produto (mais de 1 SKU) — fallback por código de barras pulado nesses casos.`);
  }

  return { validos, eanParaSku };
}

async function contarAtual(): Promise<number> {
  const { count, error } = await supabase.from("dim_produto_foto").select("sku", { count: "exact", head: true });
  if (error) throw new Error(`Contagem dim_produto_foto: ${error.message}`);
  return count ?? 0;
}

async function main() {
  const t0 = Date.now();
  console.log(`[etl-produto-fotos] baixando silver.stg_shopify_products (Data Mart, SQL nativo, paginado)...`);

  const [rowsCard, catalogo, atual] = await Promise.all([
    buscarTodasLinhas(SQL_FOTOS),
    carregarCatalogo(),
    contarAtual(),
  ]);
  console.log(`[etl-produto-fotos] ${rowsCard.length} linhas brutas da origem`);
  if (SO_CONTEUDO) {
    await gravarConteudo(rowsCard, catalogo);
    console.log(`[etl-produto-fotos] OK (só conteúdo) em ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    return;
  }
  const { rows, semMatch } = parseFotos(rowsCard, catalogo);

  if (rows.length === 0) {
    // Card vazio/inacessível não pode apagar o que já existe — aborta sem truncar.
    throw new Error(`0 linhas válidas extraídas de silver.stg_shopify_products — abortando sem tocar em dim_produto_foto (guarda contra fonte vazia/fora do ar apagar os dados).`);
  }

  if (semMatch.length > 0) {
    console.warn(`[etl-produto-fotos] ${semMatch.length} SKUs sem correspondência em dim_produto (nem por SKU nem por EAN) — puladas: ${semMatch.slice(0, 20).join(", ")}${semMatch.length > 20 ? "…" : ""}`);
  }

  const skusDistintos = new Set(rows.map((r) => r.sku)).size;
  console.log(`[etl-produto-fotos] ${rows.length} fotos, ${skusDistintos} SKUs distintos (tabela hoje: ${atual} linhas)`);

  // Fonte que perde massa em silencio nao pode apagar o que ja temos.
  if (atual > 0 && rows.length < atual * PISO_ENCOLHIMENTO) {
    const pct = ((1 - rows.length / atual) * 100).toFixed(1);
    const msg = `carga nova tem ${rows.length} linhas contra ${atual} ja gravadas (${pct}% menor, piso ${Math.round(PISO_ENCOLHIMENTO * 100)}%) — abortando sem tocar em dim_produto_foto. Se a queda for real e conferida, rode com ETL_FOTOS_FORCA=1.`;
    if (!FORCA) throw new Error(msg);
    console.warn(`[etl-produto-fotos] ATENCAO (ETL_FOTOS_FORCA=1): ${msg}`);
  }

  if (DRY) {
    await gravarConteudo(rowsCard, catalogo).catch((e) => console.warn(`[etl-produto-fotos] conteúdo (dry): ${e instanceof Error ? e.message : e}`));
    console.log("[etl-produto-fotos] ETL_FOTOS_DRY=1 — nada gravado.");
    return;
  }

  await truncate();
  await bulkInsert(rows);
  // Não crítico: fotos já gravadas; o texto falhar só deixa o da carga anterior.
  await gravarConteudo(rowsCard, catalogo).catch((e) => console.warn(`[etl-produto-fotos] conteúdo NÃO gravado: ${e instanceof Error ? e.message : e}`));

  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[etl-produto-fotos] OK — ${rows.length} linhas em ${sec}s`);
}

main().catch((e) => {
  console.error("[etl-produto-fotos] ERRO:", e instanceof Error ? e.message : e);
  process.exit(1);
});
