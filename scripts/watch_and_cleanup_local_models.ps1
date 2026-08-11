[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) '.local-monitor\cleanup-targets.json'),
    [ValidateRange(60, 86400)]
    [int]$PollSeconds = 300,
    [switch]$Once,
    [switch]$NoDelete
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$mirrorRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$configFullPath = [IO.Path]::GetFullPath($ConfigPath)
$monitorRoot = Split-Path -Parent $configFullPath
New-Item -ItemType Directory -Path $monitorRoot -Force | Out-Null
$logPath = Join-Path $monitorRoot 'cleanup-monitor.log'
$statePath = Join-Path $monitorRoot 'cleanup-state.json'
$pidPath = Join-Path $monitorRoot 'cleanup-monitor.pid'

function Write-MonitorLog {
    param(
        [Parameter(Mandatory)]
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level = 'INFO'
    )

    $line = '{0:o} [{1}] {2}' -f [DateTimeOffset]::Now, $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

function Get-Settings {
    $settingsPath = Join-Path $mirrorRoot 'models.json'
    return Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
}

function Get-ExpectedModels {
    param([Parameter(Mandatory)]$Settings)

    $expected = @{}
    foreach ($property in $Settings.models.PSObject.Properties) {
        $name = $property.Name
        $modelConfig = $property.Value
        $manifestPath = Join-Path $mirrorRoot ([string]$modelConfig.manifest)
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $weights = @($manifest.files | Where-Object { $_.kind -eq 'weight' })
        $expected[$name] = [pscustomobject]@{
            Name = $name
            Config = $modelConfig
            Manifest = $manifest
            Weights = $weights
            WeightFiles = $weights.Count
            WeightBytes = [long]$manifest.summary.weight_bytes
            WeightAssets = [int]$manifest.summary.weight_release_asset_count
        }
    }
    return $expected
}

function Invoke-GhJson {
    param([Parameter(Mandatory)][string]$Endpoint)

    $raw = & gh api $Endpoint 2>$null | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "gh api failed for $Endpoint"
    }
    return $raw | ConvertFrom-Json
}

