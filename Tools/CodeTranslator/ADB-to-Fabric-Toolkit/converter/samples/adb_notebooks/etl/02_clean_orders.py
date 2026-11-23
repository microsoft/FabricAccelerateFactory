# Databricks notebook source
# MAGIC %md
# MAGIC ### 02 - Clean orders (portable Spark only)

# COMMAND ----------

from pyspark.sql import functions as F, Window

df = spark.range(10).withColumn("v", F.col("id") * 2)
w = Window.partitionBy(F.col("id") % 3).orderBy("v")
df = df.withColumn("rn", F.row_number().over(w))
df.createOrReplaceTempView("tmp_orders")

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT rn, COUNT(*) AS n FROM tmp_orders GROUP BY rn ORDER BY rn

# COMMAND ----------

# MAGIC %pip install pandas==2.2.3
