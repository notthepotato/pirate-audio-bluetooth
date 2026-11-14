<#
  Pirate Audio music sync for Windows.

  Watches music folders on this PC and sends new songs to the Pirate Audio Pi by itself, keeping their
  Artist\Album folders. Songs already on the Pi are skipped, and nothing on this PC is changed or deleted.

  Folders it watches (the ones that exist):
    Music\Pirate Audio                       a drop folder: anything you put here goes to the Pi
    Music\iTunes\iTunes Media\Music          songs bought in iTunes
    Music\Apple Music\Media\Music            songs bought in the Apple Music app
  Copy-protected Apple Music subscription downloads can't be played on the Pi; the Pi says so and they're
  skipped.

  Run it once (shows what it sends, then keeps watching until you close the window):
      powershell -ExecutionPolicy Bypass -File pirate-sync.ps1
  Start it with Windows, hidden in the background:
      powershell -ExecutionPolicy Bypass -File pirate-sync.ps1 -Install
  Stop starting it with Windows (and stop it now):
      powershell -ExecutionPolicy Bypass -File pirate-sync.ps1 -Uninstall
  Other folders (for example where Bandcamp downloads are unzipped):
      powershell -ExecutionPolicy Bypass -File pirate-sync.ps1 -Install -Folders "D:\Music\Bandcamp"
  If the music page has a password: add -Password "..." (it's kept encrypted for your Windows user).

  What it did: %LOCALAPPDATA%\PirateSync\log.txt
#>
param(
    [string[]]$Folders,
    [string]$Pi = "@PI_ADDRESS@",
    [string]$Password,
    [int]$EverySeconds = 60,
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$AppDir = Join-Path $env:LOCALAPPDATA "PirateSync"
$SentFile = Join-Path $AppDir "sent.txt"
$LogFile = Join-Path $AppDir "log.txt"
$KeyFile = Join-Path $AppDir "password.txt"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Pirate Audio sync.lnk"
$Exts = @(".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma", ".ape",
          ".wv", ".mka", ".alac", ".jpg", ".jpeg", ".png", ".m3u", ".m3u8")
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$Pi = $Pi.TrimEnd("/")

function Write-Log([string]$Text) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    Write-Host $line
    try {
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
        if ((Get-Item $LogFile).Length -gt 512KB) {
            $keep = Get-Content $LogFile -Encoding UTF8 | Select-Object -Last 1000
            Set-Content -Path $LogFile -Value $keep -Encoding UTF8
        }
    } catch { }
}

function Get-DefaultFolders {
    $music = [Environment]::GetFolderPath("MyMusic")
    $drop = Join-Path $music "Pirate Audio"
    New-Item -ItemType Directory -Force -Path $drop | Out-Null
    @($drop, (Join-Path $music "iTunes\iTunes Media\Music"), (Join-Path $music "Apple Music\Media\Music")) |
        Where-Object { Test-Path -LiteralPath $_ }
}

# ---- password (optional), kept encrypted with Windows' own per-user protection ----------------------
if ($Password) {
    ConvertTo-SecureString $Password -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $KeyFile
}
$Auth = $null
if (Test-Path $KeyFile) {
    $secure = Get-Content $KeyFile | ConvertTo-SecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $Auth = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("pc:" + $plain))
}

