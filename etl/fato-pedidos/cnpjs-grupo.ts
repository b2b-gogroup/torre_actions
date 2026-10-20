/**
 * CNPJs de empresas DO PRÓPRIO GRUPO — venda para elas é transferência (intercompany), não receita.
 *
 * Lista ÚNICA do ETL (14/out/2026). Antes eram duas cópias divergentes —
 * `CNPJS_INTERNOS` (extract-tiny-apice.ts, 22 CNPJs, só Tiny) e `CNPJS_GRUPO` (transform.ts,
 * 5 CNPJs, Protheus + Tiny) — e nenhuma tinha a BEAUTY HUB VAREJO (raiz 60453162). A venda dela é
 * Protheus, só a lista do transform podia pegá-la, e ela entrou no Gerencial como 1º cliente de
 * out/2026 (R$ 21.314,54, 93% do Top Clientes).
 *
 * ⚠️ A FONTE NO BANCO é `cliente_interno` (ignorar_venda = true), aplicada por
 * `fn_aplica_cliente_interno()` a cada carga e por cron — ela marca `excluido = true` no que já está
 * gravado e reverte se a regra sair. Esta lista é só o corte ANTECIPADO (evita buscar detalhe caro de
 * NF no Tiny e evita gravar a linha). Empresa nova do grupo: entra AQUI e em `cliente_interno`
 * (migration `20261014x`) — se entrar só no banco, ela ainda é extraída mas fica excluída; se entrar
 * só aqui, as linhas antigas dela continuam contando.
 *
 * MAGA (38246589, unidades 40001/40002/40005/40004 no WebGex) e RITU PARTNERS (40003) entraram em
 * 18/ago/2026: a MAGA aparecia com 48,5% de share no ranking, 2 das 3 NFs pelo Protheus.
 */
export const CNPJS_GRUPO: ReadonlySet<string> = new Set([
  // BEAUTY HUB VAREJO (faltava até 14/out/2026)
  "60453162000107", "60453162000298", "60453162000379",
  // BEAUTY HUB ATACADO
  "60453002000168", "60453002000249", "60453002000320",
  // AP COSMETICS
  "48290289000157", "48290289000238", "48290289000319",
  // BB VAREJO (0001-35 = matriz)
  "54137817000135", "54137817000216", "54137817000305", "54137817000488",
  // LESCENT VAREJO
  "57344563000114", "57344563000203",
  // APICE COSMETICS
  "26301600000183", "26301600000264", "26301600000345",
  // KOKESHI VAREJO
  "58181480000114",
  // sem cadastro em dim_cliente, herdados da lista antiga do Tiny
  "54190174000355", "46803642000120",
  // MAGA COMERCIO + RITU PARTNERS
  "38246589000185", "38246589000266", "38246589000347", "38246589000428",
  "58323315000150",
]);
