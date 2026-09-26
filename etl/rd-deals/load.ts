import { getSupabaseAdmin } from "../shared/supabase-admin.js";
import { logger } from "../shared/logger.js";
import { withRetry } from "../shared/retry.js";
import type { RdDealRow, RdStageHistoryRow } from "./transform.js";

const BATCH_SIZE = 200;

async function upsertBatch<T extends Record<string, unknown>>(
  table: string,
  rows: T[],
  onConflict: string
): Promise<void> {
  return withRetry(async () => {
    const sb = getSupabaseAdmin();
    const { error } = await sb
      .from(table)
      .upsert(rows as never[], { onConflict });
    if (error) throw new Error(`Supabase upsert "${table}": ${error.message}`);
  }, { maxAttempts: 3, baseDelayMs: 2000, label: `upsert ${table}` });
}

export interface LoadResult {
  dealsTotal: number;
  historiesTotal: number;
  dealBatches: number;
  historyBatches: number;
}

export async function loadAll(
  deals: RdDealRow[],
  histories: RdStageHistoryRow[]
): Promise<LoadResult> {
  // ── Upsert deals ──────────────────────────────────────────────────────────
  logger.group("Upsert rd_deals");
  let dealBatches = 0;

  for (let i = 0; i < deals.length; i += BATCH_SIZE) {
    const batch = deals.slice(i, i + BATCH_SIZE).map(d => ({
      ...d,
      synced_at: new Date().toISOString(),
    }));
    await upsertBatch("rd_deals", batch as Record<string, unknown>[], "id");
    dealBatches++;
    logger.info(`  Batch ${dealBatches}: ${batch.length} deals`);
  }

  logger.groupEnd();

  // ── Upsert stage histories ─────────────────────────────────────────────────
  logger.group("Upsert rd_deal_stage_histories");
  let historyBatches = 0;

  for (let i = 0; i < histories.length; i += BATCH_SIZE) {
    const batch = histories.slice(i, i + BATCH_SIZE);
    await upsertBatch("rd_deal_stage_histories", batch as Record<string, unknown>[], "id");
    historyBatches++;
    logger.info(`  Batch ${historyBatches}: ${batch.length} históricos`);
  }

  logger.groupEnd();

  return {
    dealsTotal: deals.length,
    historiesTotal: histories.length,
    dealBatches,
    historyBatches,
  };
}
