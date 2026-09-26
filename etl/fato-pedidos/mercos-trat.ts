/**
 * Tratativa de pedido Mercos — fonte suplementar de `protheusTrat` (card 19611).
 *
 * ── POR QUE EXISTE ───────────────────────────────────────────────────────────
 * Irmão de `mercos-pipeline.ts`. Aquele repõe o card **19610** (`protheusPedidos`,
 * o pipeline); este repõe o **19611** (`protheusTrat`, o ENRIQUECIMENTO do faturado).
 * Os dois leem `ecommerce.so_header` no **db 48 "Middleware Hop"**, que em
 * **25/09/2026 ~23h UTC** parou de aceitar conexão — não é card quebrado, é o banco:
 * medido em 26/09, `SELECT 1` falha 3 vezes em 3, e o **db 53 "Middleware Broker"**
 * caiu junto (dois dos três middlewares ⇒ host ou rede em comum). Como as duas fontes
 * são `critical` no `extract.ts`, a carga inteira aborta e o `fato_pedidos` congela —
 * ficou parado em 25/09 22h17 até este módulo existir.
 *
 * ⚠️ **Abortar era o comportamento CERTO, e por isso este módulo repõe em vez de
 * afrouxar.** Medido nos 30 campos que o `protheusFat` (card 19609, db 47, no ar)
 * entrega sozinho: 24 vêm a 100% e **cinco vêm a ZERO** — `data_pedido`,
 * `forma_pagamento`, `parcelas`, `id_vendedor` e `nome_vendedor`. Sem `nome_vendedor`
 * toda venda Protheus cairia no sentinela e desmontaria comissão; sem `forma_pagamento`
 * a bonificação voltaria a contar como venda (o bug de `20260714`). *Número errado é
 * pior que número velho* — então a saída não é ignorar o card, é ter outra fonte.
 *
 * ── A FONTE ──────────────────────────────────────────────────────────────────
 * `mercos_vendas_detalhadas` (Supabase, alimentada por automação própria a partir do
 * Mercos). Medida em 26/09 na janela de 60 dias: **vendedor em 100%**,
 * `condicao_pagamento` em 73%, atualizada no mesmo minuto da medição, e casando com
 * **96% (2.072 de 2.162)** dos pedidos Protheus faturados pela chave do card.
 *
 * ⚠️ **É a MESMA grandeza que o card 19611 traz, não um substituto aproximado.** O card
 * deriva `id_vendedor` de `e.so_criador_id` — *quem criou o pedido no Mercos* —, que é
 * exatamente o que `mercos_vendas_detalhadas.vendedor` guarda. E `forma_pagamento` e
 * `parcelas` são AMBOS derivados de `e.so_condicao_pagamento`, que é o
 * `condicao_pagamento` desta tabela, no mesmo formato (`'058 - BOLETO 30/45/60'`).
 * Por isso a derivação abaixo é **porte literal do SQL do card**, não regra nova.
 *
 * ⚠️ **Isto NÃO faz o Mercos passar na frente da carteira**, e é o que concilia com
 * `docs/modulos/dupla-validacao-vendedor.md` ("o oficial sempre vence; o Mercos é
 * fallback e às vezes traz o analista que lançou, não o RCA"). A precedência de
 * `normalizeProtheus` é:
 *     cotitular  ||  vendedor_id_carteira (O OFICIAL)  ||  nome_vendedor  ||  placeholder
 * `nome_vendedor` já era o **terceiro** da fila — este módulo troca a fonte dele mantendo
 * a posição. Nada sobe de prioridade.
 *
 * ── LIMITES DECLARADOS ───────────────────────────────────────────────────────
 * ⚠️ `id_vendedor` sai **vazio**: o Mercos não expõe o `so_criador_id` nesta tabela.
 * Medido antes de aceitar: no trilho Protheus **ninguém consome** essa coluna (os únicos
 * usos de `id_vendedor` no ETL estão em `extract-tiny-apice.ts`, outro trilho). Se algum
 * dia passar a ser consumida, este é o primeiro lugar a olhar.
 * ⚠️ Os **4% que não casam** são três coisas, medidas: 49 pedidos de cliente que **nunca**
 * passa pelo Mercos (Amazon, SENAC, canal direto — correto não ter), 39 do caso já
 * documentado em que `pedido_mercos` veio vazio na extração e o ETL caiu no
 * `pedido_protheus` (ex.: VEMAC `004259`/`004276`), e 2 cujo `pedido_id` é texto
 * (`REMESSA PARA DESCARTE`). Nenhum deles perde dinheiro: sem `nome_vendedor` eles caem
 * na carteira do cliente, que é o mesmo destino de hoje quando o Mercos não cobre.
 * ⚠️ **SP fica fora** — o Mercos é ES/RJ (a própria `REGIAO_FILIAL` do módulo irmão só
 * mapeia os dois). Pedido SP nunca teve linha no 19611 vinda do Mercos.
 *
 * ── DESLIGA SOZINHO ──────────────────────────────────────────────────────────
 * Só gera a chave que o card NÃO trouxe. Quando o db 48 voltar, o card volta a trazer
 * tudo, `ja_no_card` cobre 100% e este módulo passa a gerar zero linha sem ninguém
 * mexer em nada. `ETL_MERCOS_TRAT=false` desliga à mão.
 */

