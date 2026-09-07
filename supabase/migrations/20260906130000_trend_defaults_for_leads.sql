-- =============================================================================
-- Trend defaults aimed at buyers rather than at peers.
-- =============================================================================
--
-- The seeded defaults in 20260906120000 described the trade we are in, and
-- every hashtag in them was a room full of people who do what we do:
-- #coding, #devops, #systemdesign, #softwaredevelopment, #techtok reach other
-- developers; #nocode, #aitools, #aiagents, #automation reach people who
-- intend to build it themselves; #saas reaches founders. None of them reach
-- the operator described in our own brief -- the one paying staff to re-key
-- data between two systems.
--
-- That matters more than it looks, because the scout is the whole funnel.
-- `generate_ideas` is instructed to ground every idea in an observed signal
-- and to invent nothing, so whatever room we scout decides what the queue can
-- possibly contain. Scouting the trade produced trade content: well-made
-- reels, watched by people who will never hire an agency. Nothing downstream
-- can recover from that, because by then the ideas are already about the
-- wrong thing.
--
-- So the hashtags below are grouped by who is watching, not by subject:
--
--   * owner and operator identity -- the person who signs the cheque
--   * the work itself, in the words they use for it rather than ours
--   * verticals that carry heavy admin AND a real software budget
--   * two trade tags kept deliberately, for the build-vs-buy half of the
--     brief and for the occasional proof-of-competence reel
--
-- Swap the verticals for the ones you actually sell to -- that group is the
-- most replaceable part of this list and the most valuable to get right. Keep
-- the grouping when you do: it is what stops the list drifting back into a
-- list of things we find interesting.
--
-- These particular tags are reasoned from who watches them, not measured --
-- hashtag volume was not checked and cannot be from here. They are a starting
-- hypothesis, and the app already records what happens to it: every idea keeps
-- its `trend_keyword`, so after a few weeks
--
--   select trend_keyword, status, count(*) from ideas group by 1, 2;
--
-- says which rooms produced ideas worth approving. Drop the ones that never do.
--
-- One knock-on worth naming: scouting #lawfirm and #medspa puts the drafting
-- model closer to regulated advice than the old list ever did, so the brief
-- below is explicit that we support those industries and never advise them.
-- =============================================================================

-- Only if nobody has touched it. `updated_by` is set by the app on every save,
-- and the hashtag comparison catches a row edited by hand or by SQL. A default
-- is a starting point for someone who has not chosen yet -- once the owner has
-- chosen, a migration overwriting that choice is a bug, and a silent one.
update public.trend_settings set
  niche_brief =
    'We are a software and AI automation agency. We build custom software and '
    'automate business workflows -- internal tools, data pipelines, AI agents, '
    'and integrations between the systems a company already runs.'
    || E'\n\n' ||
    'WHO WE ARE SPEAKING TO. The owner or operator of a small or mid-sized '
    'business who is paying people to do by hand what software could do: '
    're-keying the same data into two systems, chasing quotes and invoices, '
    'copying things into spreadsheets, answering the same enquiry twenty times '
    'a day, finding out about a problem a week after it happened. Also '
    'technical leads weighing build against buy. They are not engineers and do '
    'not want to become ones. We serve service businesses in particular: '
    'property and lettings, trades and construction, clinics, legal, '
    'e-commerce operations, and professional services.'
    || E'\n\n' ||
    'WHAT THESE REELS ARE FOR. To make one of those people recognise their own '
    'week in the first three seconds and get in touch. They are not for other '
    'developers, agencies or automation hobbyists. Reach from a peer audience '
    'is not a result: an idea whose best possible viewer is another builder is '
    'a bad idea for us, however well the format is performing.'
    || E'\n\n' ||
    'WHAT EARNS THE ATTENTION. Naming a specific, boring, recognisable '
    'situation and showing what it quietly costs -- or showing the shape of '
    'the fix in plain language. Speak in the viewer''s vocabulary, not ours: an '
    'operator says "everything lives in a spreadsheet and only Dawn '
    'understands it", never "workflow orchestration".'
    || E'\n\n' ||
    'WE MUST NOT CLAIM: specific ROI, revenue or time-saved figures without a '
    'case study behind them; that any automation is hands-off, error-free or '
    'needs no maintenance; that we are partnered with, certified by or '
    'endorsed by any vendor whose tools we use; anything identifying a client, '
    'their data or their results without written permission. We do not '
    'disparage a named competitor or product. Medical, financial, legal and '
    'regulatory advice is never ours to give -- including to the industries '
    'named above, whose admin we automate but whose professional judgement is '
    'never ours to second-guess.',
  hashtags = array[
    -- Owner and operator identity: the person who signs the cheque.
    'smallbusinessowner', 'smallbusinesstips', 'businessowner', 'solopreneur',
    -- The work itself, in their words. A spreadsheet nobody can replace and a
    -- month-end that eats a week are where this job actually starts.
    'exceltips', 'bookkeeping', 'crm',
    -- Verticals with heavy admin and a budget to remove it.
    'realestateagent', 'propertymanagement', 'contractorlife', 'medspa',
    'lawfirm', 'ecommercetips',
    -- Kept from the trade: the build-vs-buy audience reads the first, and
    -- technical leads with an internal-tools backlog read the second.
    'aiautomation', 'internaltools'
  ]
where id
  and updated_by is null
  and hashtags = array[
    'aiautomation', 'aiagents', 'aitools', 'automation', 'workflowautomation', 'nocode',
    'webdevelopment', 'webdesign',
    'appdevelopment', 'saas', 'softwaredevelopment',
    'systemdesign', 'devops', 'internaltools', 'coding', 'techtok'
  ];
