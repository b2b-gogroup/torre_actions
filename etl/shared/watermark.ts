import { getSupabaseAdmin } from "./supabase-admin.js";
import { logger } from "./logger.js";

interface WatermarkUpdate {
  status: "running" | "success" | "error";
  rowsProcessed?: number;
  errorMessage?: string;
}

/** Atualiza o status de um workflow na tabela etl_watermarks */
export async function updateWatermark(
  workflow: string,
  update: WatermarkUpdate
): Promise<void> {
  const sb = getSupabaseAdmin();

  const payload: Record<string, unknown> = {
    last_run_status: update.status,
    last_run_at: new Date().toISOString(),
  };

  if (update.rowsProcessed !== undefined) {
    payload.rows_processed = update.rowsProcessed;
  }
  if (update.errorMessage !== undefined) {
    payload.error_message = update.errorMessage;
  }
  if (update.status === "success") {
    payload.last_updated_at = new Date().toISOString();
    payload.error_message = null;
  }

  const { error } = await sb
    .from("etl_watermarks")
    .update(payload)
    .eq("workflow", workflow);

  if (error) {
    logger.warn(`Falha ao atualizar watermark "${workflow}": ${error.message}`);
  } else {
    logger.debug(`Watermark "${workflow}" atualizado: ${update.status}`);
  }
}
