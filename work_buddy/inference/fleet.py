"""Local model fleet — per-machine "what's loaded on which box" snapshot.

Answers a question the inference broker structurally cannot: which *machine*
in the local-inference fleet currently holds which model, is it reachable, and
what hardware does it have. The broker only knows the *profile* (model); it has
no per-machine view.

## Provider seam

The fleet is provider-neutral. ``merge_fleet`` is a pure function over already-
parsed inputs and knows nothing about any specific backend. A provider adapter
(today: LM Studio via the ``lms`` CLI) is the only place that talks to the
backend; swapping to vLLM / Ollama / llama.cpp later means writing a new adapter,
not touching ``merge_fleet``, the dashboard reader, the route, or the capability.

## Data layers

- **Discovery (live):** the machine roster + reachability + loaded models come
  from the provider. The local machine is always known; remote peers appear when
  reachable.
- **Hardware:** the local machine reports its own GPU/VRAM/RAM live. Remote-peer
  hardware is not readable off the wire, so it comes from a static config
  ``inference.fleet`` roster, joined by ``device_id``. A machine in the roster
  but not currently discovered is shown as offline rather than omitted.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BYTES_PER_GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Neutral data model (no provider knowledge)
# ---------------------------------------------------------------------------

@dataclass
class LoadedModel:
    """One model instance loaded on a machine."""

    model: str
    display_name: str | None = None
    kind: str | None = None  # provider's instance type, e.g. "embedding" | "llm"
    quant: str | None = None
    size_bytes: int | None = None
    context_length: int | None = None
    max_context_length: int | None = None
    status: str | None = None  # e.g. "idle" | "active"
    queued: int | None = None


@dataclass
class Gpu:
    """A single GPU on a machine."""

    name: str | None = None
    vram_gb: float | None = None


@dataclass
class Hardware:
    """A machine's compute hardware (possibly multiple GPUs). ``source`` is provenance."""

    gpus: list["Gpu"] = field(default_factory=list)
    ram_gb: float | None = None
    source: str = "unknown"  # "live" (read off the machine) | "roster" | "unknown"
    total_vram_gb: float | None = None  # summed across gpus; derived below

    def __post_init__(self) -> None:
        if self.total_vram_gb is None:
            vals = [g.vram_gb for g in self.gpus if g.vram_gb is not None]
            self.total_vram_gb = round(sum(vals), 1) if vals else None


@dataclass
class FleetMachine:
    device_id: str
    name: str
    is_local: bool = False
    reachable: bool = False
    role: str | None = None
    hardware: Hardware = field(default_factory=Hardware)
    loaded_models: list[LoadedModel] = field(default_factory=list)
    in_roster: bool = False
    discovered: bool = False  # present in the live provider read


@dataclass
class FleetSnapshot:
    machines: list[FleetMachine] = field(default_factory=list)
    local_device_id: str | None = None
    provider: str = "lmstudio"
    lms_available: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure merge — unit-testable with plain dicts, zero provider/subprocess knowledge
# ---------------------------------------------------------------------------

def _gib(num_bytes: Any) -> float | None:
    try:
        return round(float(num_bytes) / _BYTES_PER_GIB, 1)
    except (TypeError, ValueError):
        return None


def _num(val: Any) -> float | int | None:
    """Coerce to int (when whole) / float, or None for blank/non-numeric."""
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def _roster_gpus(entry: dict[str, Any]) -> list[Gpu]:
    """GPUs from a roster entry.

    Canonical shape is a ``gpus`` list of ``{name, vram_gb}``; a legacy scalar
    ``gpu``/``vram_gb`` is accepted as a single-GPU fallback so older/hand-written
    configs still render.
    """
    out: list[Gpu] = []
    raw = entry.get("gpus")
    if isinstance(raw, list):
        for g in raw:
            if isinstance(g, dict):
                name = g.get("name") or g.get("gpu")
                name = name.strip() if isinstance(name, str) and name.strip() else None
                out.append(Gpu(name=name, vram_gb=_num(g.get("vram_gb"))))
            elif isinstance(g, str) and g.strip():
                out.append(Gpu(name=g.strip()))
    if not out and (entry.get("gpu") or entry.get("vram_gb") is not None):
        nm = entry.get("gpu")
        out.append(Gpu(
            name=nm.strip() if isinstance(nm, str) and nm.strip() else None,
            vram_gb=_num(entry.get("vram_gb")),
        ))
    return out


def _loaded_model_from_ps(row: dict[str, Any]) -> LoadedModel:
    quant = row.get("quantization")
    quant_name = quant.get("name") if isinstance(quant, dict) else None
    return LoadedModel(
        model=row.get("modelKey") or row.get("identifier") or "?",
        display_name=row.get("displayName"),
        kind=row.get("type"),
        quant=quant_name,
        size_bytes=row.get("sizeBytes"),
        context_length=row.get("contextLength"),
        max_context_length=row.get("maxContextLength"),
        status=row.get("status"),
        queued=row.get("queued"),
    )


