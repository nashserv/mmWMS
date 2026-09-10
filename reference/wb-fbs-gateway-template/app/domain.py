from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Status(str, Enum):
    RECEIVED = "RECEIVED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RESERVATION_REQUESTED = "RESERVATION_REQUESTED"
    RESERVED = "RESERVED"
    RESERVATION_FAILED = "RESERVATION_FAILED"
    PICKING = "PICKING"
    PICKED = "PICKED"
    PACKING = "PACKING"
    PACKED = "PACKED"
    SUPPLY_ASSIGNED = "SUPPLY_ASSIGNED"
    LABEL_PENDING = "LABEL_PENDING"
    LABEL_READY = "LABEL_READY"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY"
    IN_DELIVERY_BATCH = "IN_DELIVERY_BATCH"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    HANDED_TO_WB = "HANDED_TO_WB"
    ACCEPTED_BY_WB = "ACCEPTED_BY_WB"
    RESHIPMENT_REQUIRED = "RESHIPMENT_REQUIRED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    RETURN_REQUESTED = "RETURN_REQUESTED"
    RETURNED_TO_WMS = "RETURNED_TO_WMS"
    CANCELLED = "CANCELLED"


WB_STATUS_MAP = {"new": Status.RECEIVED, "confirm": Status.SUPPLY_ASSIGNED, "complete": Status.OUT_FOR_DELIVERY, "cancel": Status.CANCELLED}


@dataclass
class Task:
    id: str
    seller_id: str
    account_id: str
    order_id: int
    order_uid: str
    sku: str
    destination: int
    cargo_type: int
    deadline: str | None
    required_meta: list[str] = field(default_factory=list)
    optional_meta: list[str] = field(default_factory=list)
    is_b2b: bool | None = None
    cross_border_type: int | None = None
    wb_status: str = "new"
    wb_system_status: str | None = None
    status: Status = Status.RECEIVED
    wms_task_id: str | None = None
    supply_id: str | None = None
    label_id: str | None = None
    delivery_batch_id: str | None = None
    version: int = 1
    last_wms_sequence: int = -1
    manual_reason: str | None = None
    last_reconciled_at: str | None = None
    reshipment_source_supply_id: str | None = None
    updated_at: str = field(default_factory=now)

    def public(self) -> dict[str, Any]:
        # Keep the seller projection explicit.  dataclasses.asdict recursively
        # deep-copies every scalar and was responsible for most CPU time in
        # high-volume syncs; only the two mutable metadata lists need copies.
        # The WMS sequence remains an internal replay guard.
        return {
            "id": self.id,
            "seller_id": self.seller_id,
            "account_id": self.account_id,
            "order_id": self.order_id,
            "order_uid": self.order_uid,
            "sku": self.sku,
            "destination": self.destination,
            "cargo_type": self.cargo_type,
            "deadline": self.deadline,
            "required_meta": list(self.required_meta),
            "optional_meta": list(self.optional_meta),
            "is_b2b": self.is_b2b,
            "cross_border_type": self.cross_border_type,
            "wb_status": self.wb_status,
            "wb_system_status": self.wb_system_status,
            "status": self.status.value,
            "wms_task_id": self.wms_task_id,
            "supply_id": self.supply_id,
            "label_id": self.label_id,
            "delivery_batch_id": self.delivery_batch_id,
            "version": self.version,
            "manual_reason": self.manual_reason,
            "last_reconciled_at": self.last_reconciled_at,
            "reshipment_source_supply_id": self.reshipment_source_supply_id,
            "updated_at": self.updated_at,
        }


@dataclass
class Supply:
    id: str
    account_id: str
    wb_supply_id: str
    destination: int
    cargo_type: int
    order_ids: list[int] = field(default_factory=list)
    status: str = "open"
    transport_boxes: list[str] = field(default_factory=list)
    is_b2b: bool | None = None
    cross_border_type: int | None = None


@dataclass
class DeliveryBatch:
    id: str
    destination: int
    supply_ids: list[str] = field(default_factory=list)
    status: str = "PLANNED"
    attempts: list[dict[str, Any]] = field(default_factory=list)
    departed_at: str | None = None


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def uid() -> str:
    return str(uuid.uuid4())
