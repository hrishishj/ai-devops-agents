<#
.SYNOPSIS
    Scale-Out-AKS runbook — scales up an AKS node pool or deployment.

.DESCRIPTION
    Triggered by the IT Ops Agent on high CPU / memory pressure signals.
    Supports:
      - Node pool scale-out (adds nodes to the cluster)
      - Deployment replica scale-out (adds pods to a deployment)

.PARAMETER AlertId       Originating alert ID.
.PARAMETER ClusterName   AKS cluster name.
.PARAMETER ResourceGroup Resource group for the AKS cluster.
.PARAMETER ScaleTarget   NodePool | Deployment
.PARAMETER TargetName    Node pool name or deployment name.
.PARAMETER ScaleCount    Number of nodes/replicas to add (default: 2).
.PARAMETER Namespace     Kubernetes namespace (Deployment only).
.PARAMETER MaxNodes      Maximum node count ceiling (default: 10).
#>

param (
    [Parameter(Mandatory = $true)]  [string] $AlertId,
    [Parameter(Mandatory = $true)]  [string] $ClusterName,
    [Parameter(Mandatory = $true)]  [string] $ResourceGroup,
    [Parameter(Mandatory = $true)]  [ValidateSet("NodePool","Deployment")]
                                    [string] $ScaleTarget,
    [Parameter(Mandatory = $true)]  [string] $TargetName,
    [Parameter(Mandatory = $false)] [int]    $ScaleCount  = 2,
    [Parameter(Mandatory = $false)] [string] $Namespace   = "default",
    [Parameter(Mandatory = $false)] [int]    $MaxNodes    = 10
)

$ErrorActionPreference = "Stop"
Write-Output "=========================================="
Write-Output " IT Ops Runbook: scale-out-aks"
Write-Output " AlertId    : $AlertId"
Write-Output " Cluster    : $ClusterName"
Write-Output " ScaleTarget: $ScaleTarget / $TargetName"
Write-Output " ScaleCount : +$ScaleCount"
Write-Output "=========================================="

$null = Connect-AzAccount -Identity

switch ($ScaleTarget) {

    "NodePool" {
        $nodePool = Get-AzAksNodePool `
            -ClusterName $ClusterName `
            -ResourceGroupName $ResourceGroup `
            -Name $TargetName

        if ($null -eq $nodePool) {
            throw "Node pool '$TargetName' not found."
        }

        $currentCount = $nodePool.Count
        $newCount     = [Math]::Min($currentCount + $ScaleCount, $MaxNodes)

        if ($newCount -le $currentCount) {
            Write-Output "[SKIP] Already at maximum node count ($currentCount). No scale-out."
        } else {
            Write-Output "Scaling node pool '$TargetName': $currentCount → $newCount nodes..."
            Update-AzAksNodePool `
                -ClusterName  $ClusterName `
                -ResourceGroupName $ResourceGroup `
                -Name         $TargetName `
                -NodeCount    $newCount | Out-Null
            Write-Output "[OK] Node pool scaled to $newCount nodes."
        }
    }

    "Deployment" {
        Import-AzAksCredential `
            -ResourceGroupName $ResourceGroup `
            -Name $ClusterName `
            -Force | Out-Null

        # Get current replica count
        $currentRaw = kubectl get deployment $TargetName -n $Namespace `
            -o jsonpath='{.spec.replicas}' 2>&1
        $currentReplicas = [int]$currentRaw
        $newReplicas     = $currentReplicas + $ScaleCount

        Write-Output "Scaling deployment '$TargetName': $currentReplicas → $newReplicas replicas..."

        $result = kubectl scale deployment $TargetName `
            --replicas=$newReplicas `
            -n $Namespace 2>&1

        if ($LASTEXITCODE -ne 0) {
            throw "kubectl scale failed: $result"
        }
        Write-Output "[OK] Deployment scaled: $result"
    }
}

Write-Output ""
Write-Output "=========================================="
Write-Output " Scale-out complete. AlertId: $AlertId"
Write-Output "=========================================="
