"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let csrf = null, pending = false, busy = false, notice = "";
  async function request(path, body) {
    const response = await fetch(path, {method: body ? "POST" : "GET", mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer", headers: body ? {"Content-Type": "application/json", "X-ORR-CSRF": csrf} : {}, ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout(path.endsWith("/play") ? 45000 : 20000)});
    if (!response.ok) throw new Error("Login request was not accepted. Refresh before trying again.");
    return response.json();
  }
  function render(value) {
    pending = value.active === true;
    const errors = {session_exists: "A session is already stored. It was not replaced; this does not confirm playback or account adoption.", storage_unavailable: "Private session storage is unavailable or another operation owns it.", dependency_unavailable: "The pinned TIDAL library is unavailable. Check installation.", invalid_challenge: "TIDAL returned an unsupported login challenge.", login_failed: "Login failed. No new session was saved.", expired: "The login challenge expired.", unsafe_debug: "Disable HTTP wire debugging before login."};
    byId("status").textContent = errors[value.error] || `Login status: ${String(value.state || "unknown")}. Full playback and app adoption remain unverified.`;
    if (notice) byId("status").textContent += ` ${notice}`;
    byId("start").disabled = busy || pending || value.retry_after > 0 || !csrf || !byId("consent").checked || value.state === "connected";
    byId("cancel").disabled = busy || !pending;
    byId("share").disabled = busy || pending || !csrf || value.account_shared === true;
    byId("play").disabled = busy || pending || !csrf;
    byId("unshare").disabled = busy || !csrf || value.account_shared !== true;
    if (value.account_shared === true) byId("status").textContent += " Account sharing with trusted LAN controllers is enabled.";
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
    catch (_) { byId("status").textContent = "Could not confirm login state. Refresh before another attempt."; }
  }
  async function act(action) {
    if (busy) return;
    notice = ""; busy = true; byId("start").disabled = true; byId("cancel").disabled = true;
    try { render(await request(`/orr/v1/tidal/${action}`, ({start: {confirm_experimental_login: true}, cancel: {cancel: true}, share: {confirm_lan_account_sharing: true}, unshare: {stop_lan_account_sharing: true}, play: {track_id: byId("track").value.trim(), confirm_experimental_playback: true}}[action]))); }
    catch (_) { notice = action === "share" ? "Sharing was not confirmed. An unexpired verified device session and an idle login worker are required." : "Action outcome unconfirmed. Check status; no automatic retry was sent."; byId("status").textContent = notice; }
    finally { busy = false; await refresh(); }
  }
  byId("consent").addEventListener("change", () => { if (!busy) refresh(); });
  byId("start").addEventListener("click", () => { if (byId("consent").checked) act("start"); });
  byId("play").addEventListener("click", () => { if (/^[1-9][0-9]{0,18}$/.test(byId("track").value.trim())) act("play"); else { notice = "Enter a numeric TIDAL track ID."; refresh(); } });
  byId("share").addEventListener("click", () => act("share"));
  byId("unshare").addEventListener("click", () => act("unshare"));
  byId("cancel").addEventListener("click", () => act("cancel"));
  request("/orr/v1/web/state").then(value => { csrf = value.csrf_token; return refresh(); }).catch(() => { byId("status").textContent = "Renderer unavailable. Reload this page."; });
  window.setInterval(() => { if (!busy) refresh(); }, 2000);
})();
