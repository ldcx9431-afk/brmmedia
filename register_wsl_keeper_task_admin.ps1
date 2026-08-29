#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$RunAsUser = "$env:COMPUTERNAME\$env:USERNAME"
)

$ErrorActionPreference = 'Stop'
$taskName = 'BRMMedia-WSL-Keeper'
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$arguments = '-d BRMMedia-Ubuntu -u root -- /bin/bash /srv/brmmedia/wsl_keepalive.sh'
$taskXml = Join-Path $env:TEMP "brmmedia-wsl-keeper-$PID.xml"
try {
    # WSL distributions are registered per Windows user, so this task must run
    # as that user rather than SYSTEM. S4U avoids a stale interactive/password
    # logon session while preserving access to the user's WSL distribution.
    # A boot trigger alone is insufficient:
    # WSL can exit later after an explicit shutdown or a client-process failure.
    # The five-minute calendar trigger recreates the keeper when that happens;
    # IgnoreNew preserves the existing keeper while it is healthy.
    $today = (Get-Date).ToString('yyyy-MM-dd')
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keep BRMMedia WSL alive and refresh its NAT address.</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT15S</Delay></BootTrigger>
    <CalendarTrigger>
      <StartBoundary>${today}T00:00:00</StartBoundary><Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition><Interval>PT5M</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled>
    <Hidden>false</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>$wsl</Command><Arguments>$arguments</Arguments></Exec></Actions>
</Task>
"@ | Set-Content -LiteralPath $taskXml -Encoding Unicode

    # Register without -Password so Task Scheduler uses the non-interactive
    # S4U logon type. The keeper only accesses local WSL and local drives.
    $taskDefinition = Get-Content -LiteralPath $taskXml -Raw
    Register-ScheduledTask -TaskName $taskName -Xml $taskDefinition `
        -User $RunAsUser -Force | Out-Null
} finally {
    Remove-Item -LiteralPath $taskXml -Force -ErrorAction SilentlyContinue
}

& schtasks.exe /Run /TN $taskName | Write-Host
if ($LASTEXITCODE -ne 0) { throw "Unable to start scheduled task $taskName" }

Write-Host "Registered and started scheduled task: $taskName"
