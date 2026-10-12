[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [int]$DiskNumber,

  [Parameter(Mandatory = $true)]
  [int]$PartitionNumber,

  [string]$SourceWslPath = "/ai/speaker/pi-release",

  [switch]$Delete
)

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Error "Run this script in an elevated PowerShell window (Administrator)."
  exit 1
}

$diskPath = "\\.\PHYSICALDRIVE$DiskNumber"
$mountName = "PHYSICALDRIVE${DiskNumber}p${PartitionNumber}"
$mountPath = "/mnt/wsl/$mountName"

Write-Host "Mounting $diskPath (partition $PartitionNumber)..."
wsl.exe --mount $diskPath --partition $PartitionNumber --type ext4 | Out-Null
Start-Sleep -Seconds 1

$check = wsl.exe -e bash -lc "test -d $mountPath && echo ok"
if ($check -notmatch "ok") {
  $mounted = wsl.exe -e bash -lc "ls -1 /mnt/wsl"
  Write-Error "Mount path not found: $mountPath. Mounted entries: $mounted"
  wsl.exe --unmount $diskPath | Out-Null
  exit 1
}

$deleteFlag = if ($Delete.IsPresent) { "--delete" } else { "" }
$copyCmd = @"
set -e
SRC="$SourceWslPath/ai/speaker"
DST="$mountPath/ai/speaker"
if command -v rsync >/dev/null 2>&1; then
  sudo mkdir -p "$DST"
  sudo rsync -a $deleteFlag "$SRC/" "$DST/"
else
  sudo mkdir -p "$DST"
  sudo cp -a "$SRC/." "$DST/"
fi
"@

wsl.exe -e bash -lc "$copyCmd"

wsl.exe --unmount $diskPath | Out-Null
Write-Host "Done. Copied to $mountPath/ai/speaker"
