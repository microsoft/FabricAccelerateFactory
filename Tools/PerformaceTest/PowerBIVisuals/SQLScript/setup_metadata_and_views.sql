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

-- metadata.tbl_ReportVisualDAX
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'metadata'
      AND t.name = 'tbl_ReportVisualDAX'
)
BEGIN
    CREATE TABLE [metadata].[tbl_ReportVisualDAX](
        [id] [int] IDENTITY(1,1) NOT NULL,
        [TrackName] [nvarchar](max) NOT NULL,
        [ReportName] [nvarchar](max) NOT NULL,
        [PageName] [nvarchar](max) NOT NULL,
        [visualId] [nvarchar](max) NULL,
        [visualTitle] [nvarchar](max) NULL,
        [visualType] [nvarchar](max) NULL,
        [DAXQuery] [nvarchar](max) NULL,
        [IsActive] [bit] NOT NULL
    );
END

-- metadata.tbl_LoadTestConnectionDetailsDAX
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'metadata'
      AND t.name = 'tbl_LoadTestConnectionDetailsDAX'
)
BEGIN
    CREATE TABLE [metadata].[tbl_LoadTestConnectionDetailsDAX]
    (
        [id] [int] IDENTITY(1,1) NOT NULL,
        [reportName] [nvarchar](255) NULL,
        [importModeWorkspaceName] [nvarchar](255) NULL,
        [importModeWorkspaceId] [nvarchar](255) NULL,
        [importModeDatasetName] [nvarchar](255) NULL,
        [importModeDatasetId] [nvarchar](255) NULL,
        [directLakeModeWorkspaceName] [nvarchar](255) NULL,
        [directLakeModeWorkspaceId] [nvarchar](255) NULL,
        [directLakeModeDatasetName] [nvarchar](255) NULL,
        [directLakeModeDatasetId] [nvarchar](255) NULL,
        [isActive] [bit] NULL
    );
END

-- logging.tbl_LoadTestDAX
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'logging'
      AND t.name = 'tbl_LoadTestDAX'
)
BEGIN
    CREATE TABLE [logging].[tbl_LoadTestDAX]
    (
        [RuntimeId] [nvarchar](max) NOT NULL,
        [Id] [nvarchar](max) NULL,
        [TrackName] [nvarchar](max) NULL,
        [ReportName] [nvarchar](max) NULL,
        [PageName] [nvarchar](max) NULL,
        [VisualID] [nvarchar](max) NULL,
        [VisualType] [nvarchar](max) NULL,
        [VisualTitle] [nvarchar](max) NULL,
        [Query] [nvarchar](max) NULL,
        [ImportMode_ExecutionTime] [real] NULL,
        [ImportMode_Status] [nvarchar](max) NOT NULL,
        [ImportMode_StartTime] [datetime] NULL,
        [ImportMode_EndTime] [datetime] NULL,
        [DirectLake_ExecutionTime] [real] NULL,
        [DirectLakeMode_Status] [nvarchar](max) NULL,
        [DirectLake_StartTime] [datetime] NULL,
        [DirectLake_EndTime] [datetime] NULL
    );
END

-- metadata.tbl_ReportVisualDAXQueryAnalyzer
IF NOT EXISTS (
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'metadata'
      AND t.name = 'tbl_ReportVisualDAXQueryAnalyzer'
)
BEGIN
    CREATE TABLE [metadata].[tbl_ReportVisualDAXQueryAnalyzer](
        [id] [int] IDENTITY(1,1) NOT NULL,
        [TrackName] [nvarchar](max) NULL,
        [ReportName] [nvarchar](max) NULL,
        [PageName] [nvarchar](max) NULL,
        [VisualID] [nvarchar](max) NULL,
        [VisualTitle] [nvarchar](max) NULL,
        [TableName] [nvarchar](max) NULL,
        [ColumnName] [nvarchar](max) NULL,
        [FilterExpression] [nvarchar](max) NULL
    );
END

-- ========== VIEW ==========

IF OBJECT_ID('metadata.vw_LoadTestDAX', 'V') IS NOT NULL
    DROP VIEW [metadata].[vw_LoadTestDAX]
GO
CREATE VIEW [metadata].[vw_LoadTestDAX]
AS
SELECT DISTINCT A.Id
	,A.TrackName
	,A.ReportName
	,A.PageName
	,A.VisualID
	,A.VisualType
	,A.VisualTitle
	,A.DAXQuery
	,B.importmodeworkspaceid AS sourceWorkspaceId
	,B.importModeDatasetId AS sourceDatasetId
	,B.directLakeModeWorkspaceId AS targetWorkspaceId
	,B.directLakeModeDatasetId AS targetDatasetId
