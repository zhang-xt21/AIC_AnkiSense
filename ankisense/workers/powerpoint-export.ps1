param([Parameter(Mandatory=$true)][string]$Source, [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$existingPowerPoint = @(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
$application = $null
$presentation = $null
$originalSecurity = $null
try {
    $application = New-Object -ComObject PowerPoint.Application
    $originalSecurity = $application.AutomationSecurity
    $application.AutomationSecurity = 3
    # Open read-only, without a presentation window, and never enable macros.
    $presentation = $application.Presentations.Open($Source, -1, 0, 0)
    $presentation.SaveAs($Destination, 32)
} catch {
    # Do not expose paths or document content through API error messages.
    exit 1
} finally {
    if ($null -ne $presentation) { $presentation.Close() }
    if ($null -ne $application -and $null -ne $originalSecurity) { $application.AutomationSecurity = $originalSecurity }
    if ($null -ne $application -and -not $existingPowerPoint -and $application.Presentations.Count -eq 0) { $application.Quit() }
    if ($null -ne $presentation) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation) }
    if ($null -ne $application) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) }
}
