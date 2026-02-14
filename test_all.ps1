$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-Counts {
    $data = Invoke-RestMethod http://127.0.0.1:5000/dashboard-data
    return [pscustomobject]@{
        S1 = [int]$data.server_load.S1
        S2 = [int]$data.server_load.S2
        S3 = [int]$data.server_load.S3
    }
}

function Send-Route {
    param(
        [Parameter(Mandatory = $true)][int]$Id,
        [Parameter(Mandatory = $true)][string]$Receiver,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $body = @{
        id       = $Id
        sender   = "tester"
        receiver = $Receiver
        content  = $Content
    } | ConvertTo-Json -Compress

    return Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/route -ContentType "application/json" -Body $body
}

$results = [ordered]@{
    "Round Robin" = "FAIL"
    "Failover" = "FAIL"
    "Restore" = "FAIL"
    "Edit Lock" = "FAIL"
    "Corruption Detection" = "FAIL"
}

try {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path

    Write-Utf8NoBom -Path (Join-Path $root "data/server1_messages.json") -Content "[]"
    Write-Utf8NoBom -Path (Join-Path $root "data/server2_messages.json") -Content "[]"
    Write-Utf8NoBom -Path (Join-Path $root "data/server3_messages.json") -Content "[]"

    Invoke-RestMethod -Method Post http://127.0.0.1:5000/restore/S1 | Out-Null
    Invoke-RestMethod -Method Post http://127.0.0.1:5000/restore/S2 | Out-Null
    Invoke-RestMethod -Method Post http://127.0.0.1:5000/restore/S3 | Out-Null

    $before = Get-Counts

    Send-Route -Id 1001 -Receiver "RR" -Content "rr-1" | Out-Null
    Send-Route -Id 1002 -Receiver "RR" -Content "rr-2" | Out-Null
    Send-Route -Id 1003 -Receiver "RR" -Content "rr-3" | Out-Null

    $afterRoundRobin = Get-Counts
    $d1 = $afterRoundRobin.S1 - $before.S1
    $d2 = $afterRoundRobin.S2 - $before.S2
    $d3 = $afterRoundRobin.S3 - $before.S3
    if ($d1 -eq 1 -and $d2 -eq 1 -and $d3 -eq 1) {
        $results["Round Robin"] = "PASS"
    }

    Invoke-RestMethod -Method Post http://127.0.0.1:5000/fail/S2 | Out-Null

    $beforeFail = Get-Counts
    Send-Route -Id 1004 -Receiver "FAIL" -Content "fail-1" | Out-Null
    Send-Route -Id 1005 -Receiver "FAIL" -Content "fail-2" | Out-Null
    Send-Route -Id 1006 -Receiver "FAIL" -Content "fail-3" | Out-Null
    $afterFail = Get-Counts

    $failTotal = ($afterFail.S1 + $afterFail.S2 + $afterFail.S3) - ($beforeFail.S1 + $beforeFail.S2 + $beforeFail.S3)
    $failS2Delta = $afterFail.S2 - $beforeFail.S2
    if ($failTotal -eq 3 -and $failS2Delta -eq 0) {
        $results["Failover"] = "PASS"
    }

    $restoreState = Invoke-RestMethod -Method Post http://127.0.0.1:5000/restore/S2
    $restoreRoute1 = Send-Route -Id 1007 -Receiver "RESTORE" -Content "restore-1"
    $restoreRoute2 = Send-Route -Id 1008 -Receiver "RESTORE" -Content "restore-2"

    if ($restoreState.S2 -eq "UP" -and $restoreRoute1.routed_to -and $restoreRoute2.routed_to) {
        $results["Restore"] = "PASS"
    }

    $directS1Body = @{ id = 2001; sender = "L1"; receiver = "LIFE"; content = "life-initial" } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/receive -ContentType "application/json" -Body $directS1Body | Out-Null

    $editBeforeBody = @{ content = "life-edited" } | ConvertTo-Json -Compress
    $editBefore = Invoke-RestMethod -Method Put -Uri http://127.0.0.1:5001/edit/2001 -ContentType "application/json" -Body $editBeforeBody

    Invoke-RestMethod http://127.0.0.1:5001/messages/LIFE | Out-Null

    $editLocked = $false
    try {
        Invoke-RestMethod -Method Put -Uri http://127.0.0.1:5001/edit/2001 -ContentType "application/json" -Body $editBeforeBody | Out-Null
    } catch {
        if ($_.ErrorDetails.Message -match "already read and locked") {
            $editLocked = $true
        }
    }

    if ($editBefore.message -eq "Updated successfully" -and $editLocked) {
        $results["Edit Lock"] = "PASS"
    }

    $directS2Body = @{ id = 3001; sender = "C1"; receiver = "CORR"; content = "safe" } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5002/receive -ContentType "application/json" -Body $directS2Body | Out-Null
    Invoke-RestMethod -Method Post http://127.0.0.1:5002/corrupt/3001 | Out-Null

    $corruptionCaught = $false
    try {
        Invoke-RestMethod http://127.0.0.1:5002/messages/CORR | Out-Null
    } catch {
        if ($_.ErrorDetails.Message -match "Message corrupted") {
            $corruptionCaught = $true
        }
    }

    if ($corruptionCaught) {
        $results["Corruption Detection"] = "PASS"
    }
}
catch {
    Write-Host "Test execution error: $($_.Exception.Message)"
}
finally {
    Write-Host ""
    Write-Host "===== TEST SUMMARY ====="
    Write-Host "Round Robin: $($results['Round Robin'])"
    Write-Host "Failover: $($results['Failover'])"
    Write-Host "Restore: $($results['Restore'])"
    Write-Host "Edit Lock: $($results['Edit Lock'])"
    Write-Host "Corruption Detection: $($results['Corruption Detection'])"
}
