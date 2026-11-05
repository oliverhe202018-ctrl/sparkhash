param(
    [string]$RpcUrl = "https://bsc-testnet-rpc.publicnode.com"
)

$jsonPath = "broadcast/Deploy.s.sol/97/run-latest.json"
if (-not (Test-Path $jsonPath)) {
    $jsonPath = "broadcast/Deploy.s.sol/97/dry-run/run-latest.json"
}

if (-not (Test-Path $jsonPath)) {
    Write-Error "Deployment broadcast JSON not found: $jsonPath"
    exit 1
}

$run = Get-Content $jsonPath -Raw | ConvertFrom-Json

foreach ($tx in $run.transactions) {
    if ($tx.transactionType -eq "CREATE") {
        if ($tx.contractName -eq "TimelockController") { $env:DEPLOYED_TIMELOCK = $tx.contractAddress }
        if ($tx.contractName -eq "Token") { $env:DEPLOYED_TOKEN = $tx.contractAddress }
        if ($tx.contractName -eq "StakeVault") { $env:DEPLOYED_VAULT = $tx.contractAddress }
        if ($tx.contractName -eq "MinerV1") { $env:DEPLOYED_MINER_IMPL = $tx.contractAddress }
        if ($tx.contractName -eq "TransparentUpgradeableProxy") { $env:DEPLOYED_PROXY = $tx.contractAddress }
    }
}

Write-Output "--- Loaded Deployed Contracts ---"
Write-Output "Timelock: $env:DEPLOYED_TIMELOCK"
Write-Output "Token:    $env:DEPLOYED_TOKEN"
Write-Output "Vault:    $env:DEPLOYED_VAULT"
Write-Output "Proxy:    $env:DEPLOYED_PROXY"
Write-Output "Impl:     $env:DEPLOYED_MINER_IMPL"

Write-Output "`n--- Executing VerifyPostDeploy ---"
forge script script/VerifyPostDeploy.s.sol --rpc-url $RpcUrl -vvv