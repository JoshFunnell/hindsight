/**
 * STM view-text parse + grouping.
 *
 * Contract is live/stm_ui.py (parse_view_lines / group_by_kind / health_lines).
 * The sidecar endpoints and fields ARE the contract -- do not invent a second one.
 *
 * Browser fetches go to http://127.0.0.1:8790 only. The control-plane process
 * inside the container cannot reach the host's 127.0.0.1:8790 (that address is
 * the container loopback). Do not proxy STM through /api.
 */

export const STM_API_BASE = "http://127.0.0.1:8790";
// Fallback ONLY -- reached when GET /health is unreachable. The live budget is
// /health.view_budget, which resolveBudget() prefers. Aligned 20000 -> 25000 on
// 2026-08-19 to match the live stm_client.DEFAULT_BUDGET (D:\HQ_runtime\stm_client.py:41),
// so the degraded path cannot silently truncate below the real cap.
export const DEFAULT_BUDGET = 25000;
export const REFRESH_MS = 5000;
export const STALE_PREFIX = "STM STALE-AT ";
export const LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1"] as const;

export const KIND_ORDER = [
  "HANDOVER",
  "GOAL",
  "INFLIGHT",
  "PROMISE",
  "DECIDED",
  "STEER",
  "RESIDUAL",
  "JOB",
  "MEASUREMENT",
  "PING",
  "INBOX",
  "META",
] as const;

export type StmKind = (typeof KIND_ORDER)[number] | string;

export interface StmRow {
  id: string;
  clock: string;
  clockUtc?: string;
  kind: string;
  text: string;
}

export interface HealthPayload {
  ok?: boolean;
  db_ok?: boolean;
  rows?: unknown;
  last_write?: unknown;
  promote_queue?: unknown;
  inbox_queue?: unknown;
  hindsight_reachable?: unknown;
  view_budget?: unknown;
  default_project?: unknown;
  [key: string]: unknown;
}

// id local utc KIND rest -- SIUPGRADE2 (local HH:MM:SS + UTC HH:MM:SSZ)
export const VIEW_LINE_RE =
  /^(\S+) (\d{2}:\d{2}:\d{2}) (\d{2}:\d{2}:\d{2}Z) ([A-Z][A-Z0-9]*)(?: (.*))?$/;
export const VIEW_LINE_RE_LEGACY =
  /^(\S+) (\d{2}:\d{2}:\d{2}Z) ([A-Z][A-Z0-9]*)(?: (.*))?$/;

