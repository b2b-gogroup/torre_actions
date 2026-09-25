import { google } from "googleapis";
import { logger } from "./logger.js";
import { withRetry } from "./retry.js";

let sheetsApi: ReturnType<typeof google.sheets> | null = null;

function getSheetsApi() {
  if (sheetsApi) return sheetsApi;

  const keyJson = process.env.GOOGLE_SERVICE_ACCOUNT_KEY;
  if (!keyJson) {
    throw new Error("GOOGLE_SERVICE_ACCOUNT_KEY é obrigatória");
  }

  const credentials = JSON.parse(keyJson);
  const auth = new google.auth.GoogleAuth({
    credentials,
    scopes: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
  });

  sheetsApi = google.sheets({ version: "v4", auth });
  return sheetsApi;
}

/**
 * Lê uma aba inteira de uma planilha Google e retorna como array de objetos.
 * A primeira linha é usada como header.
 */
export async function fetchGoogleSheet(
  spreadsheetId: string,
  sheetName: string
): Promise<Record<string, string>[]> {
  return withRetry(
    async () => {
      const api = getSheetsApi();
      const res = await api.spreadsheets.values.get({
        spreadsheetId,
        range: sheetName,
      });

      const rows = res.data.values;
      if (!rows || rows.length < 2) {
        logger.warn(`Google Sheets ${sheetName}: planilha vazia ou só header`);
        return [];
      }

      const headers = rows[0] as string[];
      const data = rows.slice(1).map((row) => {
        const obj: Record<string, string> = {};
        headers.forEach((h, i) => {
          obj[h] = (row[i] as string) ?? "";
        });
        return obj;
      });

      logger.info(`Google Sheets "${sheetName}": ${data.length} registros`);
      return data;
    },
    { maxAttempts: 2, baseDelayMs: 5000, label: `Google Sheets "${sheetName}"` }
  );
}
