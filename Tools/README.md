# Tools Catalog

Automation tools to accelerate Microsoft Fabric migration.

## Available Tools

| Tool | Purpose | Location |
|------|---------|----------|
| **[ADB to Synapse/Fabric Converter](./CodeTranslator/ADBtoSynapse/)** | Convert Databricks notebooks to Synapse/Fabric format | `CodeTranslator/ADBtoSynapse/` |
| **[Semantic Model Translator](./CodeTranslator/SematicModelTranslator/)** | Migrate Power BI models from Import to Direct Lake | `CodeTranslator/SematicModelTranslator/` |
| **[Data Reconciliation](./DataReconciliation/AzureSQL/)** | Validate data consistency between Azure SQL and Fabric SQL | `DataReconciliation/AzureSQL/` |
| **[SQL Performance Testing](./PerformaceTest/AzureSQLvsFabricSQL/)** | Compare Azure SQL vs Fabric SQL query performance | `PerformaceTest/AzureSQLvsFabricSQL/` |
| **[Power BI Performance Testing](./PerformaceTest/PowerBIVisuals/)** | Compare Import Model vs Direct Lake visual performance | `PerformaceTest/PowerBIVisuals/` |

## Quick Start

1. Choose tools based on your migration scenario
2. Review tool-specific documentation for setup instructions
3. Configure connections and authentication
4. Run validation tests before production use

## Migration Scenarios

**Databricks → Fabric**: Use ADB Converter + Data Reconciliation + SQL Performance Testing  
**Power BI Import → Direct Lake**: Use Semantic Model Translator + Power BI Performance Testing