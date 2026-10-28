# Outreach rebuild checkpoint

Branch: `codex/outreach-platform-rebuild`. Original checkout at `e81eb42c57cf609c808273ebf58c3d9c7aa9d73e` preserved, including user modifications. New package: `src/outreach_v2`. PostgreSQL isolation: `outreach_v2` schema. No legacy tables are dropped or reset.

Verified source counts: 57,664 companies, 15,262 hotels, 110,407 company provenance, 24,859 company contacts, 57,664 company screening, 9,787 hotel provenance, 850 hotel contacts. Full inventory in `source-inventory.json` includes 25 SQLite databases and 113 CSVs. Backup of three primary SQLite stores and working-tree patch exists outside Git at `~/.config/geo-research-outreach-agent/rebuild-backup-20261027`.

Live Outreach health reports the original `e81eb42` revision. Render inventory has no deployed Sheets cron service. Engine integration source restricts research to historical fixture profile IDs and hardcoded property/audit/opportunity mappings; it does not offer a general durable research job API. These are implementation/integration gaps, not evidence of successful automation.

V1 identified in `research_lifecycle.create_manual_invitation_draft` and saved 20261027 mail merge artifacts. Generic `config/templates/research_invitation.txt` is not the personalized V1. V1 company-specific subjects conflict with Word's single bulk-send subject. A user decision is pending; no wording is silently replaced.

E2E status: PARTIAL. No new invitations sent, consent submitted, research run, or report delivered by this rebuild yet. Existing staging remains unchanged until migration and integration acceptance.
