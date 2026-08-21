from __future__ import annotations

"""
Complaint business-logic service for Smart Power Cut Prediction System.

This version is aligned with the current SQLAlchemy models in app/models.py.
It intentionally does NOT use the old raw-SQL schema (complaint_datetime,
preferred_resolution_hours, image_path, complaint_assignments, etc.), because
those fields/tables do not exist in the current project model.

Main workflow:
    Resident submits complaint -> Submitted
    -> system assigns to active/approved Staff
    -> Staff accepts -> In Progress
    -> Staff adds repair notes -> Resolved
    -> unresolved complaint past 24/48-hour deadline -> Escalated
    -> Officer handles escalated complaint

Existing complaints created before a Staff account existed are also handled by
`auto_assign_unassigned_complaints()` once an approved Staff account is
available.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence
import re

from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Complaint, ComplaintHistory, Notification, User


# ---------------------------------------------------------------------------
# Canonical workflow values used by the current application.
# ---------------------------------------------------------------------------

SUBMITTED = "Submitted"
IN_PROGRESS = "In Progress"
RESOLVED = "Resolved"
ESCALATED = "Escalated"
WITHDRAWN = "Withdrawn"
CLOSED = "Closed"

ACTIVE_STAFF_STATUSES = {
    SUBMITTED.lower(),
    IN_PROGRESS.lower(),
    ESCALATED.lower(),
}

TERMINAL_STATUSES = {
    RESOLVED.lower(),
    WITHDRAWN.lower(),
    CLOSED.lower(),
}

ALLOWED_RESOLUTION_HOURS = {24, 48}

COMPLAINT_TYPE_ALIASES = {
    "power cut": "Power Cut",
    "powercut": "Power Cut",
    "power_cut": "Power Cut",
    "power outage": "Power Cut",
    "outage": "Power Cut",
    "streetlight": "Streetlight",
    "street light": "Streetlight",
    "street_light": "Streetlight",
    "pole damage": "Pole Damage",
    "pole_damage": "Pole Damage",
    "damaged pole": "Pole Damage",
    "voltage issue": "Voltage Issue",
    "voltage_issue": "Voltage Issue",
    "voltage": "Voltage Issue",
}

PRIORITY_ORDER = {
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}


# ---------------------------------------------------------------------------
# Small utility helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def canonical_status(value: Any) -> str:
    """Return the current application's canonical display/status value."""
    key = normalize(value).replace("-", "_").replace(" ", "_")
    aliases = {
        "submitted": SUBMITTED,
        "pending": SUBMITTED,
        "assigned": SUBMITTED,
        "accepted": IN_PROGRESS,
        "in_progress": IN_PROGRESS,
        "working": IN_PROGRESS,
        "under_review": IN_PROGRESS,
        "resolved": RESOLVED,
        "closed": CLOSED,
        "escalated": ESCALATED,
        "withdrawn": WITHDRAWN,
    }
    return aliases.get(key, str(value or "").strip() or SUBMITTED)


