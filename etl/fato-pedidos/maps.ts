// Constantes extraídas dos code nodes trat_protheus e trat_tiny do N8N
// Centralizadas aqui para evitar duplicação

export const PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000";
export const PLACEHOLDER_CNPJ = "00000000000000";
export const PLACEHOLDER_PROD = "0000000";

/** Mapa de nome normalizado da filial → UUID no Supabase */
export const mapFiliais: Record<string, string> = {
  cdes: "f02c3dba-ef48-4ea6-a295-9d7a88e3c7bd",
  cdsp: "0b17bfcb-481b-425c-baf7-3bf2752bce14",
  cdrj: "b10929b7-9831-4221-a08c-bfda145a079d",
};

/** Mapa de nome normalizado da filial Tiny → erp_origem */
export const mapTinyErpOrigem: Record<string, string> = {
  cdes: "tiny_es",
  cdsp: "tiny_sp",
  cdrj: "tiny_rj",
};

/** Mapa de nome normalizado da marca → abreviação */
export const mapMarcas: Record<string, string> = {
  apice: "AP",
  barbours: "BB",
  bysamia: "BS",
  kokeshi: "KS",
  lescent: "LC",
  rituaria: "RT",
  aua: "AU",
  yenzah: "YE",
};

/**
 * Override de marca por SKU canônico — corrige cadastros errados na origem.
 * Aplicado APÓS o de-para de SKU (skuFixMap), pois sobrescreve só a marca final.
 * Ex: Body Splash Barbours vendidos no Tiny ES sob a marca "Apice" (SKU AP209xx).
 */
export const marcaOverride: Record<string, string> = {
  BB02038: "BB", // DEO BD SPL VERY SEXY 200ml — Barbours, vinha como Apice
  BB02023: "BB", // DEO BD SPL GOOD GRACES 200ml
  BB02027: "BB", // DEO BD SPL MY SWEET DELIGHT 200ml
};

/**
 * De-para Tiny → SKU canônico, indexado por (codigo, marca).
 *
 * Indexar por marca é obrigatório: SKUs numéricos Tiny são reutilizados
 * entre marcas (ex: 20764 = COND MANTEIGA na Apice E DEO BD SPL na Barbours).
 *
 * 147 entries — 142 Apice via fuzzy match (>= 0.75 score em
 * descricao_produto ↔ dim_produto.nome) + 5 Barbours legados.
 * Histórico mapeado: R$ 35,7M / 99,3% do faturamento '0000000'.
 *
 * Próxima onda: 16 SKUs MÉDIA+BAIXA confiança em etl/dev/skus-fuzzy-match.csv.
 */
