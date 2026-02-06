-- PRMSOE Schema Migration
-- Run this against your Supabase project (SQL Editor or psql)

-- Enums
DO $$ BEGIN
  CREATE TYPE intent_type AS ENUM ('VALIDATION', 'SALES');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE contact_status AS ENUM ('NEW', 'RESEARCHING', 'DRAFT_READY', 'SENT', 'ARCHIVED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE strategy_tag AS ENUM ('PAIN_POINT', 'VALIDATION_ASK', 'DIRECT_PITCH', 'MUTUAL_CONNECTION', 'INDUSTRY_TREND');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE feedback_status AS ENUM ('PENDING', 'COMPLETED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE outcome_type AS ENUM ('REPLIED', 'GHOSTED', 'BOUNCED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('RUNNING', 'COMPLETED', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- profiles
CREATE TABLE IF NOT EXISTS profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id),
  mission_statement text NOT NULL,
  intent_type intent_type NOT NULL,
  created_at timestamptz DEFAULT now()
);

-- contacts
CREATE TABLE IF NOT EXISTS contacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id),
  full_name text,
  linkedin_url text,
  raw_role text,
  company_name text,
  status contact_status DEFAULT 'NEW',
  draft_message text,
  strategy_tag strategy_tag,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_contacts_user_status ON contacts(user_id, status);

-- research
CREATE TABLE IF NOT EXISTS research (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id uuid NOT NULL REFERENCES contacts(id) UNIQUE,
  news_summary text,
  pain_points text,
  source_url text,
  raw_response jsonb,
  last_updated timestamptz DEFAULT now()
);

-- outreach_attempts
CREATE TABLE IF NOT EXISTS outreach_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id uuid NOT NULL REFERENCES contacts(id),
  strategy_tag strategy_tag,
  message_body text NOT NULL,
  sent_at timestamptz DEFAULT now(),
  feedback_due_at timestamptz,
  feedback_status feedback_status DEFAULT 'PENDING',
  outcome outcome_type
);

CREATE INDEX IF NOT EXISTS idx_outreach_feedback ON outreach_attempts(feedback_status, feedback_due_at);

-- enrichment_jobs
CREATE TABLE IF NOT EXISTS enrichment_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id),
  total_contacts integer,
  processed_count integer DEFAULT 0,
  failed_count integer DEFAULT 0,
  status job_status DEFAULT 'RUNNING',
  created_at timestamptz DEFAULT now(),
  completed_at timestamptz
);
