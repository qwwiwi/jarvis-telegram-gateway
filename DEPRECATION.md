# Deprecation Notice

`jarvis-telegram-gateway` is deprecated. Migrate to [qwwiwi/dashi-plugin-claude-code](https://github.com/qwwiwi/dashi-plugin-claude-code) before **2026-06-15**.

## Why

On 2026-06-15 Anthropic splits billing:
- `claude -p` (Agent SDK) goes into a separate $200/mo pool
- Interactive Claude Code sessions stay in Max subscription

This gateway spawns a new `claude -p` process for every Telegram message. After the cutover, each message becomes a draw from the Agent SDK pool — not from your Max plan. For agents that handle dozens or hundreds of messages per day, this translates to a meaningful billing change.

The new architecture (`dashi-plugin-claude-code`) keeps **one live interactive Claude Code session per agent** and pushes Telegram messages into it as channel events. The session classifies as interactive → remains in Max subscription. Per-message API cost = $0.

## Timeline

| Date | What happens |
|---|---|
| **2026-06-15** | Anthropic billing split goes live. Gateway works technically, but each turn costs Agent SDK budget. |
| **2026-09-15** | This repo flipped to `archived` state. No new PRs/issues accepted, code stays readable for reference. |
| **2026-12-15** | Last compatibility patches. After this date — no support, no security fixes. |

## What you keep

Migration is non-destructive:
- Bot token stays the same
- Workspace (`CLAUDE.md`, memory) carries over
- Allowed user/chat IDs map 1-to-1 to plugin env

You don't have to throw anything away — the new architecture replaces only the gateway layer.

## How to migrate

Full guide: https://github.com/qwwiwi/dashi-plugin-claude-code/blob/main/docs/04-migration-from-gateway.md

Short version:
1. Snapshot everything (gateway, workspace, secrets, systemd unit)
2. Install plugin alongside gateway (test bot or temporarily off-loaded production bot)
3. Smoke test plugin in isolation
4. Cutover: `systemctl stop gateway` → 30s wait → `systemctl start plugin` → ping bot
5. After 1-2 weeks of stable plugin operation — remove gateway

## Help

- Issues (in the NEW repo): https://github.com/qwwiwi/dashi-plugin-claude-code/issues with tag `migration`
- Если переезд невозможен по техническим причинам — открывайте issue с тегом `cant-migrate`

## What about new features in this gateway?

After **2026-05-17**, this repo is in feature-freeze. Only security fixes will be merged. New features go into [dashi-plugin-claude-code](https://github.com/qwwiwi/dashi-plugin-claude-code).
