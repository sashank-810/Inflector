[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OperationsProfile,
    [Parameter(Mandatory = $true)]
    [string]$ResearchProfile,
    [Parameter(Mandatory = $true)]
    [string]$ModelFamily,
    [Parameter(Mandatory = $true)]
    [string]$SymbolsFile,
    [ValidateRange(0, 2200)]
    [int]$FiscalYear = 0,
    [ValidateRange(0, 4)]
    [int]$FiscalQuarter = 0,
    [Parameter(Mandatory = $true)]
    [string]$ModelSemanticVersion,
    [Parameter(Mandatory = $true)]
    [string]$GitSha,
    [Parameter(Mandatory = $true)]
    [string]$ModelEffectiveFrom,
    [string]$PythonExecutable = '.\.venv\Scripts\python.exe'
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

$operations = Get-Content -LiteralPath $OperationsProfile -Raw | ConvertFrom-Json
$timezoneId = if ($operations.timezone -eq 'Asia/Kolkata' -and $IsWindows -ne $false) {
    'India Standard Time'
} else {
    $operations.timezone
}
$timezone = [TimeZoneInfo]::FindSystemTimeZoneById($timezoneId)
$cycleAt = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $timezone).ToString('o')

$automaticEndpoint = $operations.automatic_financial_endpoint -eq $true
if (-not $automaticEndpoint -and ($FiscalYear -lt 2000 -or $FiscalQuarter -lt 1)) {
    throw 'Explicit operations profiles require FiscalYear and FiscalQuarter.'
}
$command = if ($automaticEndpoint) { 'run-cycle-auto' } else { 'run-cycle' }
$arguments = @(
    '-m', 'inflector_data.ops_cli', $command,
    '--database-url', $env:INFLECTOR_PRODUCTION_DATABASE_URL,
    '--operations-profile', $OperationsProfile,
    '--research-profile', $ResearchProfile,
    '--model-family', $ModelFamily,
    '--symbols-file', $SymbolsFile,
    '--cycle-at', $cycleAt,
    '--raw-root', $env:INFLECTOR_PRODUCTION_RAW_ROOT,
    '--nse-license-class', $env:INFLECTOR_NSE_LICENSE_CLASS,
    '--model-semantic-version', $ModelSemanticVersion,
    '--git-sha', $GitSha,
    '--model-effective-from', $ModelEffectiveFrom
)
if (-not $automaticEndpoint) {
    $arguments += @('--fiscal-year', [string]$FiscalYear, '--fiscal-quarter', [string]$FiscalQuarter)
}
if ($operations.gdelt_enabled) {
    foreach ($name in @('INFLECTOR_GDELT_RAW_ROOT', 'INFLECTOR_GDELT_LICENSE_CLASS')) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            throw "Required environment variable is missing: $name"
        }
    }
    $arguments += @(
        '--gdelt-raw-root', $env:INFLECTOR_GDELT_RAW_ROOT,
        '--gdelt-license-class', $env:INFLECTOR_GDELT_LICENSE_CLASS
    )
}

& $PythonExecutable @arguments
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Error "Inflector scheduled cycle failed with exit code $exitCode."
}
exit $exitCode
