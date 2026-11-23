# Run once from an elevated PowerShell on the Windows host.
# This deliberately enables WSL prerequisites only; it does not install a
# distribution on the system drive. The Ubuntu distribution will be imported
# to the E: SSD after the mandatory restart.

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

Write-Host "Enabling Windows Subsystem for Linux..."
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart

Write-Host "Enabling Virtual Machine Platform..."
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

Write-Host "Updating WSL runtime..."
wsl.exe --update

Write-Host ""
Write-Host "Prerequisites complete. Restart Windows before continuing." -ForegroundColor Yellow
Write-Host "After restart, the deployment imports Ubuntu into E:\\WSL\\Ubuntu-24.04; do not use the default Ubuntu install on C:."
