param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$FromDate,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$ToDate,
    [string]$Symbol,
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
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required environment variable is missing: $name"
    }
}

$cliArguments = @(
    '-m', 'inflector_data.nse_cli', 'ingest-catalyst-evidence',
    '--database-url', $env:INFLECTOR_PRODUCTION_DATABASE_URL,
    '--raw-root', $env:INFLECTOR_PRODUCTION_RAW_ROOT,
    '--license-class', $env:INFLECTOR_NSE_LICENSE_CLASS,
    '--from-date', $FromDate,
    '--to-date', $ToDate,
    '--max-announcements', $MaxAnnouncements,
    '--max-documents', $MaxDocuments
)
if (-not [string]::IsNullOrWhiteSpace($Symbol)) {
    $cliArguments += @('--symbol', $Symbol)
}

& .\.venv\Scripts\python.exe @cliArguments
exit $LASTEXITCODE
