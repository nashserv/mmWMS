"""Fail-closed production fence for worker-initiated provider side effects."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class ProviderFenceScope:
    enabled: bool
    mode: str
    tenant_id: str | None
    account_id: str | None


def provider_fence_scope(*, require_account: bool = False) -> ProviderFenceScope:
    """Return the currently authorized claim scope.

    The worker-egress inventory is activated independently from Deployments.
    Its projected marker must match this exact mode/release/change/cluster
    before a worker may lease any provider-facing job. Missing, stale or
    pre-promotion projection is a normal fail-closed state, not permission to
    process the global backlog.
    """
    mode = os.getenv("MMX_SIDE_EFFECT_MODE", "")
    if mode not in {"canary", "live"}:
        raise RuntimeError("MMX_SIDE_EFFECT_MODE must be canary or live")
    expected = {
        "release-id": os.getenv("MMX_RELEASE_ID", ""),
        "change-approval-id": os.getenv("MMX_CHANGE_APPROVAL_ID", ""),
        "cluster-uid": os.getenv("MMX_CLUSTER_UID", ""),
    }
    if not all(_IDENTIFIER.fullmatch(value) for value in expected.values()):
        raise RuntimeError("provider fence release/change/cluster binding is invalid")
    marker_dir = Path(os.getenv("MMX_PROVIDER_EGRESS_MARKER_DIR", ""))
    if not marker_dir.is_absolute():
        raise RuntimeError("MMX_PROVIDER_EGRESS_MARKER_DIR must be absolute")
    try:
        marker = {
            "enabled": (marker_dir / "enabled").read_text(encoding="utf-8").strip(),
            "mode": (marker_dir / "mode").read_text(encoding="utf-8").strip(),
            **{
                name: (marker_dir / name).read_text(encoding="utf-8").strip()
                for name in expected
            },
        }
    except OSError:
        return ProviderFenceScope(False, mode, None, None)
    if (
        marker.get("enabled") != "true"
        or marker.get("mode") != mode
        or any(marker.get(name) != value for name, value in expected.items())
    ):
        return ProviderFenceScope(False, mode, None, None)
    if mode == "live":
        return ProviderFenceScope(True, mode, None, None)
    tenant_id = os.getenv("MMX_CANARY_TENANT_ID", "")
    account_id = os.getenv("MMX_CANARY_WB_ACCOUNT_ID", "")
    if not _IDENTIFIER.fullmatch(tenant_id):
        raise RuntimeError("MMX_CANARY_TENANT_ID is invalid")
    if require_account and not _UUID.fullmatch(account_id):
        raise RuntimeError("MMX_CANARY_WB_ACCOUNT_ID is invalid")
    return ProviderFenceScope(True, mode, tenant_id, account_id if require_account else None)
