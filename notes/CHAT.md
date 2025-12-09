# CHAT.md — streamvis

## 2025-12-09 – Initial thoughts

- The “wow” factor for `streamvis` should come from:
  - How quickly it converges to a low-latency, low-chatter polling pattern that feels tailor-made for the Snoqualmie system.
  - How clearly it communicates both current conditions and near-future behavior without overloading the user.
- Forecast overlay ideas:
  - Treat the official NWPS forecast as the “backbone” curve, and use observed deviations (amplitude + timing) to gently bend that curve toward reality.
  - In the TUI detail pane, show a compact summary like “Running +0.4 ft above forecast; peak expected ~40 min earlier than guidance.”

