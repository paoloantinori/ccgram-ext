# ccgram-ext

Out-of-tree extensions for [ccgram](https://github.com/alexei-led/ccgram), the Telegram bridge for AI coding agents. Features live here instead of in core, loaded through ccgram's extension seam via standard Python entry points: presence plus config equals active, absence equals completely inert.

Features: **reaction-triggered actions** and **topic identity icons**. React to a bot message with a mapped emoji and something happens to the agent window that produced it: a PNG screenshot of the pane, a spoken voice note of the message text, or any toolbar key/text action. Proposed upstream as [#195](https://github.com/alexei-led/ccgram/issues/195) and declined for core ("hidden message state, actions by mistake"); this package is the opt-in answer. Whoever writes the config chooses the tradeoff, the same way they choose YOLO approval mode.

## Requirement

A ccgram build that carries the extension seam (the `ccgram.extensions` entry-point loader). Stock upstream ccgram does not have one as of v4.10.0, so the package stays inert there by design: nothing registers, nothing listens. The seam lives in this fork:

- repo: https://github.com/paoloantinori/ccgram
- branch: `fork/main`
- core cost of the seam: one loader module plus four integration lines (`docs/extension-seam.md` in the fork)

## Install

```bash
uv tool install git+https://github.com/paoloantinori/ccgram.git@fork/main \
    --force \
    --with git+https://github.com/paoloantinori/ccgram-ext.git
```

After restarting the service, the log must show `extension loaded: main`. No line means the extension did not ship.

## Configure

Everything lives in the same `~/.ccgram/toolbar.toml` the ccgram toolbar uses. No `[reactions]` table means the feature is off.

```toml
[reactions]
"👎" = "screenshot"     # PNG of the window's pane, posted in the same topic
"🙈" = "speak"          # voice note of the reacted message's text
"🔥" = "esc"            # any toolbar key/text action, by name

[reactions.speak]
url = "http://lan-host:3900/v1"   # OpenAI-compatible /audio/speech endpoint
model = "omnivoice"
voice = "26b31bec"
fallback_model = "pockettts"      # used on timeout or 429 (honors Retry-After)
response_format = "opus"
timeout = 240                     # LAN engines can cold-start; give them room
```

`api_key` is optional and falls back to the `CCGRAM_WHISPER_API_KEY` env var, so a LAN service that serves both speech and transcription needs one credential.

## Behavior notes

- Only bot messages the bridge itself delivered are triggerable, and only for a bounded window: 500 recent messages, one hour.
- Only emoji that are newly added fire the action; changing your mind and swapping one emoji for another is a new trigger, premium multi-reactions are handled as deltas.
- Actions typed `builtin` in the toolbar are rejected at config load: they need a CallbackQuery context and cannot run from a reaction.
- Builtin action names `screenshot` and `speak` are reserved for this feature.
- Telegram restricts bots to a fixed reaction set (no camera or speaker glyphs), which is why the examples use 👎 and 🙈.

Single-user deployments are the sweet spot: the person reacting is the person who wrote the config. In a group with several allowed users, anyone's reaction acts on the window that produced the message; weigh that before enabling.

## Topic identity icons

The topic's avatar is the identity slot; the state emoji in the title stays core's. Declined upstream as [#197](https://github.com/alexei-led/ccgram/issues/197); here it is config, inert until you write a `[topic-icons]` table:

```toml
[topic-icons]
"ccgram"  = "💻"        # window name, any "▸" segment of it, or cwd basename
"planner" = "🔭"
# heuristics = true     # opt-in: keyword table, then a deterministic hash
                        # pick so every topic still gets a stable icon
```

Icons are applied when a topic is bound to a new window, and `/icons` re-runs the pass over every bound topic (paced 1.5s per edit; icon edits share Telegram's per-chat editForumTopic bucket with title renames, so a 429 puts the chat on the same cooldown core already uses). Telegram only accepts its fixed forum icon set; configured emojis outside it are skipped.

Two semantics worth knowing. An applied icon is Telegram-side state: it persists even if the feature or this whole package is later absent (topics keep their avatars from installs long gone). And an icon is applied once per run per window: a later `/icons` skips already-latched windows, and if the resolution chain changed since an earlier install (different mapping, different heuristics), the pass may set a different emoji; Telegram's not-modified reply counts as success.

## Status

Reactions running in production since August 2026; topic icons ported from the same fork's original implementation (static map, opt-in heuristics, deterministic fallback).

## License

MIT, same as ccgram.