def merge_fleet(
    link_status: dict[str, Any] | None,
    ps: list[dict[str, Any]] | None,
    local_hardware: dict[str, Any] | None,
    roster: list[dict[str, Any]] | None,
    *,
    lms_available: bool = True,
    error: str | None = None,
) -> FleetSnapshot:
    """Compose a fleet snapshot from already-parsed provider inputs.

    Pure: deterministic, no IO. ``link_status`` is the live discovery (the
    local machine is its top-level ``deviceIdentifier``/``deviceName``; remote
    peers live under ``peers``). ``ps`` are loaded-model instances joined to
    machines by ``deviceIdentifier``. ``local_hardware`` is the normalized
    survey of the *local* machine only. ``roster`` enriches any machine
    (local or remote) by ``device_id`` with hardware specs + role.
    """
    link_status = link_status or {}
    ps = ps or []
    roster = roster or []

    roster_by_id: dict[str, dict[str, Any]] = {
        str(e["device_id"]): e for e in roster if e.get("device_id")
    }

    # Group loaded-model instances by the machine they sit on.
    ps_by_device: dict[str, list[LoadedModel]] = {}
    for row in ps:
        dev = row.get("deviceIdentifier")
        if not dev:
            continue
        ps_by_device.setdefault(str(dev), []).append(_loaded_model_from_ps(row))

    local_id = link_status.get("deviceIdentifier")
    link_online = (link_status.get("status") or "").lower() == "online"

    machines: list[FleetMachine] = []
    discovered_ids: set[str] = set()

    def _models_for(device_id: str, bare_names: list[str] | None) -> list[LoadedModel]:
        """Prefer rich ``ps`` detail; fall back to bare names from link status."""
        rich = ps_by_device.get(device_id, [])
        if rich:
            return rich
        return [LoadedModel(model=n) for n in (bare_names or [])]

    def _hardware_for(device_id: str, is_local: bool) -> Hardware:
        # Local machine reports its own hardware live; prefer that.
        if is_local and local_hardware:
            gpus = [
                Gpu(name=g.get("name"), vram_gb=_gib(g.get("vram_bytes")))
                for g in (local_hardware.get("gpus") or [])
            ]
            return Hardware(
                gpus=gpus,
                ram_gb=_gib(local_hardware.get("ram_bytes")),
                source="live",
            )
        entry = roster_by_id.get(device_id)
        if entry:
            return Hardware(
                gpus=_roster_gpus(entry),
                ram_gb=_num(entry.get("ram_gb")),
                source="roster",
            )
        return Hardware(source="unknown")

    def _build(device_id: str, name: str, *, is_local: bool, reachable: bool,
               bare_names: list[str] | None) -> FleetMachine:
        discovered_ids.add(device_id)
        entry = roster_by_id.get(device_id) or {}
        return FleetMachine(
            device_id=device_id,
            name=name or entry.get("name") or device_id[:12],
            is_local=is_local,
            reachable=reachable,
            role=entry.get("role"),
            hardware=_hardware_for(device_id, is_local),
            loaded_models=_models_for(device_id, bare_names),
            in_roster=device_id in roster_by_id,
            discovered=True,
        )

    # Local machine (always first; the top-level identity of link status).
    if local_id:
        machines.append(_build(
            str(local_id),
            link_status.get("deviceName") or "",
            is_local=True,
            reachable=link_online,
            bare_names=None,
        ))

    # Remote peers.
    for peer in link_status.get("peers") or []:
        dev = peer.get("deviceIdentifier")
        if not dev:
            continue
        machines.append(_build(
            str(dev),
            peer.get("deviceName") or "",
            is_local=False,
            reachable=(peer.get("status") or "").lower() == "connected",
            bare_names=peer.get("loadedModels"),
        ))

    # Rostered-but-not-discovered machines: show as offline, not omitted.
    for device_id, entry in roster_by_id.items():
        if device_id in discovered_ids:
            continue
        machines.append(FleetMachine(
            device_id=device_id,
            name=entry.get("name") or device_id[:12],
            is_local=False,
            reachable=False,
            role=entry.get("role"),
            hardware=_hardware_for(device_id, False),
            loaded_models=[],
            in_roster=True,
            discovered=False,
        ))

    # Order: local first, then reachable, then offline; alphabetical within group.
    machines.sort(key=lambda m: (
        0 if m.is_local else 1,
        0 if m.reachable else 1,
        (m.name or "").lower(),
    ))

    return FleetSnapshot(
        machines=machines,
        local_device_id=str(local_id) if local_id else None,
        lms_available=lms_available,
        error=error,
    )


