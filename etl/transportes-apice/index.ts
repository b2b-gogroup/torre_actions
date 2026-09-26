/**
 * ETL Transportes APICE → Supabase
 *
 * Lê a aba "Base" de um Excel (.xlsx) no Google Drive (ID fixo, atualizado
 * diariamente no MESMO arquivo) e replica na tabela transportes_apice.
 *
 * É o equivalente do ETL transportes-base (Protheus), mas para os pedidos
 * TINY/APICE — que não passam pelo Mercos e por isso não têm rastreio na
 * "Base Transportes". Assim a jornada do pedido Apice ganha, pós-faturamento,
 * transportadora, expedição, previsão e data de entrega, status e ocorrências.
 *
 * Join na jornada: por CNPJ do cliente + NF normalizada (nf_normalizada).
 *
 * Estratégia: truncate + reload a cada execução.
 * Uso local:  cd etl && npx tsx transportes-apice/index.ts
 */

import { createClient } from "@supabase/supabase-js";
import * as XLSX from "xlsx";
import { baixarXlsxDoDrive } from "../shared/drive-download.js";

// Arquivo Drive (mesmo ID sempre — o conteúdo é substituído diariamente).
// Trocado em 31/ago/2026: o ID anterior (1kozPTQ...) ficou parado desde 04/ago
// (a operação passou a subir num arquivo novo sem avisar o ETL).
const DRIVE_FILE_ID = "1_BvnlZY9MM5ZulRcevnkyZLk3NtFY7Rh";
const SHEET_NAME = "Base";

// Cliente Supabase criado sob demanda (só no main) — permite importar as funções
// de parse em testes sem exigir as env vars.
function getSupabase() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL ?? process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error("Faltam env vars: SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY");
  return createClient(url, key, { auth: { persistSession: false } });
}
type DB = ReturnType<typeof getSupabase>;

// ── Helpers de conversão ─────────────────────────────────────────────────────

// Teto de ano p/ datas: hoje + 2 anos. Mata lixo da planilha (ex.: typo "2030-05-06")
// sem barrar previsão de entrega de curto prazo. Calculado uma vez na carga.
const ANO_MAX = new Date().getUTCFullYear() + 2;

/** Serial Excel OU string DD/MM/YYYY / YYYY-MM-DD → 'YYYY-MM-DD' válido (ou null).
 *  Rejeita lixo comum da planilha: dia/mês 00, datas < 2000 (serial 0 → "1900-01-00")
 *  e datas absurdamente futuras (> hoje+2 anos, ex.: typo "2030"). */
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
  if (y < 2000 || y > ANO_MAX || mo < 1 || mo > 12 || d < 1 || d > 31) return null; // descarta lixo
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

// ── Mapeamento por NOME de coluna (robusto a reordenação) ─────────────────────
// alvo → predicado sobre o header normalizado. exato() casa o header inteiro.
const exato = (t: string) => (h: string) => h === t;
const contem = (t: string) => (h: string) => h.includes(t);

// Remapeado em 31/ago/2026 (arquivo trocado, ver DRIVE_FILE_ID acima): o header
// do arquivo novo mistura Title Case ("Data de Entrega") com snake_case cru
// ("data_emissao", "nome_cliente", "chave_nf") e renomeou "Previsão de Entrega"
// pra "PRAZO DE ENTREGA" (mesmo nome que a planilha Base usa) — os predicados
// antigos (escritos pro arquivo anterior) paravam de casar em silêncio, e é
// assim que "previsao_entrega"/"data_emissao" zeravam mesmo com o arquivo lido
// com sucesso. Sempre conferir contra o header REAL antes de aceitar um predicado.
const CAMPOS: Record<string, (h: string) => boolean> = {
  numero_tiny:        exato("pedido"),
  forma_envio:        exato("formadeenvio"),
  transportador:      exato("transportador"),
  data_envio:         exato("datadeenvio"),
  cidade:             exato("cidade"),
  uf:                 exato("uf"),
  cep:                exato("cep"),
  cnpj:               exato("cpfcnpj"),
  nome_cliente:       exato("nomecliente"),
  data_emissao:       exato("dataemissao"),
  chave_nf:           exato("chavenf"),
  total_produtos:     exato("totalprodutos"),
  marcadores:         exato("marcadores"),
  cnpj_origem:        exato("cnpjorigem"),
  cd_origem:          exato("cdorigem"),
  nota_fiscal:        exato("notafiscal"),
  transportadora:     exato("transportadora"),
  status_expedicao:   exato("statusexpedicao"),
  data_ultima_oc:     exato("dataultimaoc"),
  status_site_transp: exato("statussitetransp"),
  detalhes:           exato("detalhes"),
  previsao_site:      exato("previsaosite"),
  data_entrega:       exato("datadeentrega"),
  data_devolucao:     exato("datadevolucao"),
  data_extravio:      exato("dataextravio"),
  status_geral:       exato("statusgeral"),
  status_nota:        exato("statusnota"),
  lead_time:          exato("leadtime"),
  previsao_entrega:   exato("prazodeentrega"),
  aging:              contem("aging"),
  performance_prazo:  exato("performanceprazo"),
  data_expedicao:     exato("datadeexpedicao"),
  macro_motivo:       exato("macromotivo"),
  responsavel_atraso: exato("responsavelatraso"),
};

