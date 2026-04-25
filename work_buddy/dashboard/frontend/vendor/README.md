# Vendored frontend assets

Third-party JS/CSS that the dashboard serves directly. Each entry is
pinned to a specific version and committed verbatim — no npm, no CDN at
runtime. Re-vendor by re-downloading from the upstream source.

| File | Version | Source | License | Used by |
|------|---------|--------|---------|---------|
| `chart.umd.min.js` | 4.4.0 | https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js | MIT | Costs tab (`script_costs.py`) |

These files are served by the `/vendor/<path>` route in
`work_buddy/dashboard/service.py`. The path is `/vendor/...` rather
than `/static/...` because Flask registers a default `/static/`
endpoint that shadows app-defined routes at the same prefix.

## Re-vendoring

```bash
curl -sSfL "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" \
  -o work_buddy/dashboard/frontend/vendor/chart.umd.min.js
```

Bump the version in this README and the `<script src>` in
`work_buddy/dashboard/frontend/__init__.py` if you upgrade.
