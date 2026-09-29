/**
 * Fluxo do robô "Desconto Boleto → caso no painel" — ver o cabeçalho de `index.ts`.
 * Separado do `main` para o `preview-local.ts` simular um pedido sem disparar a rodada.
 */

import type { SupabaseClient } from "@supabase/supabase-js";
import {
  chaveNfPainel, chaveTorre, erroSemDados, etapaEntra, montarMensagem, nfInteiro,
  prazoHorasUteis, type Condicao,
} from "./regras.js";

export const MODO = (process.env.MODO ?? "dry").trim().toLowerCase();
export const EM_CI = process.env.GITHUB_ACTIONS === "true";
export const PREVIEW = process.env.PREVIEW === "1" && !EM_CI; // nunca imprime dado de cliente no Actions
export const SO_PEDIDO = (process.env.SO_PEDIDO ?? "").trim().toUpperCase(); // 'ES:1234'
const RUN_URL = EM_CI
  ? `${process.env.GITHUB_SERVER_URL}/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`
  : "local";

// Identidade do robô no painel. `created_by` é o PAPEL (o painel grava área, não e-mail —
// medido: salesops/admin/transportes/...); a Torre abre como salesops, que é quem pede
// alteração de boleto e quem é dono da etapa "Dados da Alteração".
const AUTOR = "Torre B2B (automático)";
const EMAIL_ROBO = "b2b@gobeaute.com.br";
const PAPEL = "salesops";
const TIPO_CASO = "alteracao_de_boleto";
const TIPO_OCORRENCIA = "prorrogacao_de_boleto"; // nome na tela: "Alteração de Boleto"
const ETAPA_DADOS = "dados_da_prorrogacao";      // etapa que o solicitante preenche
const MAX_TENTATIVAS_LOG = 5;
const DIAS_ESPERA_TITULO = 2; // sem título ainda: espera até 2 dias da NF antes de abrir sem a data

export type Candidato = {
  regiao: string; numero_pedido: number; data_pedido: string | null; cnpj: string;
  razao_social: string | null; nome_fantasia: string | null; vendedor: string | null;
  vendedor_tipo: string | null; condicao: string | null; total_pedido: number | null;
  desconto_boleto: string; nf_numero: string | null; data_nf: string | null;
  data_1_parcela: string | null; qtd_nfs: number; situacao: string; registro_status: string | null;
};
export type Registro = {
  id: number; chave: string; caso_id: string | null; caso_display_id: string | null;
  ocorrencia_id: string | null; ocorrencia_display_id: string | null; tentativas: number;
};
export type Etapa = { id: string; key: string; name: string; sla_hours: number | null; sort_order: number; inclusion_condition: Condicao };
export type Referencia = {
  tipoCaso: { id: string; name: string; default_team: string | null };
  tipoOcorrencia: { id: string; version: number; display_id_prefix: string };
  etapas: Etapa[];
  obrigatorios: string[];
};
export type Resultado =
  | "criado" | "anexado" | "ja_existia" | "retomado" | "pulado" | "aguardando_titulo"
  | "planejado_novo" | "planejado_anexar" | "planejado_ja_existia";

// ── util ─────────────────────────────────────────────────────────────────────────

export function exigirEnv(nome: string): string {
  const v = process.env[nome];
  if (!v) throw new Error(`falta a variável de ambiente ${nome}`);
  return v;
}

/** Desembrulha a resposta do supabase-js: erro vira exceção (nunca "veio vazio"). */
export function ok<T>(res: { data: T | null; error: unknown }, oque: string): T {
  if (res.error) {
    const e = res.error as { message?: string };
    const err = new Error(`${oque}: ${e.message ?? "erro"}`) as Error & { causa?: unknown };
    err.causa = res.error;
    throw err;
  }
  return res.data as T;
}

function hojeBrtIso(): string {
  return new Date(Date.now() - 3 * 3600 * 1000).toISOString().slice(0, 10);
}
function diasDesde(iso: string | null): number {
  if (!iso) return Infinity;
  return (Date.parse(hojeBrtIso()) - Date.parse(iso.slice(0, 10))) / 86_400_000;
}