# ---- start with Windows --------------------------------------------------------------------------------
if ($Uninstall) {
    Remove-Item $Shortcut -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like "*pirate-sync.ps1*" -and $_.ProcessId -ne $PID } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "Pirate Audio sync won't start with Windows any more (and isn't running now)."
    exit 0
}
if ($Install) {
    $copy = Join-Path $AppDir "pirate-sync.ps1"
    if ($PSCommandPath -ne $copy) { Copy-Item -Force $PSCommandPath $copy }
    $args2 = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$copy`" -Pi `"$Pi`" -EverySeconds $EverySeconds"
    if ($Folders) { $args2 += " -Folders " + (($Folders | ForEach-Object { "`"$_`"" }) -join ",") }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Shortcut)
    $lnk.TargetPath = Join-Path $PSHOME "powershell.exe"
    $lnk.Arguments = $args2
    $lnk.WorkingDirectory = $AppDir
    $lnk.WindowStyle = 7
    $lnk.Description = "Sends new music to the Pirate Audio Pi"
    $lnk.Save()
    Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -ArgumentList $args2 -WindowStyle Hidden
    Write-Host "Pirate Audio sync now starts with Windows and is running in the background."
    Write-Host "Watching:" ((& { if ($Folders) { $Folders } else { Get-DefaultFolders } }) -join "; ")
    Write-Host "Log: $LogFile"
    exit 0
}

# ---- only one copy at a time --------------------------------------------------------------------------
$mutex = New-Object System.Threading.Mutex($false, "Local\PirateAudioSync")
if (-not $mutex.WaitOne(0)) { Write-Host "Pirate Audio sync is already running."; exit 0 }

# ---- talking to the Pi ------------------------------------------------------------------------------------
function Invoke-Pi([string]$Method, [string]$Path, $File) {
    $req = [System.Net.HttpWebRequest]::Create($Pi + $Path)
    $req.Method = $Method
    $req.Proxy = $null  # the Pi is on this network
    $req.Timeout = 30000
    $req.ReadWriteTimeout = 600000
    if ($Auth) { $req.Headers.Add("Authorization", $Auth) }
    if ($File) {
        $req.Timeout = 600000
        $req.AllowWriteStreamBuffering = $false
        $req.ContentType = "application/octet-stream"
        $req.ContentLength = $File.Length
        $in = [IO.File]::OpenRead($File.FullName)
        try { $out = $req.GetRequestStream(); try { $in.CopyTo($out, 1MB) } finally { $out.Close() } }
        finally { $in.Close() }
    }
    try {
        $resp = $req.GetResponse()
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if (-not $resp) { throw }
    }
    try {
        $reader = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $data = $null
        if ($body) { try { $data = $body | ConvertFrom-Json } catch { } }
        return @{ Status = [int]$resp.StatusCode; Data = $data }
    } finally { $resp.Close() }
}

function Get-Sent {
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    if (Test-Path $SentFile) { Get-Content $SentFile -Encoding UTF8 | ForEach-Object { [void]$set.Add($_) } }
    return ,$set
}

function Sync-Once($Watch, $Sent) {
    $count = 0
    foreach ($root in $Watch) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $rootFull = (Resolve-Path -LiteralPath $root).ProviderPath.TrimEnd("\")
        $files = Get-ChildItem -LiteralPath $rootFull -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $Exts -contains $_.Extension.ToLower() -and -not $_.Name.StartsWith(".") }
        foreach ($f in $files) {
            $id = "{0}|{1}|{2}" -f $f.FullName, $f.Length, $f.LastWriteTimeUtc.Ticks
            if ($Sent.Contains($id)) { continue }
            if (((Get-Date).ToUniversalTime() - $f.LastWriteTimeUtc).TotalSeconds -lt 15) { continue }  # still being written
            $rel = $f.FullName.Substring($rootFull.Length).TrimStart("\").Replace("\", "/")
            $q = [Uri]::EscapeDataString($rel)
            try {
                $check = Invoke-Pi "GET" ("/api/check?path=$q&size=" + $f.Length) $null
                if ($check.Status -eq 401) { Write-Log "The music page needs a password: run this again with -Password"; return -1 }
                if ($check.Status -ne 200) { Write-Log "The Pi answered $($check.Status) for $rel"; continue }
                if (-not $check.Data.ok) {
                    Write-Log "Skipped $rel - $($check.Data.error)"
                } elseif ($check.Data.exists) {
                    # already on the Pi
                } else {
                    # (not "$sent": PowerShell names ignore case, and $Sent is the list of files done)
                    $put = Invoke-Pi "PUT" "/api/upload?path=$q" $f
                    if ($put.Status -eq 200) { Write-Log "Sent $rel"; $count++ }
                    elseif ($put.Status -in 400, 411, 413, 415) { Write-Log "Skipped $rel - $($put.Data.error)" }
                    else { Write-Log "Couldn't send $rel ($($put.Status)); trying again later"; continue }
                }
                [void]$Sent.Add($id)
                Add-Content -Path $SentFile -Value $id -Encoding UTF8
            } catch {
                Write-Log "Can't reach the Pi at $Pi right now ($($_.Exception.Message)); trying again later"
                return -1
            }
        }
    }
    return $count
}

$watch = if ($Folders) { $Folders } else { Get-DefaultFolders }
Write-Log ("Watching: " + ($watch -join "; ") + "  ->  $Pi")
$piId = $null
$sentSet = $null
while ($true) {
    try {
        $st = Invoke-Pi "GET" "/api/status" $null
        if ($st.Status -eq 401) { Write-Log "The music page needs a password: run this again with -Password" }
        elseif ($st.Status -eq 200) {
            # Each Pi set-up has its own id: a Pi on a fresh SD card gets a new one, so it's sent everything it
            # doesn't have (the list of files done is kept per id).
            $id = if ($st.Data -and $st.Data.pi_id) { [string]$st.Data.pi_id } else { "" }
            if ($null -eq $sentSet -or $id -ne $piId) {
                $piId = $id
                $script:SentFile = if ($id) { Join-Path $AppDir "sent-$id.txt" } else { Join-Path $AppDir "sent.txt" }
                $sentSet = Get-Sent
            }
            $n = Sync-Once $watch $sentSet
            if ($n -gt 0) { Write-Log "$n new file(s) sent to the Pi" }
        } else { Write-Log "The Pi answered $($st.Status); trying again later" }
    } catch {
        Write-Log "Can't reach the Pi at $Pi right now ($($_.Exception.Message)); trying again later"
    }
    if ($Once) { break }
    Start-Sleep -Seconds $EverySeconds
}
