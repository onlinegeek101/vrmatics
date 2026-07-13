# Native video attachments on VR-note issues

VR-note recordings can be embedded as **real GitHub attachments** (an
inline `<video>` player, not a file committed to this repo). GitHub's
attachment pipeline is not in any API and rejects tokens — it only
accepts a logged-in **web session cookie** — so `gh_attach.py` replays
that browser flow with a cookie.

To keep the credential low-stakes the cookie comes from a **throwaway bot
account** with write access to this repo, never your personal account. If
it leaks, the blast radius is write access to one public repo.

## One-time provisioning (~10 min)

1. **Create a bot account** on github.com — e.g. `plan2vr-bot`. Use an
   email you control (a `+bot` alias is fine). GitHub allows machine
   accounts.
2. **Enable 2FA** on it (GitHub requires it) — authenticator app.
3. **Add it as a collaborator** on `onlinegeek101/vrmatics` with **Write**
   (Settings → Collaborators → Add people → accept the invite from the
   bot account). Write is the minimum that can create attachments.
4. **Grab the bot's session cookie:** log the bot into github.com in a
   browser, open DevTools → Application → Cookies → `https://github.com`,
   copy the **`user_session`** value. (Or run `gh image extract-token`
   from the maintained CLI while logged in as the bot.)
5. **Store it as an environment variable** in the Claude Code environment
   settings as `GH_ATTACH_COOKIE` (value = the `user_session` string, or
   the full `user_session=...; other=...` cookie string). Environment
   variables persist across container restarts; a repo/chat paste does
   not. Docs: https://code.claude.com/docs/en/claude-code-on-the-web

That's it. The issue loop then uploads each note's recording as an
attachment, embeds the player in the issue body, and drops the webm from
the repo.

## When the cookie expires (~every 2 weeks)

Session cookies die after ~2 weeks (or on bot logout / password change).
Failure is designed to be **loud, not silent**:

- `python plan-to-vr/tools/gh_attach.py check onlinegeek101/vrmatics`
  prints `OK as <bot>` (exit 0) or `EXPIRED …` (exit 3).
- The loop runs `check` each tick; on `EXPIRED` it posts a chat alert and
  leaves notes with their stills + webm link (still fully usable), so
  nothing is lost — only the inline player is deferred.

**Recreate (one step):** repeat provisioning step 4–5 with a fresh cookie,
then re-run the loop / `backfill`: it re-embeds players on every note
issue that is missing one. No per-note work.

## Reachability note (verify on first real cookie)

The egress proxy in the run environment allows `github.com` (verified)
but blocks `api.github.com` (so the tool scrapes the repo id from the
page instead). The **storage upload hop** (step 2 of the flow, an
S3/objects host returned by GitHub) can't be verified without a live
cookie; if that host is proxy-blocked the tool fails loudly with
`storage upload failed`. Run `gh_attach.py upload … onefile` once after
provisioning to confirm the full path before relying on it.
