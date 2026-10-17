-- Runs once when the postgres volume is first created.
-- The preview/runtime data lives in its own database, apart from platform metadata.
CREATE DATABASE orchivio_preview OWNER orchivio;
