from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.models import Complaint
from app.services.complaint_service import (
    find_expired_complaints,
    mark_escalated,
)
from app.services.notification_service import (
    notify_officers_about_escalation,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def process_expired_complaints() -> dict:
    """
    Find unresolved complaints whose selected 24/48-hour
    resolution period has expired and automatically escalate them.
    """

    expired_complaints = (
        find_expired_complaints()
    )

    processed = 0
    escalated = 0
    skipped = 0
    errors = []

    for complaint in expired_complaints:
        processed += 1

        try:
            if complaint.status in {
                "Resolved",
                "Withdrawn",
            }:
                skipped += 1
                continue

            if complaint.was_escalated:
                skipped += 1
                continue

            mark_escalated(
                complaint=complaint,
                escalated_by_user_id=None,
                reason=(
                    f"Complaint automatically escalated because "
                    f"the selected {complaint.resolution_hours}-hour "
                    f"resolution period expired."
                ),
            )

            db.session.commit()

            notify_officers_about_escalation(
                complaint
            )

            escalated += 1

        except Exception as exc:
            db.session.rollback()

            errors.append(
                {
                    "complaint_id": complaint.id,
                    "complaint_number": (
                        complaint.complaint_number
                    ),
                    "error": str(exc),
                }
            )

    return {
        "processed": processed,
        "escalated": escalated,
        "skipped": skipped,
        "errors": errors,
        "checked_at": utc_now().isoformat(),
    }


def check_complaint_deadlines() -> dict:
    """
    Execute the automatic complaint deadline check.

    This function is the main entry point for the application's
    automatic escalation process.
    """

    return process_expired_complaints()


def get_expired_complaint_count() -> int:
    """
    Return the number of unresolved complaints currently
    past their resolution deadline.
    """

    return len(
        find_expired_complaints()
    )


def get_pending_escalation_complaints():
    """
    Return complaints that have exceeded their deadline and
    still require automatic escalation processing.
    """

    return find_expired_complaints()


def process_single_complaint(
    complaint_id: int,
) -> dict:
    """
    Process the deadline of one specific complaint.

    This is useful when the application needs to check an
    individual complaint immediately instead of waiting for
    the general deadline check.
    """

    complaint = db.session.get(
        Complaint,
        complaint_id,
    )

    if not complaint:
        return {
            "success": False,
            "message": "Complaint was not found.",
        }

    if complaint.status in {
        "Resolved",
        "Withdrawn",
    }:
        return {
            "success": False,
            "message": (
                "Complaint does not require escalation."
            ),
            "complaint_id": complaint.id,
            "status": complaint.status,
        }

    if complaint.was_escalated:
        return {
            "success": False,
            "message": (
                "Complaint has already been escalated."
            ),
            "complaint_id": complaint.id,
        }

    now = utc_now()

    deadline = complaint.deadline_at

    if deadline is None:
        return {
            "success": False,
            "message": (
                "Complaint does not have a resolution deadline."
            ),
            "complaint_id": complaint.id,
        }

    if deadline.tzinfo is None:
        deadline = deadline.replace(
            tzinfo=timezone.utc
        )

    if deadline > now:
        return {
            "success": False,
            "message": (
                "Complaint resolution period has not expired."
            ),
            "complaint_id": complaint.id,
            "deadline_at": deadline.isoformat(),
        }

    try:
        mark_escalated(
            complaint=complaint,
            escalated_by_user_id=None,
            reason=(
                f"Complaint automatically escalated because "
                f"the selected {complaint.resolution_hours}-hour "
                f"resolution period expired."
            ),
        )

        db.session.commit()

        notify_officers_about_escalation(
            complaint
        )

        return {
            "success": True,
            "message": (
                "Complaint automatically escalated."
            ),
            "complaint_id": complaint.id,
            "complaint_number": (
                complaint.complaint_number
            ),
            "status": complaint.status,
            "escalated_at": (
                complaint.escalation_at.isoformat()
                if complaint.escalation_at
                else None
            ),
        }

    except Exception as exc:
        db.session.rollback()

        return {
            "success": False,
            "message": (
                "Complaint escalation failed."
            ),
            "complaint_id": complaint.id,
            "error": str(exc),
        }


def get_complaint_deadline_status(
    complaint: Complaint,
) -> dict:
    """
    Return the current deadline information for a complaint.
    """

    now = utc_now()

    deadline = complaint.deadline_at

    if deadline is None:
        return {
            "has_deadline": False,
            "expired": False,
            "remaining_seconds": None,
            "deadline_at": None,
        }

    if deadline.tzinfo is None:
        deadline = deadline.replace(
            tzinfo=timezone.utc
        )

    remaining_seconds = (
        deadline - now
    ).total_seconds()

    expired = (
        remaining_seconds <= 0
    )

    return {
        "has_deadline": True,
        "expired": expired,
        "remaining_seconds": max(
            0,
            int(
                remaining_seconds
            ),
        ),
        "deadline_at": deadline.isoformat(),
        "resolution_hours": (
            complaint.resolution_hours
        ),
        "status": complaint.status,
        "was_escalated": (
            complaint.was_escalated
        ),
    }


def should_escalate(
    complaint: Complaint,
) -> bool:
    """
    Determine whether a complaint currently qualifies for
    automatic escalation.
    """

    if complaint.status in {
        "Resolved",
        "Withdrawn",
        "Escalated",
    }:
        return False

    if complaint.was_escalated:
        return False

    deadline = complaint.deadline_at

    if deadline is None:
        return False

    if deadline.tzinfo is None:
        deadline = deadline.replace(
            tzinfo=timezone.utc
        )

    return deadline <= utc_now()


def run_automation_cycle() -> dict:
    """
    Run the complete complaint automation cycle.

    Current automation:
        1. Find unresolved complaints.
        2. Check their selected 24/48-hour deadlines.
        3. Automatically escalate expired complaints.
        4. Notify Senior Electricity Officers.
        5. Keep resolved and withdrawn complaints unchanged.
    """

    return process_expired_complaints()


def get_automation_summary() -> dict:
    """
    Return a simple automation status summary for dashboards
    or administrative monitoring.
    """

    pending = (
        get_pending_escalation_complaints()
    )

    return {
        "checked_at": utc_now().isoformat(),
        "pending_escalations": len(
            pending
        ),
        "complaint_ids": [
            complaint.id
            for complaint in pending
        ],
        "complaint_numbers": [
            complaint.complaint_number
            for complaint in pending
        ],
    }