function mapearColunas(header: unknown[]): Record<string, number> {
  const hn = header.map((h) => norm(h));
  const idx: Record<string, number> = {};
  for (const [campo, pred] of Object.entries(CAMPOS)) {
    const i = hn.findIndex((h) => h !== "" && pred(h));
    if (i >= 0) idx[campo] = i;
  }
  // "Motivo" e "Detalhe" logo após "Responsável Atraso" (headers duplicados).
  if (idx.responsavel_atraso != null) {
    idx.motivo_atraso = idx.responsavel_atraso + 1;
    idx.detalhe_atraso = idx.responsavel_atraso + 2;
  }
  return idx;
}

function rowToDb(cols: unknown[], idx: Record<string, number>) {
  const g = (campo: string) => (idx[campo] != null ? cols[idx[campo]] : undefined);
  const nota = str(g("nota_fiscal"));
  return {
    numero_tiny:        toInt(g("numero_tiny")),
    forma_envio:        str(g("forma_envio")),
    transportador:      str(g("transportador")),
    data_envio:         toDate(g("data_envio")),
    cidade:             str(g("cidade")),
    uf:                 str(g("uf")) ? String(g("uf")).trim().slice(0, 2).toUpperCase() : null,
    cep:                digits(g("cep")),
    cnpj:               digits(g("cnpj")),
    nome_cliente:       str(g("nome_cliente")),
    data_emissao:       toDate(g("data_emissao")),
    chave_nf:           (() => { const d = digits(g("chave_nf")); return d && d.length === 44 ? d : null; })(),
    total_produtos:     toFloat(g("total_produtos")),
    marcadores:         str(g("marcadores")),
    cnpj_origem:        digits(g("cnpj_origem")),
    cd_origem:          str(g("cd_origem")),
    nota_fiscal:        nota,
    nf_normalizada:     toInt(nota),
    transportadora:     str(g("transportadora")),
    status_expedicao:   str(g("status_expedicao")),
    data_ultima_oc:     toDate(g("data_ultima_oc")),
    status_site_transp: str(g("status_site_transp")),
    detalhes:           str(g("detalhes")),
    previsao_site:      toDate(g("previsao_site")),
    data_entrega:       toDate(g("data_entrega")),
    data_devolucao:     toDate(g("data_devolucao")),
    data_extravio:      toDate(g("data_extravio")),
    status_geral:       str(g("status_geral")),
    status_nota:        str(g("status_nota")),
    lead_time:          toInt(g("lead_time")),
    previsao_entrega:   toDate(g("previsao_entrega")),
    aging:              str(g("aging")),
    performance_prazo:  str(g("performance_prazo")),
    data_expedicao:     toDate(g("data_expedicao")),
    macro_motivo:       str(g("macro_motivo")),
    responsavel_atraso: str(g("responsavel_atraso")),
    motivo_atraso:      str(g("motivo_atraso")),
    detalhe_atraso:     str(g("detalhe_atraso")),
    atualizado_em:      new Date().toISOString(),
  };
}
type Registro = ReturnType<typeof rowToDb>;

// ── Download do Drive (arquivo público; trata interstício de confirmação) ─────
// Implementação em etl/shared/drive-download.ts (compartilhada com transportes-base).

export async function baixarXlsx(): Promise<ArrayBuffer> {
  return baixarXlsxDoDrive(DRIVE_FILE_ID);
}

// ── Supabase ──────────────────────────────────────────────────────────────────