function Get-AndValidateFinalProof {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][hashtable]$ExpectedModels
    )

    $repository = [string]$Config.repository
    $verificationTag = [string]$Config.verification_tag
    $finalMarker = [string]$Config.final_marker
    try {
        $release = Invoke-GhJson "repos/$repository/releases/tags/$verificationTag"
    }
    catch {
        return $null
    }

    $asset = @($release.assets | Where-Object {
        $_.name -eq $finalMarker -and $_.state -eq 'uploaded'
    }) | Select-Object -First 1
    if ($null -eq $asset) {
        return $null
    }

    $digest = [string]$asset.digest
    if (-not $digest.StartsWith('sha256:', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Final proof asset has no GitHub SHA-256 digest'
    }

    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $temp = Join-Path $tempRoot ('glm-final-proof-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temp | Out-Null
    try {
        & gh release download $verificationTag `
            --repo $repository `
            --pattern $finalMarker `
            --dir $temp `
            --clobber 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to download final proof asset'
        }
        $proofPath = Join-Path $temp $finalMarker
        $actualDigest = (Get-FileHash -LiteralPath $proofPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $expectedDigest = $digest.Substring(7).ToLowerInvariant()
        if ($actualDigest -ne $expectedDigest) {
            throw 'Final proof asset failed its GitHub SHA-256 check'
        }
        $proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
    }
    finally {
        $resolvedTemp = [IO.Path]::GetFullPath($temp)
        if ([IO.Directory]::GetParent($resolvedTemp).FullName.TrimEnd('\') -ne $tempRoot) {
            throw "Refusing to remove unexpected temporary path: $resolvedTemp"
        }
        if (Test-Path -LiteralPath $resolvedTemp) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }

    if ([string]$proof.repository -ne $repository) {
        throw 'Final proof names a different repository'
    }
    $minimumVerifiedAt = [DateTimeOffset]::Parse([string]$Config.minimum_verified_at)
    $verifiedAt = [DateTimeOffset]::Parse([string]$proof.verified_at)
    if ($verifiedAt -lt $minimumVerifiedAt) {
        throw 'Final proof predates this cleanup authorization'
    }

    $proofModels = @($proof.models)
    if ($proofModels.Count -ne $ExpectedModels.Count) {
        throw "Final proof covers $($proofModels.Count) models; expected $($ExpectedModels.Count)"
    }
    $expectedTotalFiles = 0
    $expectedTotalBytes = [long]0
    foreach ($name in $ExpectedModels.Keys) {
        $expected = $ExpectedModels[$name]
        $entry = @($proofModels | Where-Object { $_.model -eq $name })
        if ($entry.Count -ne 1) {
            throw "Final proof does not uniquely cover $name"
        }
        if ([string]$entry[0].source_repository -ne [string]$expected.Config.hf_repo -or
            [string]$entry[0].source_revision -ne [string]$expected.Config.revision -or
            [int]$entry[0].weight_files -ne $expected.WeightFiles -or
            [long]$entry[0].weight_bytes -ne $expected.WeightBytes) {
            throw "Final proof metadata mismatch for $name"
        }
        $releaseCheck = @($proof.release_checks | Where-Object { $_.model -eq $name })
        if ($releaseCheck.Count -ne 1 -or -not [bool]$releaseCheck[0].complete -or
            [string]$releaseCheck[0].release_tag -ne [string]$expected.Config.release_tag -or
            [int]$releaseCheck[0].expected_weight_assets -ne $expected.WeightAssets -or
            [int]$releaseCheck[0].present_valid_weight_assets -ne $expected.WeightAssets) {
            throw "Final Release check mismatch for $name"
        }
        $expectedTotalFiles += $expected.WeightFiles
        $expectedTotalBytes += $expected.WeightBytes
    }
    if ([int]$proof.total_weight_files -ne $expectedTotalFiles -or
        [long]$proof.total_weight_bytes -ne $expectedTotalBytes) {
        throw 'Final proof aggregate totals do not match the pinned manifests'
    }
    return $proof
}

function Confirm-ReleasesDirectly {
    param(
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][hashtable]$ExpectedModels
    )

    $script = Join-Path $mirrorRoot 'scripts\verify_release.py'
    $raw = & python $script --repo $Repository 2>$null | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw 'Direct Release verification did not pass'
    }
    $results = @($raw | ConvertFrom-Json)
    if ($results.Count -ne $ExpectedModels.Count) {
        throw 'Direct Release verification returned an unexpected model count'
    }
    foreach ($result in $results) {
        if (-not [bool]$result.complete -or
            [int]$result.present_valid_weight_assets -ne [int]$result.expected_weight_assets -or
            [long]$result.present_valid_weight_bytes -ne [long]$result.expected_weight_bytes) {
            throw "Direct Release verification failed for $($result.model)"
        }
    }
}

