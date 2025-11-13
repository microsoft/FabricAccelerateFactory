# Define the alias dictionary file path
$RootDirectory = (split-path -parent $MyInvocation.MyCommand.Definition).replace('\Script',"\")
$TabularMetadataFilePath = $RootDirectory + "TabularMetadata\tabular_metadata.csv"
$TableAliasDictionaryPath = $RootDirectory+"TabularMetadata\table_alias_dict.json"
$ColumnAliasDictionaryPath = $RootDirectory+"TabularMetadata\column_alias_dict.json"
$SourceTabularBimFilePath = $RootDirectory + "Bim\Source.bim"
$TargetTabularBimFilePath = $RootDirectory + "Bim\Target.bim"
$OutputBimFilePath = $RootDirectory + "Bim\Output.bim"

# Import the CSV file
$TabularMetadata = Import-Csv -Path $TabularMetadataFilePath


###################################### Table dictionary ###################################################

# Select distinct values for TableName, ColumnName, and SourceColumn
$distinctValues = $TabularMetadata | Select-Object -Property SourceTableName, TableName -Unique

# Order the distinct values by TableName
$orderedValues = $distinctValues | Sort-Object -Property TableName

# Initialize an ordered dictionary to store the concatenated values
$orderedDictionary = [ordered]@{}

# Iterate over the ordered values and concatenate TableName and SourceColumn
foreach ($row in $orderedValues) {
    if($row.SourceTableName -ne ""){
    $key = "$($row.SourceTableName)"
    $value = $row.TableName
    $orderedDictionary[$key] = $value
    }
}

# Convert the ordered dictionary to JSON format
$jsonOutput = $orderedDictionary | ConvertTo-Json -Depth 10

# Write the JSON output to a file
Set-Content -Path $TableAliasDictionaryPath -Value $jsonOutput -Force

###################################### Column dictionary ###################################################

# Select distinct values for TableName, ColumnName, and SourceColumn
$distinctValues = $TabularMetadata | Select-Object -Property TableName, ColumnName, SourceColumn -Unique

# Order the distinct values by TableName
$orderedValues = $distinctValues | Sort-Object -Property TableName

# Initialize an ordered dictionary to store the concatenated values
$orderedDictionary = [ordered]@{}

# Iterate over the ordered values and concatenate TableName and SourceColumn
foreach ($row in $orderedValues) {
    if($row.SourceColumn -ne ""){
    $key = "$($row.TableName)-$($row.SourceColumn)"
    $value = $row.ColumnName
    $orderedDictionary[$key] = $value
    }
}

# Convert the ordered dictionary to JSON format
$jsonOutput = $orderedDictionary | ConvertTo-Json -Depth 10


# Write the JSON output to a file
Set-Content -Path $ColumnAliasDictionaryPath -Value $jsonOutput -Force

############################################ Renaming Table ##############################################################################

# Define paths to the main text file, rename mappings JSON file, and the log output CSV file
$bimFilePath = $TargetTabularBimFilePath
$bimFilePath_renamed = $OutputBimFilePath
$lookupFilePath = $TableAliasDictionaryPath
$logCsvFilePath = $RootDirectory + "Logs\TableRenamelog.csv"


# Load the .bim file and lookup JSON file as PowerShell objects
$bimData = Get-Content -Path $bimFilePath -Raw | ConvertFrom-Json
$lookupData = Get-Content -Path $lookupFilePath -Raw | ConvertFrom-Json

# Initialize an array to store details of the replacements for logging
$replacementDetails = @()

# Iterate over each table in the .bim file
foreach ($table in $bimData.model.tables) {
    $oldName = $table.name
    
    # Check if the old name exists in the lookup data
    if ($lookupData.PSObject.Properties[$oldName]) {
        $newName = $lookupData.$oldName
        
        # Log the replacement details
        $replacementDetails += [pscustomobject]@{
            LineNumber    = $null     # Placeholder for line number if needed
            PreviousValue = $oldName
            NewValue      = $newName
        }
        
        # Update the table name in the .bim file
        $table.name = $newName
    }
    
}

# Convert the updated .bim data back to JSON format
$updatedBimJson = $bimData | ConvertTo-Json -Depth 10

# Save the updated .bim file
Set-Content -Path $bimFilePath_renamed -Value $updatedBimJson -Force

# Export the replacement details to a CSV file
$replacementDetails | Export-Csv -Path $logCsvFilePath -NoTypeInformation -Force

############################# Renaming Column ###########################################################################################

# Define paths to the main text file, rename mappings JSON file, and the log output CSV file
$bimFilePath = $OutputBimFilePath
$bimFilePath_renamed = $OutputBimFilePath
$lookupFilePath = $ColumnAliasDictionaryPath
$logCsvFilePath = $RootDirectory + "Logs\ColumnRenamelog.csv"


# Load the .bim file and lookup JSON file as PowerShell objects
$bimData = Get-Content -Path $bimFilePath -Raw | ConvertFrom-Json
$lookupData = Get-Content -Path $lookupFilePath -Raw | ConvertFrom-Json

# Initialize an array to store details of the replacements for logging
$replacementDetails = @()

# Iterate over each table in the .bim file
foreach ($table in $bimData.model.tables) {
    $tablename = $table.name
    foreach ($column in $table.columns) {
    $oldName = $tablename+'-'+ $column.name
    
    # Check if the old name exists in the lookup data
    if ($lookupData.PSObject.Properties[$oldName]) {
        $newName = $lookupData.$oldName
        
        # Log the replacement details
        $replacementDetails += [pscustomobject]@{
            LineNumber    = $null     # Placeholder for line number if needed
            PreviousValue = $oldName
            NewValue      = $newName
        }
        
        # Update the table name in the .bim file
        $column.name = $newName
    }
    }

    
}

# Convert the updated .bim data back to JSON format
$updatedBimJson = $bimData | ConvertTo-Json -Depth 10

# Save the updated .bim file
Set-Content -Path $bimFilePath_renamed -Value $updatedBimJson -Force

# Export the replacement details to a CSV file
$replacementDetails | Export-Csv -Path $logCsvFilePath -NoTypeInformation -Force

Write-Output "The column names in the .bim file have been updated. Details saved to $logCsvFilePath"

########################################### UPDATE HIDDEN TABLES and COLUMNS #######################################################################
# Define paths to the main text file, rename mappings JSON file, and the log output CSV file
$SourcebimFilePath = $SourceTabularBimFilePath
$TargetbimFilePath = $OutputBimFilePath
$ModifiedbimFilePath = $OutputBimFilePath
$logCsvFilePath = $RootDirectory + "Logs\HiddenColumnslog.csv"

# Load the .bim file and lookup JSON file as PowerShell objects
$SourcebimData = Get-Content -Path $SourcebimFilePath -Raw | ConvertFrom-Json
$TargetbimData = Get-Content -Path $TargetbimFilePath -Raw | ConvertFrom-Json

# Initialize an array to store details of the replacements for logging
$replacementDetails = @()

# Iterate over each table in the source .bim file
foreach ($Sourcetable in $SourcebimData.model.tables) {
    $SourcetableName = $Sourcetable.name
    $SourcetableIsHidden = $Sourcetable.isHidden

    # Find the matching table in the target .bim file
    foreach ($targettable in $TargetbimData.model.tables) {
        $targettableName = $targettable.name
        if ($targettableName -eq $SourcetableName) {
            
            # Update or add 'isHidden' if not null
            if ($SourcetableIsHidden -ne $null) {
                if (-not $targettable.PSObject.Properties["isHidden"]) {
                    # Add the property if it doesn't exist
                    $targettable | Add-Member -MemberType NoteProperty -Name "isHidden" -Value $SourcetableIsHidden
                } else {
                    # Update the existing property
                    $targettable.isHidden = $SourcetableIsHidden
                }
            }

            # Iterate over each column in the source table
            foreach ($SourceColumn in $Sourcetable.columns) {
                $SourceColumnName = $SourceColumn.name
                $SourceColumnIsHidden = $SourceColumn.isHidden

                # Find the matching column in the target table
                foreach ($TargetColumn in $targettable.columns) {
                    $TargetColumnName = $TargetColumn.name
                    if ($TargetColumnName -eq $SourceColumnName) {
                        # Update or add 'isHidden' if not null
                        if ($SourceColumnIsHidden -ne $null) {
                            if (-not $TargetColumn.PSObject.Properties["isHidden"]) {
                                # Add the property if it doesn't exist
                                $TargetColumn | Add-Member -MemberType NoteProperty -Name "isHidden" -Value $SourceColumnIsHidden
                            } else {
                                # Update the existing property
                                $TargetColumn.isHidden = $SourceColumnIsHidden
                            }
                        }
                    }
                }
            }

            # Log the replacement details
            $replacementDetails += [pscustomobject]@{
                TargetTable    = $targettableName
                IsHidden       = if ($SourcetableIsHidden -ne $null) { $SourcetableIsHidden } else { "Not Updated" }
            }
        }
    }
}

# Convert the updated .bim data back to JSON format
$updatedBimJson = $TargetbimData | ConvertTo-Json -Depth 10

# Save the updated .bim file
Set-Content -Path $ModifiedbimFilePath -Value $updatedBimJson -Force

# Export the replacement details to a CSV file
$replacementDetails | Export-Csv -Path $logCsvFilePath -NoTypeInformation -Force

Write-Output "The isHidden properties in the .bim file have been updated. Details saved to $logCsvFilePath"

###############################################Create Hierarchy and Measures ###########################################################

# Define paths to the main text file, rename mappings JSON file, and the log output CSV file
$SourcebimFilePath = $SourceTabularBimFilePath
$TargetbimFilePath = $OutputBimFilePath
$ModifiedbimFilePath = $OutputBimFilePath
$logCsvFilePath = $RootDirectory + "Logs\HierachyandMeasureslog.csv"

# Load the .bim file and lookup JSON file as PowerShell objects
$SourcebimData = Get-Content -Path $SourcebimFilePath -Raw | ConvertFrom-Json
$TargetbimData = Get-Content -Path $TargetbimFilePath -Raw | ConvertFrom-Json

# Initialize an array to store details of the replacements for logging
$replacementDetails = @()

# Iterate over each table in the source .bim file
foreach ($Sourcetable in $SourcebimData.model.tables) {
    $SourcetableName = $Sourcetable.name
    $SourcetableHierarchies = $Sourcetable.hierarchies
    $SourcetableMeasures = $Sourcetable.measures

    # Find the matching table in the target .bim file
    foreach ($targettable in $TargetbimData.model.tables) {
        $targettableName = $targettable.name
        if ($targettableName -eq $SourcetableName) {
            
            # Update or add 'hierarchies' if not null
            if ($SourcetableHierarchies -ne $null) {
                if (-not $targettable.PSObject.Properties["hierarchies"]) {
                    # Add the property if it doesn't exist
                    $targettable | Add-Member -MemberType NoteProperty -Name "hierarchies" -Value $SourcetableHierarchies
                } else {
                    # Update the existing property
                    $targettable.hierarchies = $SourcetableHierarchies
                }
            }

            # Update or add 'measures' if not null
            if ($SourcetableMeasures -ne $null) {
                if (-not $targettable.PSObject.Properties["measures"]) {
                    # Add the property if it doesn't exist
                    $targettable | Add-Member -MemberType NoteProperty -Name "measures" -Value $SourcetableMeasures
                } else {
                    # Update the existing property
                    $targettable.measures = $SourcetableMeasures
                }
            }

            # Log the replacement details
            $replacementDetails += [pscustomobject]@{
                TargetTable    = $targettableName
                Hierarchies    = if ($SourcetableHierarchies -ne $null) { ($SourcetableHierarchies | ConvertTo-Json -Depth 10 -Compress) } else { "Not Updated" }
                Measures       = if ($SourcetableMeasures -ne $null) { ($SourcetableMeasures | ConvertTo-Json -Depth 10 -Compress) } else { "Not Updated" }
            }
        }
    }
}

# Convert the updated .bim data back to JSON format
$updatedBimJson = $TargetbimData | ConvertTo-Json -Depth 10

# Save the updated .bim file
Set-Content -Path $ModifiedbimFilePath -Value $updatedBimJson -Force

# Export the replacement details to a CSV file
$replacementDetails | Export-Csv -Path $logCsvFilePath -NoTypeInformation -Force

Write-Output "The hierarchies and measures in the .bim file have been updated. Details saved to $logCsvFilePath"

############################ Copy Relationships and Roles ##############################################

# Define paths to the main text file, rename mappings JSON file, and the log output CSV file
$SourcebimFilePath = $SourceTabularBimFilePath
$TargetbimFilePath = $OutputBimFilePath
$ModifiedbimFilePath = $OutputBimFilePath
$logCsvFilePath = $RootDirectory + "Logs\RolesAndRelationshipsLog.csv"

# Load the .bim file and lookup JSON file as PowerShell objects
$SourcebimData = Get-Content -Path $SourcebimFilePath -Raw | ConvertFrom-Json
$TargetbimData = Get-Content -Path $TargetbimFilePath -Raw | ConvertFrom-Json

# Initialize an array to store details of the replacements for logging
$replacementDetails = @()

# Copy roles from source to target
if ($SourcebimData.model.roles -ne $null) {
    if (-not $TargetbimData.model.PSObject.Properties["roles"]) {
        # Add the property if it doesn't exist
        $TargetbimData.model | Add-Member -MemberType NoteProperty -Name "roles" -Value $SourcebimData.model.roles
    } else {
        # Update the existing property
        $TargetbimData.model.roles = $SourcebimData.model.roles
    }
    $replacementDetails += [pscustomobject]@{
        Property = "roles"
        Status = "Copied"
    }
} else {
    $replacementDetails += [pscustomobject]@{
        Property = "roles"
        Status = "Not Updated"
    }
}

# Copy relationships from source to target
if ($SourcebimData.model.relationships -ne $null) {
    if (-not $TargetbimData.model.PSObject.Properties["relationships"]) {
        # Add the property if it doesn't exist
        $TargetbimData.model | Add-Member -MemberType NoteProperty -Name "relationships" -Value $SourcebimData.model.relationships
    } else {
        # Update the existing property
        $TargetbimData.model.relationships = $SourcebimData.model.relationships
    }
    $replacementDetails += [pscustomobject]@{
        Property = "relationships"
        Status = "Copied"
    }
} else {
    $replacementDetails += [pscustomobject]@{
        Property = "relationships"
        Status = "Not Updated"
    }
}

# Convert the updated .bim data back to JSON format
$updatedBimJson = $TargetbimData | ConvertTo-Json -Depth 10

# Save the updated .bim file
Set-Content -Path $ModifiedbimFilePath -Value $updatedBimJson -Force

# Export the replacement details to a CSV file
$replacementDetails | Export-Csv -Path $logCsvFilePath -NoTypeInformation -Force

Write-Output "The roles and relationships in the .bim file have been updated. Details saved to $logCsvFilePath"

#########################################################################################################################################################