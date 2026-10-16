# Experimental standard-library device login

This separate experiment uses the regular `tidalapi==0.8.11` package from PyPI
(the upstream project is EbbLabs/python-tidal). It does not extract device
firmware credentials or copy the library's client credentials into ORR source.
Provider approval for this integration and API availability remain uncertain.
No account or full-playback acceptance is established by offline tests.

## User action on the renderer

After a tested installation, open the renderer's existing web interface and
choose **Experimental TIDAL device login** (`/ui/tidal`). Read the limitations,
check the explicit confirmation box, and choose **Start device login**.
Only then does the renderer contact TIDAL. Follow the displayed official
verification link and complete the account login/consent yourself in TIDAL's
page. The renderer never asks for a password or a token in its UI.

The existing interface trusts the local network: Host, Origin and CSRF checks
protect browser admission, but do **not** authenticate other LAN users. No new
listener, public relay or remote execution operation is introduced. Use this
only on the trusted network already accepted for renderer control.

Only the official verification URI and the human-facing one-time `user_code`
are shown. The internal polling `device_code`, access token, refresh token and
account identity are never returned by this web API. Do not share login links,
codes or screenshots. The browser status does not attest full playback.

A successful complete session is stored in the current renderer user's
`~/.local/share/open-room-renderer/components/tidal/session.json`, independently
of release directories and renderer configuration. The existing components
parent must exist. The final directory is private (0700), and the token file
is private (0600), atomically replaced under exclusive directory ownership.
Tokens are plaintext on disk, protected by ownership/modes. No token enters a
GitHub job, diagnostic artifact or chat. An existing saved session is rejected,
never silently replaced by another login.

Cancel prevents local adoption. The upstream library's already-running polling
thread may continue until it completes or expires; the operation remains active
while draining, and its lease is retained. Cancel is not provider revocation.
Close and restart behavior must not be represented as provider logout.

## Explicit limits

This milestone does not start audio, advertise a native TIDAL capability,
return the saved account to WiiM Home, resolve a track, refresh a token, or
establish full-playback rights. Those require separate actual acceptance checks.
The independent own-client PKCE CLI remains available as a separate route.

The package dependency is pinned because its observed API is version-specific.
Its transitive dependencies are installed by the existing native installer;
actual target installation and login must be verified before calling this a
working XU4 login. Do not bypass installation failures or CI gates.

Sources:
- https://pypi.org/project/tidalapi/0.8.11/
- https://github.com/EbbLabs/python-tidal/tree/v0.8.11

Do not deploy or restart while login is `pending` or `cancelling`; wait until
`active` is false. The installed user service has a 45-second stop timeout. If
upstream polling outlives it, a forced stop is not a clean shutdown and the
existing deployment guard can refuse installation. Do not weaken that guard.
