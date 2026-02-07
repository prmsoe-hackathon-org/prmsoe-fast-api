-- Draft Lab: 2 new DRAFT_READY contacts (Mert Gulsun + Umar Ghani)
-- Using UUIDs in 200-range to avoid collision with bulk seed

DO $$
DECLARE
  uid uuid := 'c877835e-4609-4075-9892-84bf9c3e8f97';
  c_mert uuid := 'a0000001-0001-4000-8000-000000000201';
  c_umar uuid := 'a0000001-0001-4000-8000-000000000202';
BEGIN

INSERT INTO contacts (id, user_id, full_name, linkedin_url, raw_role, company_name, status, strategy_tag, draft_message, created_at) VALUES
  (c_mert, uid, 'Mert Gulsun', 'https://www.linkedin.com/in/mert-gulsun', 'Senior Associate Consultant', 'Bain & Company', 'DRAFT_READY', 'VALIDATION_ASK',
   'Hi Mert, I saw Bain''s work with UiPath on agentic AI transformation — fascinating stuff. I''m building a tool that helps people automate the parts of their work they don''t enjoy. Would love to hear how Bain is thinking about AI-driven automation for clients. Quick chat?',
   now() - interval '2 days'),
  (c_umar, uid, 'Umar Ghani', 'https://www.linkedin.com/in/umarghani1', 'Incoming Engineer Intern', 'Qualcomm', 'DRAFT_READY', 'INDUSTRY_TREND',
   'Hi Umar, Qualcomm''s Dragonwing robotics platform and push into physical AI is exciting — the $1T market by 2040 is massive. I''m working on helping people automate tedious work with AI. Curious how you''re thinking about the automation wave at Qualcomm.',
   now() - interval '1 day')
ON CONFLICT (id) DO NOTHING;

INSERT INTO research (id, contact_id, news_summary, pain_points, source_url, last_updated) VALUES
  (gen_random_uuid(), c_mert,
   'Bain & Company partnered with UiPath on agentic AI transformation; majority of Bain client projects now use GenAI tools; firm building proprietary AI platform "Sage".',
   'AI and automation accelerating business change across consulting clients; need for scalable automation strategies.',
   'https://www.uipath.com/newsroom/ai-and-automation-accelerating-business-change-finds-new-bain-uipath-report',
   now() - interval '2 days'),
  (gen_random_uuid(), c_umar,
   'Qualcomm unveiled Dragonwing robotics platform and Snapdragon X2 Elite at CES 2026; pushing into physical AI worth $1T by 2040.',
   'Expanding into robotics and physical AI; competing with Nvidia Jetson; building developer ecosystem for edge AI.',
   'https://www.automate.org/news/ces-2026-qualcomm-targets-nvidia-jetson-with-new-robotics-developer-platform',
   now() - interval '1 day')
ON CONFLICT (contact_id) DO NOTHING;

END $$;
