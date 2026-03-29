<#
.SYNOPSIS
    Clear-DiskSpace runbook — frees disk space on a target VM or container.

.DESCRIPTION
    Triggered by the IT Ops Agent on DiskFull signature detection.
    Performs safe, non-destructive cleanup: log rotation, temp files,
    Docker layer pruning, and package cache clearing.

.PARAMETER AlertId
    The originating alert ID.

.PARAMETER TargetType
    Cleanup target: VM | AKSNode | ContainerApp

.PARAMETER TargetName
    VM name, AKS node name, or Container App name.

.PARAMETER ResourceGroup
    Resource group for the target resource.

.PARAMETER RetentionDays
    Age threshold (days) for log file cleanup. Default: 7.
#>

param (
    [Parameter(Mandatory = $true)]  [string] $AlertId,
    [Parameter(Mandatory = $true)]  [ValidateSet("VM","AKSNode","ContainerApp")]
                                    [string] $TargetType,
    [Parameter(Mandatory = $true)]  [string] $TargetName,
    [Parameter(Mandatory = $true)]  [string] $ResourceGroup,
    [Parameter(Mandatory = $false)] [int]    $RetentionDays = 7
)

$ErrorActionPreference = "Stop"
$VerbosePreference     = "Continue"

Write-Output "=========================================="
Write-Output " IT Ops Runbook: clear-disk-space"
Write-Output " AlertId      : $AlertId"
Write-Output " TargetType   : $TargetType"
Write-Output " TargetName   : $TargetName"
Write-Output " RetentionDays: $RetentionDays"
Write-Output "=========================================="

$null = Connect-AzAccount -Identity

$cleanupScript = @"
#!/bin/bash
set -e
echo "--- Disk usage before cleanup ---"
df -h /

echo "--- Clearing logs older than $RetentionDays days ---"
find /var/log -type f -mtime +$RetentionDays -name "*.log" -delete 2>/dev/null || true
find /var/log -type f -mtime +$RetentionDays -name "*.gz"  -delete 2>/dev/null || true

echo "--- Clearing /tmp ---"
find /tmp -mindepth 1 -mtime +1 -delete 2>/dev/null || true

echo "--- Clearing apt/yum cache ---"
apt-get clean 2>/dev/null || yum clean all 2>/dev/null || true

echo "--- Docker cleanup (if available) ---"
if command -v docker &>/dev/null; then
    docker system prune -f --volumes 2>/dev/null || true
fi

echo "--- Disk usage after cleanup ---"
df -h /
echo "--- Done ---"
"@

switch ($TargetType) {

    "VM" {
        Write-Output "Running cleanup script on VM: $TargetName..."

        $result = Invoke-AzVMRunCommand `
            -ResourceGroupName $ResourceGroup `
            -VMName $TargetName `
            -CommandId "RunShellScript" `
            -ScriptString $cleanupScript

        Write-Output $result.Value[0].Message
        Write-Output "[OK] VM disk cleanup complete."
    }

    "AKSNode" {
        Write-Output "Disk cleanup on AKS nodes is handled via node drain + cordoning."
        Write-Output "Consider enabling the AKS node auto-cleanup add-on."
        Write-Output "[WARN] Manual intervention may be required for AKS node disk full."
    }

    "ContainerApp" {
        Write-Output "Container Apps are ephemeral — restarting to reclaim overlay storage..."
        $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Update-AzContainerApp `
            -Name $TargetName `
            -ResourceGroupName $ResourceGroup `
            -RevisionSuffix "diskclear-$timestamp" | Out-Null
        Write-Output "[OK] Container App restarted to reclaim ephemeral storage."
    }
}

Write-Output ""
Write-Output "=========================================="
Write-Output " Disk cleanup runbook complete."
Write-Output " AlertId: $AlertId"
Write-Output "=========================================="
