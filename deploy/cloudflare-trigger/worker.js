// Every cron tick: POST a workflow_dispatch for the raij workflow. The workflow's `concurrency` group keeps
// one run at a time (a second dispatch just waits as the single pending run), and its RAIJ_ENABLED check
// makes disabled runs skip in seconds, so blind dispatching is safe. No HTTP route: fetch() only reports.
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  async fetch(request, env) {
    return new Response("raij-trigger: dispatches the GitHub workflow every 10 min (cron only)", { status: 200 });
  },
};

async function dispatch(env) {
  if (!env.GH_TOKEN) {
    console.error("GH_TOKEN secret is not set (npx wrangler secret put GH_TOKEN)");
    return;
  }
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "raij-trigger (Cloudflare Worker)",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF, inputs: { command: "tick" } }),
  });
  if (resp.status === 204) {
    console.log(`dispatched ${env.GH_WORKFLOW} on ${env.GH_REPO}`);
  } else {
    // 401/403: token expired or lacks Actions: write; 404: repo/workflow name; 422: bad ref/inputs.
    console.error(`dispatch failed: HTTP ${resp.status} ${(await resp.text()).slice(0, 200)}`);
  }
}