# ---------------------------------------------------------------------------
# LM Studio provider adapter — the ONLY place that runs `lms`
# ---------------------------------------------------------------------------

@dataclass
class _ProviderRead:
    available: bool
    link_status: dict[str, Any] | None
    ps: list[dict[str, Any]]
    local_hardware: dict[str, Any] | None
    error: str | None


def _resolve_lms_bin() -> str | None:
    """Locate the ``lms`` CLI: PATH first, then LM Studio's default bin dir.

    The dashboard/sidecar may run under a different PATH than an interactive
    shell, so fall back to the documented install location.
    """
    found = shutil.which("lms")
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".lmstudio" / "bin" / "lms.exe",
        home / ".lmstudio" / "bin" / "lms",
    ):
        if cand.exists():
            return str(cand)
    return None


def _run_lms_json(lms_bin: str, args: list[str], timeout: float = 12.0) -> Any:
    """Run ``lms <args> --json`` and return parsed JSON, or None on any failure."""
    try:
        proc = subprocess.run(
            [lms_bin, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("lms %s failed: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        logger.debug("lms %s exited %s: %s", " ".join(args), proc.returncode, proc.stderr.strip())
        return None
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        logger.debug("lms %s returned non-JSON: %s", " ".join(args), exc)
        return None


def _normalize_lms_survey(survey: Any) -> dict[str, Any] | None:
    """Flatten ``lms runtime survey --json`` to ``{gpus: [{name, vram_bytes}], ram_bytes}``.

    Collects EVERY GPU the runtime surveyed (multi-GPU rigs report several), each
    with its own dedicated VRAM. The survey is deeply nested and local-only.
    Tolerant of missing engines, a failed GPU probe, or absent fields.
    """
    if not isinstance(survey, dict):
        return None
    engines = survey.get("engines") or []
    if not engines:
        return None
    eng = engines[0]
    mem = eng.get("memoryInfo") or {}
    gpus: list[dict[str, Any]] = []
    try:
        gpu_res = eng["hardwareSurvey"]["gpuSurveyResult"]
        if (gpu_res.get("result") or {}).get("code") == "success":
            for g in (gpu_res.get("gpuInfo") or []):
                if not isinstance(g, dict):
                    continue
                name = (g.get("name") or "").strip() or None
                # Per-GPU dedicated VRAM is the reliable figure; memoryInfo.vramCapacity
                # is ambiguous across multiple devices.
                vram = g.get("dedicatedMemoryCapacityBytes") or g.get("totalMemoryCapacityBytes")
                gpus.append({"name": name, "vram_bytes": vram})
    except (KeyError, TypeError):
        gpus = []
    return {"gpus": gpus, "ram_bytes": mem.get("ramCapacity")}


def _read_lmstudio() -> _ProviderRead:
    """Run the three ``lms`` reads and return normalized provider inputs.

    Never raises. ``available`` is False when the CLI is missing or its link
    status can't be read; callers still render the static roster as offline.
    """
    lms_bin = _resolve_lms_bin()
    if not lms_bin:
        return _ProviderRead(
            available=False, link_status=None, ps=[], local_hardware=None,
            error="lms CLI not found (LM Studio not installed, or not on PATH).",
        )

    link = _run_lms_json(lms_bin, ["link", "status", "--json"])
    if not isinstance(link, dict):
        return _ProviderRead(
            available=False, link_status=None, ps=[], local_hardware=None,
            error="Could not read `lms link status` (LM Studio server / LM Link may be down).",
        )

    ps = _run_lms_json(lms_bin, ["ps", "--json"])
    ps = ps if isinstance(ps, list) else []
    survey = _run_lms_json(lms_bin, ["runtime", "survey", "--json"])
    local_hardware = _normalize_lms_survey(survey)

    return _ProviderRead(
        available=True, link_status=link, ps=ps,
        local_hardware=local_hardware, error=None,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def read_fleet(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the fleet snapshot as a JSON-serializable dict. Never raises.

    Selects the local-inference provider adapter (LM Studio today) and merges
    its live read with the static ``inference.fleet`` roster from config.
    """
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()
    roster = cfg.get("inference", {}).get("fleet", []) or []

    try:
        read = _read_lmstudio()
    except Exception as exc:  # pragma: no cover — defensive; adapter is tolerant
        logger.warning("fleet provider read failed: %s", exc)
        return merge_fleet(
            {}, [], None, roster,
            lms_available=False, error=f"fleet read failed: {exc}",
        ).to_dict()

    return merge_fleet(
        read.link_status, read.ps, read.local_hardware, roster,
        lms_available=read.available, error=read.error,
    ).to_dict()
