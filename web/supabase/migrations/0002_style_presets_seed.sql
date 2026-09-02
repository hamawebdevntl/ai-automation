-- =============================================================================
-- Style presets — the three lanes from STACK.md, with the cost and ETA that
-- Gate 1 shows at the moment of choosing.
-- =============================================================================
--
-- Cost figures come from README.md's style table. `params` holds the
-- MoneyPrinterTurbo `VideoParams` overrides for the lane; `video_source` is the
-- dominant cost driver (pexels/pixabay/coverr are free, wavespeed and
-- volcengine_seedance bill per request, HeyGen is its own lane beside MPT).
-- =============================================================================

insert into public.style_presets
  (slug, name, description, lane, video_source, est_cost_min_usd, est_cost_max_usd, est_minutes, params, sort_order)
values
  (
    'stock-broll',
    'Stock b-roll',
    'Licensed stock footage cut to the narration. The default lane — cheapest and fastest, and what volume runs on.',
    'stock',
    'pexels',
    0.10, 0.40, 6,
    '{
      "video_aspect": "9:16",
      "video_concat_mode": "random",
      "video_transition_mode": "FadeIn",
      "video_clip_duration": 4,
      "subtitle_enabled": true,
      "subtitle_position": "bottom",
      "font_size": 60,
      "stroke_width": 1.5,
      "bgm_type": "random",
      "bgm_volume": 0.2
    }'::jsonb,
    10
  ),
  (
    'generative',
    'Generative clips',
    'Text-to-video for each beat of the script. Buys visual novelty for concept-led ideas that stock cannot serve; billed per request.',
    'generative',
    'wavespeed',
    0.80, 3.00, 14,
    '{
      "video_aspect": "9:16",
      "video_concat_mode": "sequential",
      "video_transition_mode": "None",
      "video_clip_duration": 5,
      "subtitle_enabled": true,
      "subtitle_position": "bottom",
      "font_size": 60,
      "stroke_width": 1.5,
      "bgm_type": "random",
      "bgm_volume": 0.15
    }'::jsonb,
    20
  ),
  (
    'ai-presenter',
    'AI presenter',
    'A talking-head avatar delivering the script to camera. The premium option — authority content, and the one that reads best on LinkedIn.',
    'presenter',
    'heygen',
    1.00, 2.00, 18,
    '{
      "video_aspect": "9:16",
      "subtitle_enabled": true,
      "subtitle_position": "bottom",
      "font_size": 56,
      "stroke_width": 1.5,
      "bgm_type": "",
      "bgm_volume": 0.0,
      "lane": "heygen"
    }'::jsonb,
    30
  )
on conflict (slug) do update
  set name             = excluded.name,
      description      = excluded.description,
      lane             = excluded.lane,
      video_source     = excluded.video_source,
      est_cost_min_usd = excluded.est_cost_min_usd,
      est_cost_max_usd = excluded.est_cost_max_usd,
      est_minutes      = excluded.est_minutes,
      params           = excluded.params,
      sort_order       = excluded.sort_order;
