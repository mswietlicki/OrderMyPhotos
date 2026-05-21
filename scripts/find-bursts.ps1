[CmdletBinding(SupportsShouldProcess)]
param (
    [Parameter(Mandatory)]
    [string]$DirectoryPath,

    [string]$Model               = "qwen3-vl",
    [double]$SimilarityThreshold = 0.30,  # normalized RMSE; below = same burst (0=identical, 1=different)
    [int]   $MinBurstSize        = 2,     # minimum shots to treat as a burst
    [string]$ArchiveSubdir       = "_archived",
    [switch]$Recurse
)

# ── Dependency check ──────────────────────────────────────────────────────────
if (-not (Get-Command magick -ErrorAction SilentlyContinue)) {
    throw "ImageMagick (magick) is required. Install from https://imagemagick.org"
}

Add-Type -AssemblyName System.Drawing  # used only for VLM image resizing

# ── Sharpness via ImageMagick Laplacian standard deviation ────────────────────
function Get-SharpnessScore($path) {
    $tmp = Join-Path $env:TEMP "burst_tmp.jpg"
    [IO.File]::Copy($path, $tmp, $true)   # bypass -WhatIf; scoring is read-only
    $score = [double](magick "$tmp" -resize 400x400 -colorspace Gray `
        -morphology Convolve Laplacian:0 `
        -format "%[fx:u.standard_deviation]" info: 2>$null)
    [IO.File]::Delete($tmp)
    return $score
}

function Get-ResizedBytes($path, $max = 512) {
    $img = [System.Drawing.Image]::FromFile($path)
    try {
        $s   = [Math]::Min($max / $img.Width, $max / $img.Height)
        if ($s -ge 1) { return [IO.File]::ReadAllBytes($path) }
        $bmp = New-Object System.Drawing.Bitmap([int]($img.Width*$s), [int]($img.Height*$s))
        $g   = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($img, 0, 0, $bmp.Width, $bmp.Height)
        $ms  = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
        return $ms.ToArray()
    } finally {
        if ($g)   { $g.Dispose() }
        if ($bmp) { $bmp.Dispose() }
        $img.Dispose()
    }
}

# ── Visual similarity between two consecutive photos (normalized RMSE) ──────────
# Returns 0 (identical) → 1 (completely different). Copies to ASCII temp paths
# first so ImageMagick handles Unicode filenames correctly.
function Get-SimilarityScore($path1, $path2) {
    $tmp1 = Join-Path $env:TEMP "burst_cmp1.jpg"
    $tmp2 = Join-Path $env:TEMP "burst_cmp2.jpg"
    [IO.File]::Copy($path1, $tmp1, $true)
    [IO.File]::Copy($path2, $tmp2, $true)
    # Force same dimensions so compare works regardless of orientation/resolution
    magick "$tmp1" -resize 400x400! "$tmp1" 2>$null
    magick "$tmp2" -resize 400x400! "$tmp2" 2>$null
    $output = magick compare -metric RMSE "$tmp1" "$tmp2" null: 2>&1
    [IO.File]::Delete($tmp1)
    [IO.File]::Delete($tmp2)
    if ("$output" -match '\(([0-9.]+)\)') { return [double]$Matches[1] }
    return 1.0  # unparseable = treat as different scenes
}

