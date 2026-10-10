#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$AppServer,
    [string[]]$Names = @('stgiss.perodua.com.my', 'stgissrp.perodua.com.my', 'stgisssp.perodua.com.my', 'stgisscp.perodua.com.my'),
    [string]$Path = '/dev/app/',
    [string]$ModePath = '/dev/uiux/api/handover/mode',
    [string]$HubName,
    [switch]$AddHostsBypass,
    [switch]$RemoveHostsBypass,
    [string]$HostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
)

$ErrorActionPreference = 'Stop'
$Usage = 'Usage: .\client-check.ps1 [-AppServer IPV4] [-Names NAME,NAME,...] [-Path /dev/app/] [-ModePath /dev/uiux/api/handover/mode] [-HubName NAME] [-AddHostsBypass | -RemoveHostsBypass] [-HostsFile FILE]'

function Get-InnerMessage {
    param($Exception)
    $current = $Exception
    while ($null -ne $current.InnerException) {
        $current = $current.InnerException
    }
    return [string]$current.Message
}

function Test-IPv4 {
    param([string]$Text)
    return ($Text -match '^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$')
}

function Test-Administrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal -ArgumentList $identity
        return [bool]$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Split-HostsRecords {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) {
        return ,([string[]]@())
    }
    $records = @([regex]::Split($Text, '(?<=\n)') | Where-Object { $_ -ne '' })
    return ,([string[]]$records)
}

function Get-HostsEntry {
    param([string]$Line)
    $body = $Line -replace '[\r\n]+$', ''
    $content = $body
    $comment = ''
    $hash = $body.IndexOf([char]'#')
    if ($hash -ge 0) {
        $content = $body.Substring(0, $hash)
        $comment = $body.Substring($hash)
    }
    $fields = @(($content.Trim() -split '\s+') | Where-Object { $_ -ne '' })
    if ($fields.Count -lt 2) {
        return $null
    }
    $lead = ''
    if ($content -match '^(\s*)') {
        $lead = $Matches[1]
    }
    $trail = ''
    if ($content -match '(\s*)$') {
        $trail = $Matches[1]
    }
    return [pscustomobject]@{
        Address = [string]$fields[0]
        Names   = [string[]]@($fields[1..($fields.Count - 1)])
        Lead    = $lead
        Trail   = $trail
        Comment = $comment
        Ending  = $Line.Substring($body.Length)
    }
}

function Get-HostsChange {
    param(
        [string[]]$Records,
        [string[]]$Names,
        [string]$Address,
        [string]$Action
    )
    $kept = New-Object System.Collections.Generic.List[string]
    $added = New-Object System.Collections.Generic.List[string]
    $removed = New-Object System.Collections.Generic.List[string]
    $rewritten = New-Object System.Collections.Generic.List[string]
    $notes = New-Object System.Collections.Generic.List[string]
    $mapped = @{}
    foreach ($record in $Records) {
        $entry = Get-HostsEntry -Line $record
        $hits = 0
        $others = New-Object System.Collections.Generic.List[string]
        if ($null -ne $entry) {
            foreach ($entryName in $entry.Names) {
                if ($Names -contains $entryName) {
                    $hits++
                    if (-not $mapped.ContainsKey($entryName)) {
                        $mapped[$entryName] = $entry.Address
                    }
                } else {
                    $others.Add($entryName)
                }
            }
        }
        if ($Action -eq 'Remove' -and $hits -gt 0) {
            $old = $record -replace '[\r\n]+$', ''
            if ($others.Count -eq 0) {
                $removed.Add($old)
            } else {
                $line = $entry.Lead + $entry.Address + ' ' + ($others.ToArray() -join ' ') + $entry.Trail + $entry.Comment
                $rewritten.Add(('{0} -> {1}' -f $old, $line))
                $kept.Add($line + $entry.Ending)
            }
        } else {
            $kept.Add($record)
        }
    }
    if ($Action -eq 'Add') {
        foreach ($name in $Names) {
            if ($mapped.ContainsKey($name)) {
                $notes.Add(('{0} is already mapped to {1} in the hosts file: not changed' -f $name, $mapped[$name]))
            } else {
                $added.Add(('{0} {1} # perodua-bypass' -f $Address, $name))
            }
        }
    }
    $newline = "`r`n"
    $original = -join @($Records)
    if (-not $original.Contains("`r`n") -and $original.Contains("`n")) {
        $newline = "`n"
    }
    $output = New-Object System.Collections.Generic.List[string]
    foreach ($record in $kept) {
        $output.Add($record)
    }
    if ($added.Count -gt 0) {
        if ($output.Count -gt 0 -and -not $output[$output.Count - 1].EndsWith("`n", [System.StringComparison]::Ordinal)) {
            $output[$output.Count - 1] = $output[$output.Count - 1] + $newline
        }
        foreach ($line in $added) {
            $output.Add($line + $newline)
        }
    }
    return [pscustomobject]@{
        Records   = [string[]]$output.ToArray()
        Added     = [string[]]$added.ToArray()
        Removed   = [string[]]$removed.ToArray()
        Rewritten = [string[]]$rewritten.ToArray()
        Notes     = [string[]]$notes.ToArray()
        Changed   = ($added.Count -gt 0 -or $removed.Count -gt 0 -or $rewritten.Count -gt 0)
    }
}

