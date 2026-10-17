"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let csrf = null, pending = false, busy = false, notice = "", sharingRemembered = false;
  async function request(path, body) {
    const response = await fetch(path, {method: body ? "POST" : "GET", mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer", headers: body ? {"Content-Type": "application/json", "X-ORR-CSRF": csrf} : {}, ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout((path.endsWith("/play") || path.endsWith("/refresh")) ? 45000 : 20000)});
    if (!response.ok) {
      const error = new Error("TIDAL request was not accepted.");
      if (path.endsWith("/disconnect")) {
        const value = await response.json();
        if (["disconnect_busy", "disconnect_stop_first", "disconnect_storage_unavailable", "disconnect_unavailable"].includes(value.error)) error.code = value.error;
      }
      throw error;
    }
    return response.json();
  }
  function render(value) {
    pending = value.active === true;
    const errors = {session_exists: "A session is already stored. It was not replaced; this does not confirm playback or account adoption.", storage_unavailable: "Private session storage is unavailable or another operation owns it.", dependency_unavailable: "The pinned TIDAL library is unavailable. Check installation.", invalid_challenge: "TIDAL returned an unsupported login challenge.", login_failed: "Login failed. No new session was saved.", expired: "The login challenge expired.", unsafe_debug: "Disable HTTP wire debugging before login."};
    const sessions = {
      missing: "No TIDAL session is stored on this renderer.",
      stored: "An unexpired TIDAL device session is stored locally. Current provider access, playback and app adoption remain unverified.",
      expired: "The stored TIDAL session has expired. An explicit track test can attempt one session refresh before requesting playback. You can also use Refresh the saved session before sharing.",
      unsupported: "The stored TIDAL record is incomplete or has unsupported login provenance. Review the local session setup; a new login will not replace it.",
      unavailable: "Private TIDAL session storage could not be read safely. Check the local storage setup before another attempt."
    };
    const states = {starting: "Requesting a login challenge.", pending: "Complete the pending login on TIDAL's site.", cancelling: "Cancelling login; waiting for the pending worker to finish.", cancelled: "Login was cancelled.", closed: "Device login is closed."};
    byId("status").textContent = sessions[value.session_status] || "Local TIDAL session status is not yet confirmed.";
    if (errors[value.error] || states[value.state]) byId("status").textContent += ` ${errors[value.error] || states[value.state]}`;
    if (notice) byId("status").textContent += ` ${notice}`;
    const stored = value.session_status === "stored", refreshable = stored || value.session_status === "expired";
    if (typeof value.account_sharing_remembered === "boolean") {
      if (value.account_sharing_remembered || sharingRemembered) byId("remember-sharing").checked = value.account_sharing_remembered;
      sharingRemembered = value.account_sharing_remembered;
    }
    byId("remember-sharing").disabled = busy || pending || !csrf || !stored || sharingRemembered;
    byId("start").disabled = busy || pending || value.retry_after > 0 || !csrf || !byId("consent").checked || value.session_status !== "missing" || value.state === "closed";
    byId("cancel").disabled = busy || !pending;
    byId("share").disabled = busy || pending || !csrf || !stored || sharingRemembered || (value.account_shared === true && !byId("remember-sharing").checked);
    byId("play").disabled = busy || pending || !csrf || !refreshable;
    byId("refresh-session").disabled = busy || pending || !csrf || !refreshable;
    byId("disconnect").disabled = busy || pending || !csrf || !byId("forget-consent").checked || !["stored", "expired", "unsupported", "unavailable"].includes(value.session_status);
    byId("unshare").disabled = busy || !csrf;
    if (value.account_sharing_remembered === true) byId("status").textContent += value.account_shared === true ? " Account sharing with trusted LAN controllers is enabled and remembered across restarts." : " Account sharing is remembered across restarts but currently suspended. Stop account sharing removes the saved decision.";
    else if (value.account_shared === true) byId("status").textContent += " Account sharing with trusted LAN controllers is enabled until the renderer restarts.";
    byId("challenge").hidden = true;
    byId("verify").removeAttribute("href"); byId("code").textContent = "";
    if (value.state === "pending" && typeof value.verification_uri === "string" && typeof value.user_code === "string") {
      const url = new URL(value.verification_uri);
      if (url.protocol !== "https:" || !["login.tidal.com", "link.tidal.com"].includes(url.hostname) || url.username || url.password || (url.port && url.port !== "443")) return;
      byId("verify").href = url.href; byId("code").textContent = value.user_code; byId("challenge").hidden = false;
    }
  }
  async function refresh() {
    try { render(await request("/orr/v1/tidal/status")); }
    catch (_) { render({session_status: "unavailable"}); }
  }
  async function act(action) {
    if (busy) return;
    notice = ""; busy = true;
    for (const id of ["start", "cancel", "share", "unshare", "play", "refresh-session", "disconnect", "remember-sharing"]) byId(id).disabled = true;
    try {
      const result = await request(`/orr/v1/tidal/${action}`, ({start: {confirm_experimental_login: true}, cancel: {cancel: true}, share: {confirm_lan_account_sharing: true, ...(byId("remember-sharing").checked ? {remember_account_sharing: true} : {})}, unshare: {stop_lan_account_sharing: true}, play: {track_id: byId("track").value.trim(), confirm_experimental_playback: true}, refresh: {confirm_session_refresh: true}, disconnect: {confirm_forget_local_session: true}}[action]));
      if (action === "unshare" && result.account_shared === false && result.account_sharing_remembered === false) byId("remember-sharing").checked = false;
      if (action === "disconnect" && result.session_forgotten === true) {
        notice = "The saved local TIDAL session was forgotten and account sharing was removed. To sign in again, confirm and start a new device login. Your TIDAL account and provider grants were not deleted or revoked.";
        byId("consent").checked = false;
        byId("remember-sharing").checked = false;
      }
      if (action === "refresh") {
        const errors = {
          dependency_unavailable: "The pinned TIDAL library is unavailable. Check installation.",
          storage_unavailable: "Refresh could not access private session storage. Let any active operation finish and check storage.",
          unsafe_debug: "Disable HTTP wire debugging before refreshing the session.",
          invalid_timeout: "The refresh time limit is invalid. Check the renderer configuration.",
          session_unavailable: "Refresh requires a complete device-login session with refresh credentials. Review the local session setup.",
          client_mismatch: "The saved session and installed client configuration do not match. Check the pinned TIDAL installation and local session setup.",
          invalid_response: "Refresh returned an unsupported response. Inspect the local session before another explicit attempt.",
          identity_mismatch: "The refreshed account did not match the saved session. Stop and review the local session.",
          refresh_failed: "Refresh failed or timed out. Inspect the local session before another explicit attempt.",
          commit_failed: "Refreshed credentials could not be confirmed saved. Inspect the local session before another explicit attempt.",
          refresh_result_uncertain: "A previous session refresh has an uncertain result. Further refresh attempts are blocked. Use Forget saved TIDAL session below, then explicitly start a new login.",
          refresh_rejected: "TIDAL rejected the saved refresh grant. Further refresh attempts are blocked. Use Forget saved TIDAL session below, then explicitly start a new login."
        };
        notice = result.refresh_outcome === "saved" ? (result.sharing_warning ? "Session refresh saved, but account sharing was not renewed. Playback remains unverified." : "Session refresh saved. Playback remains unverified.") : `${errors[result.error] || "Session refresh was not confirmed."} ${result.refresh_outcome === "rejected" ? "The refresh grant was rejected; local session recovery is required." : result.refresh_outcome === "uncertain" ? "The provider may have rotated credentials. Do not retry automatically." : "Refresh was not started."}`;
      }
      if (action === "play" && result.playback_requested === true) notice = "Playback was requested. Complete-track playback remains unverified.";
      render(result);
    }
    catch (error) {
      const disconnectErrors = {
        disconnect_busy: "Another renderer or TIDAL operation is active. Cancel pending login or let the operation finish, then explicitly try forgetting again.",
        disconnect_stop_first: "Stop TIDAL playback first, then explicitly try forgetting again. No playback was stopped.",
        disconnect_storage_unavailable: "Local session removal was not confirmed. Storage may be busy or unsafe. Check status and local storage; no automatic retry was sent.",
        disconnect_unavailable: "Local session recovery is unavailable for this renderer's account configuration."
      };
      notice = action === "disconnect" ? disconnectErrors[error.code] || "Local session removal was not confirmed. Check status before trying again; no automatic retry was sent." : action === "share" ? "Sharing was not confirmed. An unexpired device session and an idle login worker are required." : action === "play" ? "Playback request was not confirmed. Check renderer status; no automatic retry was sent." : action === "refresh" ? "Refresh outcome is unconfirmed; the provider may have rotated credentials. Inspect the local session before another explicit attempt. No automatic retry was sent." : "Action outcome unconfirmed. Check status; no automatic retry was sent.";
      byId("status").textContent = notice;
    }
    finally { if (action === "disconnect") byId("forget-consent").checked = false; busy = false; await refresh(); }
  }
  byId("consent").addEventListener("change", () => { if (!busy) refresh(); });
  byId("forget-consent").addEventListener("change", () => { if (!busy) refresh(); });
  byId("remember-sharing").addEventListener("change", () => { if (!busy) refresh(); });
  byId("start").addEventListener("click", () => { if (byId("consent").checked) act("start"); });
  byId("play").addEventListener("click", () => { if (/^[1-9][0-9]{0,18}$/.test(byId("track").value.trim())) act("play"); else { notice = "Enter a numeric TIDAL track ID."; refresh(); } });
  byId("refresh-session").addEventListener("click", () => act("refresh"));
  byId("share").addEventListener("click", () => act("share"));
  byId("unshare").addEventListener("click", () => act("unshare"));
  byId("cancel").addEventListener("click", () => act("cancel"));
  byId("disconnect").addEventListener("click", () => { if (byId("forget-consent").checked) act("disconnect"); });
  request("/orr/v1/web/state").then(value => { csrf = value.csrf_token; return refresh(); }).catch(() => { byId("status").textContent = "Renderer unavailable. Reload this page."; });
  window.setInterval(() => { if (!busy) refresh(); }, 2000);
})();
