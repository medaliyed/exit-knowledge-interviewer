-- Runs once, on first start of an empty data volume, connected to the `eki` database.
CREATE EXTENSION IF NOT EXISTS vector;

-- Langfuse keeps its own tables in a separate database on the same server.
CREATE DATABASE langfuse;
