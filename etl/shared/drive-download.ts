/**
 * Download de arquivo .xlsx do Google Drive, por service account ou por link público.
 *
 * Extraído de etl/transportes-apice em 31/ago/2026 quando etl/transportes-base
 * passou a ler o mesmo tipo de fonte (Excel subido no Drive, não mais Google
 * Sheets nativo) — evita ter a mesma dança de confirm-token duplicada em dois
 * ETLs e divergindo com o tempo (ver CLAUDE.md convenção #23).
 *
 * 🔴 21/set/2026 — o caminho público PAROU, e ficou 11 dias parado sem ninguém
 * ver. Os dois arquivos deixaram de ser "qualquer um com o link" em algum
 * momento até 15/set, e o Drive passou a devolver **HTTP 200 com a página de
 * login** (916 KB de HTML) — nunca um 401/403. Última carga boa: 10/set 22:56;
 * `transportes_base` e `transportes_apice` congelaram com data de negócio de
 * 08-09/set, então toda a previsão de entrega da Jornada parou aí.
 *
 * Daí a service account entrar PRIMEIRO. O que ela muda não é só resiliência:
 * link "qualquer um com o link" é dado de logística da empresa aberto na
 * internet para quem tiver a URL, e a permissão que o corrige é justamente a
 * que derruba o ETL. Com a SA, fechar o arquivo passa a ser a configuração
 * CERTA em vez de um incidente.
 *
 * ⚠️ O caminho público continua existindo como fallback de propósito: enquanto
 * ninguém compartilhar os arquivos com a service account, é ele que segura —
 * e no dia em que alguém reabrir o link, volta a funcionar sem deploy.
 */

/** xlsx começa com "PK" (assinatura de zip). */
export const ehXlsx = (buf: ArrayBuffer): boolean => {
  const h = new Uint8Array(buf.slice(0, 2));
  return h[0] === 0x50 && h[1] === 0x4b;
};

const MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** Primeiros bytes como texto, para a mensagem de erro dizer o que veio no lugar do arquivo. */
function amostra(buf: ArrayBuffer): string {
  return new TextDecoder().decode(buf.slice(0, 160)).replace(/\s+/g, " ").trim();
}

/**
 * Baixa pela API do Drive, autenticado. Devolve `null` quando não há credencial
 * configurada — "não tem SA" e "a SA não alcança o arquivo" são coisas
 * diferentes, e só a segunda merece aparecer como problema no log.
 */
async function baixarPorServiceAccount(fileId: string): Promise<ArrayBuffer | null> {
  const chave = process.env.GOOGLE_SERVICE_ACCOUNT_KEY;
  if (!chave) return null;

  // Import dinâmico: quem usa só o caminho público não paga o carregamento do
  // googleapis, e um ETL sem essa dependência instalada não quebra por causa
  // de um import no topo.
  const { google } = await import("googleapis");
  const credentials = JSON.parse(chave);
  const auth = new google.auth.GoogleAuth({
    credentials,
    // readonly basta: aqui só se lê. Ver o comentário de escopo em
    // etl/produto-fotos-drive/index.ts — `drive.file` NÃO serve, porque só
    // enxerga o que a própria service account criou, e some com arquivo que
    // alguém compartilhou pelo diálogo normal.
    scopes: ["https://www.googleapis.com/auth/drive.readonly"],
  });
  const drive = google.drive({ version: "v3", auth });
  console.log(`[drive] tentando pela service account ${credentials.client_email}`);

  const meta = await drive.files.get({
    fileId,
    fields: "id, name, mimeType",
    supportsAllDrives: true,
  });
  const mime = meta.data.mimeType ?? "";

  // Planilha NATIVA do Google não tem bytes para baixar — precisa ser exportada.
  // Isso não é hipótese: `transportes_base` FOI um Sheets nativo até 31/ago/2026,
  // e trocar o arquivo por um Sheets de novo daria "só" um erro de download.
  const resp = mime === "application/vnd.google-apps.spreadsheet"
    ? await drive.files.export({ fileId, mimeType: MIME_XLSX }, { responseType: "arraybuffer" })
    : await drive.files.get(
        { fileId, alt: "media", supportsAllDrives: true },
        { responseType: "arraybuffer" },
      );

  const buf = resp.data as ArrayBuffer;
  if (!ehXlsx(buf)) {
    throw new Error(
      `a service account leu "${meta.data.name}" (mime=${mime}) mas o conteúdo não é xlsx: "${amostra(buf)}"`,
    );
  }
  console.log(`[drive] ok pela service account: "${meta.data.name}" (${buf.byteLength} bytes)`);
  return buf;
}