def canonical_complaint_type(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    key = re.sub(r"\s+", " ", text).lower()
    return COMPLAINT_TYPE_ALIASES.get(key, text.title())


def normalize_resolution_hours(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return hours if hours in ALLOWED_RESOLUTION_HOURS else default


def parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Please provide a valid complaint date.")


def parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError("Please provide a valid complaint time.")


def complaint_datetime(complaint: Complaint) -> datetime:
    """Combine the model's separate complaint_date/time fields."""
    dt = datetime.combine(complaint.complaint_date, complaint.complaint_time)
    return dt.replace(tzinfo=timezone.utc)


def create_complaint_number() -> str:
    """Create a collision-resistant human-readable complaint number."""
    stamp = utc_now().strftime("%Y%m%d%H%M%S%f")[:-3]
    candidate = f"PC-{stamp}"
    # Extremely unlikely collision protection.
    suffix = 1
    while Complaint.query.filter_by(complaint_number=candidate).first() is not None:
        candidate = f"PC-{stamp}-{suffix}"
        suffix += 1
    return candidate


def calculate_priority(complaint_type: Any, description: Any, location: Any = "") -> str:
    """Rule-based priority used by the service when a route has not already calculated it."""
    text = " ".join(
        [
            normalize(complaint_type),
            normalize(description),
            normalize(location),
        ]
    )

    critical_terms = (
        "fire",
        "sparking",
        "electrocution",
        "electric shock",
        "live wire",
        "fallen live",
        "burning transformer",
        "transformer fire",
    )
    high_terms = (
        "broken pole",
        "fallen pole",
        "damaged pole",
        "exposed wire",
        "high voltage",
        "very high voltage",
        "no power",
        "power outage",
        "power cut",
    )

    if any(term in text for term in critical_terms):
        return "Critical"
    if any(term in text for term in high_terms):
        return "High"
    if normalize(complaint_type) in {"voltage issue", "pole damage"}:
        return "High"
    return "Medium" if normalize(complaint_type) else "Low"


def _priority_rank(value: Any) -> int:
    return PRIORITY_ORDER.get(normalize(value), 5)


def _get_form_value(form: Any, *names: str) -> Any:
    if form is None:
        return None
    for name in names:
        field = getattr(form, name, None)
        if field is None:
            continue
        data = getattr(field, "data", field)
        if data not in (None, ""):
            return data
    return None


def _get_image_filename(form: Any) -> Optional[str]:
    if form is None:
        return None
    for name in ("image", "image_filename", "photo", "attachment"):
        field = getattr(form, name, None)
        if field is None:
            continue
        uploaded = getattr(field, "data", field)
        filename = getattr(uploaded, "filename", None)
        if filename:
            return secure_filename(filename) or None
        if isinstance(uploaded, str) and uploaded.strip():
            return secure_filename(uploaded.strip()) or None
    return None


# ---------------------------------------------------------------------------
# History / notification helpers
# ---------------------------------------------------------------------------


def add_history(
    complaint: Complaint,
    user_id: Optional[int],
    action: str,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    notes: Optional[str] = None,
) -> ComplaintHistory:
    history = ComplaintHistory(
        complaint_id=complaint.id,
        user_id=user_id,
        action=action,
        old_status=old_status,
        new_status=new_status,
        notes=notes,
        created_at=utc_now(),
    )
    db.session.add(history)
    return history


def create_notification(
    user_id: int,
    complaint: Optional[Complaint],
    title: str,
    message: str,
    notification_type: str,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        complaint_id=complaint.id if complaint is not None else None,
        notification_type=notification_type,
        title=title,
        message=message,
        is_read=False,
        email_sent=False,
        created_at=utc_now(),
    )
    db.session.add(notification)
    return notification


def notify_resident(
    complaint: Complaint,
    title: str,
    message: str,
    notification_type: str = "Complaint Status",
) -> None:
    if complaint.resident_id:
        create_notification(
            complaint.resident_id,
            complaint,
            title,
            message,
            notification_type,
        )


def notify_staff(
    staff_id: Optional[int],
    complaint: Complaint,
    title: str,
    message: str,
    notification_type: str = "Complaint Assignment",
) -> None:
    if staff_id:
        create_notification(
            staff_id,
            complaint,
            title,
            message,
            notification_type,
        )


# ---------------------------------------------------------------------------
# Core service
# ---------------------------------------------------------------------------


class ComplaintService:
    """Central complaint business-logic service for the current SQLAlchemy model."""

    ALLOWED_STATUSES = {
        SUBMITTED,
        IN_PROGRESS,
        RESOLVED,
        ESCALATED,
        WITHDRAWN,
        CLOSED,
    }

    @staticmethod
    def get_complaint_by_id(complaint_id: int) -> Optional[Complaint]:
        return db.session.get(Complaint, complaint_id)

    @staticmethod
    def get_resident_complaints(
        resident_id: int,
        status: Optional[str] = None,
        complaint_type: Optional[str] = None,
    ) -> list[Complaint]:
        query = Complaint.query.filter(Complaint.resident_id == resident_id)
        if status:
            query = query.filter(
                func.lower(Complaint.status) == normalize(canonical_status(status))
            )
        if complaint_type:
            canonical_type = canonical_complaint_type(complaint_type)
            if canonical_type:
                query = query.filter(
                    func.lower(Complaint.complaint_type) == normalize(canonical_type)
                )
        return query.order_by(Complaint.created_at.desc(), Complaint.id.desc()).all()

    @staticmethod
    def get_all_complaints(
        status: Optional[str] = None,
        complaint_type: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> list[Complaint]:
        query = Complaint.query
        if status:
            query = query.filter(
                func.lower(Complaint.status) == normalize(canonical_status(status))
            )
        if complaint_type:
            canonical_type = canonical_complaint_type(complaint_type)
            if canonical_type:
                query = query.filter(
                    func.lower(Complaint.complaint_type) == normalize(canonical_type)
                )
        if priority:
            query = query.filter(
                func.lower(Complaint.priority) == normalize(priority)
            )
        return (
            query.order_by(
                Complaint.created_at.asc(),
                Complaint.id.asc(),
            ).all()
        )

    @staticmethod
    def create_complaint(
        resident_id: int,
        complaint_type: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        complaint_datetime: Optional[str] = None,
        preferred_resolution_hours: Optional[int] = None,
        image_path: Optional[str] = None,
        complaint_date: Any = None,
        complaint_time: Any = None,
        resolution_hours: Optional[int] = None,
        image_filename: Optional[str] = None,
        priority: Optional[str] = None,
        form: Any = None,
    ) -> int:
        """Create a complaint using the fields of the current Complaint model."""
        if form is not None:
            complaint_type = _get_form_value(form, "complaint_type") or complaint_type
            description = _get_form_value(form, "description") or description
            location = _get_form_value(form, "location") or location
            complaint_date = _get_form_value(form, "complaint_date") or complaint_date
            complaint_time = _get_form_value(form, "complaint_time") or complaint_time
            resolution_hours = (
                _get_form_value(
                    form,
                    "resolution_hours",
                    "preferred_resolution_hours",
                    "resolution_time",
                    "preferred_resolution_time",
                )
                or resolution_hours
                or preferred_resolution_hours
            )
            image_filename = (
                _get_image_filename(form)
                or image_filename
                or image_path
            )

        canonical_type = canonical_complaint_type(complaint_type)
        if not canonical_type:
            raise ValueError("Complaint type is required.")

        description = str(description or "").strip()
        location = str(location or "").strip()
        if not description:
            raise ValueError("Complaint description is required.")
        if len(description) < 10:
            raise ValueError("Please provide a more detailed complaint description.")
        if not location:
            raise ValueError("Complaint location is required.")

        if complaint_date is None and complaint_datetime:
            text = str(complaint_datetime).strip().replace("T", " ")
            parts = text.split()
            complaint_date = parts[0] if parts else None
            complaint_time = parts[1] if len(parts) > 1 else complaint_time

        parsed_date = parse_date(complaint_date)
        parsed_time = parse_time(complaint_time)
        if parsed_date > utc_now().date():
            raise ValueError("Complaint date cannot be in the future.")

        hours = normalize_resolution_hours(
            resolution_hours if resolution_hours is not None else preferred_resolution_hours,
            default=None,
        )
        if hours is None:
            raise ValueError("Resolution time must be 24 or 48 hours.")

        now = utc_now()
        deadline = now + timedelta(hours=hours)
        selected_priority = str(priority or "").strip().title()
        if selected_priority.lower() not in PRIORITY_ORDER:
            selected_priority = calculate_priority(canonical_type, description, location)

        complaint = Complaint(
            complaint_number=create_complaint_number(),
            resident_id=int(resident_id),
            complaint_type=canonical_type,
            description=description,
            location=location,
            complaint_date=parsed_date,
            complaint_time=parsed_time,
            image_filename=image_filename,
            resolution_hours=hours,
            deadline_at=deadline,
            priority=selected_priority,
            status=SUBMITTED,
            created_at=now,
            updated_at=now,
        )

        db.session.add(complaint)
        db.session.flush()

        add_history(
            complaint,
            resident_id,
            "Complaint Submitted",
            None,
            SUBMITTED,
            "Complaint submitted by resident.",
        )
        notify_resident(
            complaint,
            "Complaint Submitted",
            f"Complaint {complaint.complaint_number} has been submitted successfully.",
            "Complaint Submitted",
        )

        # If an approved Staff account already exists, assign immediately.
        # If no Staff account exists yet, the complaint remains Submitted and
        # auto_assign_unassigned_complaints() will pick it up later.
        ComplaintService._auto_assign_single(complaint, commit=False)

        db.session.commit()
        return int(complaint.id)

    @staticmethod
    def update_complaint(
        complaint_id: int,
        **changes: Any,
    ) -> Complaint:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")

        allowed = {
            "complaint_type",
            "description",
            "location",
            "complaint_date",
            "complaint_time",
            "image_filename",
            "resolution_hours",
            "priority",
        }

        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            if key == "complaint_type":
                value = canonical_complaint_type(value)
            elif key == "complaint_date":
                value = parse_date(value)
            elif key == "complaint_time":
                value = parse_time(value)
            elif key == "resolution_hours":
                value = normalize_resolution_hours(value)
                if value is None:
                    raise ValueError("Resolution time must be 24 or 48 hours.")
            elif isinstance(value, str):
                value = value.strip()
            setattr(complaint, key, value)

        if not complaint.description:
            raise ValueError("Complaint description is required.")
        if not complaint.location:
            raise ValueError("Complaint location is required.")
        if complaint.complaint_date > utc_now().date():
            raise ValueError("Complaint date cannot be in the future.")

        if "resolution_hours" in changes:
            complaint.deadline_at = complaint.created_at + timedelta(
                hours=int(complaint.resolution_hours)
            )
        complaint.updated_at = utc_now()
        db.session.commit()
        return complaint

    @staticmethod
    def delete_complaint(complaint_id: int) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            return False
        if normalize(complaint.status) not in {WITHDRAWN.lower(), SUBMITTED.lower()}:
            raise ValueError("Only submitted or withdrawn complaints can be deleted.")
        db.session.delete(complaint)
        db.session.commit()
        return True

    @staticmethod
    def assign_complaint(
        complaint_id: int,
        staff_id: int,
        assigned_by: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")

        staff = db.session.get(User, staff_id)
        if staff is None:
            raise ValueError("Staff member not found.")
        if normalize(staff.role) != "staff":
            raise ValueError("Complaint can only be assigned to Electricity Department Staff.")
        if not staff.is_active:
            raise ValueError("Selected staff account is inactive.")
        if not approved(staff):
            raise ValueError("Selected staff account is not approved.")

        current_status = normalize(complaint.status)
        if current_status in TERMINAL_STATUSES:
            raise ValueError("A completed complaint cannot be assigned.")
        if current_status == ESCALATED.lower() and complaint.assigned_staff_id:
            # Reassignment of an escalated complaint is permitted, but the
            # officer workflow should normally use its own reassignment route.
            pass

        previous_staff_id = complaint.assigned_staff_id
        complaint.assigned_staff_id = staff.id
        complaint.updated_at = utc_now()

        actor = assigned_by if assigned_by is not None else staff.id
        action = "Complaint Reassigned" if previous_staff_id else "Complaint Assigned"
        note_text = notes.strip() if notes else ""
        if previous_staff_id and previous_staff_id != staff.id:
            note_text = (
                f"Previous staff ID: {previous_staff_id}. "
                + note_text
            ).strip()

        add_history(
            complaint,
            actor,
            action,
            complaint.status,
            complaint.status,
            note_text or f"Assigned to {staff.full_name}.",
        )
        notify_staff(
            staff.id,
            complaint,
            "New Complaint Assigned",
            (
                f"Complaint {complaint.complaint_number} has been assigned to you. "
                f"Priority: {complaint.priority or 'Not specified'}."
            ),
            "Complaint Assignment",
        )
        notify_resident(
            complaint,
            "Complaint Assigned",
            (
                f"Complaint {complaint.complaint_number} has been forwarded to "
                "Electricity Department Staff for processing."
            ),
            "Complaint Assignment",
        )
        db.session.commit()
        return True

    @staticmethod
    def _auto_assign_single(
        complaint: Complaint,
        commit: bool = True,
    ) -> Optional[int]:
        if complaint.assigned_staff_id is not None:
            return complaint.assigned_staff_id
        if normalize(complaint.status) != SUBMITTED.lower():
            return None

        staff_members = get_available_staff()
        if not staff_members:
            return None

        loads = {staff.id: active_staff_load(staff.id) for staff in staff_members}
        selected = min(staff_members, key=lambda item: (loads[item.id], item.id))

        complaint.assigned_staff_id = selected.id
        complaint.updated_at = utc_now()
        add_history(
            complaint,
            selected.id,
            "Complaint Assigned",
            complaint.status,
            complaint.status,
            "Complaint automatically assigned using workload-based assignment.",
        )
        notify_staff(
            selected.id,
            complaint,
            "New Complaint Assigned",
            (
                f"Complaint {complaint.complaint_number} has been assigned to you "
                f"for processing. Priority: {complaint.priority or 'Not specified'}."
            ),
            "Complaint Assignment",
        )
        notify_resident(
            complaint,
            "Complaint Assigned",
            (
                f"Complaint {complaint.complaint_number} has been forwarded to "
                "Electricity Department Staff for processing."
            ),
            "Complaint Assignment",
        )
        if commit:
            db.session.commit()
        return selected.id

    @staticmethod
    def auto_assign_unassigned_complaints() -> int:
        """
        Assign all existing Submitted complaints that have no staff.

        This is the key repair for complaints submitted BEFORE staff registration.
        Once an approved Staff account exists, old complaints are not lost; they
        are picked up by this method.
        """
        staff_members = get_available_staff()
        if not staff_members:
            return 0

        complaints = (
            Complaint.query
            .filter(
                Complaint.assigned_staff_id.is_(None),
                func.lower(func.coalesce(Complaint.status, "")) == SUBMITTED.lower(),
            )
            .order_by(
                Complaint.created_at.asc(),
                Complaint.id.asc(),
            )
            .all()
        )
        if not complaints:
            return 0

        loads = {staff.id: active_staff_load(staff.id) for staff in staff_members}
        assigned = 0

        for complaint in complaints:
            if complaint.assigned_staff_id is not None:
                continue
            selected = min(staff_members, key=lambda item: (loads[item.id], item.id))
            complaint.assigned_staff_id = selected.id
            complaint.updated_at = utc_now()

            add_history(
                complaint,
                selected.id,
                "Complaint Assigned",
                complaint.status,
                complaint.status,
                (
                    "Existing complaint automatically forwarded to Electricity "
                    "Department Staff after an approved staff account became available."
                ),
            )
            notify_staff(
                selected.id,
                complaint,
                "New Complaint Assigned",
                (
                    f"Complaint {complaint.complaint_number} has been assigned to you "
                    f"for processing. Priority: {complaint.priority or 'Not specified'}."
                ),
                "Complaint Assignment",
            )
            notify_resident(
                complaint,
                "Complaint Assigned",
                (
                    f"Complaint {complaint.complaint_number} has been forwarded to "
                    "Electricity Department Staff for processing."
                ),
                "Complaint Assignment",
            )
            loads[selected.id] += 1
            assigned += 1

        if assigned:
            db.session.commit()
        return assigned

    @staticmethod
    def accept_complaint(complaint_id: int, staff_id: int) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if int(complaint.assigned_staff_id or 0) != int(staff_id):
            raise PermissionError("This complaint is not assigned to you.")

        status = normalize(complaint.status)
        if status not in {SUBMITTED.lower(), ESCALATED.lower()}:
            raise ValueError("Only assigned or escalated complaints can be accepted.")

        now = utc_now()
        old_status = complaint.status
        complaint.status = IN_PROGRESS
        complaint.accepted_at = complaint.accepted_at or now
        complaint.updated_at = now

        add_history(
            complaint,
            staff_id,
            "Complaint Accepted",
            old_status,
            IN_PROGRESS,
            "Complaint accepted by the assigned Electricity Department Staff member.",
        )
        notify_resident(
            complaint,
            "Complaint Accepted",
            (
                f"Complaint {complaint.complaint_number} has been accepted by the "
                "assigned staff member and is now being processed."
            ),
            "Complaint Status",
        )
        db.session.commit()
        return True

    @staticmethod
    def start_processing(complaint_id: int, staff_id: int) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if int(complaint.assigned_staff_id or 0) != int(staff_id):
            raise PermissionError("This complaint is not assigned to you.")
        if normalize(complaint.status) not in {
            SUBMITTED.lower(),
            IN_PROGRESS.lower(),
            ESCALATED.lower(),
        }:
            raise ValueError("Complaint cannot be moved to in-progress from its current status.")
        if normalize(complaint.status) == IN_PROGRESS.lower():
            return True
        return ComplaintService.accept_complaint(complaint_id, staff_id)

    @staticmethod
    def add_repair_notes(
        complaint_id: int,
        staff_id: int,
        repair_notes: str,
    ) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if int(complaint.assigned_staff_id or 0) != int(staff_id):
            raise PermissionError("This complaint is not assigned to you.")
        notes = str(repair_notes or "").strip()
        if not notes:
            raise ValueError("Repair notes are required.")
        if normalize(complaint.status) in TERMINAL_STATUSES:
            raise ValueError("Repair notes cannot be changed after complaint completion.")

        complaint.repair_notes = notes
        complaint.updated_at = utc_now()
        add_history(
            complaint,
            staff_id,
            "Repair Notes Added",
            complaint.status,
            complaint.status,
            notes,
        )
        db.session.commit()
        return True

    @staticmethod
    def resolve_complaint(
        complaint_id: int,
        staff_id: int,
        repair_notes: Optional[str] = None,
    ) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if int(complaint.assigned_staff_id or 0) != int(staff_id):
            raise PermissionError("This complaint is not assigned to you.")
        if normalize(complaint.status) not in {
            SUBMITTED.lower(),
            IN_PROGRESS.lower(),
            ESCALATED.lower(),
        }:
            raise ValueError("Only active assigned complaints can be resolved.")

        notes = str(repair_notes or complaint.repair_notes or "").strip()
        if not notes:
            raise ValueError("Repair notes are required before resolving a complaint.")

        old_status = complaint.status
        now = utc_now()
        complaint.repair_notes = notes
        complaint.final_action = notes
        complaint.status = RESOLVED
        complaint.resolved_at = now
        complaint.updated_at = now

        add_history(
            complaint,
            staff_id,
            "Complaint Resolved",
            old_status,
            RESOLVED,
            notes,
        )
        notify_resident(
            complaint,
            "Complaint Resolved",
            f"Complaint {complaint.complaint_number} has been marked as resolved.",
            "Complaint Resolved",
        )
        db.session.commit()
        return True

    @staticmethod
    def withdraw_complaint(complaint_id: int, resident_id: int) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if int(complaint.resident_id) != int(resident_id):
            raise PermissionError("You can only withdraw your own complaints.")
        if normalize(complaint.status) != SUBMITTED.lower():
            raise ValueError("A complaint can only be withdrawn before processing.")

        old_status = complaint.status
        complaint.status = WITHDRAWN
        complaint.withdrawn_at = utc_now()
        complaint.updated_at = utc_now()
        complaint.assigned_staff_id = None

        add_history(
            complaint,
            resident_id,
            "Complaint Withdrawn",
            old_status,
            WITHDRAWN,
            "Complaint withdrawn by resident.",
        )
        notify_resident(
            complaint,
            "Complaint Withdrawn",
            f"Complaint {complaint.complaint_number} has been withdrawn.",
            "Complaint Withdrawn",
        )
        db.session.commit()
        return True

    @staticmethod
    def update_status(
        complaint_id: int,
        new_status: str,
        changed_by: int,
        notes: Optional[str] = None,
    ) -> bool:
        """Controlled generic status transition for compatibility with routes."""
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")

        target = canonical_status(new_status)
        old = canonical_status(complaint.status)

        if target == old:
            return True
        if old in {WITHDRAWN, CLOSED}:
            raise ValueError("This complaint can no longer be updated.")

        if target == IN_PROGRESS:
            if old not in {SUBMITTED, ESCALATED}:
                raise ValueError("Only submitted or escalated complaints can move to in-progress.")
            if not complaint.assigned_staff_id:
                raise ValueError("Complaint must be assigned to staff before processing.")
            complaint.status = IN_PROGRESS
            complaint.accepted_at = complaint.accepted_at or utc_now()
        elif target == RESOLVED:
            if old not in {SUBMITTED, IN_PROGRESS, ESCALATED}:
                raise ValueError("Only active complaints can be resolved.")
            if not complaint.assigned_staff_id:
                raise ValueError("Complaint must be assigned to staff before resolution.")
            repair_notes = str(notes or complaint.repair_notes or "").strip()
            if not repair_notes:
                raise ValueError("Repair notes are required before resolving a complaint.")
            complaint.repair_notes = repair_notes
            complaint.final_action = repair_notes
            complaint.resolved_at = utc_now()
            complaint.status = RESOLVED
        elif target == ESCALATED:
            if old in {RESOLVED, WITHDRAWN, CLOSED}:
                raise ValueError("This complaint cannot be escalated.")
            complaint.status = ESCALATED
            complaint.escalated_at = utc_now()
            complaint.escalation_reason = str(notes or "Resolution deadline exceeded.").strip()
        elif target == WITHDRAWN:
            if old != SUBMITTED:
                raise ValueError("Only submitted complaints can be withdrawn.")
            complaint.status = WITHDRAWN
            complaint.withdrawn_at = utc_now()
        elif target == CLOSED:
            if old != RESOLVED:
                raise ValueError("Only resolved complaints can be closed.")
            complaint.status = CLOSED
        elif target == SUBMITTED:
            raise ValueError("Complaints cannot be returned to submitted status.")
        else:
            raise ValueError("Invalid complaint status.")

        complaint.updated_at = utc_now()
        add_history(
            complaint,
            changed_by,
            f"Complaint Status Changed to {target}",
            old,
            target,
            notes,
        )

        if target == RESOLVED:
            title = "Complaint Resolved"
            message = f"Complaint {complaint.complaint_number} has been marked as resolved."
            ntype = "Complaint Resolved"
        elif target == ESCALATED:
            title = "Complaint Escalated"
            message = f"Complaint {complaint.complaint_number} has been escalated for Senior Electricity Officer review."
            ntype = "Complaint Escalated"
        else:
            title = "Complaint Status Updated"
            message = f"Complaint {complaint.complaint_number} status is now {target}."
            ntype = "Complaint Status"

        notify_resident(complaint, title, message, ntype)
        db.session.commit()
        return True

    @staticmethod
    def get_assigned_complaints(
        staff_id: int,
        status: Optional[str] = None,
    ) -> list[Complaint]:
        query = Complaint.query.filter(Complaint.assigned_staff_id == staff_id)
        if status:
            query = query.filter(
                func.lower(Complaint.status) == normalize(canonical_status(status))
            )
        return (
            query.order_by(
                Complaint.created_at.asc(),
                Complaint.id.asc(),
            ).all()
        )

    @staticmethod
    def get_location_history(location: str, limit: int = 100) -> list[Complaint]:
        if not str(location or "").strip():
            return []
        limit = max(1, min(int(limit), 1000))
        return (
            Complaint.query
            .filter(func.lower(Complaint.location) == normalize(location))
            .order_by(Complaint.created_at.desc(), Complaint.id.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_complaints_for_escalation(
        current_datetime: Optional[datetime] = None,
    ) -> list[Complaint]:
        now = current_datetime or utc_now()
        return (
            Complaint.query
            .filter(
                Complaint.deadline_at.isnot(None),
                Complaint.deadline_at <= now,
                func.lower(func.coalesce(Complaint.status, "")).in_(
                    {
                        SUBMITTED.lower(),
                        IN_PROGRESS.lower(),
                    }
                ),
            )
            .order_by(Complaint.deadline_at.asc(), Complaint.id.asc())
            .all()
        )

    @staticmethod
    def mark_escalated(
        complaint_id: int,
        officer_id: int,
        reason: str,
    ) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("An escalation reason is required.")
        if normalize(complaint.status) in TERMINAL_STATUSES:
            raise ValueError("This complaint cannot be escalated.")

        old = complaint.status
        complaint.status = ESCALATED
        complaint.escalated_at = utc_now()
        complaint.escalation_reason = reason
        complaint.updated_at = utc_now()
        add_history(
            complaint,
            officer_id,
            "Complaint Escalated",
            old,
            ESCALATED,
            reason,
        )
        notify_resident(
            complaint,
            "Complaint Escalated",
            f"Complaint {complaint.complaint_number} has been escalated for senior officer review.",
            "Complaint Escalated",
        )
        if complaint.assigned_staff_id:
            notify_staff(
                complaint.assigned_staff_id,
                complaint,
                "Complaint Escalated",
                f"Complaint {complaint.complaint_number} has been escalated for Senior Electricity Officer review.",
                "Complaint Escalated",
            )
        db.session.commit()
        return True

    @staticmethod
    def auto_escalate_overdue_complaints() -> int:
        complaints = ComplaintService.get_complaints_for_escalation()
        changed = 0
        officers = get_available_officers()

        for complaint in complaints:
            if escalation_history_exists(complaint.id):
                continue
            old = complaint.status
            complaint.status = ESCALATED
            complaint.escalated_at = utc_now()
            complaint.escalation_reason = (
                "Resolution deadline expired before the complaint was resolved."
            )
            complaint.updated_at = utc_now()

            actor = complaint.assigned_staff_id or complaint.resident_id
            add_history(
                complaint,
                actor,
                "Complaint Escalated",
                old,
                ESCALATED,
                complaint.escalation_reason,
            )
            notify_resident(
                complaint,
                "Complaint Escalated",
                (
                    f"Complaint {complaint.complaint_number} was escalated to the "
                    "Senior Electricity Officer because its resolution deadline expired."
                ),
                "Complaint Escalated",
            )
            if complaint.assigned_staff_id:
                notify_staff(
                    complaint.assigned_staff_id,
                    complaint,
                    "Complaint Escalated",
                    (
                        f"Complaint {complaint.complaint_number} exceeded its resolution "
                        "period and is now escalated for Senior Electricity Officer review."
                    ),
                    "Complaint Escalated",
                )
            for officer in officers:
                create_notification(
                    officer.id,
                    complaint,
                    "Escalated Complaint Requires Review",
                    (
                        f"Complaint {complaint.complaint_number} has been automatically "
                        "escalated because its resolution deadline expired."
                    ),
                    "Escalation Review",
                )
            changed += 1

        if changed:
            db.session.commit()
        return changed

    @staticmethod
    def get_escalated_complaints(
        status: Optional[str] = None,
    ) -> list[Complaint]:
        query = Complaint.query.filter(
            func.lower(Complaint.status) == ESCALATED.lower()
        )
        if status:
            query = query.filter(
                func.lower(Complaint.status) == normalize(canonical_status(status))
            )
        return query.order_by(Complaint.escalated_at.asc(), Complaint.id.asc()).all()

    @staticmethod
    def close_complaint(complaint_id: int, user_id: int) -> bool:
        complaint = ComplaintService.get_complaint_by_id(complaint_id)
        if complaint is None:
            raise ValueError("Complaint not found.")
        if normalize(complaint.status) != RESOLVED.lower():
            raise ValueError("Only resolved complaints can be closed.")
        return ComplaintService.update_status(
            complaint_id,
            CLOSED,
            user_id,
            "Resolved complaint closed.",
        )

    @staticmethod
    def count_complaints(
        status: Optional[str] = None,
        complaint_type: Optional[str] = None,
    ) -> int:
        query = Complaint.query
        if status:
            query = query.filter(
                func.lower(Complaint.status) == normalize(canonical_status(status))
            )
        if complaint_type:
            canonical_type = canonical_complaint_type(complaint_type)
            query = query.filter(
                func.lower(Complaint.complaint_type) == normalize(canonical_type)
            )
        return query.count()

    @staticmethod
    def get_complaint_statistics() -> dict[str, Any]:
        complaints = Complaint.query.all()
        stats = {
            "total": len(complaints),
            "submitted": 0,
            "assigned": 0,
            "in_progress": 0,
            "resolved": 0,
            "escalated": 0,
            "withdrawn": 0,
            "closed": 0,
            "unassigned": 0,
        }
        for complaint in complaints:
            status = normalize(complaint.status)
            if status == SUBMITTED.lower():
                stats["submitted"] += 1
            elif status == IN_PROGRESS.lower():
                stats["in_progress"] += 1
            elif status == RESOLVED.lower():
                stats["resolved"] += 1
            elif status == ESCALATED.lower():
                stats["escalated"] += 1
            elif status == WITHDRAWN.lower():
                stats["withdrawn"] += 1
            elif status == CLOSED.lower():
                stats["closed"] += 1
            if complaint.assigned_staff_id:
                stats["assigned"] += 1
            elif status == SUBMITTED.lower():
                stats["unassigned"] += 1
        return stats

    @staticmethod
    def get_complaints_by_type() -> list[dict[str, Any]]:
        rows = (
            db.session.query(
                Complaint.complaint_type,
                func.count(Complaint.id).label("total"),
            )
            .group_by(Complaint.complaint_type)
            .order_by(func.count(Complaint.id).desc())
            .all()
        )
        return [
            {"complaint_type": row[0], "total": int(row[1])}
            for row in rows
        ]

    @staticmethod
    def get_complaints_by_location() -> list[dict[str, Any]]:
        rows = (
            db.session.query(
                Complaint.location,
                func.count(Complaint.id).label("total"),
            )
            .group_by(Complaint.location)
            .order_by(func.count(Complaint.id).desc())
            .all()
        )
        result = []
        for location, total in rows:
            resolved = Complaint.query.filter(
                Complaint.location == location,
                func.lower(Complaint.status) == RESOLVED.lower(),
            ).count()
            escalated = Complaint.query.filter(
                Complaint.location == location,
                func.lower(Complaint.status) == ESCALATED.lower(),
            ).count()
            result.append(
                {
                    "location": location,
                    "total": int(total),
                    "resolved": int(resolved),
                    "escalated": int(escalated),
                }
            )
        return result

    @staticmethod
    def get_recent_complaints(limit: int = 10) -> list[Complaint]:
        limit = max(1, min(int(limit), 100))
        return (
            Complaint.query
            .order_by(Complaint.created_at.desc(), Complaint.id.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_complaints_by_resident(
        resident_id: int,
        status: Optional[str] = None,
        complaint_type: Optional[str] = None,
    ) -> list[Complaint]:
        return ComplaintService.get_resident_complaints(
            resident_id,
            status,
            complaint_type,
        )


# ---------------------------------------------------------------------------
# Staff / officer lookup helpers
# ---------------------------------------------------------------------------


def approved(user: User) -> bool:
    return normalize(getattr(user, "approval_status", "")) == "approved"


def get_available_staff() -> list[User]:
    return (
        User.query
        .filter(
            func.lower(func.coalesce(User.role, "")) == "staff",
            User.is_active.is_(True),
            func.lower(func.coalesce(User.approval_status, "")) == "approved",
        )
        .order_by(User.id.asc())
        .all()
    )


def get_available_officers() -> list[User]:
    return (
        User.query
        .filter(
            func.lower(func.coalesce(User.role, "")) == "officer",
            User.is_active.is_(True),
            func.lower(func.coalesce(User.approval_status, "")) == "approved",
        )
        .order_by(User.id.asc())
        .all()
    )


def active_staff_load(staff_id: int) -> int:
    return (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(func.coalesce(Complaint.status, "")).in_(
                {
                    SUBMITTED.lower(),
                    IN_PROGRESS.lower(),
                    ESCALATED.lower(),
                }
            ),
        )
        .count()
    )


def escalation_history_exists(complaint_id: int) -> bool:
    return (
        ComplaintHistory.query
        .filter(
            ComplaintHistory.complaint_id == complaint_id,
            func.lower(func.coalesce(ComplaintHistory.action, "")).like("%escalat%"),
        )
        .first()
        is not None
    )


def auto_assign_unassigned_complaints() -> int:
    return ComplaintService.auto_assign_unassigned_complaints()


def auto_escalate_overdue_complaints() -> int:
    return ComplaintService.auto_escalate_overdue_complaints()


# ---------------------------------------------------------------------------
# Module-level compatibility wrappers used by existing routes/services.
# ---------------------------------------------------------------------------


def create_complaint(
    resident_id: int,
    form: Any = None,
    complaint_type: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    complaint_datetime: Optional[str] = None,
    preferred_resolution_hours: Optional[int] = None,
    image_path: Optional[str] = None,
    complaint_date: Any = None,
    complaint_time: Any = None,
    resolution_hours: Optional[int] = None,
    image_filename: Optional[str] = None,
    priority: Optional[str] = None,
) -> int:
    return ComplaintService.create_complaint(
        resident_id=resident_id,
        form=form,
        complaint_type=complaint_type,
        description=description,
        location=location,
        complaint_datetime=complaint_datetime,
        preferred_resolution_hours=preferred_resolution_hours,
        image_path=image_path,
        complaint_date=complaint_date,
        complaint_time=complaint_time,
        resolution_hours=resolution_hours,
        image_filename=image_filename,
        priority=priority,
    )


def get_complaint_by_id(complaint_id: int) -> Optional[Complaint]:
    return ComplaintService.get_complaint_by_id(complaint_id)


def get_complaints_by_resident(
    resident_id: int,
    status: Optional[str] = None,
    complaint_type: Optional[str] = None,
) -> list[Complaint]:
    return ComplaintService.get_complaints_by_resident(
        resident_id,
        status,
        complaint_type,
    )


def get_all_complaints(
    status: Optional[str] = None,
    complaint_type: Optional[str] = None,
    priority: Optional[str] = None,
) -> list[Complaint]:
    return ComplaintService.get_all_complaints(status, complaint_type, priority)


def update_complaint(complaint_id: int, **changes: Any) -> Complaint:
    return ComplaintService.update_complaint(complaint_id, **changes)


def delete_complaint(complaint_id: int) -> bool:
    return ComplaintService.delete_complaint(complaint_id)


def update_complaint_status(
    complaint_id: int,
    new_status: str,
    changed_by: int,
    notes: Optional[str] = None,
) -> bool:
    return ComplaintService.update_status(complaint_id, new_status, changed_by, notes)


def withdraw_complaint(complaint_id: int, resident_id: int) -> bool:
    return ComplaintService.withdraw_complaint(complaint_id, resident_id)


def assign_complaint(
    complaint_id: int,
    staff_id: int,
    assigned_by: Optional[int] = None,
    notes: Optional[str] = None,
) -> bool:
    return ComplaintService.assign_complaint(
        complaint_id,
        staff_id,
        assigned_by,
        notes,
    )


def accept_complaint(complaint_id: int, staff_id: int) -> bool:
    return ComplaintService.accept_complaint(complaint_id, staff_id)


def start_processing(complaint_id: int, staff_id: int) -> bool:
    return ComplaintService.start_processing(complaint_id, staff_id)


def add_repair_notes(complaint_id: int, staff_id: int, repair_notes: str) -> bool:
    return ComplaintService.add_repair_notes(complaint_id, staff_id, repair_notes)


def resolve_complaint(
    complaint_id: int,
    staff_id: int,
    repair_notes: Optional[str] = None,
) -> bool:
    return ComplaintService.resolve_complaint(
        complaint_id,
        staff_id,
        repair_notes,
    )


def get_assigned_complaints(
    staff_id: int,
    status: Optional[str] = None,
) -> list[Complaint]:
    return ComplaintService.get_assigned_complaints(staff_id, status)


def get_location_history(location: str, limit: int = 100) -> list[Complaint]:
    return ComplaintService.get_location_history(location, limit)


def get_complaints_for_escalation(
    current_datetime: Optional[datetime] = None,
) -> list[Complaint]:
    return ComplaintService.get_complaints_for_escalation(current_datetime)


def mark_escalated(complaint_id: int, officer_id: int, reason: str) -> bool:
    return ComplaintService.mark_escalated(complaint_id, officer_id, reason)


def get_escalated_complaints(
    status: Optional[str] = None,
    active_only: Optional[bool] = None,
) -> list[Complaint]:
    complaints = ComplaintService.get_escalated_complaints(status)
    if active_only:
        complaints = [
            item for item in complaints
            if normalize(item.status) == ESCALATED.lower()
        ]
    return complaints


def close_complaint(complaint_id: int, user_id: int) -> bool:
    return ComplaintService.close_complaint(complaint_id, user_id)


def count_complaints(
    status: Optional[str] = None,
    complaint_type: Optional[str] = None,
) -> int:
    return ComplaintService.count_complaints(status, complaint_type)


def get_complaint_statistics() -> dict[str, Any]:
    return ComplaintService.get_complaint_statistics()


def get_complaints_by_type() -> list[dict[str, Any]]:
    return ComplaintService.get_complaints_by_type()


def get_complaints_by_location() -> list[dict[str, Any]]:
    return ComplaintService.get_complaints_by_location()


def get_recent_complaints(limit: int = 10) -> list[Complaint]:
    return ComplaintService.get_recent_complaints(limit)


__all__ = [
    "ComplaintService",
    "SUBMITTED",
    "IN_PROGRESS",
    "RESOLVED",
    "ESCALATED",
    "WITHDRAWN",
    "CLOSED",
    "create_complaint",
    "get_complaint_by_id",
    "get_complaints_by_resident",
    "get_all_complaints",
    "update_complaint",
    "delete_complaint",
    "update_complaint_status",
    "withdraw_complaint",
    "assign_complaint",
    "accept_complaint",
    "start_processing",
    "add_repair_notes",
    "resolve_complaint",
    "get_assigned_complaints",
    "get_location_history",
    "get_complaints_for_escalation",
    "mark_escalated",
    "get_escalated_complaints",
    "close_complaint",
    "count_complaints",
    "get_complaint_statistics",
    "get_complaints_by_type",
    "get_complaints_by_location",
    "get_recent_complaints",
    "get_available_staff",
    "get_available_officers",
    "auto_assign_unassigned_complaints",
    "auto_escalate_overdue_complaints",
]
