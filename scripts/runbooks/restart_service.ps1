<#
.SYNOPSIS
    Restart-Service runbook — restarted the specified service or AKS deployment.

.DESCRIPTION
    Triggered by the IT Ops Agent when confidence >= threshold and the
    identified remediation is a service restart.

    Supports:
      - Azure Container Apps: restarts a container app revision
      - AKS Deployments:     rolls out a zero-downtime restart
      - App Service:         restarts a web app

.PARAMETER AlertId
    The originating alert ID (for logging / audit).

.PARAMETER ServiceType
    Type of service: ContainerApp | AKS | AppService

.PARAMETER ServiceName
    Name of the service/app/deployment to restart.

.PARAMETER ResourceGroup
    Azure resource group containing the service.

.PARAMETER Namespace
    (AKS only) Kubernetes namespace of the deployment.

.PARAMETER ClusterName
    (AKS only) AKS cluster name.
#>

param (
    [Parameter(Mandatory = $true)]  [string] $AlertId,
    [Parameter(Mandatory = $true)]  [ValidateSet("ContainerApp","AKS","AppService")]
                                    [string] $ServiceType,
    [Parameter(Mandatory = $true)]  [string] $ServiceName,
    [Parameter(Mandatory = $true)]  [string] $ResourceGroup,
    [Parameter(Mandatory = $false)] [string] $Namespace    = "default",
    [Parameter(Mandatory = $false)] [string] $ClusterName  = ""
)

$ErrorActionPreference = "Stop"
$VerbosePreference     = "Continue"

Write-Output "=========================================="
Write-Output " IT Ops Runbook: restart-service"
Write-Output " AlertId      : $AlertId"
Write-Output " ServiceType  : $ServiceType"
Write-Output " ServiceName  : $ServiceName"
Write-Output " ResourceGroup: $ResourceGroup"
Write-Output "=========================================="

# Authenticate with Managed Identity
try {
    $null = Connect-AzAccount -Identity
    Write-Output "[OK] Authenticated via Managed Identity."
} catch {
    Write-Error "Authentication failed: $_"
    throw
}

$startTime = Get-Date

switch ($ServiceType) {

    "ContainerApp" {
        Write-Output "Restarting Container App: $ServiceName..."

        # Get the active revision and force a new one
        $app = Get-AzContainerApp `
            -Name $ServiceName `
            -ResourceGroupName $ResourceGroup

        if ($null -eq $app) {
            throw "Container App '$ServiceName' not found in '$ResourceGroup'."
        }

        # Trigger revision by patching a label
        $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Update-AzContainerApp `
            -Name $ServiceName `
            -ResourceGroupName $ResourceGroup `
            -RevisionSuffix "restart-$timestamp" | Out-Null

        Write-Output "[OK] Container App revision triggered."
    }

    "AKS" {
        Write-Output "Rolling restart of AKS deployment: $ServiceName in $Namespace..."

        if ([string]::IsNullOrEmpty($ClusterName)) {
            throw "ClusterName is required for AKS restarts."
        }

        # Get kubeconfig credentials
        Import-AzAksCredential `
            -ResourceGroupName $ResourceGroup `
            -Name $ClusterName `
            -Force | Out-Null

        # kubectl rollout restart — zero-downtime
        $result = kubectl rollout restart `
            "deployment/$ServiceName" `
            -n $Namespace 2>&1

        if ($LASTEXITCODE -ne 0) {
            throw "kubectl rollout restart failed: $result"
        }
        Write-Output "[OK] AKS rollout restart issued: $result"
    }

    "AppService" {
        Write-Output "Restarting App Service: $ServiceName..."

        Restart-AzWebApp `
            -Name $ServiceName `
            -ResourceGroupName $ResourceGroup | Out-Null

        Write-Output "[OK] App Service restarted."
    }
}

$elapsed = (Get-Date) - $startTime
Write-Output ""
Write-Output "=========================================="
Write-Output " Restart complete in $($elapsed.TotalSeconds.ToString('F1'))s"
Write-Output " AlertId: $AlertId"
Write-Output "=========================================="
