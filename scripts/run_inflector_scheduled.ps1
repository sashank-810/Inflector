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
    [Parameter(Mandatory = $true)]
    [ValidateRange(2000, 2200)]
    [int]$FiscalYear,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 4)]
    [int]$FiscalQuarter,
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

$arguments = @(
    '-m', 'inflector_data.ops_cli', 'run-cycle',
    '--database-url', $env:INFLECTOR_PRODUCTION_DATABASE_URL,
    '--operations-profile', $OperationsProfile,
    '--research-profile', $ResearchProfile,
    '--model-family', $ModelFamily,
    '--symbols-file', $SymbolsFile,
    '--fiscal-year', [string]$FiscalYear,
    '--fiscal-quarter', [string]$FiscalQuarter,
    '--cycle-at', $cycleAt,
    '--raw-root', $env:INFLECTOR_PRODUCTION_RAW_ROOT,
    '--nse-license-class', $env:INFLECTOR_NSE_LICENSE_CLASS,
    '--model-semantic-version', $ModelSemanticVersion,
    '--git-sha', $GitSha,
    '--model-effective-from', $ModelEffectiveFrom
)
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
