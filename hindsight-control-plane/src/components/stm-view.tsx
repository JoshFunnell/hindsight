"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  DEFAULT_BUDGET,
  DEFAULT_OPERATOR_VIEW,
  REFRESH_MS,
  STM_API_BASE,
  STALE_PREFIX,
  budgetLabel,
  groupByKind,
  healthLines,
  healthUrl,
  isHealthOk,
  coerceWorkPayload,
  fmtAge,
  normalizeApiBase,
  operatorViewUrl,
  parseViewLines,
  PROJECT_ALL,
  projectOptions,
  projectsUrl,
  type ProjectInfo,
  resolveBudget,
  resolveProject,
  type HealthPayload,
  type StmRow,
  utcNowStamp,
  viewUrl,
  viewWithoutHeader,
  type WorkItem,
  type WorkPayload,
  backlogGroups,
  workPills,
  workUrl,
} from "@/lib/stm-view";

function esc(s: unknown): string {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function groupsHtml(text: string): string {
  const grouped = groupByKind(parseViewLines(viewWithoutHeader(text)));
  if (!grouped.length) return '<p class="empty">no open entries</p>';
  const chunks: string[] = [];
  for (const [kind, rows] of grouped) {
    chunks.push('<section class="kind" data-kind="' + esc(kind) + '">');
    chunks.push("<h2>" + esc(kind) + "</h2>");
    for (const rec of rows as StmRow[]) {
      if (rec.kind === "META") {
        chunks.push('<div class="row meta">' + esc(rec.text) + "</div>");
        continue;
      }
      const utc = rec.clockUtc || rec.clock;
      chunks.push(
        '<div class="row">' +
          '<span class="id">' +
          esc(rec.id) +
          "</span> " +
          '<span class="clock" title="' +
          esc(utc) +
          '">' +
          esc(rec.clock) +
          "</span> " +
          '<span class="utc">' +
          esc(utc) +
          "</span> " +
          '<span class="text">' +
          esc(rec.text) +
          "</span></div>"
      );
    }
    chunks.push("</section>");
  }
  return chunks.join("\n");
}

function classSafe(st: string): string {
  return st.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
}

function WorkRow({ rec }: { rec: WorkItem }) {
  const st = String(rec.state || "");
  const stc = classSafe(st);
  return (
    <div className="wrow">
      <span className="wkind">{String(rec.kind || "")}</span>
      <span className={"wstate s-" + stc}>{st}</span>
      <span className="wid">{String(rec.id || "")}</span>
      <span className="wtitle">{String(rec.title || "")}</span>
    </div>
  );
}

function JobCard({ rec }: { rec: WorkItem }) {
  const state = String(rec.state || "UNKNOWN");
  const stc = classSafe(state);
  const bits: string[] = [];
  if (rec.detail) bits.push(String(rec.detail));
  if (rec.started_iso) bits.push(String(rec.started_iso));
  const age = fmtAge(rec.age_s);
  if (age) bits.push("age " + age);
  const idle = fmtAge(rec.idle_s);
  if (idle && (state === "RUNNING" || state === "STALLED")) bits.push("idle " + idle);
  if (rec.pid) bits.push("pid " + String(rec.pid));
  return (
    <div className={"job s-" + stc}>
      <div className="jhead">
        <span className={"pill s-" + stc}>{state}</span>
        <span className="lane">{String(rec.lane || "")}</span>
        <span className="jtitle">{String(rec.title || "")}</span>
      </div>
      <div className="jmeta">{bits.join(" | ")}</div>
      <div className="jline">{String(rec.last_line || "")}</div>
    </div>
  );
}

function Item({ rec }: { rec: WorkItem }) {
  if (String(rec.source || "") === "job") return <JobCard rec={rec} />;
  return <WorkRow rec={rec} />;
}

export function StmView() {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  let apiBase = STM_API_BASE;
  try {
    apiBase = normalizeApiBase(search.get("api") || STM_API_BASE);
  } catch {
    apiBase = STM_API_BASE;
  }
  const projectParam = search.get("project");
  const budgetRaw = search.get("budget");
  const [resolved, setResolved] = useState({
    project: projectParam || "",
    budget: budgetRaw == null ? DEFAULT_BUDGET : parseInt(budgetRaw, 10) || DEFAULT_BUDGET,
  });
  const project = resolved.project;
  const budget = resolved.budget;

  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [work, setWork] = useState<WorkPayload | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [operatorView, setOperatorView] = useState(DEFAULT_OPERATOR_VIEW);

  const [html, setHtml] = useState(
    '<div id="stale" class="stale" hidden></div>' +
      '<pre id="health" class="health">loading</pre>' +
      '<div id="budget" class="budget"></div>' +
      '<div id="groups"><p class="empty">no open entries</p></div>'
  );

  useEffect(() => {
    let cancelled = false;
    const hUrl = healthUrl(apiBase);

    function paint(
      stale: string | null,
      health: HealthPayload | null,
      hcode: number,
      viewText: string,
      groupsOverride?: string
    ) {
      const banner = stale
        ? '<div id="stale" class="stale">' + esc(stale) + "</div>"
        : '<div id="stale" class="stale" hidden></div>';
      const groups =
        groupsOverride !== undefined ? groupsOverride : groupsHtml(viewText);
      return (
        banner +
        '<pre id="health" class="health">' +
        esc(healthLines(health, hcode)) +
        "</pre>" +
        '<div id="budget" class="budget">' +
        esc(budgetLabel(viewText)) +
        "</div>" +
        '<div id="groups">' +
        groups +
        "</div>"
      );
    }

    function tick() {
      let hcode = 0;
      let health: HealthPayload | null = null;
      let nextProject = project;
      let nextView = viewUrl(apiBase, project, budget);
      fetch(hUrl)
        .then(function (r) {
          hcode = r.status;
          return r.ok ? r.json() : Promise.resolve(null);
        })
        .then(function (h) {
          health = h;
          nextProject = resolveProject(projectParam, h, project);
          const nextBudget = resolveBudget(budgetRaw, h, budget || DEFAULT_BUDGET);
          if (nextProject !== project || nextBudget !== budget) {
            setResolved({ project: nextProject, budget: nextBudget });
          }
          nextView = viewUrl(apiBase, nextProject, nextBudget);
          return fetch(projectsUrl(apiBase));
        })
        .then(function (r) {
          return r.ok ? r.json() : Promise.resolve(null);
        })
        .then(function (pj) {
          // A failed /projects must never blank the STM half of the page.
          if (!cancelled && pj && Array.isArray(pj.projects)) setProjects(pj.projects);
          // Own catch: a /work failure must never fall through to the outer
          // .catch and blank the STM half.
          return fetch(workUrl(apiBase))
            .then(function (r) {
              return r.ok ? r.json() : null;
            })
            .catch(function () {
              return null;
            });
        })
        .then(function (wk) {
          if (!cancelled) {
            setWork(function (prev) {
              const next = coerceWorkPayload(wk);
              return next == null ? prev : next;
            });
          }
          return fetch(nextView);
        })
        .then(function (r) {
          return r.ok ? r.text() : Promise.resolve("");
        })
        .then(function (text) {
          if (cancelled) return;
          const ok = isHealthOk(health, hcode);
          const stamp = ok ? null : STALE_PREFIX + utcNowStamp();
          if (!operatorView) {
            setHtml(paint(stamp, health, hcode, text));
            return;
          }
          const opUrl = operatorViewUrl(apiBase, nextProject, "html");
          return fetch(opUrl)
            .then(function (r) {
              return r.ok ? r.text() : Promise.reject(new Error("operator-view"));
            })
            .then(function (opHtml) {
              if (cancelled) return;
              setHtml(paint(stamp, health, hcode, text, opHtml));
            })
            .catch(function () {
              if (cancelled) return;
              // Own catch: a /stm/view-operator miss falls back to raw /stm/view
              // and must never blank JOBS or the STM chrome.
              setHtml(paint(stamp, health, hcode, text));
            });
        })
        .catch(function () {
          if (cancelled) return;
          setHtml(paint(STALE_PREFIX + utcNowStamp(), null, 0, ""));
        });
    }

    tick();
    const id = setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [apiBase, project, budget, projectParam, budgetRaw, operatorView]);

  return (
    <div className="stm-page">
      <style>{`
body{ }
.stm-page{font-family:Consolas,"Courier New",monospace;background:#111;color:#ddd;margin:0;padding:16px;min-height:100%}
.stm-page h1{font-size:18px;margin:0 0 8px}
.stm-page .stale{background:#5a1a1a;color:#fcc;padding:8px 12px;margin:8px 0;border:1px solid #a33}
.stm-page .health{background:#1a1a1a;padding:8px 12px;margin:8px 0;white-space:pre-wrap}
.stm-page .budget{color:#cc9;margin:8px 0}
.stm-page .switcher{margin:8px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.stm-page .sect{font-size:13px;letter-spacing:.08em;color:#7ad;margin:18px 0 6px;border-bottom:1px solid #2a2a2a;padding-bottom:4px}
.stm-page .jobs{margin:4px 0}
.stm-page .jpills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;align-items:center}
.stm-page .toggle{display:flex;gap:5px;align-items:center;cursor:pointer;font-size:12px;color:#999}
.stm-page .pill{padding:2px 8px;border:1px solid #444;background:#1c1c1c;color:#ccc;font-size:11px;letter-spacing:.05em}
.stm-page .pill.s-RUNNING{background:#123a1a;border-color:#2b6;color:#8f8}
.stm-page .pill.s-STALLED{background:#3a2a05;border-color:#b82;color:#fc6}
.stm-page .pill.s-FAILED,.stm-page .pill.s-STALE{background:#3a1212;border-color:#b44;color:#f99}
.stm-page .pill.s-QUEUED,.stm-page .pill.s-CLAIMED{background:#12233a;border-color:#48b;color:#9cf}
.stm-page .job{border:1px solid #262626;border-left:3px solid #444;background:#161616;padding:8px 10px;margin:6px 0}
.stm-page .job.s-RUNNING{border-left-color:#2b6}
.stm-page .job.s-STALLED{border-left-color:#b82}
.stm-page .job.s-STALE,.stm-page .job.s-FAILED{border-left-color:#b44}
.stm-page .job.s-QUEUED,.stm-page .job.s-CLAIMED{border-left-color:#48b}
.stm-page .jhead{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.stm-page .lane{color:#7ad;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.stm-page .jtitle{color:#eee;flex:1 1 240px;min-width:0;overflow-wrap:anywhere}
.stm-page .jmeta{color:#777;font-size:11px;margin-top:3px;overflow-wrap:anywhere}
.stm-page .jline{color:#9a9;font-size:11px;margin-top:3px;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media (max-width:560px){.stm-page{padding:9px}.stm-page .jtitle{flex-basis:100%}}
.stm-page .switcher select{background:#222;color:#ddd;border:1px solid #555;font-family:inherit;font-size:13px;padding:5px 4px;max-width:100%}
.stm-page .badge{background:#243;color:#9c9;padding:2px 8px;border:1px solid #363;font-size:12px}
.stm-page .kind{margin-top:16px}
.stm-page .kind h2{font-size:14px;color:#9cf;border-bottom:1px solid #333;margin:0 0 6px}
.stm-page .row{padding:2px 0;overflow-wrap:anywhere;word-break:break-word}
.stm-page .text{overflow-wrap:anywhere}
.stm-page .health{overflow-wrap:anywhere;word-break:break-word}
.stm-page .budget{overflow-wrap:anywhere}
.stm-page .id{color:#888}
.stm-page .clock{color:#9c9}
.stm-page .utc{color:#666;margin-left:6px}
.stm-page .meta{color:#aaa}
.stm-page .empty{color:#888}
.stm-page [hidden]{display:none}
.stm-page .work{margin:4px 0}
.stm-page .wsrc{border:1px solid #262626;background:#141414;padding:6px 8px;margin:6px 0}
.stm-page .wsrch{color:#7ad;font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.stm-page .wnote{color:#666;text-transform:none;letter-spacing:0;margin-left:6px}
.stm-page .wproj{color:#cc9;font-size:11px;margin:6px 0 2px}
.stm-page .wrow{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;padding:2px 0;border-bottom:1px solid #1c1c1c}
.stm-page .wkind{color:#9cf;font-size:11px;min-width:52px}
.stm-page .wstate{font-size:11px;color:#999;min-width:52px}
.stm-page .wstate.s-active{color:#8f8}
.stm-page .wstate.s-open{color:#8f8}
.stm-page .wid{color:#ddd;font-size:12px;min-width:90px}
.stm-page .wtitle{color:#bbb;flex:1 1 240px;min-width:0}
.stm-page details>summary{cursor:pointer;color:#999;font-size:12px;margin:4px 0}
.stm-page .worphan{margin:6px 0}
.stm-page .op-view{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#e6e6ea;line-height:1.45}
.stm-page .op-view h2{font-size:14px;font-weight:700;color:#9cf;border-bottom:1px solid #333;margin:16px 0 8px;padding-bottom:4px}
.stm-page .op-headline{font-size:14px;font-weight:700;color:#f3f3f6}
.stm-page .op-when{font-size:12px;color:#8f8;margin-left:8px}
.stm-page .op-prose{font-size:13px;color:#ccc;margin:6px 0}
.stm-page .op-ann,.stm-page .op-ann small{font-size:11px;color:#666}
.stm-page .op-footer{margin-top:18px;font-size:11px;color:#777;font-style:italic}
      `}</style>
      <h1>STM {project ? project : ""}</h1>
      <div className="switcher">
        <label htmlFor="project-select">project </label>
        <select
          id="project-select"
          value={project}
          onChange={(e) => {
            // Never "" -- see PROJECT_ALL. An empty value would DROP the
            // param and the sidecar would serve the DEFAULT project.
            const next = e.target.value || PROJECT_ALL;
            setResolved((r) => ({ ...r, project: next }));
            // The URL must carry it too: resolveProject() falls back to
            // health.default_project whenever ?project= is absent, which would
            // stomp this choice on the very next 5s tick.
            const qs = new URLSearchParams(Array.from(search.entries()));
            qs.set("project", next);
            router.replace(pathname + "?" + qs.toString(), { scroll: false });
          }}
        >
          {projectOptions(projects, project).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <span className="badge">{projectOptions(projects, project).length - 1} projects</span>
        <label className="toggle">
          <input
            type="checkbox"
            checked={operatorView}
            onChange={(e) => setOperatorView(e.target.checked)}
          />{" "}
          Operator view
        </label>
      </div>
      <p className="meta">loopback view over /health + /stm/view + /stm/view-operator + /projects + /work (refresh {REFRESH_MS}ms)</p>
      <p className="meta" data-stm-api={apiBase}>
        fetch {apiBase}/health + {apiBase}/stm/view
      </p>
      <h2 className="sect">JOB TRACKER</h2>
      <div className="work">
        {work === null ? (
          <p className="empty">job tracker lane unreachable (GET /work)</p>
        ) : (
          <>
            <div className="jpills">
              {workPills(work.counts).map((p, i) => (
                <span key={p.label + "-" + i} className={"pill s-" + p.state}>
                  {p.label} {p.count}
                </span>
              ))}
              <span className="badge">
                open {(work.counts && work.counts.open) || 0} | as of{" "}
                {String(work.generated_at || "")}
              </span>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={showDone}
                  onChange={(e) => setShowDone(e.target.checked)}
                />{" "}
                show finished
              </label>
            </div>
            {(() => {
              const nowRows = work.now || [];
              const backlog = work.backlog || [];
              const finished = work.finished || [];
              const errs = work.errors || [];
              return (
                <>
                  {nowRows.length
                    ? nowRows.map((r, i) => <Item key={"now-" + i} rec={r} />)
                    : <p className="empty">no now items</p>}
                  {backlogGroups(backlog).map((g) => (
                    <details key={g.slug}>
                      <summary>
                        {g.label} {g.items.length}
                      </summary>
                      {g.items.map((r, i) => (
                        <Item key={g.slug + "-" + i} rec={r} />
                      ))}
                    </details>
                  ))}
                  {showDone && finished.length ? (
                    <div className="wsrc">
                      <div className="wsrch">finished</div>
                      {finished.map((r, i) => (
                        <Item key={"fin-" + i} rec={r} />
                      ))}
                    </div>
                  ) : null}
                  {errs.length ? (
                    <div className="stale">job tracker errors: {errs.map(String).join("; ")}</div>
                  ) : null}
                </>
              );
            })()}
          </>
        )}
      </div>
      <h2 className="sect">STM</h2>
      <div id="root" dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
