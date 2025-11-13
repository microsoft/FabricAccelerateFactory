#Define the source bim file path and the log path.

$RootDirectory = (split-path -parent $MyInvocation.MyCommand.Definition).replace('\Script',"\")
$TabularMetadataFilePath = $RootDirectory + "TabularMetadata\tabular_metadata.csv"

#Change your Source tabular model bim file path here.
$SourceTabularBimFilePath = $RootDirectory + "Bim\Source.bim"

$SourceTabularContent = Get-Content $SourceTabularBimFilePath -Raw  | ConvertFrom-Json

# Initialize an array to hold output objects
$outputData = @()

# Loop through each table
foreach ($table in $SourceTabularContent.model.tables) {
    $tableName = $table.name
    
    # Loop through each column in the table
    foreach ($column in $table.columns) {
        $columnName = $column.name
        $columnDataType = $column.dataType
        $sourceColumn = $column.sourceColumn
        $isHidden = $column.isHidden
        $expression = $column.expression

        # Loop through each partition in the table (if any)
        foreach ($partition in $table.partitions) {
            $partitionName = $partition.name
            $partitionExpression = $partition.source.expression[4]
            # Split the Hive viewName using the delimiter
            $splitString = $partitionExpression -split 'TrackName_Schema\{\[Name="'
            $valuePart = $splitString[1] -split '",Kind="'
            $HiveTableName = ($valuePart[0]).replace('vw','')

            # Create a custom object with the extracted data
            $row = [PSCustomObject]@{
                TableName          = $tableName
                SourceTableName    = $HiveTableName
                ColumnName         = $columnName
                ColumnDataType     = $columnDataType
                SourceColumn       = $sourceColumn
                PartitionName      = $partitionName
                PartitionExpression = $partitionExpression
                IsHidden           = $isHidden
                Expression         = $expression
            }

            # Add the row to the output array
            $outputData += $row
        }
    }
}

# Export the collected data to a CSV file
$outputData | Export-Csv -Path $TabularMetadataFilePath -NoTypeInformation -Encoding UTF8

#>