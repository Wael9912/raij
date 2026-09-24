// Ra'ij public pages: / (about), /privacy, /terms — required by TikTok's and Meta's developer portals.
// Plain static HTML; the worker stores nothing and sets no cookies.
const UPDATED = "2026-09-24";

const page = (title, body) => `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} — Ra'ij</title>
<style>
  body{font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#222;background:#fff}
  h1{font-size:1.6rem} h2{font-size:1.15rem;margin-top:1.6em} a{color:#0a58ca} .ar{direction:rtl;text-align:right;color:#444}
  footer{margin-top:3em;font-size:.9rem;color:#666;border-top:1px solid #eee;padding-top:1em}
</style></head><body>
<p><a href="/">Ra'ij · رائج</a></p>
${body}
<footer>Last updated ${UPDATED} · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></footer>
</body></html>`;

const HOME = page("About", `
<h1>Ra'ij (رائج)</h1>
<p>Ra'ij is a personal publishing tool. Its owner uses it to prepare short Arabic explainer videos and post them to the
owner's own channels on YouTube, TikTok, Instagram and Facebook. Every video is written from scratch, voiced with a
synthetic narrator, assembled from licensed stock footage, and reviewed by a human before anything is published.</p>
<p class="ar">رائج أداة نشر شخصية يستخدمها مالكها لإعداد فيديوهات عربية قصيرة ونشرها على قنواته الخاصة. كل فيديو
يُكتب من الصفر، ويُراجَع بشرياً قبل النشر.</p>
<h2>Where to find the channel</h2>
<p>YouTube: <a href="https://www.youtube.com/channel/UCeLlvJwQe-uj4IEZEsO3YIw">رائج</a>.</p>
<h2>Contact</h2>
<p>Comment on any video or use the channel's "About" page contact option. Requests about this tool or about data are
answered there.</p>`);

const PRIVACY = page("Privacy Policy", `
<h1>Privacy Policy</h1>
<p>This policy covers the Ra'ij publishing tool ("the tool") and this website.</p>
<h2>Who uses the tool</h2>
<p>Only its owner. The tool is not offered to the public; there are no user accounts, sign-ups or third-party users.
Platform connections (YouTube, TikTok, Instagram, Facebook) are made by the owner for the owner's own channels.</p>
<h2>What data the tool handles</h2>
<ul>
  <li><strong>Platform access tokens</strong> issued to the owner after they sign in with each platform. They are stored
      only on the owner's own computer, used solely to upload the owner's videos and read the owner's own channel
      statistics, and can be revoked at any time from the platform's app settings.</li>
  <li><strong>Public information</strong> about trending topics and news articles, used to research and write the
      videos. No personal data about viewers is collected.</li>
  <li><strong>Channel statistics</strong> (views, likes) of the owner's own posts, aggregated for the owner's weekly
      report.</li>
</ul>
<h2>What the tool does not do</h2>
<ul>
  <li>It does not collect, store or share personal data of viewers or of any third party.</li>
  <li>It does not sell data, show ads, or use tracking on this website. This site sets no cookies.</li>
  <li>It does not post on behalf of anyone other than the owner, and never without the owner's review.</li>
</ul>
<h2>Third-party platforms</h2>
<p>Uploads go directly to the respective platform's API. Their handling of the published content is governed by their
own policies: <a href="https://www.tiktok.com/legal/privacy-policy">TikTok</a>,
<a href="https://policies.google.com/privacy">Google/YouTube</a>, <a href="https://www.facebook.com/privacy/policy">Meta</a>.</p>
<h2>Deleting data</h2>
<p>Revoking the tool's access in a platform's settings ends its ability to act on that account; the locally stored
token is then useless and is deleted. Ask through the channel contact for anything else.</p>
<p class="ar">الأداة شخصية ولا تجمع أي بيانات عن المشاهدين. رموز الدخول تُحفظ على جهاز المالك فقط وتُستخدم لنشر
فيديوهاته على قنواته، ويمكن إلغاؤها في أي وقت من إعدادات المنصة.</p>`);

const TERMS = page("Terms of Service", `
<h1>Terms of Service</h1>
<p>The Ra'ij tool is operated by and for its owner. It is not offered as a service to others, and no one else may use
it to post content.</p>
<h2>Content</h2>
<p>Videos are original Arabic retellings of publicly reported information, produced with licensed stock footage,
Creative-Commons or public-domain images credited in the caption, and a synthetic voice. Content generated with AI
assistance is labelled where the platform provides a label. The owner reviews every video before publication and is
responsible for what is published on the owner's channels.</p>
<h2>Platform rules</h2>
<p>Posting follows each platform's terms and community guidelines, including TikTok's Content Sharing Guidelines and
YouTube's Terms of Service. Nothing in this tool overrides them.</p>
<h2>Liability</h2>
<p>This website is informational and provided as is. Links to third-party sites are provided for convenience.</p>
<h2>Changes</h2>
<p>These terms and the privacy policy may be updated; the date at the bottom of each page shows the current version.</p>
<p class="ar">الأداة للاستخدام الشخصي لمالكها فقط، وكل المحتوى يُراجَع قبل النشر ويلتزم بشروط المنصات.</p>`);

const esc = (v) => String(v || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function callback(url) {
  const q = url.searchParams;
  const code = q.get("code"), state = q.get("state"), error = q.get("error");
  if (error) {
    return page("TikTok: not authorized", `<h1>TikTok did not authorize</h1>
<p><code>${esc(error)}</code> ${esc(q.get("error_description"))}</p><p>Close this tab and run <code>tiktok-auth</code> again.</p>`);
  }
  if (!code) return page("TikTok callback", `<h1>TikTok callback</h1><p>Nothing to do here — this page receives the code after you approve Ra'ij on TikTok.</p>`);
  const cmd = `uv run python -m src.main tiktok-auth --code "${code}" --state "${state || ""}"`;
  return page("TikTok: one more step", `<h1>TikTok approved Ra'ij ✅</h1>
<p>Last step: run this command on the Mac (paste it into the Claude session with a leading <code>!</code>, or into Terminal in the project folder):</p>
<pre id="cmd" style="white-space:pre-wrap;word-break:break-all;background:#f4f4f4;padding:12px;border-radius:8px">${esc(cmd)}</pre>
<p><button onclick="navigator.clipboard.writeText(document.getElementById('cmd').textContent).then(()=>{this.textContent='Copied ✓'})">Copy command</button></p>
<p style="color:#666">The code is single-use and expires in a few minutes. Nothing is stored on this site.</p>`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;
    if (pathname === "/tiktok/callback" || pathname === "/tiktok/callback/") {
      return new Response(callback(url), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }
    const html = (body) => new Response(body, { headers: { "content-type": "text/html; charset=utf-8",
                                                           "cache-control": "public, max-age=3600" } });
    if (pathname === "/" ) return html(HOME);
    if (pathname === "/privacy" || pathname === "/privacy/") return html(PRIVACY);
    if (pathname === "/terms" || pathname === "/terms/") return html(TERMS);
    if (env.TIKTOK_VERIFY_PATH && pathname === "/" + env.TIKTOK_VERIFY_PATH.replace(/^\/+/, "")) {
      return new Response(env.TIKTOK_VERIFY_BODY || "", { headers: { "content-type": "text/plain" } });
    }
    return new Response("Not found", { status: 404 });
  },
};
