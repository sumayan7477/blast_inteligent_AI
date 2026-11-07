<#
.SYNOPSIS
  Stop the local backend (8000) and frontend (5173); add -IncludeOllama to stop Ollama (11434) too.
  Only python / node / ollama processes listening on those ports are stopped.
#>
param([switch]$IncludeOllama)
$targets = @(@{ Port = 8000; Names = @("python", "pythonw") }, @{ Port = 5173; Names = @("node") })
if ($IncludeOllama) { $targets += @{ Port = 11434; Names = @("ollama", "ollama app") } }

foreach ($t in $targets) {
    $pids = Get-NetTCPConnection -LocalPort $t.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    if (-not $pids) { Write-Host "Port $($t.Port): nothing running"; continue }
    foreach ($p in $pids) {
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc -and ($t.Names -contains $proc.ProcessName)) {
            Stop-Process -Id $p -Force
            Write-Host "Port $($t.Port): stopped $($proc.ProcessName) (pid $p)"
        } elseif ($proc) {
            Write-Warning "Port $($t.Port): owned by '$($proc.ProcessName)' (pid $p) - not stopped."
        }
    }
}
