/**
 * Regras PURAS do robô "Desconto Boleto → caso no painel" (sem banco, sem rede).
 *
 * Ficam separadas do fluxo (`index.ts`) para serem testadas isoladamente em `teste.ts`:
 * cada regra aqui foi MEDIDA contra o que o painel de casos (CRM "Devoluções Conectadas")
 * grava quando uma pessoa abre o caso pela tela — o robô tem de gravar igual.
 */

// ── Prazo (SLA) em horário comercial ─────────────────────────────────────────────
// Medido em 29/set/2026 contra os prazos gravados pelo painel: 61 de 61 etapas abertas desde
// 01/09 batem com seg–qui 08h–18h e SEX 08h–17h, horário de Brasília, sem pular feriado.
// (Agosto seguia outra regra — as 4 divergências restantes são de antes de 01/09.)
// Brasília não tem horário de verão desde 2019, então o fuso fixo −03:00 é exato.

const BRT_MS = -3 * 3600 * 1000;

function paraBrt(d: Date): Date {
  // "relógio de parede" de Brasília guardado num Date em UTC (só para calcular dia/hora)
  return new Date(d.getTime() + BRT_MS);
}
function deBrt(d: Date): Date {
  return new Date(d.getTime() - BRT_MS);
}
function inicioDoDia(p: Date): Date {
  return new Date(Date.UTC(p.getUTCFullYear(), p.getUTCMonth(), p.getUTCDate(), 8, 0, 0, 0));
}
function fimDoDia(p: Date): Date {
  const hora = p.getUTCDay() === 5 ? 17 : 18; // sexta fecha às 17h
  return new Date(Date.UTC(p.getUTCFullYear(), p.getUTCMonth(), p.getUTCDate(), hora, 0, 0, 0));
}
function proximoDia8h(p: Date): Date {
  return new Date(Date.UTC(p.getUTCFullYear(), p.getUTCMonth(), p.getUTCDate() + 1, 8, 0, 0, 0));
}

/** Prazo = `inicio` + `horas` úteis (seg–qui 8–18h, sex 8–17h, horário de Brasília). */
export function prazoHorasUteis(inicio: Date, horas: number): Date {
  let t = paraBrt(inicio);
  let restoMs = horas * 3600 * 1000;
  for (let guarda = 0; guarda < 400; guarda++) {
    const dow = t.getUTCDay();
    if (dow === 0 || dow === 6) { t = proximoDia8h(t); continue; }
    const ini = inicioDoDia(t);
    const fim = fimDoDia(t);
    if (t < ini) t = ini;
    if (t >= fim) { t = proximoDia8h(t); continue; }
    const disponivel = fim.getTime() - t.getTime();
    if (restoMs <= disponivel) return deBrt(new Date(t.getTime() + restoMs));
    restoMs -= disponivel;
    t = proximoDia8h(t);
  }
  throw new Error(`prazoHorasUteis: não convergiu (${horas}h)`);
}

// ── Etapas que entram no fluxo ───────────────────────────────────────────────────
// O painel guarda em `flow_stages.inclusion_condition` quando uma etapa existe. Para a
// "Alteração de Boleto", "Aprovação do Desconto" (gerência comercial) só entra quando
// tipo_alteracao = 'desconto'. Operador desconhecido → ERRO (falha fechando): avaliar
// errado criaria uma ocorrência com etapas que a tela não criaria.

export type Condicao = { op: string; field: string; value: unknown } | null;

export function etapaEntra(cond: Condicao, campos: Record<string, unknown>): boolean {
  if (!cond) return true;
  const atual = campos[cond.field];
  switch (cond.op) {
    case "eq":  return atual === cond.value;
    case "neq": return atual !== cond.value;          // campo ausente ≠ valor → entra
    case "in":  return Array.isArray(cond.value) && cond.value.includes(atual);
    default:
      throw new Error(`condição de etapa com operador desconhecido: ${cond.op}`);
  }
}

// ── NF ───────────────────────────────────────────────────────────────────────────

/** '000001234' → 1234. Devolve null se não for número. */
export function nfInteiro(nf: string | null | undefined): number | null {
  const d = String(nf ?? "").replace(/\D/g, "").replace(/^0+/, "");
  if (!d) return null;
  const n = Number(d);
  return Number.isSafeInteger(n) ? n : null;
}

/** Chave canônica da NF no painel: 'P-ES-1234' (P = Protheus, que é o trilho do Mercos). */
export function chaveNfPainel(regiao: string, nf: string): string | null {
  const n = nfInteiro(nf);
  return n === null ? null : `P-${regiao}-${n}`;
}

/** Chave de idempotência gravada no registro da Torre E dentro do caso no painel. */
export function chaveTorre(regiao: string, numeroPedido: number): string {
  return `torre:desconto-boleto:${regiao}:${numeroPedido}`;
}

// ── Mensagem ─────────────────────────────────────────────────────────────────────

export type DadosMensagem = {
  regiao: string;
  pedido: number;
  nf: string;
  desconto: string;
  cliente: string;
  cnpj: string;
  vendedor: string | null;
  condicao: string | null;
  valor: number | null;
  dataPedido: string | null;   // YYYY-MM-DD
  dataNf: string | null;       // YYYY-MM-DD
  data1Parcela: string | null; // YYYY-MM-DD
};

export function formatarCnpj(cnpj: string): string {
  const d = cnpj.replace(/\D/g, "");
  if (d.length === 14) return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`;
  if (d.length === 11) return `${d.slice(0, 3)}.${d.slice(3, 6)}.${d.slice(6, 9)}-${d.slice(9)}`;
  return cnpj;
}

export function formatarData(iso: string | null): string {
  if (!iso) return "—";
  const [a, m, d] = iso.slice(0, 10).split("-");
  return a && m && d ? `${d}/${m}/${a}` : iso;
}

export function formatarReais(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  return v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

/**
 * Preenche o texto padrão (vindo de `caso_desconto_boleto_config.mensagem`) e acrescenta a
 * referência da Torre no rodapé. A referência NÃO é enfeite: é por ela que o robô reconhece,
 * numa retomada, que o comentário já foi gravado — sem ela, uma rodada que morresse depois do
 * comentário gravaria o comentário duas vezes.
 */
export function montarMensagem(modelo: string, d: DadosMensagem, chave: string): string {
  const valores: Record<string, string> = {
    regiao: d.regiao,
    pedido: String(d.pedido),
    nf: String(nfInteiro(d.nf) ?? d.nf),
    desconto: d.desconto,
    cliente: d.cliente,
    cnpj: formatarCnpj(d.cnpj),
    vendedor: d.vendedor || "—",
    condicao: d.condicao || "—",
    valor: formatarReais(d.valor),
    data_pedido: formatarData(d.dataPedido),
    data_nf: formatarData(d.dataNf),
    data_1_parcela: formatarData(d.data1Parcela),
  };
  const texto = modelo.replace(/\{(\w+)\}/g, (inteiro, nome: string) =>
    nome in valores ? valores[nome] : inteiro,
  );
  return `${texto.trim()}\n\n— Aberto automaticamente pela Torre B2B · ref ${chave}`;
}

/** Erro do PostgREST sem dado de cliente: só código e restrição (o log pode ser público). */
export function erroSemDados(e: unknown): string {
  if (e && typeof e === "object") {
    const o = e as { code?: string; message?: string; details?: string };
    const restricao = /constraint "([^"]+)"/.exec(o.message ?? "")?.[1];
    return [o.code, restricao].filter(Boolean).join(" ") || "erro sem código";
  }
  return "erro";
}
