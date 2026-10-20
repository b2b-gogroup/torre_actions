/**
 * Cliente NOVO não pode virar "Cliente não identificado" (20/out/2026).
 *
 * O transform grava `PLACEHOLDER_CNPJ` quando o CNPJ do pedido não está em `dim_cliente` (FK).
 * Quem cadastra cliente em `dim_cliente` é uma carga externa a este repo, e ela chega atrasada
 * (cliente recém-cadastrado no Mercos) ou nunca chega (cliente que só existe no Tiny/Ápice).
 * Resultado medido em out/2026: 15 pedidos / 12 clientes / R$ 40.617,98 com o cliente apagado,
 * e a Jornada mostrando o MESMO pedido duas vezes ("2 registros diferentes"), porque a linha do
 * Mercos tinha o CNPJ certo e a do fato o placeholder. Até aqui a correção era manual
 * (26/jul, 20/out).
 *
 * Aqui, ANTES do transform, todo CNPJ que a origem traz com nome e que não existe em
 * `dim_cliente` é cadastrado com o que a origem informa (nome, cidade, UF) e vendedor
 * sentinela — o mesmo formato da carga externa. O dono da carteira continua sendo decisão de
 * quem gere carteira; o vendedor da VENDA já sai certo do transform.
 *
 * Guardas:
 *  - só documento de 14 dígitos que não seja o próprio placeholder (Tiny já entrega CPF
 *    completado a 14, mesma regra de `limparCNPJ`) e nome não vazio;
 *  - nunca empresa do grupo (`CNPJS_GRUPO` — essas são cortadas na convergência);
 *  - `ON CONFLICT DO NOTHING`: nunca sobrescreve cadastro existente (a carga externa é dona);
 *  - teto por rodada: acima dele não cadastra nada e avisa — volume alto significa que a leitura
 *    de `dim_cliente` veio truncada, não que entraram centenas de clientes num dia;
 *  - falha aqui é não-crítica (index.ts): volta ao comportamento antigo (placeholder).
 */
import { logger } from "../shared/logger.js";
import { getSupabaseAdmin } from "../shared/supabase-admin.js";
import { CNPJS_GRUPO } from "./cnpjs-grupo.js";
import { PLACEHOLDER_CNPJ, PLACEHOLDER_UUID } from "./maps.js";
import type { ExtractedData } from "./extract.js";

const TETO_POR_RODADA = 150;

type ClienteNovo = { cnpj: string; nome_cliente: string; cidade: string | null; uf_id: string | null };

export async function cadastrarClientesNovos(sources: ExtractedData): Promise<number> {
  const existentes = new Set<string>();
  for (const row of sources.dimCliente) {
    const c = String(row.cnpj ?? "").trim();
    if (c) existentes.add(c);
  }
  if (existentes.size === 0) {
    logger.warn("cliente-novo: dim_cliente veio vazio — nada cadastrado");
    return 0;
  }

  const novos = new Map<string, ClienteNovo>();
  for (const [fonte, valor] of Object.entries(sources)) {
    if (!Array.isArray(valor) || fonte.startsWith("dim")) continue;
    for (const row of valor as Record<string, unknown>[]) {
      if (!row || typeof row !== "object") continue;
      const cnpj = String(row.cnpj_cliente ?? "").trim();
      if (!/^\d{14}$/.test(cnpj) || cnpj === PLACEHOLDER_CNPJ) continue;
      if (existentes.has(cnpj) || CNPJS_GRUPO.has(cnpj) || novos.has(cnpj)) continue;
      const nome = String(row.nome_cliente ?? "").trim();
      if (!nome) continue;
      const uf = String(row.uf_cliente ?? "").trim().toUpperCase();
      const cidade = String(row.cidade_cliente ?? "").trim();
      novos.set(cnpj, {
        cnpj,
        nome_cliente: nome.toUpperCase(),
        cidade: cidade ? cidade.toUpperCase() : null,
        uf_id: /^[A-Z]{2}$/.test(uf) ? uf : null,
      });
    }
  }

  if (novos.size === 0) return 0;
  if (novos.size > TETO_POR_RODADA) {
    logger.warn(`cliente-novo: ${novos.size} CNPJs sem cadastro (> teto ${TETO_POR_RODADA}) — leitura de dim_cliente provavelmente truncada; nada cadastrado`);
    return 0;
  }

  const sb = getSupabaseAdmin();
  // UF fora de dim_uf quebraria a FK do lote inteiro.
  const { data: ufs, error: errUf } = await sb.from("dim_uf").select("sigla");
  if (errUf) throw new Error(`dim_uf: ${errUf.message}`);
  const ufValida = new Set((ufs ?? []).map((u) => String((u as { sigla: string }).sigla)));

  const linhas = [...novos.values()].map((c) => ({
    ...c,
    uf_id: c.uf_id && ufValida.has(c.uf_id) ? c.uf_id : null,
    vendedor_id: PLACEHOLDER_UUID,
    ativo: true,
  }));
  const { error } = await sb.from("dim_cliente").upsert(linhas, { onConflict: "cnpj", ignoreDuplicates: true });
  if (error) throw new Error(`dim_cliente: ${error.message}`);

  // O transform monta o clienteSet a partir de sources.dimCliente: sem isto os pedidos desta
  // mesma carga ainda sairiam com placeholder.
  for (const c of linhas) sources.dimCliente.push({ cnpj: c.cnpj });
  logger.info(`cliente-novo: ${linhas.length} clientes cadastrados em dim_cliente`, {
    clientes: Object.fromEntries(linhas.map((c) => [c.cnpj, c.nome_cliente])),
  });
  return linhas.length;
}
