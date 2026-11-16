#requires -version 5.1
<#
================================================================
 L4D2 / Source 服务器 A2S 查询桥（PowerShell 版，免安装）
================================================================
 浏览器不能直接发 UDP，所以由这个脚本替网页查询服务器，
 再以 HTTP + JSON 返回给网页。

 启动：
   右键「使用 PowerShell 运行」，或双击同目录 start-bridge.bat
   命令行： powershell -NoProfile -ExecutionPolicy Bypass -File a2s-bridge.ps1

 参数：
   -Port 8765         监听端口
   -BindHost 127.0.0.1  监听地址（0.0.0.0 需管理员 + netsh 授权）

 接口：
   GET /ping
   GET /query?addr=1.2.3.4:27015
================================================================
#>

param(
    [int]$Port = 8765,
    [string]$BindHost = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$script:InfoReq = [byte[]](0xFF,0xFF,0xFF,0xFF) + [Text.Encoding]::ASCII.GetBytes("TSource Engine Query") + [byte[]](0x00)

# ------------------------------------------------------------------ 工具
function Get-CString {
    param([byte[]]$Buf, [int]$Start)
    $end = $Start
    while ($end -lt $Buf.Length -and $Buf[$end] -ne 0) { $end++ }
    $len = $end - $Start
    if ($len -lt 0) { $len = 0 }
    $s = [Text.Encoding]::UTF8.GetString($Buf, $Start, $len)
    $next = $end + 1
    return [pscustomobject]@{ Text = $s; Next = $next }
}

function Send-A2S {
    param($Udp, [System.Net.IPEndPoint]$Ep, [byte[]]$Payload)
    $Udp.Send($Payload, $Payload.Length, $Ep) | Out-Null
    $remote = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
    for ($i = 0; $i -lt 4; $i++) {
        $data = $Udp.Receive([ref]$remote)
        if ($data.Length -ge 5 -and $data[0] -eq 0xFF -and $data[1] -eq 0xFF -and $data[2] -eq 0xFF -and $data[3] -eq 0xFF) {
            return $data
        }
    }
    throw "无有效 A2S 响应"
}

function Get-A2SPlayers {
    param($Udp, [System.Net.IPEndPoint]$Ep, [int]$TimeoutMs)
    $list = @()
    try {
        $Udp.Client.ReceiveTimeout = $TimeoutMs
        $req = [byte[]](0xFF,0xFF,0xFF,0xFF,0x55,0xFF,0xFF,0xFF,0xFF)
        $resp = Send-A2S -Udp $Udp -Ep $Ep -Payload $req
        if ($resp[4] -eq 0x41) {
            $req = [byte[]](0xFF,0xFF,0xFF,0xFF,0x55) + [byte[]]@($resp[5],$resp[6],$resp[7],$resp[8])
            $resp = Send-A2S -Udp $Udp -Ep $Ep -Payload $req
        }
        if ($resp[4] -ne 0x44) { return $list }
        $count = [int]$resp[5]
        $p = 6
        for ($n = 0; $n -lt $count; $n++) {
            if ($p -ge $resp.Length) { break }
            $p++                                    # index
            $nm = Get-CString -Buf $resp -Start $p
            $p = $nm.Next
            if ($p + 8 -gt $resp.Length) { break }
            $score = [BitConverter]::ToInt32($resp, $p); $p += 4
            $dur   = [BitConverter]::ToSingle($resp, $p); $p += 4
            $list += [pscustomobject]@{ name = $nm.Text; score = $score; seconds = [int]$dur }
        }
    } catch {
        return @()
    }
    return $list
}

# ------------------------------------------------------------------ 查询
function Invoke-A2SInfo {
    param([string]$Addr, [double]$Timeout = 1.5)

    $idx = $Addr.LastIndexOf(":")
    if ($idx -lt 1) {
        return [pscustomobject]@{ ok = $false; error = "地址格式应为 host:port" }
    }
    $hostName = $Addr.Substring(0, $idx)
    $portStr  = $Addr.Substring($idx + 1)
    $portNum  = 0
    if (-not [int]::TryParse($portStr, [ref]$portNum)) {
        return [pscustomobject]@{ ok = $false; error = "端口不是数字" }
    }

    $ip = $null
    try {
        $cands = [System.Net.Dns]::GetHostAddresses($hostName)
        foreach ($c in $cands) {
            if ($c.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { $ip = $c; break }
        }
        if ($null -eq $ip) { throw "无 IPv4 地址" }
    } catch {
        return [pscustomobject]@{ ok = $false; error = ("域名解析失败: " + $_.Exception.Message); host = $hostName; port = $portNum }
    }

    $timeoutMs = [int]($Timeout * 1000)
    $udp = New-Object System.Net.Sockets.UdpClient
    $sw = [Diagnostics.Stopwatch]::StartNew()

    try {
        $ep = New-Object System.Net.IPEndPoint($ip, $portNum)
        $udp.Client.ReceiveTimeout = $timeoutMs

        $resp = Send-A2S -Udp $udp -Ep $ep -Payload $script:InfoReq

        # 现代服务器要求带挑战码再问一次
        if ($resp[4] -eq 0x41) {
            $req2 = $script:InfoReq + [byte[]]@($resp[5],$resp[6],$resp[7],$resp[8])
            $resp = Send-A2S -Udp $udp -Ep $ep -Payload $req2
        }

        if ($resp[4] -ne 0x49) {
            $sw.Stop()
            return [pscustomobject]@{ ok = $false; error = ("非 A2S_INFO 响应 (0x{0:X2})" -f $resp[4]); host = $hostName; port = $portNum; ip = $ip.ToString() }
        }

        $sw.Stop()
        $ping = [int]$sw.ElapsedMilliseconds

        $p = 5
        $protocol = [int]$resp[$p]; $p++

        $nameS = Get-CString -Buf $resp -Start $p; $p = $nameS.Next
        $mapS  = Get-CString -Buf $resp -Start $p; $p = $mapS.Next
        $foldS = Get-CString -Buf $resp -Start $p; $p = $foldS.Next
        $gameS = Get-CString -Buf $resp -Start $p; $p = $gameS.Next

        $appid = 0
        if ($p + 2 -le $resp.Length) { $appid = [BitConverter]::ToInt16($resp, $p) }
        $p += 2

        $players = [int]$resp[$p]; $p++
        $maxp    = [int]$resp[$p]; $p++
        $bots    = [int]$resp[$p]; $p++
        $stype   = [char]$resp[$p]; $p++
        $envc    = [char]$resp[$p]; $p++
        $vis     = [int]$resp[$p]; $p++
        $vacF    = [int]$resp[$p]; $p++

        $verS = Get-CString -Buf $resp -Start $p; $p = $verS.Next

        $players2 = @()
        try { $players2 = Get-A2SPlayers -Udp $udp -Ep $ep -TimeoutMs $timeoutMs } catch { }

        return [pscustomobject]@{
            ok          = $true
            name        = $nameS.Text
            map         = $mapS.Text
            folder      = $foldS.Text
            game        = $gameS.Text
            appid       = $appid
            players     = $players
            max_players = $maxp
            bots        = $bots
            server_type = $stype.ToString()
            env         = $envc.ToString()
            visibility  = $vis
            vac         = ($vacF -ne 0)
            version     = $verS.Text
            ping        = $ping
            host        = $hostName
            port        = $portNum
            ip          = $ip.ToString()
            playerlist  = @($players2)
        }

    } catch [System.Net.Sockets.SocketException] {
        $sw.Stop()
        $msg = "超时（UDP 无响应）"
        if ($_.Exception.SocketErrorCode -ne [System.Net.Sockets.SocketError]::TimedOut) {
            $msg = "网络错误: " + $_.Exception.SocketErrorCode
        }
        return [pscustomobject]@{ ok = $false; error = $msg; host = $hostName; port = $portNum; ip = $ip.ToString() }
    } catch {
        $sw.Stop()
        return [pscustomobject]@{ ok = $false; error = $_.Exception.Message; host = $hostName; port = $portNum; ip = $ip.ToString() }
    } finally {
        $udp.Close()
    }
}

# ------------------------------------------------------------------ HTTP
$prefix = "http://" + $BindHost + ":" + $Port + "/"
$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add($prefix)

try {
    $listener.Start()
} catch {
    Write-Host ""
    Write-Host "  [启动失败] 无法监听 $prefix" -ForegroundColor Red
    Write-Host "  " + $_.Exception.Message -ForegroundColor Red
    Write-Host "  端口可能被占用，换一个： powershell -File a2s-bridge.ps1 -Port 8766"
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor DarkGray
Write-Host "  L4D2 A2S 查询桥 已启动 (PowerShell)" -ForegroundColor Green
Write-Host "  监听: $prefix" -ForegroundColor Cyan
Write-Host "  自检: ${prefix}ping" -ForegroundColor Cyan
Write-Host "  现在打开 l4d2-status.html，保持本窗口不要关" -ForegroundColor Gray
Write-Host "  停止: Ctrl + C" -ForegroundColor DarkGray
Write-Host "==============================================================" -ForegroundColor DarkGray
Write-Host ""

$loop = $true
while ($loop) {
    $ctx = $null
    try {
        $ctx = $listener.GetContext()
    } catch {
        break
    }

    try {
        $req = $ctx.Request
        $res = $ctx.Response
        $res.Headers.Add("Access-Control-Allow-Origin", "*")
        $res.Headers.Add("Access-Control-Allow-Methods", "GET, OPTIONS")
        $res.Headers.Add("Access-Control-Allow-Headers", "*")
        $res.Headers.Add("Cache-Control", "no-store")

        $path = $req.Url.AbsolutePath
        $body = ""

        if ($req.HttpMethod -eq "OPTIONS") {
            $res.StatusCode = 204
            $res.OutputStream.Close()
            continue
        }

        if ($path -eq "/ping" -or $path -eq "/") {
            $obj = [pscustomobject]@{
                ok      = $true
                service = "l4d2-a2s-bridge-ps"
                version = "1.0"
                time    = [int][double]::Parse((Get-Date -UFormat %s))
            }
            $body = ($obj | ConvertTo-Json -Compress -Depth 5)
            $res.StatusCode = 200

        } elseif ($path -eq "/query") {
            $addr = $req.QueryString["addr"]
            if ([string]::IsNullOrWhiteSpace($addr)) {
                $res.StatusCode = 400
                $body = '{"ok":false,"error":"缺少 addr 参数"}'
            } else {
                $to = 1.5
                $toRaw = $req.QueryString["timeout"]
                if (-not [string]::IsNullOrWhiteSpace($toRaw)) {
                    $tmp = 0.0
                    if ([double]::TryParse($toRaw, [ref]$tmp)) {
                        if ($tmp -lt 0.3) { $tmp = 0.3 }
                        if ($tmp -gt 8) { $tmp = 8 }
                        $to = $tmp
                    }
                }
                Write-Host ("  [" + (Get-Date -Format HH:mm:ss) + "] query " + $addr) -ForegroundColor DarkGray
                $result = Invoke-A2SInfo -Addr $addr -Timeout $to
                $body = ($result | ConvertTo-Json -Compress -Depth 6)
                $res.StatusCode = 200
                $state = "FAIL"
                if ($result.ok) { $state = "OK" }
                Write-Host ("           -> " + $state) -ForegroundColor DarkGray
            }
        } else {
            $res.StatusCode = 404
            $body = '{"ok":false,"error":"unknown path"}'
        }

        $bytes = [Text.Encoding]::UTF8.GetBytes($body)
        $res.ContentType = "application/json; charset=utf-8"
        $res.ContentLength64 = $bytes.Length
        $res.OutputStream.Write($bytes, 0, $bytes.Length)
        $res.OutputStream.Close()

    } catch {
        try { $ctx.Response.StatusCode = 500; $ctx.Response.OutputStream.Close() } catch { }
    }
}

$listener.Stop()
$listener.Close()
Write-Host ""
Write-Host "已停止。" -ForegroundColor DarkGray
