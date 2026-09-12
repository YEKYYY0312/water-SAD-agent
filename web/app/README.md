# P5 Step 36 Web workspace

This Vite/React application builds the static assets served by
`water_agent.webapp`. It intentionally contains a non-interactive engineering
workspace skeleton only: no login, account data, database access, memory,
Agent invocation, or SSE completion is implemented here.

Run `npm run typecheck`, `npm test`, and `npm run build` from this directory.
The generated `dist/` directory is a build artifact and is not committed; the
Dockerfile builds it in a separate Node stage.
