[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath,
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,
    [Parameter(Mandatory = $true)]
    [string]$OperationsProfile,
    [Parameter(Mandatory = $true)]
    [string]$ResearchProfile,
    [Parameter(Mandatory = $true)]
    [string]$ModelFamily,
    [Parameter(Mandatory = $true)]
    [string]$SymbolsFile,
    [Parameter(Mandatory = $true)]
    [int]$FiscalYear,
    [Parameter(Mandatory = $true)]
    [int]$FiscalQuarter,
    [Parameter(Mandatory = $true)]
    [string]$ModelSemanticVersion,
    [Parameter(Mandatory = $true)]
    [string]$GitSha,
    [Parameter(Mandatory = $true)]
    [string]$ModelEffectiveFrom,
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [switch]$DryRun
)

$resolvedRepository = (Resolve-Path -LiteralPath $RepositoryPath).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonExecutable).Path
$runner = (Resolve-Path -LiteralPath (Join-Path $resolvedRepository 'scripts\run_inflector_scheduled.ps1')).Path
$profile = Get-Content -LiteralPath $OperationsProfile -Raw | ConvertFrom-Json
$runAt = [DateTime]::ParseExact($profile.scheduled_local_time, 'HH:mm', $null)
$localTimezone = [TimeZoneInfo]::Local.Id
$acceptedTimezoneIds = @('Asia/Kolkata', 'India Standard Time')
if ($profile.timezone -eq 'Asia/Kolkata' -and $localTimezone -notin $acceptedTimezoneIds) {
    throw 'Task Scheduler uses the host timezone; configure this host for Asia/Kolkata first.'
}

function Quote-Argument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$runnerArguments = @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', (Quote-Argument $runner),
    '-OperationsProfile', (Quote-Argument $OperationsProfile),
    '-ResearchProfile', (Quote-Argument $ResearchProfile),
    '-ModelFamily', (Quote-Argument $ModelFamily),
    '-SymbolsFile', (Quote-Argument $SymbolsFile),
    '-FiscalYear', [string]$FiscalYear,
    '-FiscalQuarter', [string]$FiscalQuarter,
    '-ModelSemanticVersion', (Quote-Argument $ModelSemanticVersion),
    '-GitSha', (Quote-Argument $GitSha),
    '-ModelEffectiveFrom', (Quote-Argument $ModelEffectiveFrom),
    '-PythonExecutable', (Quote-Argument $resolvedPython)
) -join ' '

$preview = [ordered]@{
    task_name = $TaskName
    executable = 'powershell.exe'
    arguments = $runnerArguments
    working_directory = $resolvedRepository
    scheduled_local_time = $profile.scheduled_local_time
    timezone = $profile.timezone
    one_instance = [bool]$profile.forbid_multiple_instances
    start_when_available = [bool]$profile.run_missed_jobs_when_available
    execution_time_limit = 'PT4H'
    secrets_in_command_line = $false
}

if ($DryRun -or $WhatIfPreference) {
    $preview | ConvertTo-Json -Depth 4
    return
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $runnerArguments `
    -WorkingDirectory $resolvedRepository
$trigger = New-ScheduledTaskTrigger -Daily -At $runAt
$settingsArguments = @{
    MultipleInstances = if ($profile.forbid_multiple_instances) { 'IgnoreNew' } else { 'Parallel' }
    ExecutionTimeLimit = (New-TimeSpan -Hours 4)
}
if ($profile.run_missed_jobs_when_available) {
    $settingsArguments['StartWhenAvailable'] = $true
}
$settings = New-ScheduledTaskSettingsSet @settingsArguments

if ($PSCmdlet.ShouldProcess($TaskName, 'Register Inflector production operations task')) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Inflector read-only research production cycle' | Out-Null
    $preview | ConvertTo-Json -Depth 4
}