function Update-HostsFile {
    param(
        [string]$File,
        [string[]]$Names,
        [string]$Address,
        [string]$Action
    )
    $bytes = [System.IO.File]::ReadAllBytes($File)
    if ($bytes.Length -ge 2 -and (($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) -or ($bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF))) {
        throw ('{0} is UTF-16 text. Windows reads the hosts file as ANSI or UTF-8 text. Save it as ANSI or UTF-8 first; nothing was changed.' -f $File)
    }
    $start = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $start = 3
    }
    $latin = [System.Text.Encoding]::GetEncoding(28591)
    $text = $latin.GetString($bytes, $start, $bytes.Length - $start)
    $records = Split-HostsRecords -Text $text
    $change = Get-HostsChange -Records $records -Names $Names -Address $Address -Action $Action
    if ($change.Changed) {
        $body = $latin.GetBytes((-join $change.Records))
        $stream = New-Object System.IO.MemoryStream
        try {
            if ($start -gt 0) {
                $stream.Write($bytes, 0, $start)
            }
            $stream.Write($body, 0, $body.Length)
            [System.IO.File]::WriteAllBytes($File, $stream.ToArray())
        } finally {
            $stream.Dispose()
        }
    }
    return $change
}

function Clear-NameCache {
    $command = Get-Command -Name Clear-DnsClientCache -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        try {
            Clear-DnsClientCache
            return 'Clear-DnsClientCache'
        } catch {
            $null = $_
        }
    }
    $ipconfig = Get-Command -Name ipconfig.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $ipconfig) {
        $ErrorActionPreference = 'Continue'
        $PSNativeCommandUseErrorActionPreference = $false
        $null = @(& $ipconfig.Path /flushdns 2>$null)
        if ($LASTEXITCODE -eq 0) {
            return 'ipconfig /flushdns'
        }
    }
    return ''
}

function Invoke-CurlExe {
    param(
        [string]$Curl,
        [string[]]$Arguments
    )
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    $lines = @(& $Curl @Arguments 2>$null)
    $code = $LASTEXITCODE
    return [pscustomobject]@{
        Lines    = [string[]]$lines
        ExitCode = [int]$code
    }
}

function ConvertTo-ComparableUrl {
    param([string]$Text)
    $value = $Text.Trim()
    if ($value -match '^(https://[^/:]+):443(/.*)?$') {
        $value = $Matches[1] + $Matches[2]
    } elseif ($value -match '^(http://[^/:]+):80(/.*)?$') {
        $value = $Matches[1] + $Matches[2]
    }
    return $value
}

function Test-SameUrl {
    param(
        [string]$Location,
        [string]$Url
    )
    $target = $Location.Trim()
    if ($target.StartsWith('/', [System.StringComparison]::Ordinal) -and -not $target.StartsWith('//', [System.StringComparison]::Ordinal)) {
        if ($Url -match '^(https?://[^/]+)') {
            $target = $Matches[1] + $target
        }
    }
    return ((ConvertTo-ComparableUrl -Text $target) -ieq (ConvertTo-ComparableUrl -Text $Url))
}

