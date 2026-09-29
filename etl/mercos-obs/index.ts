/**
 * ETL Mercos OBS → Supabase
 *
 * Puxa as observações dos pedidos Mercos do Metabase (card 19024 "obs-mercos":
 * int_branch, so_numero, so_observacoes, so_observacoes_internas) e aplica em
 * mercos_vendas_detalhadas.observacoes / .observacoes_internas, casando por
 * (regiao, numero_pedido).
 *
 * `so_observacoes` = observação que VAI para a NF. `so_observacoes_internas` =
 * campo extra do Mercos "Observações Internas (Não vai para a NF)", extraído
 * do JSON so_extras pelo próprio card (desde 02/set/2026) — pelo NOME do campo,
 * porque o campo_extra_id difere por filial (75737 ES, 75738 RJ).
 *
 * Idempotente: só UPDATE (não trunca). Re-aplica a cada execução, então mesmo
 * que mercos_vendas_detalhadas seja recarregada, a próxima rodada repõe as obs.
 *
 * Roda via GitHub Actions (.github/workflows/etl-mercos-obs.yml).
 *
 * Uso local:
 *   cd etl && npm ci
 *   METABASE_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... npx tsx mercos-obs/index.ts
 */

import { createClient } from "@supabase/supabase-js";
import { fetchMetabaseCard } from "../shared/metabase-client.js";

const CARD_OBS = 19024;
const FILIAL_REGIAO: Record<string, string> = { "1301": "ES", "1302": "SP", "1303": "RJ" };

const SUPABASE_URL = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error("[etl-mercos-obs] Faltam env: SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY");
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY, { auth: { persistSession: false } });

async function main() {
  const t0 = Date.now();
  console.log("[etl-mercos-obs] iniciando…");

  // 1) Busca o card 19024 no Metabase
  const rows = await fetchMetabaseCard(CARD_OBS);
  console.log(`[etl-mercos-obs] card ${CARD_OBS}: ${rows.length} linhas`);

  // 2) Monta payload {regiao, numero, obs, obs_interna} — entra a linha que tem
  //    QUALQUER uma das duas preenchidas. Campo vazio nunca apaga valor já
  //    gravado (fn_aplicar_obs_mercos só escreve o que veio preenchido).
  const payload: { regiao: string; numero: number; obs: string; obs_interna: string }[] = [];
  let comObs = 0, comInterna = 0;
  for (const r of rows) {
    const regiao = FILIAL_REGIAO[String(r.int_branch ?? "").trim()];
    const numero = parseInt(String(r.so_numero ?? "").replace(/\D/g, ""), 10);
    const obs = String(r.so_observacoes ?? "").trim();
    const obsInterna = String(r.so_observacoes_internas ?? "").trim();
    if (regiao && numero && (obs || obsInterna)) {
      payload.push({ regiao, numero, obs, obs_interna: obsInterna });
      if (obs) comObs++;
      if (obsInterna) comInterna++;
    }
  }
  console.log(`[etl-mercos-obs] obs preenchidas: ${comObs} · internas: ${comInterna} · linhas no payload: ${payload.length}`);

  // 3) Aplica em lotes via RPC (UPDATE por regiao+numero_pedido)
  const BATCH = 1000;
  let aplicados = 0;
  for (let i = 0; i < payload.length; i += BATCH) {
    const chunk = payload.slice(i, i + BATCH);
    const { data, error } = await supabase.rpc("fn_aplicar_obs_mercos", { p_obs: chunk });
    if (error) throw new Error(`fn_aplicar_obs_mercos (lote ${i}): ${error.message}`);
    aplicados += Number(data) || 0;
  }

  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[etl-mercos-obs] OK — ${aplicados} linhas atualizadas em ${sec}s`);

  // 4) Alerta WhatsApp: pedidos Concluído + observação NOVOS → grupo (via whatsapp_outbox).
  //    Destino sobrescrevível por env; default = grupo "Home Ops". Não-crítico.
  const destino = process.env.ALERTA_PEDIDO_OBS_DESTINO ?? "120363425638669474@g.us";
  const { data: alertas, error: errAlerta } = await supabase.rpc(
    "fn_disparar_alertas_pedido_obs", { p_destino: destino }
  );
  if (errAlerta) console.error(`[etl-mercos-obs] alerta WhatsApp falhou (não crítico): ${errAlerta.message}`);
  else console.log(`[etl-mercos-obs] alertas WhatsApp enfileirados: ${alertas ?? 0}`);
}

main().catch((e) => {
  console.error("[etl-mercos-obs] crash:", e instanceof Error ? e.message : e);
  process.exit(1);
});
