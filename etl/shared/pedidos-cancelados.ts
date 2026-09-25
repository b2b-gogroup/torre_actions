import { logger } from "./logger.js";
import { getSupabaseAdmin } from "./supabase-admin.js";
import { withRetry } from "./retry.js";

/**
 * Busca pedidos cancelados direto do banco (mercos_vendas_detalhadas)
 * e retorna um Set com os números de pedido (strings).
 * Substitui a leitura manual do arquivo pedidos_cancelados.xls.
 */
export async function loadPedidosCancelados(): Promise<Set<string>> {
  return withRetry(
    async () => {
      const sb = getSupabaseAdmin();
      const PAGE_SIZE = 1000;
      const cancelados = new Set<string>();
      let offset = 0;

      while (true) {
        const { data, error } = await sb
          .from("mercos_vendas_detalhadas")
          .select("numero_pedido")
          .eq("status_pedido", "Cancelado")
          .range(offset, offset + PAGE_SIZE - 1);

        if (error) throw new Error(`mercos_vendas_detalhadas cancelados: ${error.message}`);

        for (const row of data as { numero_pedido: unknown }[]) {
          if (row.numero_pedido !== null && row.numero_pedido !== undefined) {
            cancelados.add(String(row.numero_pedido).trim());
          }
        }

        if (data.length < PAGE_SIZE) break;
        offset += PAGE_SIZE;
      }

      logger.info(`Pedidos cancelados carregados do banco: ${cancelados.size} números únicos`);
      return cancelados;
    },
    { maxAttempts: 2, baseDelayMs: 3000, label: "mercos_vendas_detalhadas cancelados" }
  );
}