function ConvertFrom-CurlHeaders {
    param(
        [string[]]$Lines,
        [string]$Url
    )
    $blocks = New-Object System.Collections.Generic.List[object]
    $current = $null
    foreach ($raw in $Lines) {
        $line = ([string]$raw) -replace '\r$', ''
        if ($line -match '^HTTP/[0-9.]+\s+([0-9]{3})(\s+(.*))?$') {
            $current = [pscustomobject]@{
                StatusLine = $line.Trim()
                Code       = [int]$Matches[1]
                Reason     = [string]$Matches[3]
                Headers    = @{}
            }
            $blocks.Add($current)
        } elseif ($null -ne $current -and $line -match '^([A-Za-z0-9-]+):\s*(.*)$') {
            $key = $Matches[1].ToLowerInvariant()
            if (-not $current.Headers.ContainsKey($key)) {
                $current.Headers[$key] = $Matches[2].Trim()
            }
        }
    }
    $chosen = $null
    for ($i = 0; $i -lt $blocks.Count; $i++) {
        $block = $blocks[$i]
        if ($block.Code -lt 200) {
            continue
        }
        if ($i -lt $blocks.Count - 1 -and $block.Code -eq 200 -and $block.Reason -match 'connection established') {
            continue
        }
        $chosen = $block
        break
    }
    if ($null -eq $chosen -and $blocks.Count -gt 0) {
        $chosen = $blocks[$blocks.Count - 1]
    }
    $result = [pscustomobject]@{
        Url        = $Url
        ExitCode   = 0
        Code       = 0
        StatusLine = ''
        Server     = ''
        Location   = ''
        Loop       = $false
    }
    if ($null -ne $chosen) {
        $result.Code = $chosen.Code
        $result.StatusLine = $chosen.StatusLine
        if ($chosen.Headers.ContainsKey('server')) {
            $result.Server = [string]$chosen.Headers['server']
        }
        if ($chosen.Headers.ContainsKey('location')) {
            $result.Location = [string]$chosen.Headers['location']
            if ($chosen.Code -ge 300 -and $chosen.Code -lt 400 -and (Test-SameUrl -Location $result.Location -Url $Url)) {
                $result.Loop = $true
            }
        }
    }
    return $result
}

function Get-HttpHead {
    param(
        [string]$Curl,
        [string]$Url,
        [string]$Name,
        [string]$Direct,
        [switch]$Verify
    )
    $nullDevice = 'NUL'
    if ($env:OS -ne 'Windows_NT') {
        $nullDevice = '/dev/null'
    }
    $curlArgs = @('-s', '--max-time', '10', '-o', $nullDevice, '-D', '-')
    if ($Verify) {
        $curlArgs += '--ssl-no-revoke'
    } else {
        $curlArgs += '-k'
    }
    if ($Direct) {
        $curlArgs += @('--noproxy', $Name, '--resolve', ('{0}:443:{1}' -f $Name, $Direct))
    }
    $curlArgs += $Url
    $run = Invoke-CurlExe -Curl $Curl -Arguments $curlArgs
    $result = ConvertFrom-CurlHeaders -Lines $run.Lines -Url $Url
    $result.ExitCode = $run.ExitCode
    return $result
}

function Get-HandoverMode {
    param(
        [string]$Curl,
        [string]$Url,
        [string]$Name,
        [string]$Direct
    )
    $marker = '__client_check_http_code__'
    $curlArgs = @('-s', '-k', '--max-time', '10', '-w', ('\n' + $marker + '%{http_code}'))
    if ($Direct) {
        $curlArgs += @('--noproxy', $Name, '--resolve', ('{0}:443:{1}' -f $Name, $Direct))
    }
    $curlArgs += $Url
    $run = Invoke-CurlExe -Curl $Curl -Arguments $curlArgs
    $code = 0
    $body = New-Object System.Collections.Generic.List[string]
    foreach ($line in $run.Lines) {
        if (([string]$line).StartsWith($marker, [System.StringComparison]::Ordinal)) {
            $parsed = 0
            if ([int]::TryParse(([string]$line).Substring($marker.Length).Trim(), [ref]$parsed)) {
                $code = $parsed
            }
        } else {
            $body.Add([string]$line)
        }
    }
    $value = ''
    $text = ($body.ToArray()) -join "`n"
    if ($text.Trim().StartsWith('{', [System.StringComparison]::Ordinal)) {
        try {
            $json = ConvertFrom-Json -InputObject $text
            if ($null -ne $json.data -and $null -ne $json.data.mode) {
                $value = [string]$json.data.mode
            } elseif ($null -ne $json.mode) {
                $value = [string]$json.mode
            }
        } catch {
            $value = ''
        }
    }
    return [pscustomobject]@{
        Code     = $code
        ExitCode = $run.ExitCode
        Value    = $value
    }
}

