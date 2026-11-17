# Creates the MedusaPDF virtual printer:
#   built-in Windows PostScript driver  ->  Standard TCP/IP port 127.0.0.1:<Port> (RAW)  ->  MedusaPDF app
# Must run as administrator (MedusaPDF launches it with a UAC prompt).
param(
    [string]$PrinterName = "MedusaPDF",
    [int]$Port = 9100,
    [string]$PaperSize = "A4",
    [string]$LegacyName = "MedussaPDF",
    [string]$LogFile = "$env:TEMP\medusa_install_printer.log"
)

$ErrorActionPreference = "Stop"
if (Test-Path -LiteralPath $LogFile) { Remove-Item -LiteralPath $LogFile -Force }
function Log([string]$Message) {
    Add-Content -LiteralPath $LogFile -Value $Message -Encoding UTF8
    Write-Host $Message
}

try {
    $portName = "MedusaPDF_127.0.0.1_$Port"

    # 1. PostScript driver (both ship with Windows 10/11; colour-capable)
    $driver = $null
    foreach ($candidate in @("Microsoft PS Class Driver", "MS Publisher Color Printer")) {
        if (Get-PrinterDriver -Name $candidate -ErrorAction SilentlyContinue) { $driver = $candidate; break }
        try {
            Add-PrinterDriver -Name $candidate -ErrorAction Stop
            $driver = $candidate
            Log "Installed driver: $candidate"
            break
        } catch {
            Log "Driver '$candidate' not available: $($_.Exception.Message)"
        }
    }
    if (-not $driver) { throw "No built-in PostScript printer driver was found on this PC." }
    Log "Using driver: $driver"

    # 1b. Remove the printer/ports left by the old (misspelled) MedussaPDF name
    if ($LegacyName -and $LegacyName -ne $PrinterName) {
        if (Get-Printer -Name $LegacyName -ErrorAction SilentlyContinue) {
            Get-PrintJob -PrinterName $LegacyName -ErrorAction SilentlyContinue | Remove-PrintJob -ErrorAction SilentlyContinue
            Remove-Printer -Name $LegacyName
            Log "Removed old printer '$LegacyName'"
            Start-Sleep -Seconds 1
        }
        Get-PrinterPort -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "$($LegacyName)_127.0.0.1_*" } | ForEach-Object {
            $p = $_
            if (-not (Get-Printer -ErrorAction SilentlyContinue | Where-Object { $_.PortName -eq $p.Name })) {
                try { Remove-PrinterPort -Name $p.Name; Log "Removed old port $($p.Name)" }
                catch { Log "Note: could not remove old port $($p.Name) ($($_.Exception.Message))" }
            }
        }
    }

    # 2. Remove an existing printer with the same name (re-install / port change)
    if (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue) {
        Remove-Printer -Name $PrinterName
        Log "Removed existing printer '$PrinterName'"
    }

    # 3. Loopback TCP/IP port (RAW, SNMP off)
    if (-not (Get-PrinterPort -Name $portName -ErrorAction SilentlyContinue)) {
        Add-PrinterPort -Name $portName -PrinterHostAddress "127.0.0.1" -PortNumber $Port
        Log "Created port $portName"
    } else {
        Log "Port $portName already exists"
    }

    # 4. Printer
    Add-Printer -Name $PrinterName -DriverName $driver -PortName $portName
    Log "Created printer '$PrinterName'"

    try {
        Set-PrintConfiguration -PrinterName $PrinterName -PaperSize $PaperSize -Color $true
        Log "Default paper size: $PaperSize, colour enabled"
    } catch {
        Log "Note: could not set paper size ($($_.Exception.Message))"
    }

    Log "SUCCESS"
    exit 0
} catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
