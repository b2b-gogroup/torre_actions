import { logger } from "../shared/logger.js";
import { updateWatermark } from "../shared/watermark.js";
import { extractAll } from "./extract.js";
import { transformAll } from "./transform.js";
import { loadBatches } from "./load.js";
import { flushEmailsClienteTiny, flushReprovadosTiny, flushUplacesTiny } from "./extract-tiny-apice.js";

const WORKFLOW_NAME = "etl_fato_pedidos";

async function main() {
  const startTime = Date.now();
  // Carimbo de chegada dos pedidos desta carga (fato_pedido_carga). É o início do ETL, não o
  // do load: é este o horário que corresponde à carga agendada que a pessoa reconhece na tela
  // ("chegou na carga das 14h30"); o load começa minutos depois, quando o extract termina.
  const cargaEm = new Date(startTime);
  const cargaRun = process.env.GITHUB_RUN_ID ?? null;
  logger.info("ETL fato_pedidos iniciado");

  await updateWatermark(WORKFLOW_NAME, { status: "running" });

  try {
    const sources = await extractAll();

    // Fora do fluxo de fato_pedidos de propósito — e-mail de cliente coletado do payload do Tiny
    // (dim_cliente.email_tiny) nunca deve derrubar a carga de pedidos se falhar.
    try { await flushEmailsClienteTiny(); }
    catch (e) { logger.warn(`flushEmailsClienteTiny falhou (não-crítico): ${e}`); }

    // Idem: o número do pedido no Uplaces (tiny_pedido_uplaces) é busca na tela, não faturamento.
    try { await flushUplacesTiny(); }
    catch (e) { logger.warn(`flushUplacesTiny falhou (não-crítico): ${e}`); }

    // Etiqueta "reprovado" do Tiny (tiny_pedido_reprovado). TEM de vir antes do load: é lá que
    // fn_aplica_pedido_reprovado() lê esta tabela. Falhar aqui só atrasa a regra.
    try { await flushReprovadosTiny(); }
    catch (e) { logger.warn(`flushReprovadosTiny falhou (não-crítico): ${e}`); }

    const allItems = await transformAll(sources);

    if (allItems.length === 0) {
      logger.warn("Nenhum item para carregar");
      await updateWatermark(WORKFLOW_NAME, { status: "success", rowsProcessed: 0 });
      return;
    }

    const result = await loadBatches(allItems, {
      canceladasES: sources.tinyAPESCanceladas as string[],
      canceladasRJ: sources.tinyAPRJCanceladas as string[],
      cargaEm,
      cargaRun: cargaRun ?? undefined,
    });
    const sec = ((Date.now() - startTime) / 1000).toFixed(1);

    await updateWatermark(WORKFLOW_NAME, {
      status: result.batchesFailed > 0 ? "error" : "success",
      rowsProcessed: result.totalItems,
      errorMessage: result.batchesFailed > 0
        ? `${result.batchesFailed}/${result.batches} batches falharam`
        : undefined,
    });

    logger.info(`ETL completo em ${sec}s`, { ...result });

    await logger.summary(
      `## ETL fato_pedidos\n| Metrica | Valor |\n|---|---|\n| Itens | ${result.totalItems} |\n| Batches | ${result.batches} |\n| Falhas | ${result.batchesFailed} |\n| Duracao | ${sec}s |`
    );

    if (result.batchesFailed > 0) process.exit(1);
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    logger.error("ETL falhou", { error: msg });
    await updateWatermark(WORKFLOW_NAME, { status: "error", errorMessage: msg });
    await logger.summary(`## ETL fato_pedidos — FALHA\n\`\`\`\n${msg}\n\`\`\``);
    process.exit(1);
  }
}

main();
