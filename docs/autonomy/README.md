# CoC Telegram Bot 0.4.2

Release candidate 0.4.2. Automatic moderation and key management remain opt-in. Review a fresh preview before enabling moderation. Existing business tables and bindings are retained. After merging the release PR and successful image publication, refresh the Home Assistant add-on store and install the 0.4.2 update. Back up the add-on first; no reinstall is needed.

## Home Assistant

Manual `coc_token` remains required as fallback. Enable `coc_auto_keys` and configure `coc_email` / `coc_password` only for the developer account whose keys the add-on should manage. Password is a password-schema option. These credentials remain in HA's local options and process environment; protect HA backups. Key ownership metadata lives in `/data/coc_keys.json`, mode 0600; API keys are fetched from the portal, not persisted there.

Portal recovery runs at startup and on 403. One asyncio lock serializes it, with a five-minute minimum retry interval. Source IPv4 comes from the portal's temporary token claims. A new key is probed on the clan endpoint before switching the shared client's Authorization header. Only IDs recorded locally AND carrying this installation's random owner name are eligible for revocation. Old keys are revoked after switching. At the account's ten-key limit the add-on stops rotation and requests manual action; it never deletes a manual key to make room. Portal failures preserve the existing manual/current credentials. Portal endpoint changes, CAPTCHA or additional login challenges require manual intervention.

The adapter follows the login/list/create/revoke sequence inspected in coc.py's `coc/http.py`; it uses the existing httpx stack rather than adding an independent game client. Verification against a real developer account is outstanding. Also inspected cocapi/main/cocapi/key_manager.py: it uses httpx, optional local token persistence and name-based ownership. This adapter instead requires both a local recorded ID and an installation-specific name, preserves manual credentials, and uses the source IPv4 reported by the portal rather than third-party IP services.

## Group access

`clan_sync_auto` defaults false. `clan_sync_delay_hours` defaults 24 (HA range 1–168). A successful complete uncached roster is required; errors/empty or partial responses reset absence evidence. Any verified linked account in the clan preserves access. Unlinked users are never removed solely for lacking an account. Guests are scoped to group and user, persisted, and exempt until expiration. Revocation resets absence evidence. Bindings survive removal.

Commands in the bot's private chat:

- `/clansync`: preview for current leader/co-leader.
- `/clansync enable`: confirm after a preview within the last hour; HA auto setting must also be true.
- `/clansync guest TELEGRAM_ID DAYS`: grant/extend a guest allowance. Zero days means permanent and requires Leader.
- `/clansync revoke TELEGRAM_ID`: revoke allowance.
- `/clansync guests`: list guests.
- `/clansync invite TELEGRAM_ID DAYS`: issue an hour-long join-request link for exactly that user, with 1–30 days' guest access.

Invitation rights follow current highest CoC role using HA switches `invite_leader`, `invite_coleader`, `invite_elder`, `invite_member` (defaults true,true,false,false). Join approval rechecks the inviter's role. Forwarding a link does not authorize another user. Successful links are consumed and revoked. Registration remains unrestricted in duration for unlinked users; there is not yet a registration-reminder UI.

Telegram bot must be administrator with `can_restrict_members` for removals and `can_invite_users` for links/join approval. Removal uses ban then unban so the person can return; an interrupted unban is recorded for retry. Existing Telegram administrators/owner are not removed. No promotion is performed. Tags combine a shortened nickname and highest game role; editable administrator titles use Telegram's custom-title method. Ordinary admins that the bot cannot edit and the group owner remain untouched.

## Account strength v1

Independent of war results, activity, donations or discipline:

`index = 10 × TH + 9 × development`

Development is a weighted mean of observed `level/maxLevel` ratios: heroes 30%, troops 30%, spells 15%, siege 10%, equipment 15%. Missing components are excluded and coverage is reported. Empty/missing profiles have no index rather than zero. TH bands do not overlap. This is an index, not a percentage or a known building strength. API maxLevel is not guaranteed to be TH-specific. Same-TH percentile compares observed development among current clan peers; a single peer has no percentile. Known Builder Base entries and identified super troops are excluded; siege classification is versioned by names and requires maintenance for future units.

Widgets: Players → Account strength, ten rows per page, click a row for contributions. `/account-strength` is the internal backend endpoint. Classic UI exposes the same ranking via Ratings → Account strength and /strength, including ten-row pagination and detail contributions.

## History

Completed capital seasons are upserted by clan/start every six hours and on profile reads. Profiles expose participation count, attacks/available attacks, gold and gold per attack. Backend provides the per-season trend. Missing membership history prevents a defensible missed-week regularity score; it remains unknown. Historical pages no longer available from the API cannot be reconstructed.

Defense scans stored completed wars (up to 100) in either clan orientation. It exposes incoming stars, destruction and attacker TH. The separate index is `100 × (1 − stars/3)` averaged over equal-TH incoming hits only. Stronger attackers cannot lower this index. No incoming observations means unknown. It is an outcome index, not a measurement of defensive buildings or attack skill; it does not alter existing war selection formulas.

## Verification and remaining acceptance work

Local suite: 69 backend, 136 bot, 5 add-on tests; ruff F checks. Local executor wake-up workaround remains external to the repository as documented for 0.4.1.

Release CI checks all nine Widget screens in both themes at 320/390px, then builds ARM64/AMD64. New state payloads are tested ≤1024 bytes. Live Telegram/developer-portal and physical Raspberry Pi acceptance remains user-side. Additional work beyond this release: a raid trend UI, hall/promotion integration and broader moderation/portal fault-injection coverage. These are not claimed as completed features. Automatic features remain disabled by default.