# ── VLM 1-vs-1 match: returns { WinnerIdx (0|1), Reasoning } ─────────────────
function Invoke-MatchComparison([string]$pathA, [string]$pathB,
                                 [double]$sharpA, [double]$sharpB,
                                 [object]$sim) {
    $ic     = [System.Globalization.CultureInfo]::InvariantCulture
    $imageA = [Convert]::ToBase64String((Get-ResizedBytes $pathA 512))
    $imageB = [Convert]::ToBase64String((Get-ResizedBytes $pathB 512))
    $nameA  = Split-Path $pathA -Leaf
    $nameB  = Split-Path $pathB -Leaf

    $simLine = if ($null -ne $sim) {
        $sv   = ([double]$sim).ToString('F3', $ic)
        $desc = if ([double]$sim -lt 0.05) { 'nearly identical' } `
                elseif ([double]$sim -lt 0.15) { 'very similar' } `
                else { 'noticeably different' }
        "Similarity between them: RMSE $sv ($desc)"
    }

    $prompt = @"
Compare these 2 photos from the same burst and pick the better one.
[1] $nameA  (sharpness: $($sharpA.ToString('F2',$ic)))
[2] $nameB  (sharpness: $($sharpB.ToString('F2',$ic)))
$(if ($simLine) { "$simLine`n" })
Evaluate both for:
1. Framing & composition — subject placement, crop, background clutter
2. Lighting — exposure, shadows on faces, catchlights in eyes
3. Faces — visible, in focus, natural expression
4. Smiles & emotion — genuine vs. forced or mid-blink
5. Humor or decisive moment

Reply with ONLY 1 or 2.
"@

    $body = @{
        model    = $Model
        stream   = $false
        options  = @{ num_predict = 2048; num_ctx = 32768; temperature = 0.1; think = $true }
        messages = @(@{
            role    = 'user'
            content = $prompt
            images  = @($imageA, $imageB)
        })
    } | ConvertTo-Json -Depth 6

    try {
        $resp = Invoke-RestMethod -Uri 'http://localhost:11434/api/chat' `
                    -Method Post -ContentType 'application/json' -Body $body
        if ($resp.message.content -match '\b([12])\b') {
            return [PSCustomObject]@{ WinnerIdx = [int]$Matches[1] - 1; Reasoning = $resp.message.thinking }
        }
        Write-Warning "Match returned unparseable response: $($resp.message.content)"
    } catch {
        Write-Warning "Match comparison failed: $_"
    }
    return [PSCustomObject]@{ WinnerIdx = 0; Reasoning = $null }  # default: first photo wins
}

# ── Tournament bracket: most-similar pairs compete first ─────────────────────
function Get-BestIndices([string[]]$paths, [int]$maxKeep = 1, [object[]]$simScores = @()) {
    Write-Host "`t  Scoring sharpness..." -ForegroundColor DarkGray
    $scores  = $paths | ForEach-Object { Get-SharpnessScore $_ }
    $rounded = $scores | ForEach-Object { $_.ToString('F2', [System.Globalization.CultureInfo]::InvariantCulture) }
    Write-Host "`t  Scores: $($rounded -join ', ')" -ForegroundColor DarkGray

    $maxScore   = ($scores | Measure-Object -Maximum).Maximum
    $candidates = @(0..($paths.Count - 1) | Where-Object { $scores[$_] -ge ($maxScore * 0.3) })
    if ($candidates.Count -eq 0) { $candidates = @(0) }

    if ($candidates.Count -le $maxKeep) {
        Write-Host "`t  Keeping all $($candidates.Count) non-blurry shots." -ForegroundColor DarkGray
        return [PSCustomObject]@{ Indices = $candidates; Reasoning = $null }
    }

    Write-Host "`t  Tournament: $($candidates.Count) candidates → keep $maxKeep" -ForegroundColor DarkGray

    # Seed round 1: sort by sim-to-previous so most similar photos face each other first
    [int[]]$survivors = @($candidates | Sort-Object {
        if ($_ -lt $simScores.Count -and $null -ne $simScores[$_]) { [double]$simScores[$_] }
        else { 1.0 }
    })

    $roundLogs = [System.Collections.Generic.List[string]]::new()
    $round     = 1

    while ($survivors.Count -gt $maxKeep) {
        Write-Host "`t  Round $round  ($($survivors.Count) photos)" -ForegroundColor DarkGray
        $matchLog  = [System.Collections.Generic.List[string]]::new()
        $nextRound = [System.Collections.Generic.List[int]]::new()
        $i = 0

        while ($i -lt $survivors.Count) {
            if ($i + 1 -lt $survivors.Count) {
                $idxA  = $survivors[$i]
                $idxB  = $survivors[$i + 1]
                $nameA = Split-Path $paths[$idxA] -Leaf
                $nameB = Split-Path $paths[$idxB] -Leaf

                # Sim is known only when the two are consecutive in the original sequence
                $sim   = $null
                $later = [Math]::Max($idxA, $idxB)
                if ($later -eq [Math]::Min($idxA, $idxB) + 1 -and
                    $later -lt $simScores.Count -and $null -ne $simScores[$later]) {
                    $sim = [double]$simScores[$later]
                }

                Write-Host "`t    $nameA  vs  $nameB" -ForegroundColor DarkGray
                $match      = Invoke-MatchComparison $paths[$idxA] $paths[$idxB] $scores[$idxA] $scores[$idxB] $sim
                $winnerIdx  = if ($match.WinnerIdx -eq 0) { $idxA } else { $idxB }
                $winnerName = Split-Path $paths[$winnerIdx] -Leaf
                Write-Host "`t      → $winnerName wins" -ForegroundColor Green

                $entry = "$nameA vs $nameB → $winnerName"
                if ($match.Reasoning) { $entry += "`n$($match.Reasoning)" }
                $matchLog.Add($entry)
                $nextRound.Add($winnerIdx)
                $i += 2
            } else {
                $byeName = Split-Path $paths[$survivors[$i]] -Leaf
                Write-Host "`t    $byeName  — bye" -ForegroundColor DarkGray
                $nextRound.Add($survivors[$i])
                $i++
            }
        }

        $survivors = $nextRound.ToArray()
        $roundLogs.Add("=== Round $round ===`n$($matchLog -join "`n---`n")")
        $round++
    }

    $finalNames = $survivors | ForEach-Object { Split-Path $paths[$_] -Leaf }
    Write-Host "`t  Finalist(s): $($finalNames -join ', ')" -ForegroundColor Green

    return [PSCustomObject]@{
        Indices   = [int[]]$survivors
        Reasoning = if ($roundLogs.Count -gt 0) { $roundLogs -join "`n`n" } else { $null }
    }
}

# ── Scan images + parse timestamps ─────────────────────────────────────────────
Write-Host "Scanning: $DirectoryPath"
$exifArgs = @('-json', '-DateTimeOriginal')
if ($Recurse) { $exifArgs += '-r' }
$exifArgs += '.'

Push-Location $DirectoryPath
$raw = exiftool @exifArgs | ConvertFrom-Json
Pop-Location
$photos = $raw |
    Where-Object { $_.SourceFile -match '\.(jpg|jpeg|png)$' } |
    ForEach-Object {
        $srcFile = $_.SourceFile -replace '^\.[/\\]', ''
        $ts = $null
        if ($_.DateTimeOriginal) {
            $tsStr = $_.DateTimeOriginal -replace '\.\d+$', ''
            try { $ts = [DateTime]::ParseExact($tsStr, 'yyyy:MM:dd HH:mm:ss',
                             [System.Globalization.CultureInfo]::InvariantCulture) } catch {}
        }
        [PSCustomObject]@{ Path = Join-Path $DirectoryPath $srcFile; Timestamp = $ts; Name = $srcFile }
    } |
    Where-Object { $_ } |
    Sort-Object { if ($_.Timestamp) { $_.Timestamp.Ticks } else { $_.Name } }

if ($photos.Count -lt 2) {
    Write-Host "Not enough photos found." -ForegroundColor Yellow
    exit
}
Write-Host "Found $($photos.Count) photos."

# ── Detect burst groups by visual similarity ───────────────────────────────────
Write-Host "Computing pairwise similarity (threshold: $SimilarityThreshold)..." -ForegroundColor DarkGray

$groups  = [System.Collections.Generic.List[object[]]]::new()
$current = [System.Collections.Generic.List[object]]::new()
$current.Add([PSCustomObject]@{ Path = $photos[0].Path; SimToPrev = $null })

for ($i = 1; $i -lt $photos.Count; $i++) {
    $sim    = Get-SimilarityScore $photos[$i-1].Path $photos[$i].Path
    $simStr = $sim.ToString('F3', [System.Globalization.CultureInfo]::InvariantCulture)
    $label  = if ($sim -le $SimilarityThreshold) { '[burst]' } else { '[break]' }
    Write-Host "`t$(Split-Path $photos[$i].Path -Leaf)  RMSE $simStr  $label" -ForegroundColor DarkGray

    if ($sim -le $SimilarityThreshold) {
        $current.Add([PSCustomObject]@{ Path = $photos[$i].Path; SimToPrev = $sim })
    } else {
        if ($current.Count -ge $MinBurstSize) { $groups.Add($current.ToArray()) }
        $current = [System.Collections.Generic.List[object]]::new()
        $current.Add([PSCustomObject]@{ Path = $photos[$i].Path; SimToPrev = $null })
    }
}
if ($current.Count -ge $MinBurstSize) { $groups.Add($current.ToArray()) }

if ($groups.Count -eq 0) {
    Write-Host "No burst series found (similarity ≤ $SimilarityThreshold for $MinBurstSize+ consecutive shots). Nothing to do." -ForegroundColor Yellow
    exit
}

Write-Host "Found $($groups.Count) burst series." -ForegroundColor Cyan

# ── Process each burst ─────────────────────────────────────────────────────────
$archivePath   = Join-Path $DirectoryPath $ArchiveSubdir
$totalArchived = 0

foreach ($group in $groups) {
    $paths     = @($group | ForEach-Object { $_.Path })
    $simScores = @($group | ForEach-Object { $_.SimToPrev })
    $firstName = Split-Path $paths[0] -Leaf
    $lastName  = Split-Path $paths[-1] -Leaf

    Write-Host ""
    Write-Host "Burst ($($paths.Count) shots): $firstName … $lastName" -ForegroundColor White
    $paths | ForEach-Object { Write-Host "`t$(Split-Path $_ -Leaf)" -ForegroundColor DarkGray }

    $maxKeep     = if ($paths.Count -le 3) { 1 } elseif ($paths.Count -le 6) { 2 } else { 3 }
    $vlmResult   = Get-BestIndices $paths $maxKeep $simScores
    $bestIndices = @($vlmResult.Indices)
    $bestNames   = $bestIndices | ForEach-Object { Split-Path $paths[$_] -Leaf }
    Write-Host "`t  >> Keeping: $($bestNames -join ', ')" -ForegroundColor Green

    if (-not (Test-Path $archivePath)) {
        New-Item -ItemType Directory -Path $archivePath | Out-Null
    }

    $archivedEntries = [System.Collections.Generic.List[object]]::new()

    for ($i = 0; $i -lt $paths.Count; $i++) {
        if ($i -in $bestIndices) { continue }

        $name = Split-Path $paths[$i] -Leaf
        $dest = Join-Path $archivePath $name

        # Avoid overwriting if same filename already archived
        if (Test-Path $dest) {
            $base = [System.IO.Path]::GetFileNameWithoutExtension($name)
            $ext  = [System.IO.Path]::GetExtension($name)
            $dest = Join-Path $archivePath "$base`_$(Get-Random)$ext"
        }

        if ($PSCmdlet.ShouldProcess($paths[$i], "Move to $dest")) {
            Move-Item $paths[$i] $dest -Force
            $totalArchived++
            $archivedEntries.Add([PSCustomObject]@{
                original    = $name
                archived_as = Split-Path $dest -Leaf
            })
        }
        Write-Host "`t  Archived: $name" -ForegroundColor DarkYellow
    }

    # Write per-burst record file to archive dir for future audit
    $recordName = "burst_$([IO.Path]::GetFileNameWithoutExtension($paths[0])).json"
    $recordPath = Join-Path $archivePath $recordName
    $record = [PSCustomObject]@{
        first_file    = $firstName
        last_file     = $lastName
        shots         = $paths.Count
        kept          = @($bestIndices | ForEach-Object {
            [PSCustomObject]@{ file = Split-Path $paths[$_] -Leaf; path = $paths[$_] }
        })
        archived      = $archivedEntries
        vlm_reasoning = $vlmResult.Reasoning
        created_at    = (Get-Date -Format 'o')
    }
    if ($PSCmdlet.ShouldProcess($recordPath, "Write burst record")) {
        $record | ConvertTo-Json -Depth 4 | Set-Content $recordPath -Encoding utf8
        Write-Host "`t  Record:   $recordName" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Done. Processed $($groups.Count) burst series. Archived $totalArchived duplicate(s) to: $archivePath" -ForegroundColor Cyan
