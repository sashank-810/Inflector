export type Listing = {
  id: string;
  exchange: string;
  symbol: string;
  valid_from: string;
  valid_to: string | null;
  status: string;
};

export type Security = {
  id: string;
  isin: string;
  security_type: string;
  status: string;
  listings: Listing[];
};

export type Company = {
  id: string;
  legal_name: string;
  display_name: string;
  sector: string;
  industry: string;
  created_at: string;
  updated_at: string;
  securities: Security[];
};

type CompanyListResponse = {
  items: Company[];
  total: number;
  limit: number;
  offset: number;
};

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

function getApiBaseUrl(): string {
  const value = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (!value) {
    throw new ApiError("NEXT_PUBLIC_API_BASE_URL is not configured.");
  }
  return value.replace(/\/$/, "");
}

async function fetchApi<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, { cache: "no-store" });
  } catch {
    throw new ApiError("The Inflector API is unavailable. Start the local FastAPI service and try again.");
  }

  if (!response.ok) {
    if (response.status === 404) {
      throw new ApiError("The requested company was not found.", response.status);
    }
    throw new ApiError("The Inflector API could not complete this request.", response.status);
  }
  return (await response.json()) as T;
}

export function getCompanies(): Promise<CompanyListResponse> {
  return fetchApi<CompanyListResponse>("/api/v1/companies");
}

export function getCompany(companyId: string): Promise<Company> {
  return fetchApi<Company>(`/api/v1/companies/${companyId}`);
}
