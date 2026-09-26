import type { RdDealRaw, ExtractResult } from "./extract.js";

// ── Tipos de saída ───────────────────────────────────────────────────────────

export interface RdDealRow {
  id: string;
  name: string | null;
  win: boolean | null;
  amount_total: number;
  amount_unique: number;
  amount_montly: number;
  prediction_date: string | null;
  rating: number | null;
  hold: boolean | null;
  interactions: number;
  deal_pipeline_id: string;
  deal_pipeline_name: string | null;
  deal_stage_id: string | null;
  deal_stage_name: string | null;
  deal_stage_nickname: string | null;
  user_id: string | null;
  user_name: string | null;
  user_email: string | null;
  organization_id: string | null;
  organization_name: string | null;
  cnpj: string | null;
  marca: string[];
  deal_lost_reason_id: string | null;
  deal_lost_note: string | null;
  created_at_rd: string;
  updated_at_rd: string;
  closed_at: string | null;
  raw: unknown;
}

export interface RdStageHistoryRow {
  id: string;
  deal_id: string;
  deal_stage_id: string | null;
  start_date: string | null;
  end_date: string | null;
}

export interface TransformResult {
  deals: RdDealRow[];
  histories: RdStageHistoryRow[];
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function extractMarca(deal: RdDealRaw): string[] {
  for (const cf of deal.deal_custom_fields ?? []) {
    if (cf.custom_field?.label?.trim().toLowerCase().startsWith("empresa relacionada")) {
      const val = cf.value;
      if (Array.isArray(val)) return val.filter(Boolean);
      if (typeof val === "string" && val) return [val];
    }
  }
  return [];
}

// ── Transform principal ──────────────────────────────────────────────────────

export function transformAll({ deals, orgsMap }: ExtractResult): TransformResult {
  const dealRows: RdDealRow[] = [];
  const historyRows: RdStageHistoryRow[] = [];

  for (const d of deals) {
    const orgId = d.organization?.id ?? null;
    // Join com o mapa de orgs: CNPJ extraído no loop 3
    const cnpj = orgId ? (orgsMap.get(orgId) ?? null) : null;

    dealRows.push({
      id: d.id,
      name: d.name ?? null,
      win: d.win ?? null,
      amount_total: d.amount_total ?? 0,
      amount_unique: d.amount_unique ?? 0,
      amount_montly: d.amount_montly ?? 0,
      prediction_date: d.prediction_date ?? null,
      rating: d.rating ?? null,
      hold: d.hold ?? null,
      interactions: d.interactions ?? 0,
      deal_pipeline_id: d.deal_pipeline?.id ?? "",
      deal_pipeline_name: d.deal_pipeline?.name ?? null,
      deal_stage_id: d.deal_stage?.id ?? null,
      deal_stage_name: d.deal_stage?.name ?? null,
      deal_stage_nickname: d.deal_stage?.nickname ?? null,
      user_id: d.user?.id ?? null,
      user_name: d.user?.name?.trim() ?? null,
      user_email: d.user?.email ?? null,
      organization_id: orgId,
      organization_name: d.organization?.name ?? null,
      cnpj,
      marca: extractMarca(d),
      deal_lost_reason_id: d.deal_lost_reason_id ?? null,
      deal_lost_note: d.deal_lost_note ?? null,
      created_at_rd: d.created_at,
      updated_at_rd: d.updated_at,
      closed_at: d.closed_at ?? null,
      raw: d,
    });

    for (const h of d.deal_stage_histories ?? []) {
      historyRows.push({
        id: h.id,
        deal_id: d.id,
        deal_stage_id: h.deal_stage_id ?? null,
        start_date: h.start_date ?? null,
        end_date: h.end_date ?? null,
      });
    }
  }

  return { deals: dealRows, histories: historyRows };
}
