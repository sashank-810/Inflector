import { fetchApi } from "@/lib/api";

export const opportunityStatuses = [
  "ineligible",
  "implemented_components_unavailable",
  "partial_component_set",
  "final_score_available"
] as const;

export const opportunitySorts = [
  "knowledge_cutoff_desc",
  "opportunity_score_desc",
  "confidence_desc",
  "coverage_desc",
  "company_name_asc"
] as const;

export const auditCategories = [
  "financial_facts",
  "market_bars",
  "benchmark_bars",
  "corporate_actions",
  "announcements",
  "documents",
  "document_assets",
  "text_extractions",
  "business_events",
  "business_event_evidence",
  "quantitative_derivations",
  "quantitative_facts",
  "attention_observations",
  "attention_source_records"
] as const;

export type OpportunityStatus = (typeof opportunityStatuses)[number];
export type OpportunitySort = (typeof opportunitySorts)[number];
export type AuditCategory = (typeof auditCategories)[number];

export type OpportunityListing = {
  id: string;
  exchange: string;
  symbol: string;
  valid_from: string;
  valid_to: string | null;
  status: string;
};

export type OpportunitySecurity = {
  security_id: string;
  isin: string;
  security_type: string;
  security_status: string;
  listings: OpportunityListing[];
};

export type OpportunityCompany = {
  company_id: string;
  display_name: string;
  legal_name: string;
  sector: string;
  industry: string;
};

export type OpportunityComponentSummary = {
  component_code: string;
  score: string;
  unit: string;
  configured_top_level_weight: string;
  subfactor_weight_coverage: string;
  final_contribution: string | null;
  available_at: string | null;
  algorithm_version: string;
  missing_subfactors: string[];
  warnings: string[];
};

export type OpportunitySnapshotSummary = {
  company: OpportunityCompany;
  security: OpportunitySecurity;
  snapshot_id: string;
  algorithm_version: string;
  snapshot_status: OpportunityStatus;
  as_of_date: string;
  knowledge_cutoff: string;
  ending_fiscal_year: number;
  ending_fiscal_quarter: number;
  model_version_id: string;
  scoring_configuration_id: string;
  configuration_checksum_sha256: string;
  selected_provider_dataset_id: string | null;
  selected_filing_scope: string | null;
  final_score: string | null;
  confidence: string;
  financial_core_coverage: string;
  top_level_component_weight_coverage: string;
  available_component_codes: string[];
  missing_component_codes: string[];
  snapshot_fingerprint_sha256: string;
  created_at: string;
  components: OpportunityComponentSummary[];
};