import { logger } from "../shared/logger.js";
import { fetchSupabaseTable } from "../shared/supabase-admin.js";
import { janelaProtheus } from "./janela-protheus.js";

/**
 * Região do Mercos → filial da Torre.
 *
 * ⚠️ **SP ENTRA AQUI e NÃO entra no `mercos-pipeline.ts` — a diferença é de natureza, não
 * descuido.** Lá o assunto é **pedido em aberto**, e SP não coloca pedido no Mercos (1 em
 * toda a base), então incluí-lo geraria pipeline que não existe. Aqui o assunto é
 * **enriquecer nota já faturada**, e SP tem histórico no Mercos: 1.458 linhas.
 *
 * Copiar a constante do irmão sem medir custou uma regressão: com SP de fora sobravam
 * **7.500 linhas** de faturado Protheus sem `forma_pagamento` (R$ 8,15 mi), e medido,
 * **7.457 delas (99,4%) têm par no Mercos** — era só não ter excluído.
 *
 * ⚠️ O histórico de SP no Mercos vai de **24/01 a 01/06/2026** e para aí. As ~44 linhas
 * faturadas depois disso ficam sem forma de pagamento por **ausência na origem**, não por
 * recorte nosso — e é isso que a contagem `sem_regiao` do diagnóstico declara.
 */
const REGIAO_FILIAL: Record<string, string> = { ES: "CD ES", RJ: "CD RJ", SP: "CD SP" };

interface LinhaMercos {
  numero_pedido: number | string | null;
  regiao: string | null;
  data_emissao: string | null;
  vendedor: string | null;
  condicao_pagamento: string | null;
}

export interface ResultadoTratMercos {
  linhas: Record<string, unknown>[];
  diagnostico: {
    mercos_lidos: number;
    na_janela: number;
    ja_no_card: number;
    sem_regiao: number;
    gerados: number;
    com_vendedor: number;
    com_forma_pagamento: number;
  };
}

/**
 * Porte LITERAL do `CASE` do card 19611 sobre `so_condicao_pagamento`:
 *
 * ```sql
 * forma_pagamento = CASE WHEN c LIKE '% - % %'
 *                        THEN SPLIT_PART(TRIM(SPLIT_PART(c,' - ',2)),' ',1)
 *                        ELSE TRIM(SPLIT_PART(c,' - ',2)) END
 * parcelas        = CASE WHEN c LIKE '% - % %'
 *                        THEN SUBSTRING(TRIM(SPLIT_PART(c,' - ',2)),
 *                                       POSITION(' ' IN TRIM(SPLIT_PART(c,' - ',2))) + 1)
 *                        ELSE NULL END
 * ```
 *
 * ⚠️ `SPLIT_PART(c,' - ',2)` pega o **segundo campo**, não "o resto" — em
 * `'A - B - C'` devolve `'B'`. Por isso `split(" - ")[1]`, nunca `slice` a partir do
 * primeiro traço: a diferença só aparece em condição com dois separadores, que é
 * exatamente o caso em que o erro passaria despercebido.
 *
 * ⚠️ `SPLIT_PART` devolve **string vazia** quando não há o separador (não NULL), e é por
 * isso que `'PIX'` sem prefixo vira `forma=''` e não `'PIX'` — o card se comporta assim
 * e a paridade importa mais que a aparência.
 *
 * Exemplos reais medidos: `'011 - BONIFICAÇÃO'` → (`BONIFICAÇÃO`, null) ·
 * `'067 - PIX'` → (`PIX`, null) · `'058 - BOLETO 30/45/60'` → (`BOLETO`, `30/45/60`) ·
 * `'062 - CRÉDITO 3X'` → (`CRÉDITO`, `3X`).
 */
export function derivaPagamento(cond: string): { forma: string; parcelas: string | null } {
  const parte2 = (cond.split(" - ")[1] ?? "").trim();

  // LIKE '% - % %' — existe ' - ' e, DEPOIS dele, algum espaço.
  const i = cond.indexOf(" - ");
  const temEspacoDepois = i >= 0 && cond.indexOf(" ", i + 3) >= 0;
  if (!temEspacoDepois) return { forma: parte2, parcelas: null };

  const sp = parte2.indexOf(" ");
  // POSITION devolve 0 quando não acha, e SUBSTRING(x FROM 1) é a string inteira.
  if (sp < 0) return { forma: parte2, parcelas: parte2 };
  return { forma: parte2.slice(0, sp), parcelas: parte2.slice(sp + 1) };
}

