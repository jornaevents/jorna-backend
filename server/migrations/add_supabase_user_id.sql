-- Run once against your Postgres (e.g. Supabase SQL editor) if the table already exists.
ALTER TABLE users ADD COLUMN IF NOT EXISTS supabase_user_id VARCHAR(36) UNIQUE;