function Confirm-TargetPath {
    param([Parameter(Mandatory)]$Target)

    $full = [IO.Path]::GetFullPath([string]$Target.path).TrimEnd('\')
    $declared = ([string]$Target.path).TrimEnd('\')
    if (-not $full.Equals($declared, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target is not a canonical absolute path: $declared"
    }
    $allowedParent = [IO.Path]::GetFullPath([string]$Target.allowed_parent).TrimEnd('\')
    $actualParent = [IO.Directory]::GetParent($full).FullName.TrimEnd('\')
    if (-not $actualParent.Equals($allowedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target is outside its allowed parent: $full"
    }
    if ($allowedParent -notin @('D:\models', 'E:\models')) {
        throw "Parent is not on the explicit cleanup allowlist: $allowedParent"
    }
    return $full
}

function Test-LocalTarget {
    param(
        [Parameter(Mandatory)]$Target,
        [Parameter(Mandatory)]$Expected
    )

    $full = Confirm-TargetPath $Target
    if (-not (Test-Path -LiteralPath $full -PathType Container)) {
        throw "Expected local model directory is missing: $full"
    }
    $directory = Get-Item -LiteralPath $full -Force
    if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing a reparse-point target: $full"
    }
    foreach ($required in @('config.json', 'model.safetensors.index.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $full $required) -PathType Leaf)) {
            throw "Required model file is missing from $full`: $required"
        }
    }

    $weightFiles = @(Get-ChildItem -LiteralPath $full -Recurse -File -Filter '*.safetensors' -Force)
    if ($weightFiles.Count -ne $Expected.WeightFiles) {
        throw "$full has $($weightFiles.Count) weight files; expected $($Expected.WeightFiles)"
    }
    $weightBytes = [long]0
    foreach ($weight in $Expected.Weights) {
        $candidate = [IO.Path]::GetFullPath((Join-Path $full ([string]$weight.path)))
        if (-not $candidate.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe manifest path for $($weight.path)"
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Missing expected weight: $candidate"
        }
        $item = Get-Item -LiteralPath $candidate -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing a reparse-point weight: $candidate"
        }
        if ([long]$item.Length -ne [long]$weight.size) {
            throw "Weight size mismatch: $candidate"
        }
        $weightBytes += [long]$item.Length

        if ([bool]$Target.require_lfs_metadata) {
            $metadata = Join-Path (Join-Path $full '.cache\huggingface\download') ([string]$weight.path + '.metadata')
            if (-not (Test-Path -LiteralPath $metadata -PathType Leaf)) {
                throw "Missing LFS metadata: $metadata"
            }
            $lines = @(Get-Content -LiteralPath $metadata -TotalCount 2)
            if ($lines.Count -lt 2 -or $lines[1] -ne [string]$weight.sha256) {
                throw "LFS hash metadata mismatch: $metadata"
            }
        }
    }
    if ($weightBytes -ne $Expected.WeightBytes) {
        throw "Aggregate weight size mismatch for $full"
    }

    $allFiles = @(Get-ChildItem -LiteralPath $full -Recurse -File -Force)
    $directoryBytes = [long](($allFiles | Measure-Object Length -Sum).Sum)
    if ($allFiles.Count -ne [int]$Target.expected_directory_files -or
        $directoryBytes -ne [long]$Target.expected_directory_bytes) {
        throw "Directory baseline changed for $full"
    }
    return [pscustomobject]@{
        Model = [string]$Target.model
        Path = $full
        DirectoryFiles = $allFiles.Count
        DirectoryBytes = $directoryBytes
    }
}

function Get-BlockingProcesses {
    param([Parameter(Mandatory)][array]$ValidatedTargets)

    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $blocked = @()
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if (-not $commandLine) {
            continue
        }
        foreach ($target in $ValidatedTargets) {
            if ($commandLine.IndexOf([string]$target.Path, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $blocked += [pscustomobject]@{
                    ProcessId = $process.ProcessId
                    Name = $process.Name
                    Target = $target.Path
                }
            }
        }
    }
    return $blocked
}

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable
    }
    return @{
        schema_version = 1
        deleted = @{}
    }
}

function Save-State {
    param([Parameter(Mandatory)][hashtable]$State)

    $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Invoke-ConditionalCleanup {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][hashtable]$ExpectedModels,
        [Parameter(Mandatory)]$Proof
    )

    Confirm-ReleasesDirectly -Repository ([string]$Config.repository) -ExpectedModels $ExpectedModels
    $state = Read-State
    $validated = @()
    foreach ($target in $Config.targets) {
        $name = [string]$target.model
        if (-not $ExpectedModels.ContainsKey($name)) {
            throw "Cleanup target names an unknown model: $name"
        }
        if ($state.deleted.ContainsKey($name)) {
            continue
        }
        $validated += Test-LocalTarget -Target $target -Expected $ExpectedModels[$name]
    }

    if ($validated.Count -eq 0) {
        Write-MonitorLog 'Every authorized model directory was already removed and recorded.'
        return $true
    }
    $blocking = @(Get-BlockingProcesses -ValidatedTargets $validated)
    if ($blocking.Count -gt 0) {
        $description = $blocking | ConvertTo-Json -Compress
        Write-MonitorLog "Cleanup deferred because a process references a target: $description" 'WARN'
        return $false
    }
    if ($NoDelete) {
        Write-MonitorLog 'All remote and local safety checks passed; NoDelete prevented removal.'
        return $false
    }

    $state.proof_verified_at = [string]$Proof.verified_at
    foreach ($target in $validated) {
        $driveName = [IO.Path]::GetPathRoot([string]$target.Path).TrimEnd('\').TrimEnd(':')
        $freeBefore = [long](Get-PSDrive -Name $driveName).Free
        Write-MonitorLog "Removing authorized directory $($target.Path) ($($target.DirectoryBytes) bytes)."
        Remove-Item -LiteralPath $target.Path -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $target.Path) {
            throw "Removal did not complete: $($target.Path)"
        }
        $freeAfter = [long](Get-PSDrive -Name $driveName).Free
        $state.deleted[$target.Model] = @{
            path = $target.Path
            removed_at = [DateTimeOffset]::Now.ToString('o')
            directory_bytes = [long]$target.DirectoryBytes
            measured_free_space_gain = [long]($freeAfter - $freeBefore)
        }
        Save-State $state
        Write-MonitorLog "Removed $($target.Path); measured free-space gain: $($freeAfter - $freeBefore) bytes."
    }
    return $state.deleted.Count -eq $Config.targets.Count
}

$config = Get-Content -LiteralPath $configFullPath -Raw | ConvertFrom-Json
$settings = Get-Settings
$expectedModels = Get-ExpectedModels -Settings $settings
$targetNames = @($config.targets | ForEach-Object { [string]$_.model } | Sort-Object -Unique)
$expectedNames = @($expectedModels.Keys | Sort-Object)
if ($targetNames.Count -ne $expectedNames.Count -or
    @(Compare-Object -ReferenceObject $expectedNames -DifferenceObject $targetNames).Count -ne 0) {
    throw 'Cleanup configuration must cover every configured model exactly once'
}

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, 'Local\GLMWeightsMirrorCleanupMonitor', [ref]$createdNew)
if (-not $createdNew) {
    Write-MonitorLog 'Another cleanup monitor instance is already running.' 'WARN'
    exit 2
}

