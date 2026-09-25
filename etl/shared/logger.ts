// Logger estruturado para GitHub Actions
// Usa ::group:: / ::endgroup:: para agrupar logs
// Usa ::error:: para annotations no PR/commit

type LogLevel = "info" | "warn" | "error" | "debug";

interface LogEntry {
  level: LogLevel;
  message: string;
  [key: string]: unknown;
}

function formatExtra(extra: Record<string, unknown>): string {
  const filtered = Object.fromEntries(
    Object.entries(extra).filter(([k]) => k !== "level" && k !== "message")
  );
  if (Object.keys(filtered).length === 0) return "";
  return " " + JSON.stringify(filtered);
}

function log(entry: LogEntry): void {
  const { level, message, ...extra } = entry;
  const ts = new Date().toISOString();
  const extraStr = formatExtra(extra);
  const line = `[${ts}] [${level.toUpperCase()}] ${message}${extraStr}`;

  if (level === "error") {
    console.error(`::error::${message}`);
    console.error(line);
  } else if (level === "warn") {
    console.warn(`::warning::${message}`);
    console.warn(line);
  } else {
    console.log(line);
  }
}

export const logger = {
  info(message: string, extra: Record<string, unknown> = {}) {
    log({ level: "info", message, ...extra });
  },
  warn(message: string, extra: Record<string, unknown> = {}) {
    log({ level: "warn", message, ...extra });
  },
  error(message: string, extra: Record<string, unknown> = {}) {
    log({ level: "error", message, ...extra });
  },
  debug(message: string, extra: Record<string, unknown> = {}) {
    if (process.env.DEBUG) {
      log({ level: "debug", message, ...extra });
    }
  },

  group(name: string) {
    console.log(`::group::${name}`);
  },
  groupEnd() {
    console.log("::endgroup::");
  },

  /** Escreve resumo no $GITHUB_STEP_SUMMARY (se disponível) */
  async summary(markdown: string) {
    const summaryPath = process.env.GITHUB_STEP_SUMMARY;
    if (!summaryPath) return;
    const { appendFile } = await import("node:fs/promises");
    await appendFile(summaryPath, markdown + "\n");
  },
};
