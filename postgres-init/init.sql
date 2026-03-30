-- Create smartclerk user + database for doc-engine
CREATE USER smartclerk WITH PASSWORD 'smartclerk';
CREATE DATABASE smartclerk_de OWNER smartclerk;

-- Enable pgvector on the ml-pipeline database
\c docai
CREATE EXTENSION IF NOT EXISTS vector;
