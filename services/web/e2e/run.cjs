"use strict";
// Browser end-to-end tests: a researcher signs up, runs an AgentSociety simulation,
// talks to the AI scientist, publishes a dataset, invites a viewer; an admin reviews.
//
// Runs against a control plane with the mock model provider and a sandbox driver
// (scripts/e2e.sh starts a throwaway one). Steps drive the real UI; the API is only
// used to set up data that has no UI of its own and to check outcomes.
//
//   E2E_BASE_URL=http://127.0.0.1:8765 node e2e/run.cjs [step ...]

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const BASE = (process.env.E2E_BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const ADMIN = { email: process.env.E2E_ADMIN_EMAIL || "admin@example.com", password: process.env.E2E_ADMIN_PASSWORD || "admin-password-123" };
const OUT = path.resolve(process.env.E2E_ARTIFACTS || path.join(__dirname, "..", "e2e-artifacts"));
const FIXTURES = path.join(__dirname, "fixtures");
const SKIP_CHAT = process.env.E2E_SKIP_CHAT === "1";
const ONLY = new Set(process.argv.slice(2));
const run = Date.now().toString(36);
const researcher = { name: "Ada Researcher", email: `ada+${run}@example.com`, password: "e2e-password-123" };
const state = {};

// ----------------------------------------------------------------------- helpers

async function api(method, url, { token, body, raw } = {}) {
  const r = await fetch(`${BASE}${url}`, {
    method,
    headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(body ? { "content-type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (raw) return r;
  const text = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function login({ email, password }) {
  return (await api("POST", "/api/v1/auth/login", { body: { email, password } })).access_token;
}

async function until(what, fn, { timeout = 120_000, every = 2000 } = {}) {
  const end = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, every));
  }
}

/** Records browser errors and failed requests for the step that is running. */
function watch(page, problems) {
  page.on("pageerror", (e) => problems.push(`page error: ${String(e).slice(0, 300)}`));
  page.on("console", (m) => {
    // Failed requests are reported (with their URL) by the response handler.
    if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) problems.push(`console: ${m.text().slice(0, 300)}`);
  });
  page.on("response", (r) => {
    const url = r.url().replace(BASE, "");
    if (r.status() >= 500 || (r.status() >= 400 && !url.startsWith("/api/v1/auth/refresh"))) problems.push(`HTTP ${r.status()} ${r.request().method()} ${url}`);
  });
}

async function newPage(browser, problems) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  watch(page, problems);
  return page;
}

async function signIn(page, { email, password }) {
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${BASE}/`);
}

const sbx = (pathPart) => `/api/v1/projects/${state.projectId}/sbx/${pathPart}`;

// ------------------------------------------------------------------------- steps

const steps = [
  [
    "sign up and create a project",
    async ({ page }) => {
      await page.goto(`${BASE}/signup`);
      await page.getByLabel("Name").fill(researcher.name);
      await page.getByLabel("Email").fill(researcher.email);
      await page.getByLabel("Password").fill(researcher.password);
      await page.getByRole("button", { name: "Create account" }).click();
      await page.waitForURL(`${BASE}/`);
      await page.getByRole("button", { name: "New project" }).click();
      await page.getByLabel("Project name").fill("Echo chambers");
      await page.getByLabel("Research question").fill("How do recommendation feeds create echo chambers over time?");
      await page.getByRole("button", { name: "Create project" }).click();
      await page.waitForURL(/\/p\/prj_[a-z0-9]+/);
      state.projectId = page.url().match(/\/p\/(prj_[a-z0-9]+)/)[1];
      state.token = await login(researcher);
      await page.waitForSelector("text=Research pipeline", { timeout: 180_000 });
    },
  ],
  [
    "files",
    async ({ page }) => {
      await api("PUT", sbx("files/content"), { token: state.token, body: { path: "notes/e2e.md", content: "# Field notes\n\nThe feed ranks by engagement." } });
      await page.goto(`${BASE}/p/${state.projectId}/files?path=${encodeURIComponent("notes/e2e.md")}`);
      await page.waitForSelector("text=The feed ranks by engagement.");
      await page.goto(`${BASE}/p/${state.projectId}/files?path=TOPIC.md`);
      await page.waitForSelector("text=How do recommendation feeds create echo chambers over time?");
    },
  ],
  [
    "run a simulation and replay it",
    async ({ page }) => {
      const experiment = JSON.parse(fs.readFileSync(path.join(FIXTURES, "experiment.json"), "utf8"));
      const hypothesis = "# Engagement-ranked feeds polarise\n\nAgents with engagement-ranked feeds drift further apart.\n";
      await api("PUT", sbx("files/content"), { token: state.token, body: { path: "hypothesis_1/HYPOTHESIS.md", content: hypothesis } });
      const config = await api("PUT", sbx("experiments/1/1/config"), { token: state.token, body: { ...experiment, experiment_md: "# Baseline\n" } });
      assert.equal(config.valid, true, `experiment config invalid: ${JSON.stringify(config).slice(0, 300)}`);
      await page.goto(`${BASE}/p/${state.projectId}/experiments`);
      await page.waitForSelector("text=Hypotheses & experiments");
      await page.getByRole("button", { name: "Run", exact: true }).first().click();
      const finished = await until(
        "the simulation to finish",
        async () => {
          const st = await api("GET", sbx("experiments/1/1"), { token: state.token });
          return !st.is_running && ["completed", "failed", "stopped"].includes(st.status) ? st : null;
        },
        { timeout: 900_000, every: 5000 },
      );
      assert.equal(finished.status, "completed", `simulation ${finished.status}: ${finished.error}`);
      assert.ok(finished.has_replay, "the run wrote no replay data");
      await page.goto(`${BASE}/p/${state.projectId}/replay/1/1`);
      await page.waitForSelector("text=Alice", { timeout: 60_000 });
    },
  ],
  [
    "ask the AI scientist",
    async ({ page }) => {
      if (SKIP_CHAT) return "skipped (E2E_SKIP_CHAT=1)";
      await page.goto(`${BASE}/p/${state.projectId}/chat`);
      const box = page.getByPlaceholder(/Ask the AI social scientist/);
      await box.fill("Summarise the research question in one sentence.");
      await box.press("Enter");
      await page.waitForSelector("text=/Mock response/", { timeout: 180_000 });
    },
  ],
  [
    "research tools",
    async ({ page }) => {
      await page.goto(`${BASE}/p/${state.projectId}/tools`);
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await page.locator(".ant-drawer .ant-tag", { hasText: "succeeded" }).waitFor({ timeout: 180_000 });
      await page.waitForSelector("text=current_stage");
    },
  ],
  [
    "project settings",
    async ({ page }) => {
      await page.goto(`${BASE}/p/${state.projectId}/settings`);
      const card = page.locator(".ant-card", { hasText: "Description" }).first();
      await card.getByLabel("Description").fill("End-to-end test project");
      await card.getByRole("button", { name: "Save" }).click();
      await page.waitForSelector("text=Project saved");
      await page.waitForSelector("text=Simulation engine");
      const project = await api("GET", `/api/v1/projects/${state.projectId}`, { token: state.token });
      assert.equal(project.description, "End-to-end test project");
    },
  ],
  [
    "publish a dataset and use it in the project",
    async ({ page }) => {
      state.datasetId = `trust-survey-${run}`;
      await page.goto(`${BASE}/datasets`);
      await page.getByRole("button", { name: "New dataset" }).click();
      await page.getByLabel("Name").fill("Media trust survey");
      await page.getByLabel("ID").fill(state.datasetId);
      await page.getByLabel("Description").fill("Four respondents.");
      await page.getByRole("button", { name: "Next" }).click();
      await page.locator(".ant-upload-drag input[type=file]").first().setInputFiles([path.join(FIXTURES, "survey.csv")]);
      await page.waitForSelector("text=data/survey.csv");
      await page.getByRole("button", { name: "Upload" }).click();
      await page.waitForURL(`${BASE}/datasets/${state.datasetId}`, { timeout: 120_000 });
      await page.getByRole("tab", { name: /Files/ }).click();
      await page.waitForSelector("text=data/survey.csv");
      await page.getByRole("button", { name: "Add to a project" }).click();
      await page.locator(".ant-modal .ant-select").click();
      await page.locator(".ant-select-item-option", { hasText: "Echo chambers" }).click();
      await page.getByRole("button", { name: "Download into workspace" }).click();
      await page.waitForSelector(`text=Saved to datasets/${state.datasetId}/`, { timeout: 300_000 });
      const csv = await api("GET", `${sbx("files/content")}?path=${encodeURIComponent(`datasets/${state.datasetId}/data/survey.csv`)}`, { token: state.token });
      assert.match(csv.content, /respondent_id,age/);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Publish publicly…" }).click();
      await page.locator(".ant-popconfirm").getByRole("button", { name: "OK" }).click();
      await page.waitForSelector("text=In review");
    },
  ],
  [
    "organization library and literature search",
    async ({ page }) => {
      await page.goto(`${BASE}/literature`);
      await page.getByRole("tab", { name: /Organization library/ }).click();
      await page.getByRole("button", { name: "Add document" }).click();
      const modal = page.locator(".ant-modal");
      await modal.getByLabel("Title").fill(`Notes on filter bubbles ${run}`);
      await modal.locator("textarea").fill("Engagement optimisation amplifies homophily and polarisation in agent-based models.");
      await modal.getByRole("button", { name: "Add", exact: true }).click();
      await page.waitForSelector("text=Document added");
      await page.getByRole("tab", { name: "Search" }).click();
      await page.locator("#org-literature-search").getByLabel("Query").fill("filter bubbles homophily");
      await page.locator("#org-literature-search button[type=submit]").click();
      await page.waitForSelector(`text=Notes on filter bubbles ${run}`, { timeout: 60_000 });
    },
  ],
  [
    "organization pages and API keys",
    async ({ page }) => {
      for (const [url, text] of [
        ["/org/members", "Members"],
        ["/org/billing", "Credit history"],
        ["/org/usage", "Daily spend"],
        ["/org/models", "Models available to your projects"],
      ]) {
        await page.goto(`${BASE}${url}`);
        await page.waitForSelector(`text=${text}`);
      }
      await page.goto(`${BASE}/org/api-keys`);
      await page.getByPlaceholder(/Key name/).fill("e2e");
      await page.getByRole("button", { name: "Create key" }).click();
      const secret = (await page.locator(".ant-modal .mono").first().innerText()).trim();
      assert.match(secret, /^sk_/);
      await page.getByRole("button", { name: "Done" }).click();
      // The key works for the REST API and the OpenAI-compatible gateway.
      const me = await api("GET", "/api/v1/auth/me", { token: state.token });
      const projects = await api("GET", `/api/v1/orgs/${me.orgs[0].id}/projects`, { token: secret });
      assert.ok(projects.projects.some((p) => p.id === state.projectId));
      const reply = await api("POST", "/llm/v1/chat/completions", { token: secret, body: { model: "mock", messages: [{ role: "user", content: "hi" }] } });
      assert.match(reply.choices[0].message.content, /Mock response/);
    },
  ],
  [
    "invite a viewer",
    async ({ page, browser, problems }) => {
      await page.goto(`${BASE}/org/members`);
      await page.getByRole("button", { name: "Invite" }).click();
      const viewer = { name: "Vic Viewer", email: `vic+${run}@example.com`, password: "e2e-password-123" };
      await page.locator(".ant-modal").getByLabel("Email").fill(viewer.email);
      await page.locator(".ant-modal .ant-select").click();
      await page.locator(".ant-select-item-option", { hasText: "Viewer" }).click();
      await page.getByRole("button", { name: "Create invitation" }).click();
      const link = (await page.locator(".ant-modal .mono").first().innerText()).trim();
      await page.getByRole("button", { name: "Done" }).click();
      const guest = await newPage(browser, problems);
      await guest.goto(link.replace(/^https?:\/\/[^/]+/, BASE));
      await guest.getByRole("button", { name: "Create account" }).click();
      await guest.getByLabel("Name").fill(viewer.name);
      await guest.getByLabel("Email").fill(viewer.email);
      await guest.getByLabel("Password").fill(viewer.password);
      await guest.getByRole("button", { name: "Create account" }).click();
      await guest.waitForURL(`${BASE}/`);
      await guest.waitForSelector("text=Echo chambers");
      assert.equal(await guest.getByRole("button", { name: "New project" }).count(), 0, "a viewer can create projects");
      await guest.goto(`${BASE}/p/${state.projectId}/settings`);
      await guest.waitForSelector("text=Simulation engine");
      assert.ok(await guest.locator(".ant-card", { hasText: "Description" }).first().getByLabel("Name").isDisabled(), "a viewer can edit settings");
      await guest.context().close();
    },
  ],
  [
    "admin reviews the dataset",
    async ({ browser, problems }) => {
      const admin = await newPage(browser, problems);
      await signIn(admin, ADMIN);
      await admin.goto(`${BASE}/admin/datasets`);
      const row = admin.locator("tr", { hasText: state.datasetId });
      await row.getByRole("button", { name: "Review" }).click();
      await admin.getByRole("button", { name: "Approve and publish" }).click();
      await admin.waitForSelector("text=Published");
      for (const tab of ["overview", "orgs", "users", "models", "audit"]) {
        await admin.goto(`${BASE}/admin/${tab}`);
        await admin.waitForSelector("text=Administration");
      }
      await admin.context().close();
      const listed = await api("GET", "/api/v1/data/datasets?q=Media%20trust");
      assert.ok(listed.datasets.some((d) => d.id === state.datasetId), "the approved dataset is not public");
    },
  ],
];

// -------------------------------------------------------------------------- main

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const problems = [];
  const page = await newPage(browser, problems);
  const results = [];
  let failed = false;
  try {
    for (const [name, fn] of steps) {
      if (ONLY.size && !ONLY.has(name)) continue;
      if (failed) {
        results.push({ name, status: "not run" });
        continue;
      }
      problems.length = 0;
      const started = Date.now();
      const slug = name.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
      try {
        const note = await fn({ page, browser, problems });
        await page.waitForTimeout(500);
        if (problems.length) throw new Error(`browser problems:\n  ${[...new Set(problems)].join("\n  ")}`);
        results.push({ name, status: note ? "skipped" : "passed", seconds: (Date.now() - started) / 1000, note });
        await page.screenshot({ path: path.join(OUT, `${slug}.png`) });
      } catch (error) {
        failed = true;
        results.push({ name, status: "FAILED", seconds: (Date.now() - started) / 1000, error: String(error?.stack || error) });
        await page.screenshot({ path: path.join(OUT, `${slug}-failed.png`) }).catch(() => {});
      }
    }
  } finally {
    if (state.projectId && state.token) {
      await api("POST", `/api/v1/projects/${state.projectId}/sandbox/stop`, { token: state.token, raw: true }).catch(() => {});
    }
    await browser.close();
  }
  for (const r of results) {
    console.log(`${r.status.padEnd(8)} ${r.name}${r.seconds !== undefined ? ` (${r.seconds.toFixed(1)}s)` : ""}${r.note ? ` - ${r.note}` : ""}`);
    if (r.error) console.log(`         ${r.error.split("\n").join("\n         ")}`);
  }
  console.log(`screenshots: ${OUT}`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
