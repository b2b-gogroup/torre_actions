import { logger } from "../shared/logger.js";
import { updateWatermark } from "../shared/watermark.js";
import { extractAll } from "./extract.js";
import { transformAll } from "./transform.js";
import { loadAll } from "./load.js";

const WORKFLOW_NAME = "etl_rd_deals";

async function main() {
  const t0 = Date.now();
  logger.info("ETL rd_deals iniciado");
  await updateWatermark(WORKFLOW_NAME, { status: "running" });

  try {
    // ── Extração: 3 loops independentes ──────────────────────────────────────
    // Loop 1: deals funil Novos
    // Loop 2: deals funil ATIVOS
    // Loop 3: organizações (para obter CNPJ via custom field)
    const extracted = await extractAll();

    // ── Transform: join deals + orgsMap → rows normalizadas ──────────────────
    const { deals, histories } = transformAll(extracted);
    logger.info(`Transform: ${deals.length} deals, ${histories.length} históricos de etapa`);

    if (deals.length === 0) {
      logger.warn("Nenhum deal para carregar");
      await updateWatermark(WORKFLOW_NAME, { status: "success", rowsProcessed: 0 });
      return;
    }

    // ── Load: upsert em batches ───────────────────────────────────────────────
    const result = await loadAll(deals, histories);
    const sec = ((Date.now() - t0) / 1000).toFixed(1);

    await updateWatermark(WORKFLOW_NAME, {
      status: "success",
      rowsProcessed: result.dealsTotal,
    });

    logger.info(`ETL completo em ${sec}s`, {
      deals: result.dealsTotal,
      histories: result.historiesTotal,
      dealBatches: result.dealBatches,
      historyBatches: result.historyBatches,
    });

    await logger.summary(
      `## ETL rd_deals\n` +
      `| Métrica | Valor |\n|---|---|\n` +
      `| Deals | ${result.dealsTotal} |\n` +
      `| Históricos de etapa | ${result.historiesTotal} |\n` +
      `| Duração | ${sec}s |`
    );
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    logger.error("ETL rd_deals falhou", { error: msg });
    await updateWatermark(WORKFLOW_NAME, { status: "error", errorMessage: msg });
    await logger.summary(`## ETL rd_deals — FALHA\n\`\`\`\n${msg}\n\`\`\``);
    process.exit(1);
  }
}

main();
