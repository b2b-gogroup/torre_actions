/**
 * Cliente base para a API REST do Tiny ERP v2.
 * Documentação: https://tiny.com.br/api-docs
 */
import { logger } from "./logger.js";

const BASE_URL = "https://api.tiny.com.br/api2";
const DELAY_MS = 1000; // delay entre chamadas — 1s garante max ~2 calls/sec com concorrencia 2

async function sleep(ms: number) {
  return new Promise(r => setTimeout(r, ms));
}

export async function tinyPost(
  endpoint: string,
  token: string,
  params: Record<string, string | number>,
  attempt = 0
): Promise<Record<string, unknown>> {
  const body = new URLSearchParams({
    token,
    formato: "json",
    ...Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])),
  });

  const res = await fetch(`${BASE_URL}/${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    signal: AbortSignal.timeout(30_000),
  });

  if (!res.ok) throw new Error(`Tiny API HTTP ${res.status} — ${endpoint}`);
  const json = await res.json() as Record<string, unknown>;
  const retorno = (json.retorno ?? {}) as Record<string, unknown>;

  // Erro 6 = rate limit — retry com backoff exponencial (máx 3 tentativas)
  if (retorno.codigo_erro === 6 || retorno.codigo_erro === "6") {
    if (attempt >= 3) throw new Error(`Tiny API rate limit após ${attempt} tentativas: ${endpoint}`);
    const backoff = 2000 * Math.pow(2, attempt); // 2s, 4s, 8s
    await sleep(backoff);
    return tinyPost(endpoint, token, params, attempt + 1);
  }

  if (retorno.status === "Erro" && retorno.codigo_erro !== 20) {
    throw new Error(`Tiny API erro ${retorno.codigo_erro}: ${retorno.mensagem ?? endpoint}`);
  }
  return retorno;
}

/** Busca todas as páginas de uma pesquisa */
export async function tinyPesquisarTodas(
  endpoint: string,
  token: string,
  params: Record<string, string | number>,
  listKey: string
): Promise<Record<string, unknown>[]> {
  const all: Record<string, unknown>[] = [];
  let pagina = 1;

  while (true) {
    await sleep(DELAY_MS);
    const ret = await tinyPost(endpoint, token, { ...params, pagina });
    const items = (ret[listKey] as unknown[] | undefined) ?? [];
    all.push(...(items as Record<string, unknown>[]));
    const numPags = parseInt(String(ret.numero_paginas ?? 1), 10);
    logger.debug(`${endpoint} pág ${pagina}/${numPags}: ${items.length} itens`);
    if (pagina >= numPags) break;
    pagina++;
  }

  return all;
}

/** Busca detalhe de uma NF fiscal */
export async function tinyObterNF(
  token: string,
  id: string | number
): Promise<Record<string, unknown>> {
  await sleep(DELAY_MS);
  const ret = await tinyPost("nota.fiscal.obter.php", token, { id });
  return (ret.nota_fiscal ?? {}) as Record<string, unknown>;
}

/** Busca detalhe de um pedido */
export async function tinyObterPedido(
  token: string,
  id: string | number
): Promise<Record<string, unknown>> {
  await sleep(DELAY_MS);
  const ret = await tinyPost("pedido.obter.php", token, { id });
  return (ret.pedido ?? {}) as Record<string, unknown>;
}

/**
 * Executa chamadas de detalhe com concorrência limitada.
 * CONCURRENCY=5 → 5 calls simultâneas, 500ms cada = ~10 calls/sec.
 * Reduz tempo 5x vs sequencial, ainda dentro do rate limit do Tiny.
 */
export async function tinyProcessarConcorrente<T, R>(
  items: T[],
  fn: (item: T) => Promise<R | null>,
  concurrency = 5
): Promise<(R | null)[]> {
  const results: (R | null)[] = new Array(items.length).fill(null);
  let idx = 0;

  async function worker() {
    while (idx < items.length) {
      const i = idx++;
      try {
        results[i] = await fn(items[i]);
      } catch {
        results[i] = null;
      }
    }
  }

  await Promise.all(Array.from({ length: concurrency }, worker));
  return results;
}
