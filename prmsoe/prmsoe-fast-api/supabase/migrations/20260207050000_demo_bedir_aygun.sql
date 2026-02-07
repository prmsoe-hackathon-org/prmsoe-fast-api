-- Demo: Add Bedir Aygun contact + outreach attempt for mock Gmail reply detection
-- Using UUIDs in 300-range to avoid collision

DO $$
DECLARE
  uid uuid := 'c877835e-4609-4075-9892-84bf9c3e8f97';
  c_bedir uuid := 'a0000001-0001-4000-8000-000000000301';
  o_bedir uuid := 'a0000001-0001-4000-8000-000000000302';
BEGIN

INSERT INTO contacts (id, user_id, full_name, linkedin_url, raw_role, company_name, status, strategy_tag, draft_message, created_at) VALUES
  (c_bedir, uid, 'Bedir Aygun', 'https://www.linkedin.com/in/bediraygun', 'Software Engineer', '', 'SENT', 'DIRECT_PITCH',
   'Hi Bedir, would love to connect and chat about what you''re working on.',
   now() - interval '3 days')
ON CONFLICT (id) DO NOTHING;

INSERT INTO outreach_attempts (id, contact_id, strategy_tag, message_body, sent_at, feedback_due_at, feedback_status) VALUES
  (o_bedir, c_bedir, 'DIRECT_PITCH',
   'Hi Bedir, would love to connect and chat about what you''re working on.',
   now() - interval '3 days',
   now() - interval '1 hour',
   'PENDING')
ON CONFLICT (id) DO NOTHING;

END $$;
