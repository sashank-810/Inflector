param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$Date
)

$required = @('INFLECTOR_PRODUCTION_DATABASE_URL', 'INFLECTOR_PRODUCTION_RAW_ROOT', 'INFLECTOR_NSE_LICENSE_CLASS')
foreach ($name in $required) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required environment variable is missing: $name"
    }
}

& .\.venv\Scripts\python.exe -m inflector_data.nse_cli ingest-daily `
    --date $Date `
    --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
    --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
    --license-class $env:INFLECTOR_NSE_LICENSE_CLASS

exit $LASTEXITCODE
