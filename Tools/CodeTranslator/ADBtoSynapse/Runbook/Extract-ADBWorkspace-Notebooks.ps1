Param
(
    [string] $trackName,
    [string] $runId,
    [string] $addORedit
)

#Params
$StorageAccount = '<<storageaccountname>>'
#Prod Databricks
$databricksInstance = "<<ADBName>>.azuredatabricks.net"

$ADBresource = "<<ADB Resource ID>>"
$localPath = Join-Path -Path $env:Temp -ChildPath $trackName
$ContainerName = '<<containername>>'
$jsonADLSPath = "ADBFolders_config.json"
$jsonLocalPath = Join-Path -Path $localPath -ChildPath $jsonADLSPath
$destinationADLSPath = Join-Path -Path 'notebooks/raw/' -ChildPath $trackName

# Login to Azure using System Assigned Managed Identity
Connect-AzAccount -Identity
Write-Output "ADB Instance: " $databricksInstance
#Get token for ADB resource
$token = (Get-AzAccessToken -ResourceUrl $ADBresource).Token

#Create storage context
$storageContext = New-AzStorageContext -StorageAccountName $StorageAccount -UseConnectedAccount

# Function to export workspace items recursively
function Export-WorkspaceItems {
    param (
        [string]$workspacePath
    )

    $localRootPath = Join-Path -Path $localPath -ChildPath $workspacePath.TrimStart('/').Replace('/', '\')
    
    if (-not (Test-Path -Path $localRootPath)) {
                New-Item -ItemType Directory -Path $localRootPath -Force
    }
    # API URL to list workspace items
    $apiUrl = "https://$databricksInstance/api/2.0/workspace/list"
 
    # API Request Body
    $body = @{
        path = $workspacePath
    }
 
    # Make the API call to list workspace items
    $response = Invoke-RestMethod -Method get -Uri $apiUrl -Headers @{Authorization = "Bearer $token"} -Body $body

    foreach ($item in $response.objects) {
        $itemLocalPath = Join-Path -Path $localPath -ChildPath $item.path.TrimStart('/').Replace('/', '\')
        if ($item.object_type -eq "DIRECTORY") {
            # Create local directory
            if (-not (Test-Path -Path $itemLocalPath)) {
                New-Item -ItemType Directory -Path $itemLocalPath -Force
            }
 
            # Recurse into the directory
            Export-WorkspaceItems -workspacePath $item.path -localPath $localPath
        }
        elseif ($item.object_type -eq "NOTEBOOK") {
            # API URL to Export the notebook
            $notebookExportUrl = "https://$databricksInstance/api/2.0/workspace/export"
            $notebookBody = @{
                path = $item.path
                format = "JUPYTER"
            }
            
            try {
                if ($item.path -like "/Workspace/Shared/*"){
                    $nb = $item.path
                }
                else{
                    $nb = $item.path + "_" + $trackName
                }
                # Execute the API command                
                $notebookContent = Invoke-RestMethod -Method get -Uri $notebookExportUrl -Headers @{Authorization = "Bearer $token"} -Body $notebookBody
                $encodedContent = $notebookContent.content
                $decodedContent = [System.Convert]::FromBase64String($encodedContent)
                $itemLocalPathWithExtension = "$itemLocalPath.ipynb"

                Set-Content -Path $itemLocalPathWithExtension -Value $decodedContent -AsByteStream
                
                $destinationBlobName = Join-Path -Path $destinationADLSPath -ChildPath "$nb.ipynb"          
                Set-AzStorageBlobContent -Context $storageContext -Container $ContainerName -Blob $destinationBlobName -File $itemLocalPathWithExtension -Force > $null

                # If successful, store the file name in the list
                $Notebook = [PSCustomObject]@{NotebookName="$nb.ipynb"
                                                NotebookExtractStatus="Succeeded"
                                                ErrorMessage=$null
                                            }
                # Export the list of successful file name to a CSV file
                $Notebook | Export-Csv -Path $csvLocalPath -Append
                Write-Output "Exported notebook $destinationBlobName"
            } catch {
                Write-Output "Failed Exporting $nb.ipynb"
                $ErrorMessage = ($_ | ConvertFrom-Json).message
                # If there's an error, store the file name in the failed list
                $Notebook = [PSCustomObject]@{NotebookName="$nb.ipynb"
                                                NotebookExtractStatus="Failed"
                                                ErrorMessage=$ErrorMessage
                                            }
                $Notebook | Export-Csv -Path $csvLocalPath -Append
            }
        }
    }
}
 
$csvLocalPath = Join-Path -Path $localPath -ChildPath $trackName"_Notebooks.csv"

if (Test-Path $csvLocalPath) {
    Remove-Item $csvLocalPath -Force
}

if (-not (Test-Path -Path $localPath)) {
    New-Item -ItemType Directory -Path $localPath -Force
}

Get-AzStorageBlobContent -Container $ContainerName -Blob $jsonADLSPath -Destination $jsonLocalPath -Context $storageContext > $null

$foldersJson =  Get-Content -Path $jsonLocalPath -Raw
$foldersJson = $foldersJson | ConvertFrom-Json
$folders = $foldersJson.$trackName

# Start exporting from the root workspace folder
foreach($workspaceFolder in $folders){
    Write-Output "Exporting folder $workspaceFolder"
    Export-WorkspaceItems -workspacePath $workspaceFolder
}

# Export the CSV file
$destinationBlobName = Join-Path -Path $destinationADLSPath -ChildPath "Notebooks.csv"
Set-AzStorageBlobContent -Context $storageContext -Container $ContainerName -Blob $destinationBlobName -File $csvLocalPath -Force