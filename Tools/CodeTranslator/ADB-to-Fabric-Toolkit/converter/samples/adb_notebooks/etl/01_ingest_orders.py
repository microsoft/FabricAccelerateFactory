# Databricks notebook source
# MAGIC %md
# MAGIC # 01 - Ingest orders (sample)
# MAGIC Synthetic example used to demonstrate the converter.

# COMMAND ----------

dbutils.widgets.text("run_date", "2025-01-01", "Run date")
dbutils.widgets.dropdown("env", "dev", ["dev", "uat", "prod"], "Environment")

# COMMAND ----------

# DBTITLE 1,Configuration
run_date = dbutils.widgets.get("run_date")
env = dbutils.widgets.get("env")
storage_key = dbutils.secrets.get(scope="kv-sales", key="storage-key")
spark.conf.set("spark.databricks.delta.optimizeWrite.enabled", "true")
base = "abfss://raw@contosolake.dfs.core.windows.net/orders"

# COMMAND ----------

files = dbutils.fs.ls(base)
display(files)
df = spark.read.parquet(f"{base}/dt={run_date}")
df.write.format("delta").mode("append").saveAsTable("bronze.orders")

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT order_id, COUNT(*) AS n
# MAGIC FROM bronze.orders
# MAGIC GROUP BY order_id
# MAGIC HAVING COUNT(*) > 1

# COMMAND ----------

# MAGIC %run ./common/helpers

# COMMAND ----------

dbutils.notebook.exit("done")
