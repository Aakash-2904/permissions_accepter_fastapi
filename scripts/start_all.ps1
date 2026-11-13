# Starts the relay, an ngrok tunnel and the phone console as hidden background processes.
# They stop on reboot/sleep; just run this again.   Usage: powershell -File scripts\start_all.ps1
$p = Split-Path $PSScriptRoot -Parent
$py = "$p\.venv\Scripts\python.exe"

# Optional static ngrok domain from .env (NGROK_DOMAIN=your-name.ngrok-free.dev).
$domain = (Get-Content "$p\.env" -ErrorAction SilentlyContinue | Where-Object { $_ -match '^NGROK_DOMAIN=' }) -replace '^NGROK_DOMAIN=', ''
$ngrokArgs = @("http", "8000", "--log=stdout")
if ($domain) { $ngrokArgs = @("http", "--url=$($domain.Trim())", "8000", "--log=stdout") }

# Stop old copies so ports and topics aren't double-used.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'relay.app:app|console.phone_console' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force

Start-Process $py -ArgumentList "-m","uvicorn","relay.app:app","--host","127.0.0.1","--port","8000","--workers","1" -WorkingDirectory $p -WindowStyle Hidden -RedirectStandardOutput "$p\relay_out.log" -RedirectStandardError "$p\relay_err.log"
Start-Process ngrok -ArgumentList $ngrokArgs -WorkingDirectory $p -WindowStyle Hidden -RedirectStandardOutput "$p\ngrok_out.log" -RedirectStandardError "$p\ngrok_err.log"
Start-Process $py -ArgumentList "-m","console.phone_console" -WorkingDirectory $p -WindowStyle Hidden -RedirectStandardOutput "$p\console_out.log" -RedirectStandardError "$p\console_err.log"
Write-Output "started relay, ngrok, phone console"
