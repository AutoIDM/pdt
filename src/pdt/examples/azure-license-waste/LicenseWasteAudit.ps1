# Finds Microsoft 365 licenses the tenant pays for that nobody uses, and writes
# LicenseWaste-<date>.xlsx to PDT_OUTPUT_DIR: a Summary sheet, then one sheet
# per finding listing what is behind it.
#
# An account's paid licenses are waste when the account is disabled, has not
# signed in for LICENSE_WASTE_INACTIVE_DAYS days (90 when unset), or was
# created that long ago and never signed in. Seats bought but assigned to
# nobody are waste too.
#
# Monthly prices come from license-prices.csv. Price 0 marks a free license,
# which never counts. A license missing from the file still counts, but its
# cost is left blank and it is listed on an Unpriced Licenses sheet, so it can
# be added to the file.

#Requires -Version 7.0
#Requires -Modules Microsoft.Graph.Authentication, Microsoft.Graph.Users, Microsoft.Graph.Identity.DirectoryManagement, ImportExcel

$inactiveDays = if ($env:LICENSE_WASTE_INACTIVE_DAYS) { [int]$env:LICENSE_WASTE_INACTIVE_DAYS } else { 90 }
$cutoff = (Get-Date).AddDays(-$inactiveDays)
$prices = @{}
Import-Csv (Join-Path $PSScriptRoot 'license-prices.csv') | ForEach-Object { $prices[$_.SkuPartNumber] = $_ }