try {
    Set-Content -LiteralPath $pidPath -Value $PID -Encoding ascii
    Write-MonitorLog "Monitor started (PID $PID, poll interval $PollSeconds seconds, NoDelete=$NoDelete)."
    $startupState = Read-State
    $preflight = @()
    foreach ($target in $config.targets) {
        if ($startupState.deleted.ContainsKey([string]$target.model)) {
            continue
        }
        $preflight += Test-LocalTarget -Target $target -Expected $expectedModels[[string]$target.model]
    }
    $preflightBytes = [long](($preflight | Measure-Object DirectoryBytes -Sum).Sum)
    Write-MonitorLog "Startup preflight passed for $($preflight.Count) exact directories ($preflightBytes bytes)."
    while ($true) {
        try {
            $proof = Get-AndValidateFinalProof -Config $config -ExpectedModels $expectedModels
            if ($null -eq $proof) {
                Write-MonitorLog 'Final restore proof is not present yet; no local files were touched.'
            }
            else {
                Write-MonitorLog "Validated final proof from $($proof.verified_at)."
                if (Invoke-ConditionalCleanup -Config $config -ExpectedModels $expectedModels -Proof $proof) {
                    Write-MonitorLog 'Conditional cleanup completed successfully.'
                    break
                }
            }
        }
        catch {
            Write-MonitorLog $_.Exception.Message 'ERROR'
        }
        if ($Once) {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    }
}
finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