const ABS_URL_RE = /https?:\/\/[^\s"'<>\\]+/gi;

export function parseViewLines(text: string): StmRow[] {
  const rows: StmRow[] = [];
  for (const raw of (text || "").split(/\n/)) {
    const line = raw.replace(/\r$/, "");
    if (!line.trim()) continue;
    const m = line.match(VIEW_LINE_RE);
    if (m) {
      rows.push({
        id: m[1],
        clock: m[2],
        clockUtc: m[3],
        kind: m[4],
        text: m[5] || "",
      });
      continue;
    }
    const old = line.match(VIEW_LINE_RE_LEGACY);
    if (old) {
      rows.push({
        id: old[1],
        clock: old[2],
        clockUtc: old[2],
        kind: old[3],
        text: old[4] || "",
      });
    } else {
      rows.push({
        id: "",
        clock: "",
        kind: "META",
        text: line,
      });
    }
  }
  return rows;
}

export function groupByKind(rows: StmRow[]): Array<[string, StmRow[]]> {
  const buckets: Record<string, StmRow[]> = {};
  for (const rec of rows) {
    if (!buckets[rec.kind]) buckets[rec.kind] = [];
    buckets[rec.kind].push(rec);
  }
  const out: Array<[string, StmRow[]]> = [];
  const seen: Record<string, number> = {};
  for (const kind of KIND_ORDER) {
    if (buckets[kind]) {
      out.push([kind, buckets[kind]]);
      seen[kind] = 1;
    }
  }
  for (const kind of Object.keys(buckets)) {
    if (!seen[kind]) out.push([kind, buckets[kind]]);
  }
  return out;
}

export function healthLines(health: HealthPayload | null | undefined, code: number): string {
  if (!health) return "health unreachable (http " + code + ")";
  return (
    "ok=" +
    health.ok +
    " db_ok=" +
    health.db_ok +
    " rows=" +
    health.rows +
    " last_write=" +
    health.last_write +
    " promote_queue=" +
    health.promote_queue +
    " inbox_queue=" +
    health.inbox_queue +
    " hindsight_reachable=" +
    health.hindsight_reachable
  );
}

export function urlHost(url: string): string {
  const raw = (url || "").trim();
  if (!raw || raw.startsWith("#") || raw.startsWith("/") || raw.startsWith(".") || raw.startsWith("?")) {
    return "";
  }
  try {
    return new URL(raw).hostname.toLowerCase();
  } catch {
    return "";
  }
}

export function isLoopbackUrl(url: string): boolean {
  const host = urlHost(url);
  if (!host) return true;
  return (LOOPBACK_HOSTS as readonly string[]).includes(host);
}

export function urlsIn(text: string): string[] {
  const found: string[] = [];
  const abs = text.match(ABS_URL_RE) || [];
  found.push(...abs);
  const attrRe = /(?:href|src|action)\s*=\s*["']([^"']+)["']/gi;
  let m: RegExpExecArray | null;
  while ((m = attrRe.exec(text))) found.push(m[1]);
  const fetchRe = /fetch\s*\(\s*["']([^"']+)["']/gi;
  while ((m = fetchRe.exec(text))) found.push(m[1]);
  return found;
}

export function normalizeApiBase(raw: string | null | undefined): string {
  const text = (raw || STM_API_BASE).trim() || STM_API_BASE;
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    throw new Error("api_base must be http loopback");
  }
  if (parsed.protocol !== "http:") {
    throw new Error("api_base must be http loopback");
  }
  const host = (parsed.hostname || "").toLowerCase();
  if (!(LOOPBACK_HOSTS as readonly string[]).includes(host)) {
    throw new Error("api_base host must be loopback (got " + JSON.stringify(host) + ")");
  }
  if (!parsed.port) return "http://" + host;
  return "http://" + host + ":" + parsed.port;
}

export function viewUrl(apiBase: string, project: string, budget: number): string {
  const q: string[] = [];
  if (project) q.push("project=" + encodeURIComponent(project));
  if (budget) q.push("budget=" + encodeURIComponent(String(budget)));
  return apiBase + "/stm/view" + (q.length ? "?" + q.join("&") : "");
}

/** Derived operator projection. Default ON for :19999/stm; raw /stm/view stays one click away. */
export const DEFAULT_OPERATOR_VIEW = true;

export function operatorViewUrl(
  apiBase: string,
  project: string,
  format: string = "html"
): string {
  const q: string[] = [];
  if (project) q.push("project=" + encodeURIComponent(project));
  if (format) q.push("format=" + encodeURIComponent(format));
  return normalizeApiBase(apiBase) + "/stm/view-operator" + (q.length ? "?" + q.join("&") : "");
}

/** First line of /stm/view when the sidecar rendered render_view_header. */
export const VIEW_HEADER_PREFIX = "STM project=";

/** Client-computed `budget 17469/18000` — the second count this page must never emit. */
export const CLIENT_BUDGET_COUNT_RE = /\bbudget\s+\d+\s*\/\s*\d+/;

export function extractViewHeader(text: string): string | null {
  const first = (text || "").split(/\n/, 1)[0].replace(/\r$/, "");
  if (first.startsWith(VIEW_HEADER_PREFIX)) return first;
  return null;
}

export function viewWithoutHeader(text: string): string {
  const raw = text || "";
  const first = raw.split(/\n/, 1)[0];
  if (!first.replace(/\r$/, "").startsWith(VIEW_HEADER_PREFIX)) return raw;
  const nl = raw.indexOf("\n");
  return nl === -1 ? "" : raw.slice(nl + 1);
}

export function budgetLabel(viewText: string): string {
  return extractViewHeader(viewText) || "";
}

export function formatBudgetDiv(viewText: string): string {
  return '<div id="budget" class="budget">' + budgetLabel(viewText) + "</div>";
}

export function resolveBudget(
  budgetParam: string | null | undefined,
  health: HealthPayload | null | undefined,
  fallback = DEFAULT_BUDGET
): number {
  if (budgetParam != null) return parseInt(budgetParam, 10) || fallback;
  const n = Number(health && health.view_budget);
  if (Number.isFinite(n) && n > 0) return n;
  return fallback;
}

export function resolveProject(
  projectParam: string | null | undefined,
  health: HealthPayload | null | undefined,
  current = ""
): string {
  if (projectParam != null && projectParam !== "") return projectParam;
  return String((health && health.default_project) || current || "");
}

/**
 * The literal that means "merged view across every project".
 *
 * It must never be sent as "" . Since 2026-08-18 the sidecar treats a MISSING
 * ?project= as the DEFAULT project (stm_service do_GET /stm/view), and keeps the
 * merged view behind this exact literal -- and viewUrl() drops a falsy project.
 * So "" does not mean "all", it means "hq-e694d972". Emitting "" here is the
 * bug that made the merged view unreachable from the :8790 page.
 */
export const PROJECT_ALL = "all";

export interface ProjectInfo {
  slug?: string;
  /** Presentation name computed by the sidecar from the stable slug (DESIGN-STM-NAMING.md). */
  display?: unknown;
  rows?: unknown;
  owner_agents?: unknown;
  [key: string]: unknown;
}

export function projectsUrl(apiBase: string): string {
  return apiBase + "/projects";
}

export interface ProjectOption {
  value: string;
  label: string;
}

/**
 * Options for the switcher: "all" first, then each distinct slug in SERVER order (the
 * sidecar sorts by rank, then recency -- DESIGN-STM-NAMING.md), labelled with the
 * sidecar's `display` name when it sends one and the raw slug otherwise, plus the row
 * count in brackets: "HQ (main) [884]", "job: stmui4 [3]". The option VALUE is always
 * the slug -- the display name is presentation only and never a key. The bracket form
 * is the same one stm_ui.py's switcher_html / switcherHtml use, so :8790 and the
 * control plane read identically.
 */
export function projectOptions(
  projects: ProjectInfo[] | null | undefined,
  current: string
): ProjectOption[] {
  const out: ProjectOption[] = [{ value: PROJECT_ALL, label: "(all projects merged)" }];
  const seen: Record<string, number> = {};
  for (const rec of projects || []) {
    const slug = String((rec && rec.slug) || "");
    if (!slug || seen[slug]) continue;
    seen[slug] = 1;
    const rows = rec && rec.rows;
    const name = String((rec && rec.display) || slug);
    out.push({ value: slug, label: rows == null ? name : name + " [" + rows + "]" });
  }
  // A project the switcher has not heard of (fresh slug, /projects down) must
  // still be selectable, or the <select> would silently snap to another value.
  if (current && current !== PROJECT_ALL && !seen[current]) {
    out.push({ value: current, label: current });
  }
  return out;
}

/**
 * Background-job lanes, served by the sidecar from stm_jobs.py: STM rows of
 * kind JOB plus the grok / agy / watcher lanes on disk.
 *
 * The STATE values are DERIVED there, copying grok_detached.job_metrics rather
 * than reinventing them -- including the 3h pid cap that keeps a recycled pid
 * from being reported as a job still running (measured 2026-08-15: several
 * 08-12 jobs claimed 75h of runtime). This module only renders what it is told;
 * it must never recompute a state client-side.
 */
export function jobsUrl(apiBase: string): string {
  return apiBase + "/stm/jobs/all";
}

export interface JobRow {
  lane?: string;
  id?: string;
  state?: string;
  title?: string;
  detail?: string;
  last_line?: string;
  started_iso?: string;
  age_s?: number | null;
  idle_s?: number | null;
  pid?: unknown;
  [key: string]: unknown;
}

export interface JobsPayload {
  jobs?: JobRow[];
  counts?: Record<string, number>;
  active?: number;
  generated_at?: string;
  stall_min?: number;
  [key: string]: unknown;
}

/** Attention order, same ranking stm_jobs._STATE_RANK uses. STALLED outranks
 * RUNNING on purpose: a stalled job is the one needing a decision. */
export const JOB_STATE_ORDER = [
  "STALLED",
  "RUNNING",
  "CLAIMED",
  "QUEUED",
  "STALE",
  "FAILED",
  "DONE",
  "UNKNOWN",
] as const;

export const JOB_ACTIVE_STATES = ["RUNNING", "STALLED", "CLAIMED", "QUEUED", "STALE"];

/**
 * Keep the previous payload unless the new one is genuinely usable.
 *
 * This IS the failure posture, extracted so it can be tested: /stm/jobs/all
 * stats the filesystem and is the most likely of the four fetches to fail, and
 * a failure there must never blank the STM half of the page. Returning `prev`
 * means a transient error shows slightly stale jobs rather than an empty page.
 */
export function coerceJobsPayload(
  raw: unknown,
  prev: JobsPayload | null
): JobsPayload | null {
  if (!raw || typeof raw !== "object") return prev;
  const jobs = (raw as JobsPayload).jobs;
  if (!Array.isArray(jobs)) return prev;
  return raw as JobsPayload;
}

/** Jobs to show: active only, unless the operator asked for finished ones. */
export function visibleJobs(
  jobs: JobRow[] | null | undefined,
  showDone: boolean
): JobRow[] {
  const all = jobs || [];
  if (showDone) return all;
  return all.filter((j) => JOB_ACTIVE_STATES.indexOf(String(j && j.state)) !== -1);
}

/** Per-lane totals for the summary pills (states come from the server). */
export function laneCounts(jobs: JobRow[] | null | undefined): Array<[string, number]> {
  const out: Record<string, number> = {};
  for (const j of jobs || []) {
    const lane = String((j && j.lane) || "?");
    out[lane] = (out[lane] || 0) + 1;
  }
  return Object.keys(out)
    .sort()
    .map((k) => [k, out[k]] as [string, number]);
}

/** Compact age: 45s / 12m / 3h / 2d. */
export function fmtAge(seconds: unknown): string {
  if (seconds === null || seconds === undefined) return "";
  const s = typeof seconds === "number" ? seconds : parseInt(String(seconds), 10);
  if (!Number.isFinite(s) || s < 0) return "";
  if (s < 90) return s + "s";
  if (s < 5400) return Math.floor(s / 60) + "m";
  if (s < 172800) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

/**
 * work/2 union served by GET {apiBase}/work (stm_work.collect).
 *
 * Mirror of stm_ui.work_html / workHtml. Read-only. A /work failure must
 * never throw here -- coerceWorkPayload returns null and the page keeps
 * the previous payload (same posture as coerceJobsPayload, minus prev
 * because the brief pins this signature).
 */
export function workUrl(apiBase: string): string {
  return normalizeApiBase(apiBase) + "/work";
}

export const JOB_PILL_STATES = ["RUNNING", "STALLED", "QUEUED", "CLAIMED"] as const;

export interface WorkItem {
  tier?: string;
  source?: string;
  lane?: string;
  kind?: string;
  id?: string;
  title?: string;
  state?: string;
  project?: string;
  /**
   * Presentation name stamped by the sidecar (stm_work._record via
   * stm_service.project_display). Optional -- an older /work payload
   * omits it and the heading falls back to `project`. Never a grouping
   * key.
   */
  project_display?: string;
  owner?: string;
  age_s?: number | null;
  idle_s?: number | null;
  started_iso?: string | null;
  detail?: string | null;
  last_line?: string | null;
  tokens?: unknown;
  pid?: unknown;
  link?: { id?: string } | null;
  [key: string]: unknown;
}

export interface WorkCounts {
  now?: number;
  backlog?: number;
  finished?: number;
  orphan?: number;
  unparsed?: number | null;
  linked?: number;
  open?: number;
  RUNNING?: number;
  STALLED?: number;
  QUEUED?: number;
  CLAIMED?: number;
  [key: string]: unknown;
}

export interface WorkLink {
  job_id?: string;
  work_id?: string;
}

export interface WorkPayload {
  schema?: string;
  now?: WorkItem[];
  backlog?: WorkItem[];
  finished?: WorkItem[];
  orphan?: WorkItem[];
  linked?: WorkLink[];
  unparsed?: number | null;
  counts?: WorkCounts;
  errors?: string[];
  generated_at?: string;
  cache_age_s?: number;
  sources?: unknown;
  design?: string;
  read_only?: boolean;
  [key: string]: unknown;
}

export interface WorkPill {
  state: string;
  label: string;
  count: number | string;
}

function asCount(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Null unless `x` is an object (not array) whose now/backlog/finished/orphan/
 * linked (and legacy hq/stm/operator_local) are each an array or missing, and
 * whose counts is an object or missing. Never throws.
 */
export function coerceWorkPayload(x: unknown): WorkPayload | null {
  if (x === null || x === undefined) return null;
  if (typeof x !== "object" || Array.isArray(x)) return null;
  const rec = x as Record<string, unknown>;
  for (const key of ["now", "backlog", "finished", "orphan", "linked", "hq", "stm", "operator_local"]) {
    const v = rec[key];
    if (v !== undefined && !Array.isArray(v)) return null;
  }
  const counts = rec.counts;
  if (
    counts !== undefined &&
    (counts === null || typeof counts !== "object" || Array.isArray(counts))
  ) {
    return null;
  }
  return rec as WorkPayload;
}

/**
 * Pills for the one JOB TRACKER header. Job states omitted at 0; now/backlog/finished/open
 * always; orphan / unparsed / linked omitted at 0 (unparsed ? when null).
 */
export function workPills(counts: WorkCounts | null | undefined): WorkPill[] {
  const c = counts || {};
  const pills: WorkPill[] = [];
  for (const st of JOB_PILL_STATES) {
    const n = asCount(c[st]);
    if (!n) continue;
    pills.push({ state: st, label: st, count: n });
  }
  pills.push({ state: "RUNNING", label: "now", count: asCount(c.now) });
  pills.push({ state: "QUEUED", label: "backlog", count: asCount(c.backlog) });
  pills.push({ state: "DONE", label: "finished", count: asCount(c.finished) });
  const orphan = asCount(c.orphan);
  if (orphan) pills.push({ state: "STALE", label: "orphan", count: orphan });
  if (c.unparsed) pills.push({ state: "STALLED", label: "unparsed", count: asCount(c.unparsed) });
  else if (c.unparsed === null) pills.push({ state: "STALLED", label: "unparsed", count: "?" });
  const linked = asCount(c.linked);
  if (linked) pills.push({ state: "CLAIMED", label: "linked", count: linked });
  return pills;
}

/**
 * Presentation heading for one operator-local backlog group.
 *
 * Same shape as projectOptions: prefer the sidecar display field
 * (`project_display` on the first row of the group), fall back to the
 * grouping slug, never empty. Grouping stays on the slug so two
 * programmes that share a display name stay two groups.
 */
export function localHeading(
  rec: WorkItem | null | undefined,
  slug: string
): string {
  const name = String((rec && rec.project_display) || slug || "-");
  return name || "-";
}

export interface BacklogGroup {
  /** Machine key -- React `key` and group identity. Never a display name. */
  slug: string;
  /** Operator-facing <summary> text. */
  label: string;
  items: WorkItem[];
}

/**
 * HQ repo first, then HQ session, then operator-local by first-seen slug.
 * Labels for local groups prefer project_display; HQ/STM headings stay
 * "backlog" / "HQ session" (same as stm_ui._backlog_groups).
 */
export function backlogGroups(
  rows: WorkItem[] | null | undefined
): BacklogGroup[] {
  const hq: WorkItem[] = [];
  const stm: WorkItem[] = [];
  const localOrder: string[] = [];
  const localMap: Record<string, WorkItem[]> = {};
  for (const rec of rows || []) {
    const source = String(rec.source || "");
    if (source === "hq") hq.push(rec);
    else if (source === "stm") stm.push(rec);
    else {
      const proj = String(rec.project || "-");
      if (!localMap[proj]) {
        localOrder.push(proj);
        localMap[proj] = [];
      }
      localMap[proj].push(rec);
    }
  }
  const groups: BacklogGroup[] = [];
  if (hq.length) groups.push({ slug: "backlog", label: "backlog", items: hq });
  if (stm.length) {
    groups.push({ slug: "HQ session", label: "HQ session", items: stm });
  }
  for (const proj of localOrder) {
    const items = localMap[proj];
    groups.push({
      slug: proj,
      label: localHeading(items[0], proj),
      items,
    });
  }
  return groups;
}

export function healthUrl(apiBase: string): string {
  return apiBase + "/health";
}

export function utcNowStamp(d = new Date()): string {
  return d.toISOString().replace(/\.\d+Z$/, "Z");
}

export function isHealthOk(health: HealthPayload | null | undefined, code: number): boolean {
  return code === 200 && !!health && health.ok === true;
}
