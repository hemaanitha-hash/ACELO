# ACELO — Agentic Optimization Platform (Frontend Prototype)

A client-facing UI prototype for **ACELO**, an enterprise AI-powered data
platform optimization product. This is a **frontend-only prototype**: all
data is demo/mock data served through a service layer designed to be swapped
for a real FastAPI backend later, with no changes required to any page or
component.

> This phase is UI-only. It does **not** connect to Databricks or any live
> backend. See [Connecting a real backend](#connecting-a-real-backend) below.

---

## 1. Requirements

- Node.js **18+** (Node 20 LTS recommended)
- npm 9+ (ships with Node)

Check your versions:

```bash
node -v
npm -v
```

---

## 2. Install

From the project root:

```bash
npm install
```

---

## 3. Run locally (development)

```bash
npm run dev
```

This starts Vite's dev server, by default at:

```
http://localhost:5173
```

Open that URL in your browser. Hot reload is enabled — edits to any file in
`src/` will refresh automatically.

---

## 4. Build for production

```bash
npm run build
```

This type-checks the project with `tsc` and produces an optimized static
build in `dist/`.

---

## 5. Preview the production build locally

```bash
npm run preview
```

This serves the `dist/` folder locally (default `http://localhost:4173`) so
you can sanity-check the production build before deploying.

---

## 6. Deploying to Render

ACELO's frontend is a static single-page app, so deploy it on Render as a
**Static Site**:

1. Push this project to a Git repository (GitHub/GitLab/Bitbucket).
2. In the Render dashboard, choose **New → Static Site** and connect the repo.
3. Configure the service:
   - **Build Command:** `npm install && npm run build`
   - **Publish Directory:** `dist`
4. Add a rewrite rule so client-side routing works for direct links/refreshes:
   - **Source:** `/*`
   - **Destination:** `/index.html`
   - **Action:** Rewrite
5. Deploy. Render will rebuild automatically on every push to the connected
   branch.

No environment variables are required for this UI-only phase.

---

## 7. Project structure

```
src/
  components/     Reusable UI building blocks (Sidebar, Topbar, tables, cards, etc.)
  pages/          One file per screen (Overview, AI Agent, Optimizations, ...)
  data/           demoData.ts — ALL demo/mock values live here, isolated from components
  services/       api.ts — the only place the UI calls for data; wraps demo data today,
                  FastAPI calls tomorrow
  types/          Shared TypeScript types for the whole app
  App.tsx         Route definitions
  main.tsx        App entry point
```

### Why the service layer matters

Every page calls functions like `getOverview()`, `getOptimizations()`,
`runAgent()`, `approveOptimization()`, etc. from `src/services/api.ts`. Right
now those functions resolve local demo data with a small simulated delay.
When the backend is ready, only `src/services/api.ts` needs to change —
no component or page needs to be touched.

---

## 8. Connecting a real backend

The intended FastAPI contract (see comments at the top of
`src/services/api.ts`) is:

| Frontend function        | Future endpoint                     |
|---------------------------|--------------------------------------|
| `getOverview()`           | `GET /api/overview`                 |
| `runAgent(prompt)`        | `POST /api/agent`                   |
| `getOptimizations()`      | `GET /api/optimizations`            |
| `getRecommendation(id)`   | `GET /api/recommendations/:id`      |
| `requestApproval(id)`     | `POST /api/approvals`               |
| `approveOptimization(id)` | `POST /api/approvals/:id/approve`   |
| `startExecution(id)`      | `POST /api/executions`              |
| `getExecutionStatus(id)`  | `GET /api/executions/:id`           |
| `getHistory()`            | `GET /api/history`                  |

The intended full architecture:

```
React UI  →  FastAPI  →  ACELO Agent  →  Platform Router  →  Databricks Adapter | Fabric Adapter
```

The frontend has no dependency on Databricks or Fabric SDKs — all platform
specifics live behind the future FastAPI layer, so migrating from Databricks
to Microsoft Fabric will not require any frontend changes.

---

## 9. Product principle reflected throughout the UI

> AI recommends. Human approves. ACELO executes. System validates.
> Everything is traceable.

This shows up directly in the navigation and flow: **AI Agent →
Optimizations → Recommendation Detail → Approval Center → Execution →
Results → History**. No optimization is ever executed without an explicit
human approval step.
