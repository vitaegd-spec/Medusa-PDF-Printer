# Removes the MedusaPDF virtual printer and its loopback port(s).
# The built-in PostScript driver is left installed (it is part of Windows).
param(
    [string]$PrinterName = "MedusaPDF",
    [string]$LogFile = "$env:TEMP\medusa_uninstall_printer.log"
)

$ErrorActionPreference = "Stop"
if (Test-Path -LiteralPath $LogFile) { Remove-Item -LiteralPath $LogFile -Force }
function Log([string]$Message) {
    Add-Content -LiteralPath $LogFile -Value $Message -Encoding UTF8
    Write-Host $Message
}

try {
    if (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue) {
        Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue | Remove-PrintJob -ErrorAction SilentlyContinue
        Remove-Printer -Name $PrinterName
        Log "Removed printer '$PrinterName'"
    } else {
        Log "Printer '$PrinterName' was not installed"
    }

    Start-Sleep -Seconds 1
    $ports = Get-PrinterPort -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "MedusaPDF_127.0.0.1_*" }
    foreach ($p in $ports) {
        $inUse = Get-Printer -ErrorAction SilentlyContinue | Where-Object { $_.PortName -eq $p.Name }
        if ($inUse) {
            Log "Port $($p.Name) is still used by another printer - left in place"
            continue
        }
        try {
            Remove-PrinterPort -Name $p.Name
            Log "Removed port $($p.Name)"
        } catch {
            Log "Note: could not remove port $($p.Name) ($($_.Exception.Message))"
        }
    }

    Log "SUCCESS"
    exit 0
} catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
