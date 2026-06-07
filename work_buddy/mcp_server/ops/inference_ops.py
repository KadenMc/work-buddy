"""Inference-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). Adding this module is
enough to register its ops — ``load_builtin_ops`` auto-discovers every module in
this package.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _fleet_status() -> dict:
    """Local model fleet snapshot: per-machine reachability, loaded models, hardware."""
    from work_buddy.inference.fleet import read_fleet
    return read_fleet()


def _fleet_roster_dispatch(
    action: str = "set",
    device_id: str = "",
    role: str | None = None,
    gpus: list | None = None,
    ram_gb: float | None = None,
) -> dict:
    """Add/update (``set``) or clear (``remove``) a machine's ``inference.fleet`` entry.

    Persists to ``config.local.yaml`` (the user-override layer; comments there are
    machine-managed and not preserved). The fleet roster only *enriches* live-
    discovered machines, joined by ``device_id`` — so only ``device_id`` is
    required. ``role`` is the primary human label; hardware specs are optional
    (peers can't be auto-detected; the local machine reports its own live, so its
    roster specs are ignored for display). ``gpus`` is a list of
    ``{name, vram_gb}`` — machines can have several. Returns ``{"success": bool,
    ...}``; validation failures include ``errors_by_field`` for the dashboard form.
    """
    from work_buddy.config import read_config_local, write_config_local

    did = (device_id or "").strip()
    action = (action or "set").strip().lower()
    if action not in ("set", "remove"):
        return {"success": False, "error": f"unknown action {action!r} (expected set|remove)"}
    if not did:
        return {"success": False, "errors_by_field": {"device_id": "device_id is required."}}

    local = read_config_local()
    inf = dict(local.get("inference") or {})
    fleet = [dict(e) for e in (inf.get("fleet") or [])]
    idx = next((i for i, e in enumerate(fleet) if str(e.get("device_id")) == did), None)

    if action == "remove":
        existed = idx is not None
        if existed:
            fleet.pop(idx)
            inf["fleet"] = fleet
            write_config_local("inference", inf)
        return {
            "success": True, "action": "remove", "device_id": did,
            "note": "Roster entry cleared." if existed else "No roster entry to clear.",
        }

    # ---- set ----
    # Merge semantics per field: ``None`` (the default) means "omitted — leave
    # untouched", an empty value clears it, and a value sets it. So a partial
    # update (just the role) preserves existing hardware, and the dashboard form
    # (which sends every editable field) is a full replace.
    entry = dict(fleet[idx]) if idx is not None else {"device_id": did}

    if role is not None:
        s = role.strip()
        if s:
            entry["role"] = s
        else:
            entry.pop("role", None)

    errors: dict[str, str] = {}

    # ram_gb: scalar (None=omit, ""=clear, number=set)
    if ram_gb is not None and not (isinstance(ram_gb, str) and not ram_gb.strip()):
        try:
            r = float(ram_gb)
            entry["ram_gb"] = int(r) if r == int(r) else r
        except (TypeError, ValueError):
            errors["ram_gb"] = "ram_gb must be a number."
    elif isinstance(ram_gb, str) and not ram_gb.strip():
        entry.pop("ram_gb", None)

    # gpus: list of {name, vram_gb} (None=omit, []=clear, [..]=set). Migrates away
    # any legacy scalar gpu/vram_gb keys on this entry.
    if gpus is not None:
        if not isinstance(gpus, list):
            errors["gpus"] = "gpus must be a list of {name, vram_gb}."
        else:
            normalized: list[dict] = []
            for i, g in enumerate(gpus):
                if not isinstance(g, dict):
                    errors["gpus"] = "each GPU must be an object with name / vram_gb."
                    break
                name = g.get("name")
                name = name.strip() if isinstance(name, str) and name.strip() else None
                vram = None
                v = g.get("vram_gb")
                if v is not None and not (isinstance(v, str) and not v.strip()):
                    try:
                        fv = float(v)
                        vram = int(fv) if fv == int(fv) else fv
                    except (TypeError, ValueError):
                        errors["gpus"] = f"VRAM for GPU {i + 1} must be a number."
                        break
                if not name and vram is None:
                    continue  # skip blank rows
                item = {}
                if name:
                    item["name"] = name
                if vram is not None:
                    item["vram_gb"] = vram
                normalized.append(item)
            if "gpus" not in errors:
                if normalized:
                    entry["gpus"] = normalized
                else:
                    entry.pop("gpus", None)
                entry.pop("gpu", None)
                entry.pop("vram_gb", None)

    if errors:
        return {"success": False, "errors_by_field": errors}

    if idx is None:
        fleet.append(entry)
    else:
        fleet[idx] = entry
    inf["fleet"] = fleet
    write_config_local("inference", inf)
    return {
        "success": True, "action": "set", "device_id": did,
        "note": "Saved. The fleet view reflects it now.",
    }


def _register() -> None:
    # replace=True so a hot ``mcp_registry_reload`` (which re-imports this module)
    # doesn't raise on the already-registered op id.
    register_op("op.wb.fleet_status", _fleet_status, replace=True)
    register_op("op.wb.fleet_roster", _fleet_roster_dispatch, replace=True)


_register()
