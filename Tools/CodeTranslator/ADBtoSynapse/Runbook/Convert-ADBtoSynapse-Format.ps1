Param
(
    # Get webhook data
    #[Parameter(Mandatory=$False,Position=1)]
    #[object] $WebhookData
    [string] $trackName,
    [string] $runId,
    [string] $addORedit
)

# Get all parameters from body (passed from Data Factory Web Activity)
#$Parameters = (ConvertFrom-Json -InputObject $WebhookData.RequestBody)

# Read and store the callBackUri which
# is only provided by the Webhook activity
<#
If ($Parameters.callBackUri)
{
    $callBackUri = $Parameters.callBackUri
}
#>

# Get the track name
#$trackName = $Parameters.trackName

# Variables
$adlsAccountName = <<ADLS Gen2 storage account>>
$adlsContainerName = "test"
$adlsPath = 'notebooks'
$projectName = <<Project Name>>
$configFilePath = "syntax_config.json"
$folderConfigFilePath = "folder_config.json"
#$configFilePath = Join-Path -Path $adlsPath -ChildPath "configfiletest.json"
$destinationADLSPath = 'notebooks/raw/'+$trackName


# Login to Azure
Connect-AzAccount -Identity

#Create Storage Context
$storageContext = New-AzStorageContext -StorageAccountName $adlsAccountName -UseConnectedAccount
$Blob_config = Get-AzStorageBlob -Context $StorageContext -Container $adlsContainerName -Blob $configFilePath
$Folder_config = Get-AzStorageBlob -Context $StorageContext -Container $adlsContainerName -Blob $folderConfigFilePath

$json_data = $Blob_config.ICloudBlob.DownloadText() | ConvertFrom-Json
$allsyntaxes = @()
$json_data.psobject.Properties | ForEach {
    $allsyntaxes += @{ADBSyntax=$_.Name;SynapseSyntax=$_.Value}
}
$allsyntaxes_output = $allsyntaxes | ForEach-Object { new-object PSObject -Property $_} 

$folder_json_data = $Folder_config.ICloudBlob.DownloadText() | ConvertFrom-Json
$folderallsyntaxes = @()
$folder_json_data.psobject.Properties | ForEach {
    $folderallsyntaxes += @{ADBFolder=$_.Name;SynapseFolder=$_.Value}
}
$folderallsyntaxes_output = $folderallsyntaxes | ForEach-Object { new-object PSObject -Property $_} 


#Logic to replace required content of ipynb files present in adls folder 
try 
{
    Write-Output 'ADLS Folder Path',$destinationADLSPath
    $ChildItems = Get-AzDataLakeGen2ChildItem `
                    -Context $StorageContext `
                    -FileSystem $adlsContainerName `
                    -OutputUserPrincipalName `
                    -Path $destinationADLSPath `
                    -recurse

    Foreach ($ChildItem in $ChildItems)
    {
        if ($ChildItem.name -Match 'ipynb')
        {
            $ipynb_file = "/" + $ChildItem.name
            Write-Output 'ipynb_file',$ipynb_file

            $Blob = Get-AzStorageBlob -Context $StorageContext -Container $adlsContainerName -Blob $ipynb_file
            Write-Output 'Blob Path', $Blob
            $txt = $Blob.ICloudBlob.DownloadText()

            Foreach($syntax in $allsyntaxes_output)
            {
                $txt = $txt -ireplace [regex]::Escape($syntax.ADBSyntax.Replace("""","\""")), [regex]::Escape($syntax.SynapseSyntax).replace("\.",".")                
            }

            $localPath = Join-Path -Path $env:Temp -ChildPath $ipynb_file
            if (-not (Test-Path -Path $localPath)) {
                New-Item -ItemType File -Path $localPath -Force
            }

            Foreach($folders in $folderallsyntaxes_output)
            {
                $ipynb_file = $ipynb_file -ireplace [regex]::Escape($folders.ADBFolder.Replace("""","\""")), [regex]::Escape($folders.SynapseFolder).replace("\.",".")
            }

            Write-Output 'changed ipynb path is ', $ipynb_file

            New-AzDataLakeGen2Item -Context $StorageContext -FileSystem $adlsContainerName -Path $ipynb_file.replace("notebooks/","notebooks_converted/") -Source $localPath -Force
            $Blob_Converted = Get-AzStorageBlob -Context $StorageContext -Container $adlsContainerName -Blob $ipynb_file.replace("notebooks/","notebooks_converted/")            
            Write-Output 'Blob Upload Path', $Blob_Converted
            $Blob_Converted.ICloudBlob.UploadText($txt)     
        }    
    }
}
catch 
{
    Write-Output "Entered Final Cache"
}