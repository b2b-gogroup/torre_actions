/**
 * ETL Transportes Base → Supabase
 *
 * Lê a aba "BASE" de um Excel (.xlsx) no Google Drive e replica na tabela
 * transportes_base no Supabase. Alimenta a previsão/data de entrega da
 * Jornada do Pedido (Mercos/Protheus, join por pedido_mercos + região).
 *
 * Até 31/ago/2026 a fonte era um Google Sheets nativo (CSV export) mapeado
 * por ÍNDICE FIXO de coluna. A operação parou de alimentar aquele Sheets em
 * 04/ago/2026 e passou a subir um Excel no Drive — o ETL antigo continuou
 * rodando 2x/dia sem erro, só que sempre lendo o mesmo conteúdo velho
 * (truncate+reload do que já não mudava), e todo pedido de 05/ago em diante
 * ficou sem previsão de entrega sem nenhum alarme disparar. O arquivo novo
 * também reordenou/trocou colunas (sumiu bairro/endereco_entrega/uf_entrega,
 * entraram CNPJ+NF/PRAZO(DIAS ÚTEIS)/AGING/etc.) — por isso o parser passou
 * a mapear por NOME de coluna normalizado (mesmo padrão de
 * etl/transportes-apice), não mais por índice: índice fixo quebra em
 * silêncio na próxima reordenação da planilha, nome não.
 *
 * Estratégia: truncate + reload a cada execução, com guarda anti-wipe (não
 * zera a tabela se o parse vier muito abaixo do que já existe).
 *
 * Uso local:
 *   cd etl && npx tsx transportes-base/index.ts
 */

import { createClient } from "@supabase/supabase-js";
import * as XLSX from "xlsx";
import { baixarXlsxDoDrive } from "../shared/drive-download.js";

// Arquivo Drive (mesmo ID sempre — o conteúdo é substituído pela operação).
// Trocado em 31/ago/2026: o Google Sheets anterior (1XImT5e...) ficou parado
// desde 03/ago (a operação migrou pra Excel no Drive sem avisar o ETL).
const DRIVE_FILE_ID = "1bTEokl2Mi5w5cGoGMvtpJQlwZDP_Ty2c";
const SHEET_NAME = "BASE";

function getSupabase() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL ?? process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error("Faltam env vars: SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY");
  return createClient(url, key, { auth: { persistSession: false } });
}
type DB = ReturnType<typeof getSupabase>;

// ── Helpers de conversão (mesmo comportamento de etl/transportes-apice) ──────

const ANO_MAX = new Date().getUTCFullYear() + 2;