function Test-TcpPort {
    param(
        [string]$Address,
        [int]$Port
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(5000, $false)) {
            return [pscustomobject]@{ Open = $false; Detail = 'no answer within 5 s (blocked, or no route)' }
        }
        $client.EndConnect($pending)
        return [pscustomobject]@{ Open = $true; Detail = 'open' }
    } catch {
        return [pscustomobject]@{ Open = $false; Detail = ('closed: {0}' -f (Get-InnerMessage -Exception $_.Exception)) }
    } finally {
        $client.Close()
    }
}

function Resolve-HostName {
    param([string]$Name)
    try {
        $all = [System.Net.Dns]::GetHostAddresses($Name)
        $v4 = @($all | Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } | ForEach-Object { $_.IPAddressToString })
        $v6 = @($all | Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 } | ForEach-Object { $_.IPAddressToString })
        return [pscustomobject]@{
            IPv4  = [string[]]$v4
            IPv6  = [string[]]$v6
            Error = ''
        }
    } catch {
        return [pscustomobject]@{
            IPv4  = [string[]]@()
            IPv6  = [string[]]@()
            Error = (Get-InnerMessage -Exception $_.Exception)
        }
    }
}

function Get-CurlExitText {
    param([int]$Code)
    switch ($Code) {
        0 { return 'no HTTP status line in the answer' }
        6 { return 'the name does not resolve' }
        7 { return 'the connection failed (refused, or no route)' }
        28 { return 'no answer within 10 s' }
        35 { return 'the TLS handshake failed' }
        60 { return 'the certificate is not trusted on this PC, or it does not cover the name' }
        52 { return 'the server closed the connection without an answer' }
        56 { return 'the connection broke while receiving' }
        default { return 'curl failed' }
    }
}

function Test-HttpWorks {
    param($Result)
    if ($null -eq $Result) {
        return $false
    }
    return ($Result.Code -ge 200 -and $Result.Code -lt 400 -and -not $Result.Loop)
}

function Format-HttpResult {
    param($Result)
    if ($Result.Code -eq 0) {
        return ('no answer (curl exit {0}: {1})' -f $Result.ExitCode, (Get-CurlExitText -Code $Result.ExitCode))
    }
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add($Result.StatusLine)
    if ($Result.Server) {
        $parts.Add(('Server: {0}' -f $Result.Server))
    }
    if ($Result.Location) {
        $parts.Add(('Location: {0}' -f $Result.Location))
    }
    $text = $parts.ToArray() -join '; '
    if ($Result.Loop) {
        $text = $text + ' -> LOOP: a redirect to the same URL'
    }
    return $text
}

function Format-HttpShort {
    param($Result)
    if ($null -eq $Result) {
        return '-'
    }
    if ($Result.Code -eq 0) {
        return ('no answer (curl {0})' -f $Result.ExitCode)
    }
    $text = [string]$Result.Code
    if ($Result.Loop) {
        $text = $text + ' LOOP'
    }
    if ($Result.Server) {
        $text = $text + ' ' + $Result.Server
    }
    return $text
}

function Format-ModeMiss {
    param($Mode)
    if ($Mode.Code -gt 0) {
        return ('HTTP {0}, no mode in the answer' -f $Mode.Code)
    }
    return ('no answer (curl exit {0}: {1})' -f $Mode.ExitCode, (Get-CurlExitText -Code $Mode.ExitCode))
}

function New-Verdict {
    param(
        [string]$Verdict,
        [string]$Reason
    )
    return [pscustomobject]@{
        Verdict = $Verdict
        Reason  = $Reason
    }
}

function Get-CertificateCheck {
    param($Result)
    if ($Result.Code -gt 0) {
        return [pscustomobject]@{
            State = 'OK'
            Text  = 'OK trusted on this PC for this name (checked without -k, as a browser does)'
        }
    }
    if ($Result.ExitCode -eq 60 -or $Result.ExitCode -eq 35) {
        return [pscustomobject]@{
            State = 'PROBLEM'
            Text  = ('PROBLEM curl exit {0}: {1}. Browsers on this PC show a certificate error for this name.' -f $Result.ExitCode, (Get-CurlExitText -Code $Result.ExitCode))
        }
    }
    return [pscustomobject]@{
        State = 'NOTE'
        Text  = ('NOTE not checked: no answer without -k (curl exit {0}: {1})' -f $Result.ExitCode, (Get-CurlExitText -Code $Result.ExitCode))
    }
}

