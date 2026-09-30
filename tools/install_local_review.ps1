# Install a reviewed Deckster build while preserving the previous app and user state.
# Example: .\tools\install_local_review.ps1 -SourceExe .\dist\Deckster-v0.6.0.exe -StartHidden
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceExe,
    [switch]$StartHidden
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Same-Path([string]$Left, [string]$Right) {
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd('\'),
        [IO.Path]::GetFullPath($Right).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
}

$source = (Resolve-Path -LiteralPath $SourceExe -ErrorAction Stop).ProviderPath
$sourceItem = Get-Item -LiteralPath $source -ErrorAction Stop
if ($sourceItem.PSIsContainer -or $sourceItem.Extension -ine '.exe' -or
    $sourceItem.Name -notmatch '^Deckster(?:-v[^\\/]*)?\.exe$' -or $sourceItem.Length -lt 1MB) {
    throw 'SourceExe must be an existing Deckster executable build.'
}
$stream = [IO.File]::OpenRead($source)
try {
    if ($stream.ReadByte() -ne 0x4D -or $stream.ReadByte() -ne 0x5A) {
        throw 'SourceExe is missing the Windows executable signature.'
    }
} finally {
    $stream.Dispose()
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$distDir = Join-Path $repoRoot 'dist'
$installedDir = Join-Path $env:LOCALAPPDATA 'Deckster'
$installedExe = Join-Path $installedDir 'Deckster.exe'
$dataDir = Join-Path $env:LOCALAPPDATA 'StreamControl'
if (Same-Path $source $installedExe) {
    throw 'SourceExe already points to the installed executable.'
}
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash

$shell = New-Object -ComObject WScript.Shell
$desktop = [string]$shell.SpecialFolders.Item('Desktop')
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'The current user Desktop folder could not be found.'
}
$desktopDirs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
[void]$desktopDirs.Add([IO.Path]::GetFullPath($desktop))
foreach ($candidate in @(
    (Join-Path $env:USERPROFILE 'Desktop'),
    (Join-Path $env:USERPROFILE 'OneDrive\Desktop')
)) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
        [void]$desktopDirs.Add([IO.Path]::GetFullPath($candidate))
    }
}

$shortcuts = @(
    foreach ($directory in $desktopDirs) {
        Get-ChildItem -LiteralPath $directory -Filter 'Deckster*.lnk' -File -ErrorAction Stop
    }
)
$canonical = Join-Path $desktop 'Deckster.lnk'
if (-not ($shortcuts | Where-Object { Same-Path $_.FullName $canonical })) {
    $shortcuts += [pscustomobject]@{ FullName = $canonical; Name = 'Deckster.lnk' }
}

New-Item -ItemType Directory -Path $installedDir -Force | Out-Null
$backup = Join-Path (Join-Path $installedDir 'backups') (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$oldExe = Join-Path $backup 'Deckster.exe'
$hadInstalledExe = Test-Path -LiteralPath $installedExe -PathType Leaf
$shortcutBackups = @()
$replaced = $false
try {
    # Back up the complete data directory, including settings, sound library,
    # presentation, paired-device allowlist, and TLS certificate/key. Never log it.
    if ($hadInstalledExe) {
        Copy-Item -LiteralPath $installedExe -Destination $oldExe -ErrorAction Stop
        if ((Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $oldExe -Algorithm SHA256).Hash) {
            throw 'The executable backup failed integrity verification.'
        }
    }
    if (Test-Path -LiteralPath $dataDir -PathType Container) {
        Copy-Item -LiteralPath $dataDir -Destination (Join-Path $backup 'StreamControl') -Recurse -ErrorAction Stop
    }
    $shortcutDir = Join-Path $backup 'shortcuts'
    New-Item -ItemType Directory -Path $shortcutDir -Force | Out-Null
    foreach ($shortcut in $shortcuts) {
        if (Test-Path -LiteralPath $shortcut.FullName -PathType Leaf) {
            $saved = Join-Path $shortcutDir ('{0:D3}-{1}' -f $shortcutBackups.Count, $shortcut.Name)
            Copy-Item -LiteralPath $shortcut.FullName -Destination $saved -ErrorAction Stop
            $shortcutBackups += [pscustomobject]@{ Original = $shortcut.FullName; Saved = $saved }
        }
    }

    # Stop only an installed Deckster or a versioned build running directly from this repo's dist.
    $running = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'Deckster%.exe'" |
        Where-Object {
            $path = [string]$_.ExecutablePath
            if (-not $path) { return $false }
            if (Same-Path $path $installedExe) { return $true }
            return ((Same-Path ([IO.Path]::GetDirectoryName($path)) $distDir) -and
                    ([IO.Path]::GetFileName($path) -match '^Deckster-v[^\\/]+\.exe$'))
        })
    foreach ($process in $running) {
        if (-not (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
            continue
        }
        try { Stop-Process -Id $process.ProcessId -ErrorAction Stop }
        catch {
            if (-not (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
                continue
            }
            throw
        }
        try { Wait-Process -Id $process.ProcessId -Timeout 15 -ErrorAction Stop }
        catch {
            if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
                throw "Deckster process $($process.ProcessId) did not stop."
            }
        }
    }
    # Take a second state snapshot after shutdown so the backup includes the
    # last atomic settings/library writes from this Deckster process.
    if (Test-Path -LiteralPath $dataDir -PathType Container) {
        $savedData = Join-Path $backup 'StreamControl'
        if (-not (Test-Path -LiteralPath $savedData -PathType Container)) {
            New-Item -ItemType Directory -Path $savedData | Out-Null
        }
        Get-ChildItem -LiteralPath $dataDir -Force -ErrorAction Stop |
            Copy-Item -Destination $savedData -Recurse -Force -ErrorAction Stop
    }

    $replaced = $true
    Copy-Item -LiteralPath $source -Destination $installedExe -Force -ErrorAction Stop
    if ((Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash -ne $sourceHash) {
        throw 'Installed executable hash differs from SourceExe.'
    }

    foreach ($shortcut in $shortcuts) {
        $link = $shell.CreateShortcut($shortcut.FullName)
        $link.TargetPath = $installedExe
        $link.Arguments = ''
        $link.WorkingDirectory = $installedDir
        $link.IconLocation = "$installedExe,0"
        $link.Save()
        $check = $shell.CreateShortcut($shortcut.FullName)
        if (-not (Same-Path $check.TargetPath $installedExe)) {
            throw "Shortcut target did not update: $($shortcut.FullName)"
        }
    }

    if ($StartHidden) {
        Start-Process -FilePath $installedExe -ArgumentList '--start-hidden' -WorkingDirectory $installedDir -WindowStyle Hidden
    }
    Write-Output "Installed SHA-256: $sourceHash"
    Write-Output "Updated $($shortcuts.Count) Desktop shortcut(s)."
    Write-Output "Backup: $backup"
} catch {
    $failure = $_
    if ($replaced -and $hadInstalledExe) {
        try { Copy-Item -LiteralPath $oldExe -Destination $installedExe -Force -ErrorAction Stop }
        catch { Write-Warning 'The previous executable could not be restored automatically.' }
    }
    foreach ($saved in $shortcutBackups) {
        try { Copy-Item -LiteralPath $saved.Saved -Destination $saved.Original -Force -ErrorAction Stop }
        catch { Write-Warning 'A previous Desktop shortcut could not be restored automatically.' }
    }
    throw $failure
}
