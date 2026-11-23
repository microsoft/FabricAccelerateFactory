-- Databricks notebook source
-- MAGIC %md
-- MAGIC # SQL notebook sample

-- COMMAND ----------

CREATE TABLE IF NOT EXISTS bronze.sample (id INT, name STRING) USING DELTA;

-- COMMAND ----------

OPTIMIZE bronze.sample ZORDER BY (id);
