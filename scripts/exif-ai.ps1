[CmdletBinding()]
param (
    [Parameter()]
    [string]
    $DirectoryPath = "C:\Users\mateu\OneDrive\Pictures\Albums\2024\2024-09-06 Norwegia z Łukaszem",
    [Parameter()]
    [string]
    $Model = "qwen3-vl",
    [Parameter()]
    [string]
    $ContextModel = "qwen2.5:3b"
)

Add-Type -AssemblyName System.Drawing

function Get-ResizedImageBytes($imagePath, $maxDimension = 1024) {
    $img = [System.Drawing.Image]::FromFile($imagePath)
    try {
        $scale = [Math]::Min($maxDimension / $img.Width, $maxDimension / $img.Height)
        if ($scale -ge 1) { return [IO.File]::ReadAllBytes($imagePath) }
        $newW = [int]($img.Width * $scale)
        $newH = [int]($img.Height * $scale)
        $bmp = New-Object System.Drawing.Bitmap($newW, $newH)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($img, 0, 0, $newW, $newH)
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
        return $ms.ToArray()
    } finally {
        if ($g)   { $g.Dispose() }
        if ($bmp) { $bmp.Dispose() }
        $img.Dispose()
    }
}

function Invoke-Agent($messages, $message, $model = $script:Model) {
    $messages += $message
    $failCount = 0
    do {
        $body = @{
            model    = $model
            stream   = $false
            options = @{
                num_predict    = 2048
                temperature    = 0.1
                top_p          = 0.8
                repeat_penalty = 1.05
                num_ctx        = 8192
                think          = $false
            }
            messages = $messages
        } | ConvertTo-Json -Depth 6

        $response = Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post -ContentType "application/json" -Body $body

        Write-Debug "Response: $($response | ConvertTo-Json -Depth 6)"

    } while ($response.done_reason -notin @('stop', 'length') -and (++$failCount -lt 5))

    if ($failCount -ge 5) {
        throw "Failed to get a complete response from the agent after multiple attempts."
    }

    if ($response.done_reason -eq 'length') {
        Write-Warning "Response truncated (hit token limit) — result may be incomplete."
    }

    $messages += $response.message

    $content = ($response.message.content -replace '(?s)<think>.*?</think>', '').Trim()
    return @{
        messages = $messages
        response = $content
    }
}

function Get-LocationName([double]$lat, [double]$lon) {
    $key = "{0:F3},{1:F3}" -f $lat, $lon
    if ($script:geoCache.ContainsKey($key)) { return $script:geoCache[$key] }

    try {
        $uri = "https://nominatim.openstreetmap.org/reverse?lat=$lat&lon=$lon&format=json&zoom=10"
        $resp = Invoke-RestMethod -Uri $uri -Headers @{ 'User-Agent' = 'OrderMyPhotos/1.0' } -TimeoutSec 10
        $addr = $resp.address
        $locality = $addr.village ?? $addr.town ?? $addr.city ?? $addr.municipality
        $location = (@($locality, $addr.county, $addr.country) | Where-Object { $_ }) -join ', '
    }
    catch {
        $location = ""
    }

    $script:geoCache[$key] = $location
    Start-Sleep -Milliseconds 1100  # Nominatim rate limit: 1 req/sec
    return $location
}

$images = Get-ChildItem -Path $DirectoryPath -Recurse -Include *.jpg, *.jpeg, *.png
$contextDesc = Get-Content -Path "$DirectoryPath\context.txt" -ErrorAction SilentlyContinue
if (-not $ContextModel) { $ContextModel = $Model }
$tripContextFile = "$DirectoryPath\_trip-context.txt"
$tripContext = if (Test-Path $tripContextFile) { Get-Content $tripContextFile -Raw -Encoding utf8 } else { Split-Path $DirectoryPath -Leaf }  # persisted across runs
$geoCache = @{}

