-- ========== SCHEMAS ==========
-- Create schema [metadata] if not exists
IF NOT EXISTS (
    SELECT 1
    FROM sys.schemas
    WHERE name = 'metadata'
)
BEGIN
    EXEC('CREATE SCHEMA [metadata]')
END

-- Create schema [logging] if not exists
IF NOT EXISTS (
    SELECT 1
    FROM sys.schemas
    WHERE name = 'logging'
)
BEGIN
    EXEC('CREATE SCHEMA [logging]')
END

-- ========== TABLES ==========

-- [metadata].[tbl_Reconciliation_ConnectionDetails]
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'metadata'
      AND t.name = 'tbl_Reconciliation_ConnectionDetails'
)
BEGIN
    CREATE TABLE [metadata].[tbl_Reconciliation_ConnectionDetails](
		[ID] [int] IDENTITY(1,1) NOT NULL,
		[TrackName] [nvarchar](500) NULL,
		[DatabaseName] [nvarchar](500) NULL,
		[SourceServerName] [nvarchar](500) NULL,
		[SourceDatabaseName] [nvarchar](500) NULL,
		[TargetServerName] [nvarchar](500) NULL,
		[TargetDatabaseName] [nvarchar](500) NULL,
		[IsActive] [int] NOT NULL,
		[CreatedOn] [datetime] NULL
	) ON [PRIMARY]
	GO

	ALTER TABLE [metadata].[tbl_Reconciliation_ConnectionDetails] ADD  DEFAULT (getdate()) FOR [CreatedOn]
	GO
END

-- [logging].[tbl_Reconciliation_RecordCountsDiff_Current]
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'logging'
      AND t.name = 'tbl_Reconciliation_RecordCountsDiff_Current'
)
BEGIN
    CREATE TABLE [logging].[tbl_Reconciliation_RecordCountsDiff_Current](
		[TABLE_SCHEMA] [nvarchar](max) NULL,
		[TABLE_NAME] [nvarchar](max) NULL,
		[count_prod] [bigint] NULL,
		[count_fabric] [float] NULL,
		[executionDateTime] [datetime] NOT NULL,
		[trackName] [nvarchar](max) NOT NULL,
		[databaseName] [nvarchar](max) NOT NULL
	)
END

-- [logging].[tbl_Reconciliation_SchemaDiff_Current]
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'logging'
      AND t.name = 'tbl_Reconciliation_SchemaDiff_Current'
)
BEGIN
    CREATE TABLE [logging].[tbl_Reconciliation_SchemaDiff_Current](
		[TABLE_SCHEMA] [nvarchar](max) NULL,
		[TABLE_NAME] [nvarchar](max) NULL,
		[COLUMN_NAME] [nvarchar](max) NULL,
		[DATA_TYPE_Prod] [nvarchar](max) NULL,
		[IS_NULLABLE_Prod] [nvarchar](max) NULL,
		[DATA_TYPE_Fabric] [nvarchar](max) NULL,
		[IS_NULLABLE_Fabric] [nvarchar](max) NULL,
		[_merge] [nvarchar](max) NULL,
		[executionDateTime] [datetime] NOT NULL,
		[trackName] [nvarchar](max) NOT NULL,
		[databaseName] [nvarchar](max) NOT NULL
	) 
END

-- [logging].[tbl_Reconciliation_TableDiff_Current]
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'logging'
      AND t.name = 'tbl_Reconciliation_TableDiff_Current'
)
BEGIN
    CREATE TABLE [logging].[tbl_Reconciliation_TableDiff_Current](
		[TABLE_SCHEMA] [nvarchar](max) NULL,
		[TABLE_NAME] [nvarchar](max) NULL,
		[COMPARE_RESULT] [nvarchar](max) NULL,
		[databaseName] [nvarchar](max) NULL,
		[trackName] [nvarchar](max) NULL,
		[executionDateTime] [datetime] NULL
	)
END


-- ========== VIEW ==========

IF OBJECT_ID('logging.vw_Reconciliation_TableDiff', 'V') IS NOT NULL
	DROP VIEW [logging].[vw_Reconciliation_TableDiff]
GO

CREATE VIEW [logging].[vw_Reconciliation_TableDiff]
AS
SELECT *
	,CASE 
		WHEN TABLE_NAME LIKE 'vw%'
			THEN 'VIEW'
		ELSE 'TABLE'
		END AS ObjectType
FROM logging.tbl_Reconciliation_TableDiff_Current;

IF OBJECT_ID('logging.vw_Reconciliation_SchemaDiff', 'V') IS NOT NULL
	DROP VIEW [logging].[vw_Reconciliation_SchemaDiff]
GO
CREATE VIEW [logging].[vw_Reconciliation_SchemaDiff]
AS
SELECT TrackName
	,DatabaseName
	,TABLE_SCHEMA AS 'SchemaName'
	,TABLE_NAME AS 'TableName'
	,COLUMN_NAME AS ColumnName
	,DATA_TYPE_Prod AS 'DataType(Prod)'
	,DATA_TYPE_Fabric AS 'DataType(Fabric)'
	,IS_NULLABLE_Fabric
	,IS_NULLABLE_Prod
	,executionDateTime
	,CASE 
		WHEN ISNULL(DATA_TYPE_Prod, '') <> ISNULL(DATA_TYPE_Fabric, '')
			THEN 'Not Matched'
		ELSE 'Matched'
		END AS Result
	,CASE 
		WHEN TABLE_NAME LIKE 'vw%'
			THEN 'VIEW'
		ELSE 'TABLE'
		END AS [ObjectType]
FROM logging.tbl_Reconciliation_SchemaDiff_Current;


IF OBJECT_ID('logging.vw_Reconciliation_RecordCountDiff', 'V') IS NOT NULL
	DROP VIEW [logging].[vw_Reconciliation_RecordCountDiff]
GO

CREATE VIEW [logging].[vw_Reconciliation_RecordCountDiff]
AS
SELECT *
    ,CASE 
        WHEN isnull(count_prod, 0) <> isnull(count_fabric, 0)
            THEN 'Not Matched'
        ELSE 'Matched'
        END AS Result
    ,CASE 
        WHEN count_prod = 0
            AND count_fabric = 0
            THEN 0
        WHEN count_prod = 0
            AND count_fabric > 0
            THEN 100
        ELSE ROUND(((count_fabric - count_prod) * 100.0) / count_prod, 2)
        END AS VariancePercentage
    ,CASE 
        WHEN TABLE_NAME LIKE 'vw%'
            THEN 'VIEW'
        ELSE 'TABLE'
        END AS ObjectType
	,case 		
		when TABLE_NAME ='<<ObjectName>>' and databaseName='<<DATABASE>>'
		then 'Data is stale in Prod'
		else
		''
		end as ExceptionScenario,
		FORMAT(executionDateTime AT TIME ZONE 'UTC' AT TIME ZONE 'Pacific Standard Time','yyyy-MM-dd HH:00:00'
  ) AS FilterDate
FROM logging.tbl_Reconciliation_RecordCountsDiff_Current