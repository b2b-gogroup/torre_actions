import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { logger } from "./logger.js";
import { withRetry } from "./retry.js";

let client: SupabaseClient | null = null;

export function getSupabaseAdmin(): SupabaseClient {
  if (client) return client;

  const url = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!url || !key) {
    throw new Error("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórias");
  }

  client = createClient(url, key);
  return client;
}

/** Busca todos os registros de uma tabela dimensão (paginado para contornar limite de 1000 do Supabase) */
export async function fetchSupabaseTable(
  table: string
): Promise<Record<string, unknown>[]> {
  return withRetry(
    async () => {
      const sb = getSupabaseAdmin();
      const PAGE_SIZE = 1000;
      const allRows: Record<string, unknown>[] = [];
      let offset = 0;

      while (true) {
        const { data, error } = await sb
          .from(table)
          .select("*")
          .range(offset, offset + PAGE_SIZE - 1);

        if (error) {
          throw new Error(`Supabase ${table}: ${error.message}`);
        }

        allRows.push(...(data as Record<string, unknown>[]));

        if (data.length < PAGE_SIZE) break;
        offset += PAGE_SIZE;
      }

      logger.info(`Supabase "${table}": ${allRows.length} registros`);
      return allRows;
    },
    { maxAttempts: 2, baseDelayMs: 3000, label: `Supabase "${table}"` }
  );
}
