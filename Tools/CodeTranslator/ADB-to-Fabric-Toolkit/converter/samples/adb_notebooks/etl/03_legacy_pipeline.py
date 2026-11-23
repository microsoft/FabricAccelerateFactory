# Databricks notebook source
# MAGIC %md
# MAGIC ### 03 - Legacy pipeline (many Databricks-only features)

# COMMAND ----------

import dlt
from databricks.sdk import WorkspaceClient

# COMMAND ----------

dbutils.fs.mount(source="wasbs://c@acct.blob.core.windows.net", mount_point="/mnt/legacy",
                 extra_configs={"fs.azure.account.key.acct.blob.core.windows.net": dbutils.secrets.get("kv", "k")})
df = spark.read.csv("/mnt/legacy/input/orders.csv", header=True)
stream = (spark.readStream.format("cloudFiles").option("cloudFiles.format", "json").load("dbfs:/landing/"))

# COMMAND ----------

@dlt.table
def orders_clean():
    return dlt.read("orders_raw").where("amount > 0")

# COMMAND ----------

# MAGIC %sql
# MAGIC USE CATALOG main;
# MAGIC COPY INTO main.sales.orders FROM '/mnt/legacy/in' FILEFORMAT = CSV;
# MAGIC SELECT * FROM main.sales.orders WHERE dt = '${run_date}'

# COMMAND ----------

# MAGIC %scala
# MAGIC val x = dbutils.fs.ls("/mnt/legacy")
# MAGIC println(x.length)

# COMMAND ----------

# MAGIC %sh
# MAGIC ls -la /dbfs/mnt/legacy

# COMMAND ----------

# MAGIC %fs ls /mnt/legacy

# COMMAND ----------

# MAGIC %conda install numpy

# COMMAND ----------

query = """
COPY INTO bronze.t FROM '/mnt/x'
"""
spark.sql(query)
