/**
 * Testes das regras puras do robô Desconto Boleto. Sem banco, sem rede.
 *   cd etl && npx tsx caso-desconto-boleto/teste.ts
 *
 * Os prazos esperados NÃO foram calculados por este código: são os que o PAINEL gravou em
 * etapas reais (occurrence_stage_states.sla_deadline, lidos em 29/set/2026). Se um dia o
 * painel mudar a régua de horário, estes testes quebram — que é o aviso certo.
 */
import assert from "node:assert/strict";
import {
  chaveNfPainel, chaveTorre, erroSemDados, etapaEntra, formatarCnpj, montarMensagem,
  nfInteiro, prazoHorasUteis,
} from "./regras.js";

let passou = 0;
function caso(nome: string, fn: () => void) {
  fn();
  passou++;
  console.log(`  ok  ${nome}`);
}

console.log("prazo em horário comercial (casos reais do painel):");
const prazo = (inicioUtc: string, h: number) => prazoHorasUteis(new Date(inicioUtc), h).toISOString();
caso("sexta 11h50 + 8h → segunda 10h50 (sexta fecha às 17h)", () =>
  assert.equal(prazo("2026-09-25T14:50:55.000Z", 8), "2026-09-28T13:50:55.000Z"));
caso("sexta 17h21 (já fechado) + 24h → quarta 12h00", () =>
  assert.equal(prazo("2026-09-25T20:21:39.242Z", 24), "2026-09-30T15:00:00.000Z"));
caso("quarta 13h49 + 24h → segunda 08h49 (atravessa a sexta curta)", () =>
  assert.equal(prazo("2026-09-23T16:49:25.000Z", 24), "2026-09-28T11:49:25.000Z"));
caso("terça 09h42 + 16h → quarta 15h42 (AVA-0014)", () =>
  assert.equal(prazo("2026-09-29T12:42:46.996Z", 16), "2026-09-30T18:42:46.996Z"));
caso("segunda 07h00 (antes do expediente) + 2h → segunda 10h00", () =>
  assert.equal(prazo("2026-09-28T10:00:00.000Z", 2), "2026-09-28T13:00:00.000Z"));
caso("sábado + 8h → segunda 16h00", () =>
  assert.equal(prazo("2026-09-26T15:00:00.000Z", 8), "2026-09-28T19:00:00.000Z"));
caso("quinta 17h00 + 1h → quinta 18h00 (último minuto do dia conta)", () =>
  assert.equal(prazo("2026-10-01T20:00:00.000Z", 1), "2026-10-01T21:00:00.000Z"));

console.log("etapas que entram no fluxo (inclusion_condition do painel):");
const aprovacao = { op: "eq", field: "tipo_alteracao", value: "desconto" };
const atualizacao = { op: "neq", field: "aprovacao_resultado", value: "nao_aprovado" };
caso("sem condição → entra", () => assert.equal(etapaEntra(null, {}), true));
caso("Aprovação do Desconto entra quando tipo = desconto", () =>
  assert.equal(etapaEntra(aprovacao, { tipo_alteracao: "desconto" }), true));
caso("Aprovação do Desconto NÃO entra quando tipo = prorrogar", () =>
  assert.equal(etapaEntra(aprovacao, { tipo_alteracao: "prorrogar" }), false));
caso("Atualização de Boletos entra antes de existir resultado de aprovação", () =>
  assert.equal(etapaEntra(atualizacao, {}), true));
caso("operador desconhecido → erro (falha fechando)", () =>
  assert.throws(() => etapaEntra({ op: "gt", field: "x", value: 1 }, { x: 2 })));

console.log("NF e chaves:");
caso("NF com zeros → inteiro", () => assert.equal(nfInteiro("000001234"), 1234));
caso("NF vazia → null", () => assert.equal(nfInteiro(""), null));
caso("chave do painel no formato P-ES-1234", () => assert.equal(chaveNfPainel("ES", "000001234"), "P-ES-1234"));
caso("chave da Torre por pedido", () => assert.equal(chaveTorre("RJ", 1156), "torre:desconto-boleto:RJ:1156"));
caso("CNPJ mascarado", () => assert.equal(formatarCnpj("11222333000181"), "11.222.333/0001-81"));

console.log("mensagem:");
caso("preenche os marcadores e põe a referência no rodapé", () => {
  const m = montarMensagem("Pedido {regiao} {pedido} · NF {nf} · {desconto} · {valor} · {data_pedido} · {x}", {
    regiao: "ES", pedido: 5678, nf: "000001234", desconto: "Desconto 10%", cliente: "C", cnpj: "11222333000181",
    vendedor: null, condicao: null, valor: 1500.5, dataPedido: "2026-09-28", dataNf: null, data1Parcela: null,
  }, "torre:desconto-boleto:ES:5678");
  assert.match(m, /^Pedido ES 5678 · NF 1234 · Desconto 10% · R\$\s?1\.500,50 · 28\/09\/2026 · \{x\}/);
  assert.match(m, /ref torre:desconto-boleto:ES:5678$/);
});

console.log("log sem dado de cliente:");
caso("erro de chave duplicada vira só código + restrição (o CNPJ não vaza)", () => {
  const s = erroSemDados({ code: "23505", message: 'duplicate key value violates unique constraint "clientes_cnpj_key"', details: "Key (cnpj)=(11222333000181) already exists." });
  assert.equal(s, "23505 clientes_cnpj_key");
  assert.doesNotMatch(s, /\d{14}/);
});

console.log(`\n${passou} testes passaram.`);
