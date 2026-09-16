"""ui — the ops console (PRD: docs/design/ops-console-prd.md).

A self-contained assembly package: the backend router (ui.api, mounted at
/api/v1/console/* and included by host.main) + the build-less frontend
static assets (ui/static/, mounted at /console by host.main). The layering
contract (host → apps → atoms → nexus) does not cover ui/ — its dependency
direction matches the host layer (imports atoms/nexus only) and is
assembled by the host.

P0 scope: pattern read-only views (list / detail: yml + mermaid +
declaration tree), the plugin and tool catalogs, knowledge-base CRUD /
scopes / the search preview. The edit and publish flows (P1) are outside
this package.
"""
