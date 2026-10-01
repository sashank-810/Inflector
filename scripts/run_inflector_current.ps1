param(
    [Parameter(Mandatory = $true)]
    [string]$SymbolsFile,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$MarketFromDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$MarketToDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$FinancialFromDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$FinancialToDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$CatalystFromDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$CatalystToDate,
    [Parameter(Mandatory = $true)]
    [ValidateRange(2000, 2200)]
    [int]$FiscalYear,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 4)]
    [int]$FiscalQuarter,
    [Parameter(Mandatory = $true)]
    [string]$KnowledgeCutoff,
    [Parameter(Mandatory = $true)]
    [string]$ModelFamily,
    [Parameter(Mandatory = $true)]
    [string]$ModelSemanticVersion,
    [Parameter(Mandatory = $true)]
    [string]$GitSha,
    [Parameter(Mandatory = $true)]
    [string]$EffectiveFrom,
    [string]$ResearchProfile = 'config/research/production_research_v1.json',
    [switch]$IngestGdeltNews,
    [ValidateRange(1, 100)]
    [int]$MaxSymbols = 25,
    [ValidateRange(1, 100)]
    [int]$MaxFilings = 20,
    [ValidateRange(1, 1000)]
    [int]$MaxAnnouncements = 100,
    [ValidateRange(0, 500)]
    [int]$MaxDocuments = 100
)

$required = @(
    'INFLECTOR_PRODUCTION_DATABASE_URL',
    'INFLECTOR_PRODUCTION_RAW_ROOT',
    'INFLECTOR_NSE_LICENSE_CLASS'
)
if ($IngestGdeltNews) {
    $required += @('INFLECTOR_GDELT_RAW_ROOT', 'INFLECTOR_GDELT_LICENSE_CLASS')
}
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required environment variable is missing: $name"
    }
}

function Invoke-Stage {
    param([string]$Name, [string[]]$Arguments)
    Write-Host "Starting stage: $Name"
    & .\.venv\Scripts\python.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Stage failed: $Name"
    }
}

$databaseUrl = $env:INFLECTOR_PRODUCTION_DATABASE_URL
$rawRoot = $env:INFLECTOR_PRODUCTION_RAW_ROOT
$nseLicense = $env:INFLECTOR_NSE_LICENSE_CLASS

Invoke-Stage 'NSE universe' @(
    '-m', 'inflector_data.nse_cli', 'ingest-universe',
    '--database-url', $databaseUrl, '--raw-root', $rawRoot,
    '--license-class', $nseLicense
)
Invoke-Stage 'NSE market and benchmark range' @(
    '-m', 'inflector_data.nse_cli', 'ingest-market-range',
    '--database-url', $databaseUrl, '--raw-root', $rawRoot,
    '--license-class', $nseLicense,
    '--from-date', $MarketFromDate, '--to-date', $MarketToDate
)
Invoke-Stage 'NSE financial bootstrap' @(
    '-m', 'inflector_data.nse_cli', 'ingest-financials',
    '--database-url', $databaseUrl, '--raw-root', $rawRoot,
    '--license-class', $nseLicense, '--symbols-file', $SymbolsFile,
    '--max-symbols', $MaxSymbols, '--max-filings', $MaxFilings,
    '--from-date', $FinancialFromDate, '--to-date', $FinancialToDate
)
Invoke-Stage 'NSE corporate actions' @(
    '-m', 'inflector_data.nse_cli', 'ingest-corporate-actions',
    '--database-url', $databaseUrl, '--raw-root', $rawRoot,
    '--license-class', $nseLicense,
    '--from-date', $CatalystFromDate, '--to-date', $CatalystToDate
)
Invoke-Stage 'NSE catalyst evidence' @(
    '-m', 'inflector_data.nse_cli', 'ingest-catalyst-evidence',
    '--database-url', $databaseUrl, '--raw-root', $rawRoot,
    '--license-class', $nseLicense,
    '--from-date', $CatalystFromDate, '--to-date', $CatalystToDate,
    '--max-announcements', $MaxAnnouncements, '--max-documents', $MaxDocuments
)
if ($IngestGdeltNews) {
    Invoke-Stage 'GDELT news attention' @(
        '-m', 'inflector_data.research_cli', 'ingest-gdelt-news',
        '--database-url', $databaseUrl, '--research-profile', $ResearchProfile,
        '--symbols-file', $SymbolsFile, '--max-symbols', $MaxSymbols,
        '--knowledge-cutoff', $KnowledgeCutoff,
        '--gdelt-raw-root', $env:INFLECTOR_GDELT_RAW_ROOT,
        '--gdelt-license-class', $env:INFLECTOR_GDELT_LICENSE_CLASS
    )
}
Invoke-Stage 'Production model initialization' @(
    '-m', 'inflector_data.research_cli', 'init-model',
    '--database-url', $databaseUrl, '--research-profile', $ResearchProfile,
    '--model-family', $ModelFamily,
    '--model-semantic-version', $ModelSemanticVersion,
    '--git-sha', $GitSha, '--effective-from', $EffectiveFrom
)
Invoke-Stage 'Current V5 research' @(
    '-m', 'inflector_data.research_cli', 'run-current',
    '--database-url', $databaseUrl, '--research-profile', $ResearchProfile,
    '--model-family', $ModelFamily, '--symbols-file', $SymbolsFile,
    '--max-symbols', $MaxSymbols, '--fiscal-year', $FiscalYear,
    '--fiscal-quarter', $FiscalQuarter, '--knowledge-cutoff', $KnowledgeCutoff
)