FROM metadata.tbl_ReportVisualDAX A
JOIN metadata.tbl_LoadTestConnectionDetailsDAX B ON A.reportName = B.reportName
WHERE A.isActive = 1
GO

IF OBJECT_ID('logging.vwLoadTestDAXLog', 'V') IS NOT NULL
    DROP VIEW [logging].[vwLoadTestDAXLog]
GO
CREATE VIEW [logging].[vwLoadTestDAXLog]
AS
SELECT CAST(HASHBYTES('MD5', CONCAT (
				TrackName
				,'|'
				,ReportName
				,'|'
				,PageName
				,'|'
				,VisualID
				)) AS UNIQUEIDENTIFIER) AS CompositeKey
	,A.*
	,CASE 
		WHEN DifferenceVal > - 200
			AND DifferenceVal <= - 3.9999
			THEN 'Improved'
		WHEN DifferenceVal > - 3.9999
			AND DifferenceVal <= 2.9999
			THEN 'Neutral'
		WHEN DifferenceVal > 2.9999
			AND DifferenceVal <= 200
			THEN 'Decreased'
		ELSE 'Neutral'
		END AS flag
FROM (
	SELECT RANK() OVER (
			PARTITION BY Id ORDER BY ImportMode_StartTime
				,PageName
			) AS RunId
		,Id
		,TrackName
		,ReportName
		,PageName
		,VisualID
		,VisualType
		,VisualTitle
		,Query
		,ImportMode_ExecutionTime
		,DirectLake_ExecutionTime
		,(DirectLake_ExecutionTime - ImportMode_ExecutionTime) AS DifferenceVal
		,FORMAT(ImportMode_StartTime, 'yyyy-MM-dd HH:mm:ss') AS ImportMode_StartTime
		,FORMAT(ImportMode_EndTime, 'yyyy-MM-dd HH:mm:ss') AS ImportMode_EndTime
		,ImportMode_Status
		,FORMAT(DirectLake_StartTime, 'yyyy-MM-dd HH:mm:ss') AS DirectLake_StartTime
		,FORMAT(DirectLake_EndTime, 'yyyy-MM-dd HH:mm:ss') AS DirectLake_EndTime
		,DirectLakeMode_Status
	FROM [logging].tbl_LoadTestDAX
	) A;
GO

IF OBJECT_ID('metadata.vw_ReportVisualDAXQueryAnalyzer', 'V') IS NOT NULL
    DROP VIEW [metadata].[vw_ReportVisualDAXQueryAnalyzer]
GO
CREATE VIEW [metadata].[vw_ReportVisualDAXQueryAnalyzer]
AS
WITH StageData
AS (
	SELECT TrackName
		,ReportName
		,PageName
		,VisualTitle
		,VisualID
		,Query
		,ImportMode_ExecutionTime
		,DirectLake_ExecutionTime
		,DifferenceVal
		,CASE 
			WHEN DifferenceVal BETWEEN 4
					AND 10.9999
				THEN '4–10 Sec'
			WHEN DifferenceVal BETWEEN 11
					AND 30.9999
				THEN '11–30 Sec'
			WHEN DifferenceVal >= 31
				THEN '>30 Sec'
			ELSE '<4 Sec'
			END AS DifferenceBucket
		,RunId
		,CAST(HASHBYTES('MD5', CONCAT (
					TrackName
					,'|'
					,ReportName
					,'|'
					,PageName
					,'|'
					,VisualID
					)) AS UNIQUEIDENTIFIER) AS CompositeKey
	FROM logging.vwLoadTestDAXLog
	WHERE DifferenceVal > 4
	)
SELECT A.CompositeKey
	,A.TrackName
	,A.ReportName
	,A.PageName
	,A.VisualTitle
	,A.VisualID
	,A.Query
	,A.ImportMode_ExecutionTime
	,A.DirectLake_ExecutionTime
	,MAX(A.DifferenceVal) AS DifferenceVal
	,A.DifferenceBucket
	,B.TableName
	,B.ColumnName
	,B.FilterExpression
FROM StageData A
JOIN metadata.tbl_ReportVisualDAXQueryAnalyzer B ON A.TrackName = B.TrackName
	AND A.ReportName = B.ReportName
	AND A.PageName = B.PageName
	AND A.VisualID = B.VisualID
GROUP BY A.CompositeKey
	,A.TrackName
	,A.ReportName
	,A.PageName
	,A.VisualTitle
	,A.VisualID
	,A.Query
	,A.ImportMode_ExecutionTime
	,A.DirectLake_ExecutionTime
	,A.DifferenceBucket
	,B.TableName
	,B.ColumnName
	,B.FilterExpression;
GO