async function proximoDisplayId(painel: SupabaseClient, escopo: string, prefixo: string, tabela: string): Promise<string> {
  const d = ok(await painel.rpc("next_display_id_safe", {
    p_scope: escopo, p_prefix: prefixo, p_width: 4, p_table: tabela, p_column: "display_id",
  }), `numeração ${escopo}`);
  const v = typeof d === "string" ? d : Array.isArray(d) ? d[0] : d;
  const texto = typeof v === "string" ? v : v && typeof v === "object" ? Object.values(v)[0] : null;
  if (typeof texto !== "string" || !texto.startsWith(prefixo)) {
    throw new Error(`numeração ${escopo}: resposta inesperada do painel`);
  }
  return texto;
}

// ── referência do painel (lida a cada rodada, falha fechando) ────────────────────

export async function carregarReferencia(painel: SupabaseClient): Promise<Referencia> {
  const tc = ok(await painel.from("case_types").select("id,name,default_team,is_active").eq("key", TIPO_CASO).maybeSingle(), "tipo de caso");
  if (!tc || !(tc as { is_active: boolean }).is_active) throw new Error(`tipo de caso '${TIPO_CASO}' ausente ou inativo no painel`);
  const to = ok(await painel.from("occurrence_types").select("id,version,display_id_prefix,is_active").eq("key", TIPO_OCORRENCIA).maybeSingle(), "tipo de ocorrência");
  if (!to || !(to as { is_active: boolean }).is_active) throw new Error(`tipo de ocorrência '${TIPO_OCORRENCIA}' ausente ou inativo no painel`);
  const tipoOcorrencia = to as { id: string; version: number; display_id_prefix: string };

  const etapas = ok(await painel.from("flow_stages")
    .select("id,key,name,sla_hours,sort_order,inclusion_condition")
    .eq("occurrence_type_id", tipoOcorrencia.id).order("sort_order"), "etapas") as Etapa[];
  if (!etapas.length || etapas[0].key !== ETAPA_DADOS) {
    throw new Error(`o fluxo da Alteração de Boleto mudou no painel (1ª etapa não é '${ETAPA_DADOS}') — revisar o robô antes de gravar`);
  }
  const campos = ok(await painel.from("flow_field_defs").select("key,required,stage_key")
    .eq("occurrence_type_id", tipoOcorrencia.id), "campos") as { key: string; required: boolean; stage_key: string | null }[];
  const obrigatorios = campos.filter((c) => c.required && !c.stage_key).map((c) => c.key);

  return { tipoCaso: tc as Referencia["tipoCaso"], tipoOcorrencia, etapas, obrigatorios };
}

// ── um pedido ────────────────────────────────────────────────────────────────────