function Get-Verdict {
    param(
        $Normal,
        $Direct,
        $Tcp,
        $Certificate,
        [string]$ModeValue,
        [string]$DirectModeValue,
        [string]$Expected,
        [string]$ModePath,
        [bool]$ViaAppServer,
        [string]$FrontAddress
    )
    if ($null -eq $Normal) {
        return New-Verdict -Verdict 'NOT CHECKED' -Reason 'curl.exe is missing, so no HTTP check ran.'
    }
    if ($Normal.Loop -and $ViaAppServer) {
        return New-Verdict -Verdict 'SERVER problem' -Reason 'this PC goes straight to the App server, and the App server redirects to the same URL. On the App server, run: sudo bash https.sh diagnose'
    }
    if ($Normal.Loop -and $null -ne $Direct -and $Direct.Loop) {
        return New-Verdict -Verdict 'SERVER problem' -Reason 'the direct path to the App server also redirects to the same URL, so the loop is on the App server, not on the front. On the App server, run: sudo bash https.sh diagnose'
    }
    if ($Normal.Loop) {
        $front = 'the front'
        if ($FrontAddress) {
            $front = 'the front at ' + $FrontAddress
        }
        if ($Normal.Server) {
            $front = $front + ' (Server: ' + $Normal.Server + ')'
        }
        return New-Verdict -Verdict 'LOOP' -Reason ($front + ' forwards HTTPS requests to port 80 of the App server, and port 80 redirects them back to HTTPS. Ask the front''s owner to forward HTTPS to port 443 with the original Host header, or, on the App server, add the http option to the route of this name in the routes table of https.sh, then run: sudo bash https.sh apply (README: Behind a TLS front on port 80).')
    }
    $normalWorks = Test-HttpWorks -Result $Normal
    $directWorks = Test-HttpWorks -Result $Direct
    if ($normalWorks) {
        if ($ModeValue -and ($ModeValue -ine $Expected)) {
            if ($ViaAppServer) {
                return New-Verdict -Verdict 'SERVER problem' -Reason ('the App server gives the mode {0} for this name, expected {1}. Check the host names of the App release.' -f $ModeValue, $Expected)
            }
            return New-Verdict -Verdict 'FRONT problem' -Reason ('the hand-over mode is {0}, expected {1}: the front changes the Host header. Ask the front''s owner to keep the original Host header.' -f $ModeValue, $Expected)
        }
        if (-not $ModeValue -and $DirectModeValue -and -not $ViaAppServer) {
            return New-Verdict -Verdict 'FRONT problem' -Reason ('the hand-over API {0} answers on the direct path (mode {1}), but not through the front: the front blocks or changes the App API path, for example with a JavaScript challenge or a verification code. Ask the front''s owner to let the App API path through unchanged.' -f $ModePath, $DirectModeValue)
        }
        if ($null -ne $Certificate -and $Certificate.State -eq 'PROBLEM') {
            if ($ViaAppServer) {
                return New-Verdict -Verdict 'SERVER problem' -Reason 'this PC goes straight to the App server, and this PC does not trust its certificate for this name. On the App server, run: sudo bash https.sh status'
            }
            return New-Verdict -Verdict 'FRONT problem' -Reason 'this PC does not trust the certificate of the normal path for this name: browsers show a certificate error. Ask the front''s owner for a certificate that covers this name and that this PC trusts.'
        }
        return New-Verdict -Verdict 'OK' -Reason 'the page answers through the normal path.'
    }
    $normalText = Format-HttpShort -Result $Normal
    if ($Normal.Code -eq 0 -and $Normal.ExitCode -eq 6) {
        return New-Verdict -Verdict 'UNREACHABLE' -Reason 'this PC cannot resolve the name. Check the DNS server of this PC (the internal DNS, or the VPN).'
    }
    if ($null -ne $Direct) {
        if ($directWorks) {
            return New-Verdict -Verdict 'FRONT problem' -Reason ('the direct path to the App server works, the normal path fails ({0}). Send this output to the front''s owner.' -f $normalText)
        }
        if ($Direct.Code -eq 0) {
            if ($null -ne $Tcp -and $Tcp.Open) {
                return New-Verdict -Verdict 'SERVER problem' -Reason ('port 443 of the App server accepts the connection, but the direct path gets no HTTP answer ({0}). On the App server, run: sudo bash https.sh diagnose' -f (Format-HttpShort -Result $Direct))
            }
            if ($Normal.Code -eq 0) {
                return New-Verdict -Verdict 'UNREACHABLE' -Reason 'neither the normal path nor the App server answers. Check the network of this PC (VPN, firewall), then the App server.'
            }
            return New-Verdict -Verdict 'UNREACHABLE' -Reason ('the normal path fails ({0}), and this PC gets no answer from port 443 of the App server, so the script cannot tell a front problem from a server problem. Run it again on a PC that reaches port 443 of the App server.' -f $normalText)
        }
        return New-Verdict -Verdict 'SERVER problem' -Reason ('the direct path fails too ({0}). On the App server, run: sudo bash https.sh diagnose' -f (Format-HttpShort -Result $Direct))
    }
    return New-Verdict -Verdict 'UNREACHABLE' -Reason ('the normal path fails ({0}). Run again with -AppServer to tell a front problem from a server problem.' -f $normalText)
}

