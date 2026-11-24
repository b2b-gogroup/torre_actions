import { logger } from "../shared/logger.js";
import { updateWatermark } from "../shared/watermark.js";
import { extractAll } from "./extract.js";
import { transformAll } from "./transform.js";
import { loadBatches } from "./load.js";
import { flushEmailsClienteTiny, flushReprovadosTiny, flushUplacesTiny, registrarNFsProcessadas } from "./extract-tiny-apice.js";
import { cadastrarClientesNovos } from "./cliente-novo.js";

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

    // Cliente novo (ainda fora de dim_cliente) é cadastrado com o que a origem traz, em vez de
    // virar o CNPJ placeholder ("Cliente não identificado"). Antes do transform, que monta o
    // clienteSet a partir de sources.dimCliente. Falhar aqui volta ao placeholder.
    try { await cadastrarClientesNovos(sources); }
    catch (e) { logger.warn(`cadastrarClientesNovos falhou (não-crítico): ${e}`); }

    const allItems = await transformAll(sources);

    if (allItems.length === 0) {
      logger.warn("Nenhum item para carregar");
      await updateWatermark(WORKFLOW_NAME, { status: "success", rowsProcessed: 0 });
      return;
    }

    const result = await loadBatches(allItems, {
      canceladasES: sources.tinyAPESCanceladas as string[],
      canceladasRJ: sources.tinyAPRJCanceladas as string[],
      abertosFalhou: sources.abertosFalhou,
      cargaEm,
      cargaRun: cargaRun ?? undefined,
    });

    // Cache de "NF já detalhada" (tiny_nf_processadas) só é gravado AQUI, depois do load —
    // nunca dentro do extract. Até 11/nov/2026 ele era gravado no fim de fetchNFsFaturadas,
    // antes de qualquer confirmação de que os itens tinham entrado em fato_pedidos: um batch
    // que falhasse no load (loadBatches não aborta por isso, só conta em batchesFailed) perdia
    // a NF para sempre, porque o próximo run pulava ela achando que já estava carregada. Com
    // batchesFailed > 0 não há como saber QUAIS NFs couberam no batch que falhou sem rastrear
    // batch a batch — então nenhuma NF deste run é cacheada, e todas são re-detalhadas no
    // próximo (upsert é idempotente; custa só a chamada extra à API do Tiny).
    if (result.batchesFailed === 0) {
      try { await registrarNFsProcessadas("tiny_es", (sources.tinyAPESProcessadas as string[]) ?? []); }
      catch (e) { logger.warn(`registrarNFsProcessadas tiny_es falhou (não-crítico): ${e}`); }
      try { await registrarNFsProcessadas("tiny_rj", (sources.tinyAPRJProcessadas as string[]) ?? []); }
      catch (e) { logger.warn(`registrarNFsProcessadas tiny_rj falhou (não-crítico): ${e}`); }
    } else {
      logger.warn(
        `${result.batchesFailed}/${result.batches} batches falharam — NFs deste run NÃO entram ` +
        `no cache tiny_nf_processadas, serão re-detalhadas no próximo run`
      );
    }

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