/**
 * Baixa pelo link público. Endpoint moderno (drive.usercontent) resolve o
 * interstício direto; uc?export é fallback. De IPs de datacenter (GitHub
 * Actions) o Drive às vezes devolve uma página HTML com token de confirmação —
 * tratamos extraindo o token e refazendo.
 */
async function baixarPorLinkPublico(fileId: string): Promise<{ buf?: ArrayBuffer; diag: string }> {
  const urls = [
    `https://drive.usercontent.google.com/download?id=${fileId}&export=download&confirm=t`,
    `https://drive.google.com/uc?export=download&confirm=t&id=${fileId}`,
  ];
  let diag = "";
  for (const url of urls) {
    const res = await fetch(url, { redirect: "follow" });
    const buf = await res.arrayBuffer();
    if (res.ok && ehXlsx(buf)) return { buf, diag: "" };

    const txt = new TextDecoder().decode(buf.slice(0, 8000));
    const confirm = txt.match(/name="confirm"\s+value="([^"]+)"/)?.[1] ?? txt.match(/[?&]confirm=([0-9A-Za-z_-]+)/)?.[1];
    const uuid = txt.match(/name="uuid"\s+value="([^"]+)"/)?.[1];
    if (confirm) {
      let u2 = `https://drive.usercontent.google.com/download?id=${fileId}&export=download&confirm=${confirm}`;
      if (uuid) u2 += `&uuid=${uuid}`;
      const r2 = await fetch(u2, { redirect: "follow" });
      const b2 = await r2.arrayBuffer();
      if (r2.ok && ehXlsx(b2)) return { buf: b2, diag: "" };
      diag = `retry status=${r2.status}`;
    }
    // ⚠️ O Drive responde 200 com a PÁGINA DE LOGIN quando o arquivo não é
    // público — nunca 401/403. Reconhecer isso é o que separa "perdeu a
    // permissão" de "o Drive está instável", que têm consertos opostos.
    const pedindoLogin = /accounts\.google\.com|\/v3\/signin/.test(txt);
    diag = `status=${res.status} ct="${res.headers.get("content-type") ?? ""}"` +
      (pedindoLogin ? " -> veio a PÁGINA DE LOGIN do Google: o arquivo não está público" : ` body="${txt.slice(0, 160).replace(/\s+/g, " ").trim()}"`);
  }
  return { diag };
}

export async function baixarXlsxDoDrive(fileId: string): Promise<ArrayBuffer> {
  let erroSa = "";
  try {
    const buf = await baixarPorServiceAccount(fileId);
    if (buf) return buf;
    erroSa = "GOOGLE_SERVICE_ACCOUNT_KEY não está no ambiente deste workflow";
  } catch (e) {
    // Não aborta: o link público ainda pode estar aberto, e nesse caso a carga
    // do dia não deve morrer porque falta um compartilhamento.
    erroSa = (e as Error).message;
    console.warn(`[drive] service account não resolveu (${erroSa}) — tentando link público`);
  }

  const { buf, diag } = await baixarPorLinkPublico(fileId);
  if (buf) return buf;

  throw new Error(
    `Falha ao baixar o xlsx do Drive (id=${fileId}).\n` +
      `  - service account: ${erroSa}\n` +
      `  - link público: ${diag}\n` +
      `  Conserto (preferido): compartilhar o arquivo como LEITOR com a service account ` +
      `(o e-mail aparece no log acima quando a chave está configurada; é a mesma ` +
      `GOOGLE_SERVICE_ACCOUNT_KEY já usada por etl/produto-fotos-drive).\n` +
      `  Alternativa: reabrir o link como "qualquer um com o link" — volta a funcionar, mas ` +
      `deixa a base de logística acessível a quem tiver a URL.`,
  );
}
