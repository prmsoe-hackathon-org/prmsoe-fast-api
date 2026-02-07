-- Demo: Add outreach attempt for Bedir Aygun so he shows in feedback queue
-- UUID in 300-range

INSERT INTO outreach_attempts (id, contact_id, strategy_tag, message_body, sent_at, feedback_due_at, feedback_status) VALUES
  ('a0000001-0001-4000-8000-000000000302',
   'a0000001-0001-4000-8000-000000000301',
   'DIRECT_PITCH',
   'Hi Bedir, would love to connect and chat about what you''re working on.',
   now() - interval '3 days',
   now() - interval '1 hour',
   'PENDING')
ON CONFLICT (id) DO NOTHING;
