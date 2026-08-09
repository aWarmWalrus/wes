# WES Discord bot launcher — started at logon by the "WES Discord" scheduled task.
# Bridges owner DMs/@mentions to the local server's /respond_text (pc/wes_discord.py).
# WES_DISCORD_TOKEN / WES_DISCORD_OWNER_ID are read from the user environment (setx).

$base = "C:\Users\awarm\wes-pc"
# Code now runs from a LOCAL clone, not the Z: share: the PC must not need
# the Pi up to start (see #032), and execution policy blocks unsigned
# scripts on a share entirely. WES_REPO overrides for a different checkout.
$repo = if ($env:WES_REPO) { $env:WES_REPO } else { "C:\Users\awarm\wes" }
$py = "$base\.venv\Scripts\python.exe"
$bot = "$repo\pc\wes_discord.py"
$logdir = "$base\logs"

New-Item -ItemType Directory -Force -Path $logdir | Out-Null

# Ensure the creds are present even if the task's env snapshot is stale.
foreach ($name in "WES_DISCORD_TOKEN", "WES_DISCORD_OWNER_ID") {
    if (-not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        Set-Item "env:$name" ([System.Environment]::GetEnvironmentVariable($name, "User"))
    }
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
# Reset the log on each start so it doesn't grow unbounded across restarts.
Set-Content "$logdir\discord.log" "==== WES discord bot starting $stamp ===="
& $py $bot *>> "$logdir\discord.log"