$secret = ConvertTo-SecureString $env:PDT_AZURE_CLIENT_SECRET -AsPlainText -Force
Connect-MgGraph -TenantId $env:PDT_AZURE_TENANT_ID -NoWelcome `
    -ClientSecretCredential ([pscredential]::new($env:PDT_AZURE_CLIENT_ID, $secret))

# SkuId -> the license's name, monthly price ($null when unknown), and seat counts.
$licenses = @{}
foreach ($sku in Get-MgSubscribedSku -All) {
    $price = $prices[$sku.SkuPartNumber]
    $licenses[$sku.SkuId] = [pscustomobject]@{
        Name  = if ($price) { $price.Name } else { $sku.SkuPartNumber }
        Price = if ($price) { [decimal]$price.MonthlyPrice } else { $null }
        Sku   = $sku
    }
}

# One entry per wasted license with no price: the finding it falls under, and how many.
$unpricedHits = [System.Collections.Generic.List[object]]::new()

$users = Get-MgUser -All -Property DisplayName, UserPrincipalName, Department, AccountEnabled,
    CreatedDateTime, AssignedLicenses, SignInActivity
$accounts = foreach ($user in $users) {
    $paid = @($user.AssignedLicenses | ForEach-Object { $licenses[$_.SkuId] } | Where-Object Price -NE 0)
    if (-not $paid) { continue }
    $lastSignIn = @($user.SignInActivity.LastSignInDateTime, $user.SignInActivity.LastNonInteractiveSignInDateTime) |
        Sort-Object -Descending | Select-Object -First 1
    $finding = if (-not $user.AccountEnabled) {
        'Disabled'
    } elseif (-not $lastSignIn -and $user.CreatedDateTime -lt $cutoff) {
        'Never Signed In'
    } elseif ($lastSignIn -and $lastSignIn -lt $cutoff) {
        "Inactive $inactiveDays+ Days"
    }
    if (-not $finding) { continue }
    $unpriced = @($paid | Where-Object { $null -eq $_.Price })
    foreach ($license in $unpriced) {
        $unpricedHits.Add([pscustomobject]@{ Finding = $finding; License = $license.Name; Count = 1 })
    }
    [pscustomobject]@{
        Finding           = $finding
        DisplayName       = $user.DisplayName
        UserPrincipalName = $user.UserPrincipalName
        Department        = $user.Department
        Created           = $user.CreatedDateTime
        LastSignIn        = $lastSignIn
        Licenses          = $paid.Name -join ', '
        LicenseCount      = $paid.Count
        MonthlyCost       = if (-not $unpriced) { ($paid | Measure-Object Price -Sum).Sum }
    }
}

$seats = foreach ($license in $licenses.Values) {
    $unassigned = $license.Sku.PrepaidUnits.Enabled - $license.Sku.ConsumedUnits
    if ($unassigned -gt 0 -and $license.Price -ne 0 -and $license.Sku.AppliesTo -eq 'User') {
        if ($null -eq $license.Price) {
            $unpricedHits.Add([pscustomobject]@{ Finding = 'Unassigned Seats'; License = $license.Name; Count = $unassigned })
        }
        [pscustomobject]@{
            License      = $license.Name
            Purchased    = $license.Sku.PrepaidUnits.Enabled
            Assigned     = $license.Sku.ConsumedUnits
            LicenseCount = $unassigned
            MonthlyPrice = $license.Price
            MonthlyCost  = if ($null -ne $license.Price) { $unassigned * $license.Price }
        }
    }
}

# Sheet name -> its rows. Each Summary row totals one of these sheets.
$sheets = [ordered]@{}
foreach ($finding in 'Disabled', 'Never Signed In', "Inactive $inactiveDays+ Days") {
    $sheets[$finding] = @($accounts | Where-Object Finding -EQ $finding | Sort-Object DisplayName |
        Select-Object -ExcludeProperty Finding)
}
$sheets['Unassigned Seats'] = @($seats | Sort-Object MonthlyCost -Descending)

$summary = foreach ($name in $sheets.Keys) {
    $cost = [decimal]($sheets[$name] | Measure-Object MonthlyCost -Sum).Sum
    [pscustomobject]@{
        Finding     = $name
        Accounts    = if ($name -ne 'Unassigned Seats') { $sheets[$name].Count }
        Licenses    = [int]($sheets[$name] | Measure-Object LicenseCount -Sum).Sum
        MonthlyCost = $cost
        AnnualCost  = $cost * 12
        Unpriced    = [int]($unpricedHits | Where-Object Finding -EQ $name | Measure-Object Count -Sum).Sum
    }
}

$unpricedLicenses = @($unpricedHits | Group-Object License | ForEach-Object {
    [pscustomobject]@{ License = $_.Name; LicenseCount = [int]($_.Group | Measure-Object Count -Sum).Sum }
} | Sort-Object LicenseCount -Descending)

$path = Join-Path ($env:PDT_OUTPUT_DIR ?? $PSScriptRoot) "LicenseWaste-$(Get-Date -Format yyyy-MM-dd).xlsx"
Remove-Item $path -ErrorAction Ignore

# Sheet name -> the line under its title. Every sheet holds one table under
# that title; the styling pass below gives them their look.
$about = [ordered]@{
    'Summary'                       = "{0}  ·  paid licenses on disabled accounts, accounts idle $inactiveDays+ days, and seats nobody holds" -f
        (Get-Date -Format 'MMMM d, yyyy')
    'Disabled'                      = 'Accounts that are turned off but still hold paid licenses'
    'Never Signed In'               = "Accounts created more than $inactiveDays days ago that have never signed in"
    "Inactive $inactiveDays+ Days"  = "Accounts with no sign-in in the last $inactiveDays days"
    'Unassigned Seats'              = 'Paid seats the tenant bought that nobody holds'
    'Unpriced Licenses'             = 'Wasted licenses with no price in license-prices.csv; add them there to count their cost'
}
$sheets['Unpriced Licenses'] = $unpricedLicenses
$summary | Export-Excel $path -WorksheetName 'Summary' -TableName 'Summary' -TableStyle None -StartRow 4
foreach ($name in $sheets.Keys) {
    if ($sheets[$name]) {
        $sheets[$name] | Export-Excel $path -WorksheetName $name -TableName ($name -replace '\W') -TableStyle None -StartRow 4
    }
}

# AutoIDM brand colors, from autoidm.com.
$blue  = [System.Drawing.ColorTranslator]::FromHtml('#007DD9')
$ink   = [System.Drawing.ColorTranslator]::FromHtml('#080C0F')
$muted = [System.Drawing.ColorTranslator]::FromHtml('#5B646F')
$band  = [System.Drawing.ColorTranslator]::FromHtml('#EDF2F8')
$line  = [System.Drawing.ColorTranslator]::FromHtml('#DFE1E4')

$excel = Open-ExcelPackage $path
$summaryTable = $excel.Workbook.Worksheets['Summary'].Tables[0]
$summaryTable.ShowTotal = $true
$summaryTable.Columns[0].TotalsRowLabel = 'Total'
foreach ($column in 'Accounts', 'Licenses', 'MonthlyCost', 'AnnualCost', 'Unpriced') {
    $summaryTable.Columns[$column].TotalsRowFunction = 'Sum'
}
$excel.Workbook.Worksheets['Summary'].TabColor = $blue

foreach ($sheet in $excel.Workbook.Worksheets) {
    $table = $sheet.Tables[0]
    $top, $bottom = $table.Address.Start.Row, $table.Address.End.Row
    $width = $table.Address.End.Column
    $sheet.View.ShowGridLines = $false
    $sheet.View.FreezePanes(($top + 1), 1)
    $sheet.PrinterSettings.Orientation = 'Landscape'
    $sheet.PrinterSettings.FitToPage = $true
    $sheet.PrinterSettings.FitToWidth = 1
    $sheet.PrinterSettings.FitToHeight = 0

    $sheet.Cells[1, 1].Value = if ($sheet.Name -eq 'Summary') { 'Microsoft 365 License Waste' } else { $sheet.Name }
    $sheet.Cells[1, 1].Style.Font.Size = 18
    $sheet.Cells[1, 1].Style.Font.Bold = $true
    $sheet.Cells[1, 1].Style.Font.Color.SetColor($blue)
    $sheet.Cells[2, 1].Value = $about[$sheet.Name]
    $sheet.Cells[2, 1].Style.Font.Color.SetColor($muted)
    $sheet.Row(1).Height = 28

    $all = $sheet.Cells[$top, 1, $bottom, $width]
    $all.Style.Font.Color.SetColor($ink)
    $all.Style.VerticalAlignment = 'Center'
    $all.Style.Border.Bottom.Style = 'Thin'
    $all.Style.Border.Bottom.Color.SetColor($line)
    foreach ($row in $top..$bottom) { $sheet.Row($row).Height = 20 }
    for ($row = $top + 2; $row -le $bottom - $table.ShowTotal; $row += 2) {
        $sheet.Cells[$row, 1, $row, $width].Style.Fill.PatternType = 'Solid'
        $sheet.Cells[$row, 1, $row, $width].Style.Fill.BackgroundColor.SetColor($band)
    }

    $header = $sheet.Cells[$top, 1, $top, $width]
    $header.Style.Font.Bold = $true
    $header.Style.Font.Color.SetColor([System.Drawing.Color]::White)
    $header.Style.Fill.PatternType = 'Solid'
    $header.Style.Fill.BackgroundColor.SetColor($blue)
    $sheet.Row($top).Height = 24

    if ($table.ShowTotal) {
        $totalRow = $sheet.Cells[$bottom, 1, $bottom, $width]
        $totalRow.Style.Font.Bold = $true
        $totalRow.Style.Border.Top.Style = 'Medium'
        $totalRow.Style.Border.Top.Color.SetColor($blue)
        $totalRow.Style.Border.Bottom.Style = 'None'
    }

    # Format each column by what its header says it holds, then give the header spaces.
    for ($i = 0; $i -lt $width; $i++) {
        $column = $table.Columns[$i]
        $cells = $sheet.Cells[$top, ($i + 1), $bottom, ($i + 1)]
        $format, $align = switch -Regex ($column.Name) {
            'Cost$|Price$'   { '$#,##0.00', 'Right'; break }
            'Created|SignIn' { 'yyyy-mm-dd', 'Center'; break }
            '^(Accounts|Licenses|Purchased|Assigned|Unpriced)$|Count' { '#,##0', 'Right'; break }
        }
        # Account sheets have a Licenses column of names, not a count.
        if ($format -and $sheet.Cells[($top + 1), ($i + 1)].Value -isnot [string]) {
            $cells.Style.Numberformat.Format = $format
            $cells.Style.HorizontalAlignment = $align
        }
        $column.Name = $column.Name -creplace '(?<=[a-z])(?=[A-Z])', ' '
    }

    # -AutoSize needs a font library Linux images lack, so size columns from their text.
    # Summary totals are formulas with no text until Excel calculates them, so leave room.
    foreach ($col in 1..$width) {
        $longest = $sheet.Cells[$top, $col, $bottom, $col] |
            ForEach-Object { $_.Text.Length } | Measure-Object -Maximum
        $sheet.Column($col).Width = [math]::Min(60, [math]::Max(14, 5 + $longest.Maximum))
    }
}

# Setting a width unhides a column, so hide the Unpriced column only once sizing is done.
$excel.Workbook.Worksheets['Summary'].Column($summaryTable.Columns['Unpriced'].Position + 1).Hidden = -not $unpricedLicenses
Close-ExcelPackage $excel

$total = $summary | Measure-Object Licenses, MonthlyCost -Sum
Write-Output ("Found {0} wasted licenses costing {1:N2} a month. Wrote {2}." -f
    $total[0].Sum, $total[1].Sum, (Split-Path $path -Leaf))
if ($unpricedLicenses) {
    Write-Output ("{0} of them have no price in license-prices.csv, so that total leaves them out: {1}." -f
        ($unpricedLicenses | Measure-Object LicenseCount -Sum).Sum, ($unpricedLicenses.License -join ', '))
}
