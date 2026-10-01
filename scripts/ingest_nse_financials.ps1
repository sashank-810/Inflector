param(
    [string]$Symbol,
    [string]$SymbolsFile,
    [ValidateRange(1, 100)]
    [int]$MaxSymbols = 25,
    [ValidateRange(1, 100)]
    [int]$MaxFilings = 20,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$FromDate,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$ToDate
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

if ([string]::IsNullOrWhiteSpace($Symbol) -eq [string]::IsNullOrWhiteSpace($SymbolsFile)) {
    throw 'Supply exactly one of -Symbol or -SymbolsFile.'
}
if ([string]::IsNullOrWhiteSpace($FromDate) -ne [string]::IsNullOrWhiteSpace($ToDate)) {
    throw 'Supply -FromDate and -ToDate together.'
}

$cliArguments = @(
    '-m', 'inflector_data.nse_cli', 'ingest-financials',
    '--database-url', $env:INFLECTOR_PRODUCTION_DATABASE_URL,
    '--raw-root', $env:INFLECTOR_PRODUCTION_RAW_ROOT,
    '--license-class', $env:INFLECTOR_NSE_LICENSE_CLASS,
    '--max-symbols', $MaxSymbols,
    '--max-filings', $MaxFilings
)
if (-not [string]::IsNullOrWhiteSpace($Symbol)) {
    $cliArguments += @('--symbol', $Symbol)
} else {
    $cliArguments += @('--symbols-file', $SymbolsFile)
}
if (-not [string]::IsNullOrWhiteSpace($FromDate)) {
    $cliArguments += @('--from-date', $FromDate, '--to-date', $ToDate)
}

& .\.venv\Scripts\python.exe @cliArguments
exit $LASTEXITCODE