export async function processar(
  torre: SupabaseClient, painel: SupabaseClient, ref: Referencia, modelo: string, c: Candidato,
  preview: (titulo: string, obj: unknown) => void,
): Promise<Resultado> {
  const nf = c.nf_numero as string;
  const nfInt = nfInteiro(nf);
  const nfKey = chaveNfPainel(c.regiao, nf);
  if (nfInt === null || !nfKey) throw new Error("NF sem número válido");
  const chave = chaveTorre(c.regiao, c.numero_pedido);

  // Sem título ainda (boleto não gerado): espera um pouco — a "Data da 1ª parcela" é campo
  // obrigatório do formulário. Passado o prazo, abre assim mesmo e o texto declara a falta.
  if (!c.data_1_parcela && diasDesde(c.data_nf) < DIAS_ESPERA_TITULO) return "aguardando_titulo";

  const aplicar = MODO === "aplicar";

  // 1. reserva atômica (só no modo aplicar — o dry não escreve em lugar nenhum)
  let reg: Registro | null = null;
  if (aplicar) {
    const linhas = ok(await torre.rpc("fn_caso_desconto_boleto_reservar", {
      p_regiao: c.regiao, p_numero_pedido: c.numero_pedido, p_cnpj: c.cnpj,
      p_nf_numero: nf, p_desconto_boleto: c.desconto_boleto, p_run_url: RUN_URL,
    }), "reserva") as Registro[];
    if (!linhas.length) return "pulado"; // já enviado, com outra rodada, ou desistido
    reg = linhas[0];
  }
  const anotar = async (campos: Record<string, unknown>) => {
    if (!reg) return;
    ok(await torre.from("caso_desconto_boleto").update({ ...campos, atualizado_em: new Date().toISOString() }).eq("id", reg.id), "registro");
  };

  // 2. a rodada anterior já gravou no painel? (a chave vai dentro da ocorrência)
  const jaGravada = ok(await painel.from("occurrences").select("id,display_id,case_id")
    .eq("field_values->>torre_chave", chave).limit(1), "busca da chave") as { id: string; display_id: string; case_id: string }[];

  // 3. casos da NF no painel (canônico por nf_key; fallback CNPJ+NF para os ~2% sem nf_key)
  const casosNf = ok(await painel.from("cases")
    .select("id,display_id,status,cnpj,created_at")
    .or(`nf_key.eq.${nfKey},and(cnpj.eq.${c.cnpj},nf_numero.eq.${nfInt})`), "casos da NF") as
    { id: string; display_id: string; status: string; cnpj: string | null; created_at: string }[];
  const casosMesmoCliente = casosNf.filter((x) => (x.cnpj ?? "").replace(/\D/g, "") === c.cnpj);

  // 4. já existe pedido de DESCONTO no boleto desta NF? (aberto por uma pessoa)
  if (!jaGravada.length && casosMesmoCliente.length) {
    const occs = ok(await painel.from("occurrences").select("id,display_id,case_id,overall_status,field_values")
      .in("case_id", casosMesmoCliente.map((x) => x.id)).eq("occurrence_type_id", ref.tipoOcorrencia.id), "ocorrências da NF") as
      { id: string; display_id: string; case_id: string; overall_status: string; field_values: Record<string, unknown> }[];
    const desconto = occs.find((o) => o.overall_status !== "cancelado" && o.field_values?.tipo_alteracao === "desconto");
    if (desconto) {
      if (!aplicar) return "planejado_ja_existia";
      const caso = casosMesmoCliente.find((x) => x.id === desconto.case_id)!;
      await anotar({ status: "ja_existia", caso_id: caso.id, caso_display_id: caso.display_id,
        ocorrencia_id: desconto.id, ocorrencia_display_id: desconto.display_id, enviado_em: new Date().toISOString(), ultimo_erro: null });
      return "ja_existia";
    }
  }

  const casoAberto = casosMesmoCliente
    .filter((x) => !["resolvido", "fechado"].includes(x.status))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];

  // 5. dados da ocorrência (o formulário da tela) + etapas que entram
  const cli = ok(await painel.from("clientes").select("id,contribuinte,vendedor_tipo").eq("cnpj", c.cnpj).maybeSingle(), "cliente") as
    { id: string; contribuinte: boolean | null; vendedor_tipo: string | null } | null;
  const mensagem = montarMensagem(modelo, {
    regiao: c.regiao, pedido: c.numero_pedido, nf, desconto: c.desconto_boleto,
    cliente: c.razao_social || c.nome_fantasia || c.cnpj, cnpj: c.cnpj, vendedor: c.vendedor,
    condicao: c.condicao, valor: c.total_pedido === null ? null : Number(c.total_pedido),
    dataPedido: c.data_pedido, dataNf: c.data_nf, data1Parcela: c.data_1_parcela,
  }, chave);
  const campos: Record<string, unknown> = {
    tipo_alteracao: "desconto",
    order_number: String(c.numero_pedido),
    ...(c.data_1_parcela ? { data_da_1_parcela: c.data_1_parcela } : {}),
    prorrogacao: `Aplicar desconto no boleto: ${c.desconto_boleto}` +
      (c.data_1_parcela ? "" : "\n(Data da 1ª parcela não encontrada no título do Protheus — conferir.)"),
    nf_saida: String(nfInt),
    erp_origem: "protheus",
    cd_faturamento: c.regiao,
    vendedor_tipo: cli?.vendedor_tipo ?? c.vendedor_tipo ?? null,
    is_contributor: cli?.contribuinte ?? false,
    description_text: mensagem,
    torre_chave: chave,
  };
  const faltando = ref.obrigatorios.filter((k) => k !== "data_da_1_parcela" && (campos[k] === undefined || campos[k] === ""));
  if (faltando.length) throw new Error(`campos obrigatórios sem valor: ${faltando.join(", ")}`);

  const etapas = ref.etapas.filter((e) => etapaEntra(e.inclusion_condition, campos));
  const [etapaDados, etapaAtual] = etapas;
  if (!etapaAtual) throw new Error("o fluxo não tem etapa depois de 'Dados da Alteração'");

  if (!aplicar) {
    preview("CASO", casoAberto
      ? { acao: "ANEXAR ocorrência ao caso aberto", caso: casoAberto.display_id }
      : { acao: "CRIAR caso novo", subject: ref.tipoCaso.name, assignee_team: ref.tipoCaso.default_team,
          filial: c.regiao, nf_key: nfKey, conflito_nf: casosNf.length > 0, cliente_no_painel: !!cli });
    preview("OCORRÊNCIA", { tipo: "Alteração de Boleto", campos });
    preview("ETAPAS", etapas.map((e, i) => ({ etapa: e.name, status: i === 0 ? "concluido" : "pendente", atual: i === 1,
      prazo: i === 1 ? prazoHorasUteis(new Date(), e.sla_hours ?? 8).toISOString() : null })));
    preview("COMENTÁRIO", mensagem);
    return casoAberto ? "planejado_anexar" : "planejado_novo";
  }
  if (!reg) throw new Error("sem reserva no modo aplicar");

  // 6. o caso: retomado (rodada anterior) → marca na ocorrência → caso aberto da NF → novo
  let casoId = reg.caso_id ?? jaGravada[0]?.case_id ?? null;
  let casoDisplay = reg.caso_display_id ?? null;
  let anexado = false;
  if (!casoId && casoAberto) { casoId = casoAberto.id; casoDisplay = casoAberto.display_id; anexado = true; }
  if (!casoId) {
    let clienteId = cli?.id ?? null;
    if (!clienteId) {
      const ins = await painel.from("clientes").insert({
        cnpj: c.cnpj, razao_social: c.razao_social || c.nome_fantasia || c.cnpj,
        nome_fantasia: c.nome_fantasia, contribuinte: false, source: "manual",
      }).select("id").single();
      if (ins.error && (ins.error as { code?: string }).code === "23505") {
        clienteId = (ok(await painel.from("clientes").select("id").eq("cnpj", c.cnpj).single(), "cliente") as { id: string }).id;
      } else {
        clienteId = (ok(ins, "cadastro do cliente") as { id: string }).id;
      }
    }
    const ano = hojeBrtIso().slice(0, 4);
    const display = await proximoDisplayId(painel, `CASE-${ano}`, `CASE-${ano}-`, "cases");
    const caso = ok(await painel.from("cases").insert({
      display_id: display, cliente_id: clienteId, cnpj: c.cnpj, subject: ref.tipoCaso.name,
      status: "aberto", channel: "interno", priority: "normal",
      assignee_team: ref.tipoCaso.default_team, assignee_email: null,
      requester_name: AUTOR, requester_email: EMAIL_ROBO, created_by: PAPEL,
      case_type_id: ref.tipoCaso.id, nf_number: String(nfInt), nf_numero: nfInt,
      filial: c.regiao, erp_origem: "protheus", erp_confirmado: true,
      nf_key: nfKey, nf_key_conflito: casosNf.length > 0,
    }).select("id,display_id").single(), "criação do caso") as { id: string; display_id: string };
    casoId = caso.id; casoDisplay = caso.display_id;
    await anotar({ caso_id: casoId, caso_display_id: casoDisplay }); // antes de seguir: retomada sabe o caso
    ok(await painel.from("case_team_history").insert({
      case_id: casoId, team: ref.tipoCaso.default_team, changed_by: EMAIL_ROBO,
    }), "histórico de time");
  } else if (!reg.caso_id) {
    await anotar({ caso_id: casoId, caso_display_id: casoDisplay });
  } else if (!anexado) {
    // Retomada: a rodada anterior criou o caso e pode ter morrido antes do histórico de time.
    // Só completa caso aberto PELO ROBÔ — num caso existente (anexado) o histórico é da tela.
    const dono = ok(await painel.from("cases").select("requester_email").eq("id", casoId).single(), "caso retomado") as { requester_email: string | null };
    if (dono.requester_email === EMAIL_ROBO) {
      const hist = ok(await painel.from("case_team_history").select("id").eq("case_id", casoId).limit(1), "histórico de time") as unknown[];
      if (!hist.length) {
        ok(await painel.from("case_team_history").insert({ case_id: casoId, team: ref.tipoCaso.default_team, changed_by: EMAIL_ROBO }), "histórico de time");
      }
    }
  }

  // 7. a ocorrência (com a chave dentro)
  let occ = jaGravada[0] ?? null;
  if (!occ) {
    const display = await proximoDisplayId(painel, ref.tipoOcorrencia.display_id_prefix.replace(/-$/, ""),
      ref.tipoOcorrencia.display_id_prefix, "occurrences");
    occ = ok(await painel.from("occurrences").insert({
      display_id: display, case_id: casoId, occurrence_type_id: ref.tipoOcorrencia.id,
      type_version: ref.tipoOcorrencia.version, current_stage_id: etapaAtual.id,
      overall_status: "pendente", field_values: campos, created_by: PAPEL,
    }).select("id,display_id,case_id").single(), "criação da ocorrência") as { id: string; display_id: string; case_id: string };
  }
  await anotar({ caso_id: casoId, caso_display_id: casoDisplay, ocorrencia_id: occ.id, ocorrencia_display_id: occ.display_id });

  // 8. etapas (só se ainda não existirem)
  const jaTem = ok(await painel.from("occurrence_stage_states").select("id").eq("occurrence_id", occ.id), "etapas gravadas") as unknown[];
  if (!jaTem.length) {
    const t0 = new Date().toISOString();
    ok(await painel.from("occurrence_stage_states").insert(etapas.map((e, i) => ({
      occurrence_id: occ!.id, stage_id: e.id, sort_order: e.sort_order, sla_hours: e.sla_hours,
      status: i === 0 ? "concluido" : "pendente",
      entered_at: i <= 1 ? t0 : null,
      exited_at: i === 0 ? t0 : null,
      sla_deadline: i === 1 ? prazoHorasUteis(new Date(t0), e.sla_hours ?? 8).toISOString() : null,
      cumulative_seconds: 0,
    }))), "criação das etapas");
  }

  // 9. comentário (a referência no rodapé é o que evita gravar duas vezes numa retomada)
  const jaComentou = ok(await painel.from("comments").select("id").eq("case_id", casoId)
    .ilike("content", `%${chave}%`).limit(1), "comentário gravado") as unknown[];
  if (!jaComentou.length) {
    ok(await painel.from("comments").insert({ case_id: casoId, author_name: AUTOR, content: mensagem, mentions: [] }), "comentário");
  }
  if (anexado) {
    ok(await painel.from("cases").update({ updated_at: new Date().toISOString() }).eq("id", casoId), "toque no caso");
  }

  await anotar({ status: "criado", enviado_em: new Date().toISOString(), ultimo_erro: null });
  if (jaGravada.length) return "retomado";
  return anexado ? "anexado" : "criado";
}
