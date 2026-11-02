
function Invoke-Agent {
    param (
        $messages
    )
    $body = @{
        model    = "llama3.2-vision"
        stream   = $false
        messages = $messages
    } | ConvertTo-Json -Depth 6

    return Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post -ContentType "application/json" -Body $body
}

$photoPath = "C:\Users\mateu\OneDrive\Pictures\Albums\2022\2022-02-12 Skitury z Piotrkiem\20220212_140410.jpg"
$imgB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($photoPath))

$messages = @()
        
$messages += @{
        role    = "user"
        content = "This image was taken in 57, Zwalisko, Suche, Szczyrk, powiat bielski, województwo śląskie, 43-370, Polska. Describe this image. Look at the subject, mood, action, place, color, lighting. Write 2 sentences describing the image in detail."
        images  = @($imgB64)
    }

$firstResponse = Invoke-Agent($messages)

$firstResponse.message

$messages += $firstResponse.message
$messages += @{
        role    = "user"
        content = "Base on the image and your previous description, generate a list of 10 single-word tags for the image provided. Look at the subject, mood, action, place, color. Return the tags as ONLY JSON list, all in lowercase, with no duplicates or extra text."
    }

$response = Invoke-Agent($messages)
$response.message

# "Generate a list of 10 single-word tags for the image provided. Look at the subject, mood, action, place, color, . Return the tags as ONLY JSON list, all in lowercase, with no duplicates or extra text."