foreach ($image in $images) {
    try {
        Write-Host "------------------------------"
        Write-Host "$($image.FullName):"

        Copy-Item $image.FullName .\tmp.jpg

        $exif = exiftool -json -n .\tmp.jpg | ConvertFrom-Json

        $metaParts = @()
        if ($exif.DateTimeOriginal) { $metaParts += "Date/time: $($exif.DateTimeOriginal)" }
        if ($exif.GPSLatitude -and $exif.GPSLongitude) {
            $location = Get-LocationName $exif.GPSLatitude $exif.GPSLongitude
            $metaParts += if ($location) { "Location: $location" } else { "GPS: $($exif.GPSLatitude), $($exif.GPSLongitude)" }
        }
        if ($exif.GPSAltitude) { $metaParts += "Altitude: $($exif.GPSAltitude)" }
        $metaContext = if ($metaParts) { "Photo metadata — " + ($metaParts -join "; ") + "." } else { "" }

        if ($exif.Description) {
            Write-Host "Image already has description. Skipping..." -ForegroundColor Yellow
            continue
        }

        $imgB64 = [Convert]::ToBase64String((Get-ResizedImageBytes $image.FullName))

        $result = Invoke-Agent -messages @(@{
                role    = "system"
                content = "You are a photo analysis assistant. Describe images factually in plain natural language. Do not guess identities or names. When asked for tags, return only a JSON list.$(if ($tripContext) { " Album context so far: $tripContext" }) $contextDesc"
            }) -message @{
            role    = "user"
            content = "$metaContext Describe this photo for a searchable photo gallery. Write 2-3 natural sentences covering: (1) the main subject(s) and what they are doing, (2) the setting or location type (e.g. mountain trail, city street, indoor room, beach), (3) key visual details such as objects, colors, weather, or time of day, and (4) the overall mood or atmosphere. Be specific and factual. Maximum 80 words."
            images  = @($imgB64)
        }
        $description = $result.response
        Write-Host "`tDescription: $description" -ForegroundColor Green

        $result = Invoke-Agent -messages $result.messages -message @{
            role    = "user"
            content = "Base on the image and your previous description, generate short 2-4 word photo title. No extra text."
        }
        $title = $result.response
        Write-Host "`tTitle: $title" -ForegroundColor Green

        $result = Invoke-Agent -messages $result.messages -message @{
            role    = "user"
            content = "Base on the image and your previous description, generate a list of 10 single-word tags for the image provided. Look at the subject, mood, action, place, color. Return the tags as ONLY JSON list, all in lowercase, with no duplicates or extra text."
        }
        $tagsJson = $result.response -replace '(?s)```(?:json)?\s*([\s\S]*?)\s*```', '$1'
        $tags = $tagsJson | ConvertFrom-Json | Out-String -Stream | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" -and $_.Length -gt 2 } | Sort-Object -Unique | Join-String -Separator ", "
        Write-Host "`tTags: $tags" -ForegroundColor Green

        exiftool -XMP-dc:Description="$description" -XMP-dc:Title="$title" -XMP-dc:Subject="$tags" ./tmp.jpg

        Copy-Item .\tmp.jpg $image.FullName -Force

        $ctxResult = Invoke-Agent -model $ContextModel -messages @(@{
                role    = "system"
                content = "You are a photo album summarization assistant. Return only the requested summary, no extra commentary."
            }) -message @{
            role    = "user"
            content = "Photo title: '$title'. Description: '$description'. Previous album summary: '$tripContext'. Update the album/trip summary in 1-2 sentences covering location, event type, and recurring subjects or activities. No extra text."
        }
        if ($ctxResult.response -and $ctxResult.response.Trim()) {
            $tripContext = $ctxResult.response
            Set-Content -Path $tripContextFile -Value $tripContext -Encoding utf8
        }
        Write-Host "`tTrip context: $tripContext" -ForegroundColor DarkCyan
    }
    catch {
        $output = @{
            Image      = $image.FullName
            Exception  = $_.Exception.Message
            Result = $result
        } | ConvertTo-Json -Depth 6
        Write-Warning $output
        $output | Out-File -FilePath "error_log_$($image.Name).json" -Encoding utf8
        
        Write-Error "Error processing $($image.FullName): $_"
    }
}