export type OpportunityScoreListResponse = {
  items: OpportunitySnapshotSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type ModelVersion = {
  id: string;
  model_family: string;
  semantic_version: string;
  git_sha: string;
  status: string;
};

export type ScoringConfiguration = {
  id: string;
  configuration_name: string;
  configuration_version: string;
  status: string;
  checksum_sha256: string;
  effective_from: string | null;
  effective_to: string | null;
};

export type OpportunityExplanation = {
  id: string;
  factor_code: string;
  rank: number;
  raw_value: string;
  raw_unit: string;
  normalized_score: string;
  configured_weight: string;
  effective_weight: string;
  component_contribution: string;
  input_available_at: string;
  evidence_type: string;
  template_code: string;
  direction: string | null;
  evidence_manifest: Record<string, unknown>;
};

export type OpportunityComponentDetail = OpportunityComponentSummary & {
  detail: Record<string, unknown>;
  explanations: OpportunityExplanation[];
};

export type OpportunityScoreDetail = Omit<OpportunitySnapshotSummary, "components"> & {
  eligibility: { eligible: boolean; reasons: string[]; warnings: string[] };
  confidence_details: Record<string, unknown>;
  context_resolution: Record<string, unknown>;
  model_version: ModelVersion;
  scoring_configuration: ScoringConfiguration;
  components: OpportunityComponentDetail[];
};

export type CompanyResearchContext = {
  company_id: string;
  security: OpportunitySecurity;
  model_version: ModelVersion;
  scoring_configuration: ScoringConfiguration;
  latest_snapshot: OpportunitySnapshotSummary;
};

export type CompanyResearchContextListResponse = {
  items: CompanyResearchContext[];
  total: number;
  limit: number;
  offset: number;
};

export type OpportunityComponentChange = {
  component_code: string;
  current_score: string | null;
  comparison_score: string | null;
  score_delta: string | null;
  current_available: boolean;
  comparison_available: boolean;
  availability_change: "unchanged" | "added" | "removed";
};

export type OpportunityScoreChange = {
  current_snapshot_id: string;
  comparison_snapshot_id: string;
  current_status: OpportunityStatus;
  comparison_status: OpportunityStatus;
  status_changed: boolean;
  final_score_delta: string | null;
  confidence_delta: string;
  coverage_delta: string;
  newly_available_component_codes: string[];
  newly_missing_component_codes: string[];
  component_changes: OpportunityComponentChange[];
};

export type OpportunityScoreHistoryResponse = {
  items: OpportunitySnapshotSummary[];
  total: number;
  limit: number;
  offset: number;
  latest_change: OpportunityScoreChange | null;
};

export type OpportunityAuditSummary = {
  snapshot_id: string;
  snapshot_fingerprint_sha256: string;
  algorithm_version: string;
  curve_algorithm_version: string;
  opportunity_score_aggregation_version: string;
  component_algorithm_versions: Record<string, unknown>[];
  phase3_algorithm_versions: Record<string, unknown>[];
  categories: { category: AuditCategory; count: number }[];
};

export type OpportunityAuditCategoryResponse = {
  snapshot_id: string;
  snapshot_fingerprint_sha256: string;
  category: AuditCategory;
  items: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
};

type QueueQuery = {
  modelFamily: string;
  configurationChecksum?: string;
  status?: OpportunityStatus;
  sort?: OpportunitySort;
  limit?: number;
  offset?: number;
};

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  return params.toString();
}

export function getOpportunityScores(query: QueueQuery): Promise<OpportunityScoreListResponse> {
  const search = queryString({
    model_family: query.modelFamily,
    configuration_checksum_sha256: query.configurationChecksum,
    status: query.status,
    sort: query.sort,
    limit: query.limit,
    offset: query.offset
  });
  return fetchApi<OpportunityScoreListResponse>(`/api/v1/opportunity-scores?${search}`);
}

export function getOpportunityScore(snapshotId: string): Promise<OpportunityScoreDetail> {
  return fetchApi<OpportunityScoreDetail>(`/api/v1/opportunity-scores/${snapshotId}`);
}

export function getCompanyResearchContexts(
  companyId: string,
  modelFamily: string,
  limit = 100,
  offset = 0
): Promise<CompanyResearchContextListResponse> {
  const search = queryString({ model_family: modelFamily, limit, offset });
  return fetchApi<CompanyResearchContextListResponse>(
    `/api/v1/companies/${companyId}/research-contexts?${search}`
  );
}

export function getOpportunityScoreHistory(
  companyId: string,
  query: {
    modelFamily: string;
    securityId: string;
    scoringConfigurationId: string;
    limit?: number;
    offset?: number;
  }
): Promise<OpportunityScoreHistoryResponse> {
  const search = queryString({
    model_family: query.modelFamily,
    security_id: query.securityId,
    scoring_configuration_id: query.scoringConfigurationId,
    limit: query.limit,
    offset: query.offset
  });
  return fetchApi<OpportunityScoreHistoryResponse>(
    `/api/v1/companies/${companyId}/opportunity-score-history?${search}`
  );
}

export function getOpportunityScoreAudit(snapshotId: string): Promise<OpportunityAuditSummary> {
  return fetchApi<OpportunityAuditSummary>(`/api/v1/opportunity-scores/${snapshotId}/audit`);
}

export function getOpportunityScoreAuditCategory(
  snapshotId: string,
  category: AuditCategory,
  limit = 50,
  offset = 0
): Promise<OpportunityAuditCategoryResponse> {
  const search = queryString({ limit, offset });
  return fetchApi<OpportunityAuditCategoryResponse>(
    `/api/v1/opportunity-scores/${snapshotId}/audit/${encodeURIComponent(category)}?${search}`
  );
}