async function truncate(supabase: DB): Promise<void> {
  const { error } = await supabase.from("transportes_apice").delete().gt("id", 0);
  if (error) throw new Error(`Truncate transportes_apice: ${error.message}`);
}

async function bulkInsert(supabase: DB, rows: Registro[]): Promise<void> {
  const BATCH = 500;
  for (let i = 0; i < rows.length; i += BATCH) {
    const { error } = await supabase.from("transportes_apice").insert(rows.slice(i, i + BATCH));
    if (error) throw new Error(`Insert transportes_apice (chunk ${i}): ${error.message}`);
  }
}

// ── Parse (puro — reutilizável em teste) ──────────────────────────────────────

/** Lê o workbook, mapeia colunas por nome, deduplica e retorna as linhas prontas. */
export function parseWorkbook(buf: ArrayBuffer): Registro[] {
  const wb = XLSX.read(buf, { type: "array", cellDates: false });
  const ws = wb.Sheets[SHEET_NAME];
  if (!ws) throw new Error(`Aba "${SHEET_NAME}" não encontrada. Abas: ${wb.SheetNames.join(", ")}`);

  const matrix = XLSX.utils.sheet_to_json<unknown[]>(ws, { header: 1, raw: true, defval: null });
  if (matrix.length < 2) throw new Error("Aba vazia ou só cabeçalho");

  const idx = mapearColunas(matrix[0]);
  // "transportador" (singular) saiu do header no arquivo atual (só "Transportadora"
  // continua existindo) — exigir a que realmente está presente, não a legada.
  const faltando = ["nota_fiscal", "cnpj", "data_entrega", "transportadora"].filter((c) => idx[c] == null);
  if (faltando.length) throw new Error(`Colunas essenciais não encontradas no header: ${faltando.join(", ")}`);

  const dataRows = matrix.slice(1).filter((r) => r.some((c) => c !== null && String(c).trim() !== ""));
  const parsed = dataRows.map((r) => rowToDb(r, idx)).filter((r) => r.nota_fiscal || r.chave_nf);

  // Dedup por chave_nf (fallback cnpj|nf): mantém o registro mais recente.
  const recencia = (r: Registro) => r.data_ultima_oc ?? r.data_entrega ?? r.data_emissao ?? "";
  const dedup = new Map<string, Registro>();
  for (const r of parsed) {
    const key = r.chave_nf ?? `${r.cnpj ?? ""}|${r.nf_normalizada ?? ""}`;
    const prev = dedup.get(key);
    if (!prev || recencia(r) >= recencia(prev)) dedup.set(key, r);
  }
  return Array.from(dedup.values());
}

// ── Main (IO) ─────────────────────────────────────────────────────────────────

async function main() {
  const t0 = Date.now();
  console.log("[etl-transportes-apice] v3 — datas saneadas (ano 2000-2100) + download usercontent");
  console.log("[etl-transportes-apice] baixando Drive…");
  const buf = await baixarXlsx();
  const rows = parseWorkbook(buf);
  console.log(`[etl-transportes-apice] ${rows.length} registros após dedup`);

  const supabase = getSupabase();

  // Guarda anti-wipe: NUNCA zerar/decimar o rastreio por causa de um dia de planilha ruim
  // (download parcial, aba renomeada, linhas sem NF). Se o parse veio muito abaixo do que
  // já existe, aborta ANTES de truncar — falha visível no Actions e dado preservado.
  const { count: atual } = await supabase
    .from("transportes_apice")
    .select("*", { count: "exact", head: true });
  const piso = Math.max(100, Math.floor((atual ?? 0) * 0.5));
  if (rows.length < piso) {
    throw new Error(
      `Parse retornou ${rows.length} registros (piso ${piso}; tabela atual ${atual ?? 0}). ` +
      `Abortando SEM truncar para não zerar o rastreio Apice — verifique a planilha do Drive.`,
    );
  }

  await truncate(supabase);
  await bulkInsert(supabase, rows);

  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[etl-transportes-apice] OK — ${rows.length} registros (piso ${piso}, anterior ${atual ?? 0}) em ${sec}s`);
}

// Só executa quando rodado direto (não quando importado por um script de teste).
import { pathToFileURL } from "url";
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    console.error("[etl-transportes-apice] ERRO:", e instanceof Error ? e.message : e);
    process.exit(1);
  });
}
