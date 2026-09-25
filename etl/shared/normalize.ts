// Funções de normalização reutilizadas em todo o ETL
// Extraídas dos code nodes do N8N (trat_protheus, trat_tiny, trat_1, trat_valores)

/** Remove acentos, espaços e converte para minúsculas */
export function normalizeStr(s: unknown): string {
  if (!s) return "";
  return s
    .toString()
    .trim()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, "");
}

/**
 * Remove pontuação de CNPJ/CPF (pontos, traços, barras) e preenche com zeros à
 * esquerda até 14 dígitos — mesma convenção usada em dim_cliente.cnpj (CHAR(14))
 * pra CPF de pessoa física (11 dígitos). Sem o padding, um CPF nunca bate contra
 * o cadastro existente (ou cria um cadastro novo com 11 dígitos, divergente do
 * resto da base) mesmo quando o cliente já está cadastrado.
 */
export function limpaCnpj(s: unknown): string {
  if (!s) return "";
  const digits = s.toString().replace(/[.\-/\s]/g, "");
  return digits.length > 0 && digits.length < 14 ? digits.padStart(14, "0") : digits;
}

/** Converte data ISO (YYYY-MM-DD...) para inteiro YYYYMMDD */
export function dateToInt(dateStr: unknown): number | null {
  if (!dateStr) return null;
  const s = String(dateStr);
  if (s.length < 10) return null;
  return parseInt(s.substring(0, 10).replace(/-/g, ""), 10);
}

/** Extrai prefixo de marca do SKU (2 primeiros chars) */
export function skuToMarca(sku: unknown): string | null {
  if (!sku) return null;
  const prefix = String(sku).substring(0, 2).toUpperCase();
  const validas = ["AP", "BB", "BS", "KS", "LC", "RT", "AU"];
  return validas.includes(prefix) ? prefix : null;
}

/**
 * Normaliza valor numérico de formato brasileiro para number.
 * "1.234,56" → 1234.56
 */
export function normalizarNumero(valor: unknown): number {
  if (valor === null || valor === undefined || valor === "") return 0;
  if (typeof valor === "number") return valor;
  let s = String(valor).trim();
  if (s.includes(",")) {
    s = s.replace(/\./g, "").replace(",", ".");
  }
  return parseFloat(s) || 0;
}