function Format-TableRow {
    param(
        [string[]]$Cells,
        [int[]]$Widths
    )
    $parts = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $Cells.Count; $i++) {
        $cell = [string]$Cells[$i]
        if ($i -eq $Cells.Count - 1) {
            $parts.Add($cell)
        } else {
            $parts.Add($cell.PadRight($Widths[$i]))
        }
    }
    return ($parts.ToArray() -join '  ')
}

$cleanNames = New-Object System.Collections.Generic.List[string]
$seenNames = @{}
foreach ($item in @($Names)) {
    foreach ($part in (([string]$item) -split '[,;\s]+')) {
        if ($part -and -not $seenNames.ContainsKey($part)) {
            $seenNames[$part] = $true
            $cleanNames.Add($part)
        }
    }
}
$Names = $cleanNames.ToArray()
$AppServer = ([string]$AppServer).Trim()
$HubName = ([string]$HubName).Trim()
if (-not $HubName -and $Names.Count -gt 0) {
    $HubName = $Names[0]
}

$refusals = New-Object System.Collections.Generic.List[string]
if ($Names.Count -eq 0) {
    $refusals.Add('-Names is empty.')
}
foreach ($name in $Names) {
    if ($name -notmatch '^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$') {
        $refusals.Add(('"{0}" is not a host name.' -f $name))
    }
}
if ($AppServer -and -not (Test-IPv4 -Text $AppServer)) {
    $refusals.Add(('-AppServer "{0}" is not an IPv4 address, such as 192.168.23.13.' -f $AppServer))
}
if ($Path -notmatch '^/\S*$') {
    $refusals.Add(('-Path "{0}" must start with /, such as /dev/app/.' -f $Path))
}
if ($ModePath -notmatch '^/\S*$') {
    $refusals.Add(('-ModePath "{0}" must start with /, such as /dev/uiux/api/handover/mode.' -f $ModePath))
}
if ($AddHostsBypass -and $RemoveHostsBypass) {
    $refusals.Add('Give -AddHostsBypass or -RemoveHostsBypass, not both.')
}
if ($AddHostsBypass -and -not $AppServer) {
    $refusals.Add('-AddHostsBypass needs -AppServer: the hosts file lines map each name to that address.')
}
if ($refusals.Count -gt 0) {
    foreach ($message in $refusals) {
        Write-Output ('ERROR {0}' -f $message)
    }
    Write-Output $Usage
    exit 2
}

Write-Output ('client-check.ps1 on {0} at {1}, PowerShell {2}' -f $env:COMPUTERNAME, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'), $PSVersionTable.PSVersion)

if ($AddHostsBypass -or $RemoveHostsBypass) {
    $action = 'Remove'
    $switchName = '-RemoveHostsBypass'
    if ($AddHostsBypass) {
        $action = 'Add'
        $switchName = '-AddHostsBypass'
    }
    if (-not (Test-Administrator)) {
        Write-Output ('ERROR {0} changes {1} and needs administrator rights. Open PowerShell with "Run as administrator" and run the command again. Nothing was changed.' -f $switchName, $HostsFile)
        exit 2
    }
    try {
        $HostsFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($HostsFile)
        $change = Update-HostsFile -File $HostsFile -Names $Names -Address $AppServer -Action $action
    } catch {
        Write-Output ('ERROR {0} was not changed: {1}' -f $HostsFile, (Get-InnerMessage -Exception $_.Exception))
        exit 2
    }
    Write-Output ('Hosts file: {0}' -f $HostsFile)
    foreach ($line in $change.Added) {
        Write-Output ('  added:   {0}' -f $line)
    }
    foreach ($line in $change.Removed) {
        Write-Output ('  removed: {0}' -f $line)
    }
    foreach ($line in $change.Rewritten) {
        Write-Output ('  changed: {0} (the other names on the line stay)' -f $line)
    }
    foreach ($line in $change.Notes) {
        Write-Output ('  NOTE {0}' -f $line)
    }
    if (-not $change.Changed) {
        Write-Output '  no change'
    }
    $cleared = Clear-NameCache
    if ($cleared) {
        Write-Output ('  DNS cache cleared ({0})' -f $cleared)
    } else {
        Write-Output '  NOTE the DNS cache was not cleared: Clear-DnsClientCache and ipconfig /flushdns are not available. Close and open the browser.'
    }
    if ($action -eq 'Add') {
        Write-Output '  These lines send the names straight to the App server and skip the front. Remove them after the front is repaired: run this script again with -RemoveHostsBypass.'
    }
}

$curl = ''
$curlCommand = Get-Command -Name curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -ne $curlCommand) {
    $curl = [string]$curlCommand.Path
}
if ($curl) {
    $versionRun = Invoke-CurlExe -Curl $curl -Arguments @('--version')
    $versionLine = ''
    if ($versionRun.Lines.Count -gt 0) {
        $versionLine = $versionRun.Lines[0]
    }
    Write-Output ('curl.exe: {0} ({1})' -f $curl, $versionLine)
    Write-Output 'HTTP checks: curl.exe GET, at most 10 s each, redirects not followed. The Certificate line checks the certificate as a browser does; every other check uses -k (no certificate check).'
} else {
    Write-Output 'NOTE curl.exe was not found. Windows 10 (version 1803 and later) and Windows 11 have it in C:\Windows\System32. The HTTP checks are skipped.'
}

