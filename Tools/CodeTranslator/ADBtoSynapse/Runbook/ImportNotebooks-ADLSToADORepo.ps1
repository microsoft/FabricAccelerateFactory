# Parameters
Param
(
    [string] $trackName,
    [string] $runId,
    [string] $addORedit
)

################################### Variables ###############################################################
$adlsAccountName = "<<ADLS Gen2 AccountName>>"
$KeyVaultName = "<<Keyvault Name>>"
$certSecretName = "<<SPN SNI Certficate Name>>"
$ADOTenantId = "<<TenantID>>"
$ADOSPNClientId = "<<ADO SPN ClientID>>"
$adlsContainerName = "<<ContainerName>>"
$adlsPath = 'notebooks_converted/raw/' + $trackName + '/'
$devOpsOrgUrl = "https://dev.azure.com/<<devOps Org/repo>>"
$projectName = "<<ADO Project Name>>"
$repoID = "<<Repo ID>>" 
$featureBranchName = $trackName + "_" + $runId
$baseBranchName = 'dev-migration'
$localPath = Join-Path -Path $env:Temp -ChildPath $trackName
$devOpsRootFolder= '/SynapseWorkspace'
$ADOResourceURL = "<<ADO Resource ID>>"
################################## Get the Certificate from Key Vault ###############################################################
# Login to Azure
Connect-AzAccount -Identity

# Get the certificate from Azure Key Vault
try {
    $cert = Get-AzKeyVaultSecret -VaultName $keyVaultName -Name $certSecretName -AsPlainText -ErrorAction Stop
    Write-Output "Connection to Key Vault succeeded" 
} catch {
    Write-Output "Connection to Key Vault failed, exiting the runbook" 
    Exit 0
}
##################################### Get all notebooks from ADLS #############################################################.
try {
    if (Test-Path $localPath) {
        Remove-Item $localPath -recurse -Force
        Write-Output "Removed $localPath"
    }
    $adlsContext = New-AzStorageContext -StorageAccountName $adlsAccountName -UseConnectedAccount
    $ChildItems = Get-AzDataLakeGen2ChildItem -Context $adlsContext -FileSystem $adlsContainerName -Path $adlsPath -recurse
    Foreach ($ChildItem in $ChildItems)
    {
        if ($ChildItem.name -Match 'ipynb'){
            $wspath = $ChildItem.path -replace [regex]::Escape($adlsPath), ""
            $localNotebook = Join-Path -Path $localPath -ChildPath $wspath
            $localNotebookPath = Split-Path -Path $localNotebook -Parent
            Write-Output "localNotebookPath $localNotebookPath"
            if (-not (Test-Path -Path $localNotebookPath)) {
                    New-Item -ItemType Directory -Path $localNotebookPath -Force
            }
            # Download file from ADLS
            Get-AzStorageBlobContent -Container $adlsContainerName -Blob $ChildItem.path -Destination $localNotebook -Context $adlsContext > $null
            Write-Output "Downloaded $($ChildItem.path) from ADLS"
        }
    } 
}catch {
    Write-Output "Failed, exiting the runbook"
    Exit 1
}
######################### Add the certificate to the store #################################################################
$certBytes = [System.Convert]::FromBase64String($cert)
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certBytes)
$storeName = [System.Security.Cryptography.X509Certificates.StoreName]::My
$storeLocation = [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
$store = [System.Security.Cryptography.X509Certificates.X509Store]::new($storeName, $storeLocation)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
$store.Add($certificate)

######################### Get the Access Token to connect to ADO ############################################################
Connect-AzAccount -ServicePrincipal -TenantId $ADOTenantId -ApplicationId $ADOSPNClientId -CertificateThumbprint $certificate.Thumbprint -SendCertificateChain

# Generate a token using the certificate, SPN, and the specific resource for Azure DevOps
$tokenResponse = Get-AzAccessToken -ResourceUrl $ADOResourceURL
$accessToken = $tokenResponse.Token

$headers = @{
    Authorization = "Bearer $accessToken"
}
######################### Function to get latest commit id of a branch #######################################################
Function Get-LatestCommitId{
    param (
        [string] $branchName
    )
    $branchDetails = Invoke-RestMethod -Uri "$devOpsOrgUrl/$projectName/_apis/git/repositories/$repoID/refs?filter=heads/$branchName&api-version=7.1" -Method Get -Headers $headers
    return $branchDetails.value[0].objectId
}
######################### Create feature branch based on pipeline run id ####################
# Create the new branch
$createBranchUrl = "$devOpsOrgUrl/$projectName/_apis/git/repositories/$repoID/refs?api-version=7.1" 
$createBranchBody = ConvertTo-Json @(
@{
    name = "refs/heads/$featureBranchName"
    newObjectId = Get-LatestCommitId -branchName $baseBranchName
    oldObjectId = "0000000000000000000000000000000000000000"
})
$createBranchresponse = Invoke-RestMethod -Uri $createBranchUrl -Headers $headers -Method Post -Body $createBranchBody -ContentType "application/json"
Write-Output "Created new branch $featureBranchName based on $baseBranchName"

############################# Commit all notebooks to new branch #####################################################
$localNotebooks = Get-ChildItem -Path $localPath -Recurse -File
Foreach ($localNotebook in $localNotebooks) {
        # Upload the file to Azure DevOps Repo
        $content = [System.IO.File]::ReadAllBytes($localNotebook.FullName)
        $commitMessage = "Adding $($localNotebook.name) from ADLS for $trackName"
        $wsPath = $localNotebook.FullName.Substring($localPath.Length + 1)
        $devOpsPath = Join-Path -Path $devOpsRootFolder -ChildPath $wsPath 
        $devOpsPath = $devOpsPath -replace '\\','/'
        $featureCommitId = Get-LatestCommitId -branchName $featureBranchName
        $jsonContent = @"
        {
            "refUpdates": [
                {
                    "name": "refs/heads/$featureBranchName",
                    "oldObjectId": "$featureCommitId"
                }
            ],
            "commits": [
                {
                    "comment": "$commitMessage",
                    "changes": [
                        {
                            "changeType": "$addOrEdit",
                            "item": {
                                "path": "$devOpsPath"
                            },
                            "newContent": {
                                "content": "$([System.Convert]::ToBase64String($content))",
                                "contentType": "base64encoded"
                            }
                        }
                    ]
                }
            ]
        }
"@
        $pushResponse = Invoke-RestMethod -Uri "$devOpsOrgUrl/$projectName/_apis/git/repositories/$repoID/pushes?api-version=7.1" -Method Post -Headers $headers -Body $jsonContent -ContentType "application/json"
        Write-Output "Uploaded $devOpsPath to $featureBranchName branch"
    }
################################# Create Pull request ############################################
$createPRbody = @{
    sourceRefName = "refs/heads/$featureBranchName"
    targetRefName = "refs/heads/$baseBranchName"
    title = "ADB - Synapse Migration for $featureBranchName"
    description = "New PR created for $trackName"
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri "$devOpsOrgUrl/$projectName/_apis/git/repositories/$repoID/pullrequests?api-version=7.1" -Method Post -Body $createPRbody -Headers $headers -ContentType "application/json"
$prNumber = $response.pullRequestId
Write-Output "Created Pull request $prNumber created for $trackName"
############################### Remove the certificate form store #################################
$store.Remove($certificate)
$store.Close()