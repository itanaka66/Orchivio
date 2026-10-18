# Helper for add-user.bat: reads the password without echo (or from $env:ORCHIVIO_NEW_PASSWORD) and runs `orchivio user add`,
# passing the password on stdin. Values arrive in environment variables so that no quoting can go wrong.
$ErrorActionPreference = 'Stop'

function ConvertTo-Plain([System.Security.SecureString] $secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$password = $env:ORCHIVIO_NEW_PASSWORD
if (-not $password) {
    $first = ConvertTo-Plain (Read-Host 'Password' -AsSecureString)
    $second = ConvertTo-Plain (Read-Host 'Repeat the password' -AsSecureString)
    if ($first -ne $second) { [Console]::Error.WriteLine('error: the passwords differ'); exit 1 }
    $password = $first
}

$cli = @('user', 'add', '--email', $env:ORCHIVIO_U_EMAIL, '--name', $env:ORCHIVIO_U_NAME, '--password-stdin')
if ($env:ORCHIVIO_U_ORG) { $cli += @('--org', $env:ORCHIVIO_U_ORG, '--role', $env:ORCHIVIO_U_ROLE) }

$bin = if ($env:ORCHIVIO_BIN) { $env:ORCHIVIO_BIN } else { 'orchivio' }
$parts = @($bin -split ' ' | Where-Object { $_ })
$exe = $parts[0]
$prefix = if ($parts.Count -gt 1) { $parts[1..($parts.Count - 1)] } else { @() }

# UTF-8 without a byte-order mark, so passwords with non-ASCII characters arrive intact
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$password | & $exe @prefix @cli
exit $LASTEXITCODE