$tcp = $null
if ($AppServer) {
    $tcp = Test-TcpPort -Address $AppServer -Port 443
    Write-Output ('App server: {0}' -f $AppServer)
} else {
    Write-Output 'App server: not given. Without -AppServer there is no direct check, and a front problem and a server problem look the same.'
}
if (-not ($Names -contains $HubName)) {
    Write-Output ('NOTE the sign-in host {0} is not in -Names: every name expects the mode module.' -f $HubName)
}

$rows = New-Object System.Collections.Generic.List[object]
foreach ($name in $Names) {
    $expected = 'module'
    if ($name -ieq $HubName) {
        $expected = 'hub'
    }
    $url = 'https://{0}{1}' -f $name, $Path
    Write-Output ''
    Write-Output ('{0} (expected mode: {1})' -f $name, $expected)
    Write-Output ('  URL:          {0}' -f $url)

    $resolved = Resolve-HostName -Name $name
    $addresses = @($resolved.IPv4) + @($resolved.IPv6)
    if ($addresses.Count -gt 0) {
        Write-Output ('  Resolves to:  {0}' -f ($addresses -join ', '))
    } else {
        Write-Output ('  Resolves to:  none ({0})' -f $resolved.Error)
    }
    $viaAppServer = $false
    $frontAddress = ''
    if ($AppServer -and ($resolved.IPv4 -contains $AppServer)) {
        $viaAppServer = $true
        Write-Output ('  NOTE this PC sends {0} straight to the App server (hosts file or DNS): the normal path is the direct path, and the front is not checked.' -f $name)
    } elseif ($resolved.IPv4.Count -gt 0) {
        $frontAddress = $resolved.IPv4[0]
    }

    $normal = $null
    $direct = $null
    $certificate = $null
    $modeValue = ''
    $directModeValue = ''
    $modeCell = '-'
    if ($curl) {
        $normal = Get-HttpHead -Curl $curl -Url $url -Name $name -Direct ''
        Write-Output ('  Normal path:  {0}' -f (Format-HttpResult -Result $normal))
        if ($normal.Code -gt 0) {
            $certificate = Get-CertificateCheck -Result (Get-HttpHead -Curl $curl -Url $url -Name $name -Direct '' -Verify)
            Write-Output ('  Certificate:  {0}' -f $certificate.Text)
        }
    } else {
        Write-Output '  Normal path:  not checked (no curl.exe)'
    }
    if ($AppServer) {
        Write-Output ('  TCP 443:      {0} {1}' -f $AppServer, $tcp.Detail)
        if ($curl) {
            $direct = Get-HttpHead -Curl $curl -Url $url -Name $name -Direct $AppServer
            Write-Output ('  Direct:       {0}' -f (Format-HttpResult -Result $direct))
        }
    }
    if ($curl) {
        $modeUrl = 'https://{0}{1}' -f $name, $ModePath
        $mode = Get-HandoverMode -Curl $curl -Url $modeUrl -Name $name -Direct ''
        $modeValue = $mode.Value
        if (-not $modeValue -and $AppServer -and -not $viaAppServer) {
            $directModeValue = (Get-HandoverMode -Curl $curl -Url $modeUrl -Name $name -Direct $AppServer).Value
        }
        if (-not $modeValue -and $directModeValue) {
            $modeCell = 'blocked'
            Write-Output ('  Mode:         PROBLEM not read through the normal path ({0}), but the direct path gives {1}: the front blocks or changes {2}' -f (Format-ModeMiss -Mode $mode), $directModeValue, $ModePath)
        } elseif (-not $modeValue -and -not $AppServer -and $mode.Code -ge 200 -and $mode.Code -lt 300) {
            $modeCell = 'not read'
            Write-Output ('  Mode:         NOTE not read: HTTP {0}, but not the App''s answer: a JavaScript challenge or a verification page of the front, or an App release without this API. Run again with -AppServer to compare with the direct path.' -f $mode.Code)
        } elseif (-not $modeValue) {
            $modeCell = 'not read'
            Write-Output ('  Mode:         NOTE not read: {0}' -f (Format-ModeMiss -Mode $mode))
        } elseif ($modeValue -ieq $expected) {
            $modeCell = $modeValue
            Write-Output ('  Mode:         OK {0}' -f $modeValue)
        } elseif ($viaAppServer) {
            $modeCell = '{0} (expected {1})' -f $modeValue, $expected
            Write-Output ('  Mode:         PROBLEM {0}, expected {1}: the App release does not give {2} this role' -f $modeValue, $expected, $name)
        } else {
            $modeCell = '{0} (expected {1})' -f $modeValue, $expected
            Write-Output ('  Mode:         PROBLEM {0}, expected {1}: the front changes the Host header' -f $modeValue, $expected)
        }
    }

    $verdict = Get-Verdict -Normal $normal -Direct $direct -Tcp $tcp -Certificate $certificate -ModeValue $modeValue -DirectModeValue $directModeValue -Expected $expected -ModePath $ModePath -ViaAppServer $viaAppServer -FrontAddress $frontAddress
    Write-Output ('  Verdict:      {0}: {1}' -f $verdict.Verdict, $verdict.Reason)

    $directCell = '-'
    if ($AppServer -and $curl) {
        $directCell = Format-HttpShort -Result $direct
    }
    $normalCell = 'not checked'
    if ($curl) {
        $normalCell = Format-HttpShort -Result $normal
    }
    $rows.Add([pscustomobject]@{
        Name    = [string]$name
        Normal  = [string]$normalCell
        Direct  = [string]$directCell
        Mode    = [string]$modeCell
        Verdict = [string]$verdict.Verdict
    })
}