export const skuFixMap: Record<string, Record<string, string>> = {
  "10083": { AP: "AP01085" },
  "10277": { AP: "AP01090" },
  "10483": { AP: "AP99042" },
  "10534": { AP: "AP99020" },
  "10535": { AP: "AP99021" },
  "10536": { AP: "AP99052" },
  "20004": { AP: "AP01013" },
  "20014": { AP: "AP99050" },
  "20015": { AP: "AP99050" },
  "20016": { AP: "AP99050" },
  "20036": { AP: "AP01022" },
  "20038": { AP: "AP01023" },
  "20039": { AP: "AP01037" },
  "20040": { AP: "AP01038" },
  "20043": { AP: "AP99035" },
  "20049": { AP: "AP99043" },
  "20051": { AP: "AP01030" },
  "20052": { AP: "AP01029" },
  "20077": { AP: "AP01044" },
  "20078": { AP: "AP01045" },
  "20080": { AP: "AP01048" },
  "20081": { AP: "AP01049" },
  "20258": { AP: "AP01056" },
  "20260": { AP: "AP01057" },
  "20261": { AP: "AP01058" },
  "20262": { AP: "AP99024" },
  "20269": { AP: "AP99058" },
  "20272": { AP: "AP01071" },
  "20273": { AP: "AP01069" },
  "20313": { AP: "AP99068" },
  "20314": { AP: "AP01082" },
  "20321": { AP: "AP01099" },
  "20322": { AP: "AP01100" },
  "20323": { AP: "AP01103" },
  "20324": { AP: "AP01103" },
  "20325": { AP: "AP01103" },
  "20328": { AP: "AP99033" },
  "20333": { AP: "AP99066" },
  "20351": { AP: "AP99034" },
  "20482": { AP: "AP99067" },
  "20485": { AP: "AP01008" },
  "20486": { AP: "AP01008" },
  "20487": { AP: "AP01028" },
  "20488": { AP: "AP01028" },
  "20490": { AP: "AP01043" },
  "20491": { AP: "AP01043" },
  "20492": { AP: "AP01055" },
  "20497": { AP: "AP01098" },
  "20498": { AP: "AP01098" },
  "20502": { AP: "AP01013" },
  "20503": { AP: "AP01090" },
  "20504": { AP: "AP01032" },
  "20505": { AP: "AP01028" },
  "20526": { AP: "AP99001" },
  "20540": { AP: "AP01086" },
  "20551": { AP: "AP01106" },
  "20552": { AP: "AP01073" },
  "20558": { AP: "AP01076" },
  "20559": { AP: "AP01077" },
  "20563": { AP: "AP01036" },
  "20564": { AP: "AP01034" },
  "20565": { AP: "AP01074" },
  "20566": { AP: "AP01042" },
  "20567": { AP: "AP01064" },
  "20568": { AP: "AP01063" },
  "20569": { AP: "AP01075" },
  "20570": { AP: "AP01109" },
  "20571": { AP: "AP01108" },
  "20580": { AP: "AP01021" },
  "20581": { AP: "AP99063" },
  "20582": { AP: "AP99037" },
  "20585": { AP: "AP01006" },
  "20586": { AP: "AP01007" },
  "20587": { AP: "AP01012" },
  "20588": { AP: "AP01029" },
  "20591": { AP: "AP99062" },
  "20623": { AP: "AP99061" },
  "20625": { AP: "AP03001" },
  "20626": { AP: "AP99039" },
  "20628": { AP: "AP03002" },
  "20649": { AP: "AP01110" },
  "20650": { AP: "AP01081" },
  "20658": { AP: "AP99047" },
  "20659": { AP: "AP99065" },
  "20670": { AP: "AP01065" },
  "20674": { AP: "AP01067" },
  "20675": { AP: "AP99023" },
  "20676": { AP: "AP99036" },
  "20677": { AP: "AP01084" },
  "20678": { AP: "AP01105" },
  "20679": { AP: "AP99031" },
  "20680": { AP: "AP01111" },
  "20681": { AP: "AP01083" },
  "20683": { AP: "AP01025" },
  "20684": { AP: "AP01059" },
  "20685": { AP: "AP01060" },
  "20686": { AP: "AP01070" },
  "20687": { AP: "AP01101" },
  "20688": { AP: "AP01102" },
  "20689": { AP: "AP01010" },
  "20690": { AP: "AP01011" },
  "20692": { AP: "AP01066" },
  "20693": { AP: "AP01020" },
  "20694": { AP: "AP01088" },
  "20696": { AP: "AP01046" },
  "20697": { AP: "AP01047" },
  "20698": { AP: "AP01024" },
  "20729": { AP: "AP01015" },
  "20732": { AP: "AP01054" },
  "20733": { AP: "AP01016" },
  "20734": { AP: "AP01107" },
  "20735": { AP: "AP01062" },
  "20736": { AP: "AP01050" },
  "20737": { AP: "AP01072" },
  "20738": { AP: "AP01017" },
  "20739": { AP: "AP01078" },
  "20740": { AP: "AP01079" },
  "20755": { AP: "AP99062" },
  "20756": { AP: "AP01005" },
  "20760": { AP: "AP01095", BB: "BB02075" },
  "20761": { BB: "BB02074" },
  "20762": { BB: "BB02071" },
  "20763": { BB: "BB02073" },
  "20764": { AP: "AP01053", BB: "BB02072" },
  "20765": { AP: "AP01052" },
  "25127": { AP: "AP99024" },
  "35129": { AP: "AP99047" },
  "35131": { AP: "AP99055" },
  "35134": { AP: "AP99064" },
  "35135": { AP: "AP99057" },
  "35142": { AP: "AP99032" },
  "35147": { AP: "AP99056" },
  "35148": { AP: "AP99040" },
  "35149": { AP: "AP99054" },
  "35150": { AP: "AP99046" },
  "35151": { AP: "AP99025" },
  "35152": { AP: "AP99026" },
  "35153": { AP: "AP99027" },
  "35154": { AP: "AP99038" },
  "35155": { AP: "AP99041" },
  "35156": { AP: "AP99029" },
  "35157": { AP: "AP99028" },
  "35158": { AP: "AP99055" },
  "35159": { AP: "AP99041" },
  "35160": { AP: "AP99048" },
  "60007": { AP: "AP99060" },
  "60010": { AP: "AP99030" },
  // Acessórios / brindes Ápice — adicionados jun/2026
  "60006": { AP: "AP60006" },
  "60009": { AP: "AP60009" },
  "60014": { AP: "AP60014" },
  "60015": { AP: "AP60015" },
  // Cosméticos Ápice sem mapeamento anterior — adicionados jun/2026
  "10481": { AP: "AP10481" },
  "10530": { AP: "AP10530" },
  "10531": { AP: "AP10531" },
  "15128": { AP: "AP15128" },
  "16002": { AP: "AP16002" },
  "20493": { AP: "AP20493" },
  "20521": { AP: "AP20521" },
  "20527": { AP: "AP20527" },
  "20536": { AP: "AP20536" },
  // Linha REFIL 300ml — adicionados jun/2026
  "20900": { AP: "AP20900" },
  "20901": { AP: "AP20901" },
  "20902": { AP: "AP20902" },
  // Linha ÓLEO 60ml — adicionados jun/2026
  "20906": { AP: "AP20906" },
  "20907": { AP: "AP20907" },
  "20908": { AP: "AP20908" },
  "20909": { AP: "AP20909" },
  "20910": { AP: "AP20910" },
  "20911": { AP: "AP20911" },
  // Perfume Amber + DEO BD SPL
  "20595": { AP: "AP20595" },
  // DEO BD SPL — mesmo caso do marcaOverride: produto é Barbours, catálogo não tem
  // SKU "AP209xx" (nunca existiu) — o SKU real é o BB0xxxx correspondente.
  "20903": { AP: "BB02038" }, // DEO BD SPL VERY SEXY 200ml
  "20904": { AP: "BB02023" }, // DEO BD SPL GOOD GRACES 200ml
  "20905": { AP: "BB02027" }, // DEO BD SPL MY SWEET DELIGHT 200ml
  // Eletrônicos
  "50000": { AP: "AP50000" },

  // Achados na varredura de produto_id='0000000' (26/jul/2026) — códigos Tiny sem
  // de-para, que caíam no placeholder toda vez que o produto era vendido de novo.
  // Onde já existia um cadastro "ERRO-AP-<codigo>" (patch manual antigo que nunca
  // fechou o loop aqui), aponta pra ele em vez de criar duplicata.
  "10054": { AP: "ERRO-AP-10054" },
  "16003": { AP: "ERRO-AP-16003" },
  "20159": { AP: "ERRO-AP-20159" }, // Necessaire poliamida
  "20528": { AP: "ERRO-AP-20528" }, // Hidratante Shine
  "20537": { AP: "ERRO-AP-20537" }, // Kit Saches Sortidos 4un
  "20538": { AP: "ERRO-AP-20538" }, // Kit saches Sortidos 2un
  "20539": { AP: "ERRO-AP-20539" }, // Kit saches Sortidos 7un
  "20593": { AP: "ERRO-AP-20593" }, // Perfume capilar Toranja
  "20594": { AP: "ERRO-AP-20594" }, // Perfume capilar Cristal
  "35143": { AP: "ERRO-AP-35143" }, // Sacola Grande
  "50005": { AP: "ERRO-AP-50005" }, // Folders
  // Duplicatas — mesmo produto ganhou SKU novo em cada patch manual anterior
  // (AP99001/99002/99003 = mesma "Escova Flex Quadrada"); aponta pro mais usado.
  "20660": { AP: "AP99003" },
  "25159": { AP: "AP99007" }, // Escova massageadora
  // Genuinamente novos (nunca cadastrados) — dim_produto criado na mesma varredura.
  "10276": { AP: "AP01115" }, // Óleo Vegetal de Jojoba 30 ml
  "60000": { AP: "AP99069" }, // Brinde - Camisa P
  "60001": { AP: "AP99070" }, // Brinde - Camisa M
  "60003": { AP: "AP99071" }, // Brinde - Camisa G
  "35233": { AP: "AP99072" }, // ESC PIRULITO 4018 DISP BRASIL 9UN
  "35234": { AP: "AP99073" }, // ESC PIRULITO 4018 CORAL BRASIL (REFIL)
  "35235": { AP: "AP99074" }, // ESC PIRULITO 4018 VERDE BRASIL (REFIL)
  "35236": { AP: "AP99075" }, // ESC PIRULITO 4018 DOURADO BRASIL (REFIL)
};

/** Campos numéricos que precisam de normalização BR → decimal */
export const camposNumericos = [
  "quantidade",
  "valor_unitario",
  "valor_total",
  "valor_desconto",
  "valor_frete",
] as const;