/** `'CD ES-6392'` — sem zero à esquerda, que é como o `protheusFat` escreve a chave. */
function chave(regiao: string | null, numero: number | string | null): string | null {
  const cd = REGIAO_FILIAL[String(regiao ?? "").trim().toUpperCase()];
  if (!cd) return null;
  const n = String(numero ?? "").trim().replace(/^0+(?=\d)/, "");
  if (!n) return null;
  return `${cd}-${n}`;
}

export async function montarTratMercos(
  tratDoCard: Record<string, unknown>[],
  diasJanela = 60,
): Promise<ResultadoTratMercos> {
  // 🔴 A JANELA SEGUE O ESCOPO DO ETL, NÃO É FIXA — e isto custou uma regressão medida.
  //
  // A 1ª versão usava 60 d sempre, copiando o módulo irmão. No **intraday** o escopo do
  // Protheus também é 60 d, então cobria tudo e o resultado foi ótimo (`forma_pagamento`
  // de 59% → 96%). No **full** o escopo é o ANO INTEIRO e o `DELETE` reescreve tudo desde
  // 01/jan: as ~10 meses fora dos 60 d ficavam sem `protheusTrat` e **perdiam o
  // `forma_pagamento` que o card preenchia**. Medido no 1º full: **98% → 31%**, com
  // **39.518 linhas** de faturado antigo zeradas.
  //
  // ⚠️ *Suplemento que repõe fonte tem de cobrir o MESMO recorte que o `DELETE` apaga.*
  // Cobrir menos não deixa buraco onde não havia dado — apaga dado que havia.
  //
  // ⚠️ O dado existe: `mercos_vendas_detalhadas` vai de **24/01/2026** até hoje (12.342
  // linhas), então o ano inteiro está lá. Era só eu não ter recortado.
  //
  // ⚠️ **NÃO fazer o mesmo no `mercos-pipeline.ts`**, e a diferença é de natureza: aquele
  // CRIA linha de pedido, então janela maior que a do `DELETE` deixa órfão vivo para
  // sempre (a nota longa lá explica). Este só ENRIQUECE linha que o `protheusFat` já
  // trouxe — não cria nada, e por isso alargar é seguro.
  const jp = janelaProtheus();
  const corte = jp.ativa
    ? new Date(Date.now() - Math.min(diasJanela, jp.dias) * 86_400_000).toISOString().slice(0, 10)
    // full: `corteInt` é o YYYYMMDD do início do escopo (20260101) — a MESMA constante que
    // o `load.ts` usa no DELETE, para as duas contas não poderem divergir.
    : `${String(jp.corteInt).slice(0, 4)}-${String(jp.corteInt).slice(4, 6)}-${String(jp.corteInt).slice(6, 8)}`;
  const diasEfetivo = jp.ativa ? Math.min(diasJanela, jp.dias) : 0;

  const mercos = (await fetchSupabaseTable(
    "mercos_vendas_detalhadas",
  )) as unknown as LinhaMercos[];

  // Chaves que o card JÁ trouxe — o suplemento nunca sobrescreve o middleware.
  const jaNoCard = new Set(
    tratDoCard
      .map((r) => String(r.filial_pedido ?? "").trim())
      .filter(Boolean),
  );

  const diag: ResultadoTratMercos["diagnostico"] = {
    mercos_lidos: mercos.length,
    na_janela: 0,
    ja_no_card: 0,
    sem_regiao: 0,
    gerados: 0,
    com_vendedor: 0,
    com_forma_pagamento: 0,
  };

  const linhas: Record<string, unknown>[] = [];
  const vistos = new Set<string>();

  for (const m of mercos) {
    const data = String(m.data_emissao ?? "").slice(0, 10);
    if (!data || data < corte) continue;
    diag.na_janela++;

    const k = chave(m.regiao, m.numero_pedido);
    if (!k) { diag.sem_regiao++; continue; }
    if (jaNoCard.has(k)) { diag.ja_no_card++; continue; }
    // A tabela é grão de PEDIDO, mas defende-se de duplicata mesmo assim: o `mergeEnrich`
    // monta um Map por chave e a última linha venceria em silêncio.
    if (vistos.has(k)) continue;
    vistos.add(k);

    const { forma, parcelas } = derivaPagamento(String(m.condicao_pagamento ?? ""));
    const vendedor = String(m.vendedor ?? "").trim();

    linhas.push({
      filial_pedido: k,
      data_pedido: data,
      // ⚠️ Vazio de propósito — ver "LIMITES DECLARADOS" no topo.
      id_vendedor: "",
      nome_vendedor: vendedor,
      forma_pagamento: forma,
      parcelas,
    });

    diag.gerados++;
    if (vendedor) diag.com_vendedor++;
    if (forma) diag.com_forma_pagamento++;
  }

  logger.info(
    `Trat Mercos (suplemento do card 19611): ${diag.gerados} linha(s) geradas ` +
      `(escopo ${diasEfetivo ? `${diasEfetivo}d` : "FULL"} desde ${corte}; ${diag.ja_no_card} já no card, ` +
      `${diag.com_vendedor} com vendedor, ${diag.com_forma_pagamento} com forma de pagamento)`,
  );

  return { linhas, diagnostico: diag };
}