$headers = @('Name', 'Normal path', 'Direct', 'Mode', 'Verdict')
$widths = @(4, 11, 6, 4, 7)
foreach ($row in $rows) {
    $cells = @($row.Name, $row.Normal, $row.Direct, $row.Mode, $row.Verdict)
    for ($i = 0; $i -lt $cells.Count; $i++) {
        if ($cells[$i].Length -gt $widths[$i]) {
            $widths[$i] = $cells[$i].Length
        }
    }
}
Write-Output ''
Write-Output 'Summary'
Write-Output (Format-TableRow -Cells $headers -Widths $widths)
foreach ($row in $rows) {
    Write-Output (Format-TableRow -Cells @($row.Name, $row.Normal, $row.Direct, $row.Mode, $row.Verdict) -Widths $widths)
}

$notOk = @($rows | Where-Object { $_.Verdict -ne 'OK' })
$frontFaults = @($rows | Where-Object { $_.Verdict -eq 'LOOP' -or $_.Verdict -eq 'FRONT problem' })
if ($frontFaults.Count -gt 0) {
    Write-Output ''
    if ($AppServer) {
        Write-Output ('Workaround until the front is repaired: in an administrator PowerShell, run this script with -AppServer {0} -AddHostsBypass. Remove the lines after the repair with -RemoveHostsBypass.' -f $AppServer)
    } else {
        Write-Output 'Workaround until the front is repaired: in an administrator PowerShell, run this script with -AppServer APP_SERVER_IP -AddHostsBypass. Remove the lines after the repair with -RemoveHostsBypass.'
    }
}
Write-Output ''
if ($notOk.Count -eq 0) {
    Write-Output ('Result: all {0} names OK' -f $rows.Count)
    exit 0
}
Write-Output ('Result: {0} of {1} names not OK' -f $notOk.Count, $rows.Count)
exit 4