/** Serial Excel OU string DD/MM/YYYY / YYYY-MM-DD → 'YYYY-MM-DD' válido (ou null). */
function toDate(v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  let iso: string | null = null;
  if (typeof v === "number" && v > 20000 && v < 80000) {
    const d = new Date(Date.UTC(1899, 11, 30) + Math.round(v) * 86400000);
    if (!isNaN(d.getTime())) iso = d.toISOString().slice(0, 10);
  } else {
    const s = String(v).trim();
    const br = /^(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(s);
    if (br) iso = `${br[3]}-${br[2].padStart(2, "0")}-${br[1].padStart(2, "0")}`;
    else if (/^\d{4}-\d{2}-\d{2}/.test(s)) iso = s.slice(0, 10);
  }
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  if (y < 2000 || y > ANO_MAX || mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  return iso;
}

function toInt(v: unknown): number | null {
  const n = parseInt(String(v ?? "").replace(/\D/g, ""), 10);
  return isNaN(n) ? null : n;
}

function toFloat(v: unknown): number | null {
  if (typeof v === "number") return isNaN(v) ? null : v;
  const s = String(v ?? "").trim().replace(/\./g, "").replace(",", ".");
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

const str = (v: unknown): string | null => {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
};
const digits = (v: unknown): string | null => {
  const s = String(v ?? "").replace(/\D/g, "");
  return s === "" ? null : s;
};
const norm = (s: unknown): string =>
  String(s ?? "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]/g, "");

// ── Mapeamento por NOME de coluna (robusto a reordenação) ────────────────────
// alvo → predicado sobre o header normalizado. exato() casa o header inteiro.
const exato = (t: string) => (h: string) => h === t;
const contem = (t: string) => (h: string) => h.includes(t);

// Colunas do arquivo atual (31/ago/2026) que não têm mais equivalente na
// planilha nova (bairro, endereco_entrega, bairro_entrega, uf_entrega,
// macro/micro_motivo_ocorrencia, setor_pendencia, chave_cd_nf) ficam de fora
// do mapa — a coluna correspondente em transportes_base grava NULL, não erro.
const CAMPOS: Record<string, (h: string) => boolean> = {
  data_inclusao:      exato("datainclusao"),
  filial:             exato("filial"),
  cd:                 exato("cd"),
  pedido_mercos:      exato("pedido"),
  nota_fiscal:        exato("notafiscal"),
  chave_nf:           exato("chavenf"),
  pedido_protheus:    exato("pedidoprotheus"),
  data_emissao:       exato("dataemissao"),
  marca:              exato("marca"),
  transportadora:     exato("transportadora"),
  volumes:            exato("volumes"),
  peso_bruto:         exato("pesobruto"),
  valor_bruto_total:  exato("valorbrutototal"),
  cod_cliente:        exato("codcliente"),
  nome_cliente:       exato("nomecliente"),
  cnpj:               exato("cpfcnpj"),
  cep:                exato("cep"),
  endereco:           exato("endereco"),
  cidade:             exato("cidade"),
  uf:                 exato("uf"),
  cnpj_nf:            exato("cnpjnf"),
  duplicidade:        exato("duplicidade"),
  status_expedicao:   exato("statusexpedicao"),
  status_nf:          exato("statusnf"),
  status_geral:       exato("statusgeral"),
  previsao_entrega:   exato("prazodeentrega"),
  data_entrega:       exato("dataentrega"),
  data_devolucao:     exato("datadevolucao"),
  data_extravio:      exato("dataextravio"),
  // 22/set/2026: o arquivo novo chama a coluna "Detalhes Site Transportador
  // status" (sufixo novo), e com exato() ela deixava de casar -- gravando NULL
  // em SILENCIO num campo que tem 6.518 de 6.644 linhas preenchidas hoje.
  // Mesmo modo de falha de 31/ago. contem() sobrevive a sufixo novo.
  tracking_site:      contem("detalhessitetransportador"),
};

function mapearColunas(header: unknown[]): Record<string, number> {
  const hn = header.map((h) => norm(h));
  const idx: Record<string, number> = {};
  for (const [campo, pred] of Object.entries(CAMPOS)) {
    const i = hn.findIndex((h) => h !== "" && pred(h));
    if (i >= 0) idx[campo] = i;
  }
  return idx;
}

function rowToDb(cols: unknown[], idx: Record<string, number>) {
  const g = (campo: string) => (idx[campo] != null ? cols[idx[campo]] : undefined);
  return {
    data_inclusao:           toDate(g("data_inclusao")),
    filial:                  str(g("filial")),
    cd:                      str(g("cd")),
    pedido_mercos:           toInt(g("pedido_mercos")),
    nota_fiscal:             str(g("nota_fiscal")),
    chave_nf:                str(g("chave_nf")),
    pedido_protheus:         str(g("pedido_protheus")),
    data_emissao:            toDate(g("data_emissao")),
    marca:                   str(g("marca")),
    transportadora:          str(g("transportadora")),
    volumes:                 toInt(g("volumes")),
    peso_bruto:              toFloat(g("peso_bruto")),
    valor_bruto_total:       toFloat(g("valor_bruto_total")),
    cod_cliente:             str(g("cod_cliente")),
    nome_cliente:            str(g("nome_cliente")),
    cnpj:                    digits(g("cnpj")),
    cep:                     digits(g("cep")),
    endereco:                str(g("endereco")),
    cidade:                  str(g("cidade")),
    uf:                      str(g("uf")) ? String(g("uf")).trim().slice(0, 2).toUpperCase() : null,
    bairro:                  null as string | null,
    endereco_entrega:        null as string | null,
    bairro_entrega:          null as string | null,
    uf_entrega:              null as string | null,
    cnpj_nf:                 str(g("cnpj_nf")),
    duplicidade:             str(g("duplicidade")),
    status_expedicao:        str(g("status_expedicao")),
    status_prazo_atual:      null as string | null,
    status_nf:               str(g("status_nf")),
    status_geral:            str(g("status_geral")),
    macro_motivo_ocorrencia: null as string | null,
    micro_motivo_ocorrencia: null as string | null,
    setor_pendencia:         null as string | null,
    previsao_entrega:        toDate(g("previsao_entrega")),
    data_entrega:            toDate(g("data_entrega")),
    data_devolucao:          toDate(g("data_devolucao")),
    data_extravio:           toDate(g("data_extravio")),
    tracking_site:           str(g("tracking_site")),
    chave_cd_nf:             null as string | null,
    atualizado_em:           new Date().toISOString(),
  };
}
type Registro = ReturnType<typeof rowToDb>;

// ── Parse (puro — reutilizável em teste) ──────────────────────────────────────

/** Lê o workbook, mapeia colunas por nome, deduplica e retorna as linhas prontas. */
export function parseWorkbook(buf: ArrayBuffer): Registro[] {
  const wb = XLSX.read(buf, { type: "array", cellDates: false });
  const ws = wb.Sheets[SHEET_NAME];
  if (!ws) throw new Error(`Aba "${SHEET_NAME}" não encontrada. Abas: ${wb.SheetNames.join(", ")}`);

  const matrix = XLSX.utils.sheet_to_json<unknown[]>(ws, { header: 1, raw: true, defval: null });
  if (matrix.length < 2) throw new Error("Aba vazia ou só cabeçalho");

  const idx = mapearColunas(matrix[0]);
  const faltando = ["pedido_mercos", "nota_fiscal", "previsao_entrega"].filter((c) => idx[c] == null);
  if (faltando.length) throw new Error(`Colunas essenciais não encontradas no header: ${faltando.join(", ")}`);

  const dataRows = matrix.slice(1).filter((r) => r.some((c) => c !== null && String(c).trim() !== ""));
  const parsed = dataRows.map((r) => rowToDb(r, idx)).filter((r) => r.pedido_mercos != null || r.chave_nf);

  // Dedup por chave_nf (fallback pedido_mercos): mantém o registro mais recente.
  const recencia = (r: Registro) => r.data_entrega ?? r.data_emissao ?? "";
  const dedup = new Map<string, Registro>();
  for (const r of parsed) {
    const key = r.chave_nf ?? `pedido:${r.pedido_mercos ?? ""}`;
    const prev = dedup.get(key);
    if (!prev || recencia(r) >= recencia(prev)) dedup.set(key, r);
  }
  return Array.from(dedup.values());
}

// ── Supabase ──────────────────────────────────────────────────────────────────

async function truncate(supabase: DB): Promise<void> {
  const { error } = await supabase.from("transportes_base").delete().gt("id", 0);
  if (error) throw new Error(`Truncate transportes_base: ${error.message}`);
}

async function bulkInsert(supabase: DB, rows: Registro[]): Promise<void> {
  const BATCH = 500;
  for (let i = 0; i < rows.length; i += BATCH) {
    const { error } = await supabase.from("transportes_base").insert(rows.slice(i, i + BATCH));
    if (error) throw new Error(`Insert transportes_base (chunk ${i}): ${error.message}`);
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  const t0 = Date.now();
  console.log("[etl-transportes-base] v2 — fonte Excel/Drive, mapeamento por nome de coluna");
  console.log("[etl-transportes-base] baixando Drive…");
  const buf = await baixarXlsxDoDrive(DRIVE_FILE_ID);
  const rows = parseWorkbook(buf);
  console.log(`[etl-transportes-base] ${rows.length} registros após dedup`);

  const supabase = getSupabase();

  // Guarda anti-wipe: NUNCA zerar/decimar o rastreio por causa de um dia de planilha
  // ruim (download parcial, aba renomeada, linhas sem pedido). Se o parse veio muito
  // abaixo do que já existe, aborta ANTES de truncar — falha visível no Actions e
  // dado preservado (mesmo padrão de etl/transportes-apice).
  const { count: atual } = await supabase
    .from("transportes_base")
    .select("*", { count: "exact", head: true });
  const piso = Math.max(100, Math.floor((atual ?? 0) * 0.5));
  if (rows.length < piso) {
    throw new Error(
      `Parse retornou ${rows.length} registros (piso ${piso}; tabela atual ${atual ?? 0}). ` +
      `Abortando SEM truncar para não zerar o rastreio — verifique a planilha do Drive.`,
    );
  }

  await truncate(supabase);
  await bulkInsert(supabase, rows);

  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[etl-transportes-base] OK — ${rows.length} registros (piso ${piso}, anterior ${atual ?? 0}) em ${sec}s`);
}

// Só executa quando rodado direto (não quando importado por um script de teste).
import { pathToFileURL } from "url";
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    console.error("[etl-transportes-base] ERRO:", e instanceof Error ? e.message : e);
    process.exit(1);
  });
}
