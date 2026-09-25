import { logger } from "./logger.js";

interface RetryOptions {
  maxAttempts?: number;
  baseDelayMs?: number;
  label?: string;
}

/** Executa fn com retry e backoff exponencial */
export async function withRetry<T>(
  fn: () => Promise<T>,
  opts: RetryOptions = {}
): Promise<T> {
  const { maxAttempts = 3, baseDelayMs = 2000, label = "operation" } = opts;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      const isLast = attempt === maxAttempts;
      const msg = err instanceof Error ? err.message : String(err);

      if (isLast) {
        logger.error(`${label}: falhou após ${maxAttempts} tentativas`, { error: msg });
        throw err;
      }

      const delay = baseDelayMs * Math.pow(2, attempt - 1);
      logger.warn(`${label}: tentativa ${attempt}/${maxAttempts} falhou, retry em ${delay}ms`, {
        error: msg,
      });
      await new Promise((r) => setTimeout(r, delay));
    }
  }

  throw new Error("unreachable");
}
