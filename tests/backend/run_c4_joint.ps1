param(
    [string[]] $Tests = @('test_c4_joint.CompleteJoint'),
    [string] $HoldFile = '',
    [string] $StaticDirectory = ''
)
$ErrorActionPreference = 'Stop'
$taskPlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$taskProductsRoot = Split-Path $taskPlatformRoot -Parent
$taskWorkspaceRoot = (Resolve-Path (Join-Path $taskProductsRoot '../..')).Path
$taskContractsRoot = Join-Path $taskWorkspaceRoot 'worktrees/quality-life-20261003/coordination'
$taskMemoryRoot = Join-Path $taskProductsRoot 'memory'
$taskPython = Join-Path $taskWorkspaceRoot 'worktrees/quality-life-20261003/platform/.runtime/venv/Scripts/python.exe'
$env:TS050_RUNTIME = Join-Path $taskPlatformRoot '.runtime/c4-joint'
New-Item -ItemType Directory -Force -Path $env:TS050_RUNTIME | Out-Null
$env:TS050_CONTRACTS = Join-Path $taskContractsRoot 'contracts/text-dialogue/v1'
$env:TS012_CONTRACT_DIR = $env:TS050_CONTRACTS
$env:TS_ROLE_GATEWAY_ROOT = Join-Path $taskProductsRoot 'gateway'
$env:TIANSHU_CORE_REPO = Join-Path $taskProductsRoot 'companion'
$env:TS_C4_PLATFORM_ROOT = $taskPlatformRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (@(
    $taskPlatformRoot,
    (Join-Path $taskPlatformRoot 'tests/backend'),
    (Join-Path $env:TIANSHU_CORE_REPO 'src'),
    (Join-Path $env:TIANSHU_CORE_REPO 'tests'),
    (Join-Path $taskMemoryRoot 'src'),
    (Join-Path $env:TS_ROLE_GATEWAY_ROOT 'src'),
    (Join-Path $taskContractsRoot 'tests/integration'),
    (Join-Path $taskMemoryRoot '.runtime/c5-clean-install/venv/Lib/site-packages')
) -join ';')
if ($HoldFile) { $env:TS_C4_HOLD_FILE = $HoldFile } else { Remove-Item Env:TS_C4_HOLD_FILE -ErrorAction SilentlyContinue }
if ($StaticDirectory) { $env:TS_C4_STATIC_DIR = $StaticDirectory } else { Remove-Item Env:TS_C4_STATIC_DIR -ErrorAction SilentlyContinue }
Push-Location $taskPlatformRoot
try { & $taskPython -B -m unittest @Tests -v; exit $LASTEXITCODE }
finally { Pop-Location }
