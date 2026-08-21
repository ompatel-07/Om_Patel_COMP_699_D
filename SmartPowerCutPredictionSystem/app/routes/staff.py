from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import case, func

from app.extensions import db
from app.models import (
    Complaint,
    ComplaintHistory,
    Notification,
    User,
)


staff_bp = Blueprint(
    "staff",
    __name__,
    url_prefix="/staff",
)


SUBMITTED = "Submitted"
IN_PROGRESS = "In Progress"
RESOLVED = "Resolved"
ESCALATED = "Escalated"
WITHDRAWN = "Withdrawn"
ASSIGNED = "Assigned"

# Complaints that are waiting for an ordinary staff assignment.
# "Assigned" is retained for compatibility with older database records
# created by earlier versions of the application.
ASSIGNABLE_STATUSES = {
    SUBMITTED.lower(),
    ASSIGNED.lower(),
}


PRIORITY_ORDER = case(
    (func.lower(func.coalesce(Complaint.priority, "")) == "critical", 1),
    (func.lower(func.coalesce(Complaint.priority, "")) == "high", 2),
    (func.lower(func.coalesce(Complaint.priority, "")) == "medium", 3),
    (func.lower(func.coalesce(Complaint.priority, "")) == "low", 4),
    else_=5,
)


# ============================================================================
# GENERAL HELPERS
# ============================================================================

def utc_now():
    return datetime.now(timezone.utc)


def normalize(value):
    return str(value or "").strip().lower()


def approved(user):
    return normalize(getattr(user, "approval_status", None)) == "approved"


def current_staff():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def staff_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        user_id = session.get("user_id")

        if not user_id:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("auth.login"))

        user = db.session.get(User, user_id)

        if user is None:
            session.clear()
            flash("Your account could not be found.", "error")
            return redirect(url_for("auth.login"))

        if normalize(getattr(user, "role", None)) != "staff":
            flash(
                "You are not authorized to access the staff area.",
                "error",
            )
            return redirect(url_for("auth.login"))

        if not getattr(user, "is_active", False):
            session.clear()
            flash("Your account is inactive.", "error")
            return redirect(url_for("auth.login"))

        if not approved(user):
            session.clear()
            flash(
                "Your staff account has not been approved.",
                "error",
            )
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped_view


# ============================================================================
# HISTORY / NOTIFICATIONS
# ============================================================================

def add_history(
    complaint,
    user_id,
    action,
    old_status=None,
    new_status=None,
    notes=None,
):
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
    user_id,
    complaint,
    title,
    message,
    notification_type,
):
    notification = Notification(
        user_id=user_id,
        complaint_id=complaint.id if complaint else None,
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
    complaint,
    title,
    message,
    notification_type,
):
    if getattr(complaint, "resident_id", None):
        create_notification(
            user_id=complaint.resident_id,
            complaint=complaint,
            title=title,
            message=message,
            notification_type=notification_type,
        )


def notify_staff(
    staff,
    complaint,
    title,
    message,
    notification_type,
):
    if staff is not None and getattr(staff, "id", None):
        create_notification(
            user_id=staff.id,
            complaint=complaint,
            title=title,
            message=message,
            notification_type=notification_type,
        )


def get_staff_notifications(staff_id, unread_only=False, limit=None):
    query = (
        Notification.query
        .filter(Notification.user_id == staff_id)
        .order_by(
            Notification.created_at.desc(),
            Notification.id.desc(),
        )
    )

    if unread_only:
        query = query.filter(
            Notification.is_read.is_(False)
        )

    if limit:
        query = query.limit(limit)

    return query.all()


# ============================================================================
# AUTOMATIC INITIAL ASSIGNMENT
# ============================================================================

def get_available_staff():
    """
    Return only active, approved Electricity Department Staff accounts.

    Officers and administrators are deliberately excluded from ordinary
    complaint assignment.
    """
    return (
        User.query
        .filter(
            func.lower(func.coalesce(User.role, "")) == "staff",
            User.is_active.is_(True),
            func.lower(
                func.coalesce(User.approval_status, "")
            ) == "approved",
        )
        .order_by(User.id.asc())
        .all()
    )


def active_staff_load(staff_id):
    return (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(
                ASSIGNABLE_STATUSES
                | {
                    IN_PROGRESS.lower(),
                    ESCALATED.lower(),
                }
            ),
        )
        .count()
    )


def auto_assign_unassigned_complaints():
    """
    Forward Submitted complaints that still have no staff assignment.

    The current resident submission flow creates Submitted complaints without
    assigned_staff_id. The project workflow requires those complaints to be
    forwarded to Electricity Department Staff. This function fills that gap.

    Assignment policy:
    - Submitted complaints are assigned; legacy Assigned records with no
      staff member are also repaired automatically;
    - withdrawn/resolved/escalated complaints are not assigned here;
    - active approved staff are eligible;
    - the staff member with the smallest active workload is selected;
    - existing assignments are never changed.
    """
    staff_members = get_available_staff()

    if not staff_members:
        return 0

    complaints = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id.is_(None),
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(ASSIGNABLE_STATUSES),
        )
        .order_by(
            PRIORITY_ORDER.asc(),
            Complaint.created_at.asc(),
            Complaint.id.asc(),
        )
        .all()
    )

    if not complaints:
        return 0

    loads = {
        staff.id: active_staff_load(staff.id)
        for staff in staff_members
    }

    assigned = 0

    for complaint in complaints:
        if complaint.assigned_staff_id is not None:
            continue

        selected = min(
            staff_members,
            key=lambda item: (loads[item.id], item.id),
        )

        now = utc_now()
        complaint.assigned_staff_id = selected.id

        if hasattr(Complaint, "assigned_at"):
            complaint.assigned_at = now

        if hasattr(Complaint, "updated_at"):
            complaint.updated_at = now

        add_history(
            complaint=complaint,
            user_id=selected.id,
            action="Complaint Assigned",
            old_status=complaint.status,
            new_status=complaint.status,
            notes=(
                "Complaint forwarded to Electricity Department Staff "
                "using automatic workload-based assignment."
            ),
        )

        notify_staff(
            staff=selected,
            complaint=complaint,
            title="New Complaint Assigned",
            message=(
                f"Complaint {complaint.complaint_number} has been "
                f"assigned to you for processing. "
                f"Priority: {complaint.priority or 'Not specified'}."
            ),
            notification_type="Complaint Assignment",
        )

        notify_resident(
            complaint=complaint,
            title="Complaint Assigned",
            message=(
                f"Complaint {complaint.complaint_number} has been "
                "forwarded to Electricity Department Staff for processing."
            ),
            notification_type="Complaint Assignment",
        )

        loads[selected.id] += 1
        assigned += 1

    if assigned:
        db.session.commit()

    return assigned


# ============================================================================
# AUTOMATIC ESCALATION
# ============================================================================

def get_officers():
    return (
        User.query
        .filter(
            func.lower(func.coalesce(User.role, "")) == "officer",
            User.is_active.is_(True),
            func.lower(
                func.coalesce(User.approval_status, "")
            ) == "approved",
        )
        .order_by(User.id.asc())
        .all()
    )


def escalation_history_exists(complaint_id):
    return (
        ComplaintHistory.query
        .filter(
            ComplaintHistory.complaint_id == complaint_id,
            func.lower(
                func.coalesce(ComplaintHistory.action, "")
            ).like("%escalat%"),
        )
        .first()
        is not None
    )


def auto_escalate_overdue_complaints():
    """
    Escalate active unresolved complaints after deadline_at.

    Complaint.was_escalated is intentionally NOT used because that attribute
    does not exist in the current Complaint model. Status and complaint
    history are used instead.
    """
    now = utc_now()

    overdue = (
        Complaint.query
        .filter(
            Complaint.deadline_at.isnot(None),
            Complaint.deadline_at <= now,
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(
                {
                    SUBMITTED.lower(),
                    IN_PROGRESS.lower(),
                }
            ),
        )
        .order_by(
            Complaint.deadline_at.asc(),
            Complaint.id.asc(),
        )
        .all()
    )

    if not overdue:
        return 0

    officers = get_officers()
    changed = 0

    for complaint in overdue:
        if escalation_history_exists(complaint.id):
            continue

        old_status = complaint.status
        complaint.status = ESCALATED

        if hasattr(Complaint, "updated_at"):
            complaint.updated_at = now

        changed_by = (
            complaint.assigned_staff_id
            or complaint.resident_id
        )

        add_history(
            complaint=complaint,
            user_id=changed_by,
            action="Complaint Escalated",
            old_status=old_status,
            new_status=ESCALATED,
            notes=(
                "Complaint automatically escalated because the selected "
                f"{complaint.resolution_hours or ''}-hour resolution "
                "period expired without resolution."
            ),
        )

        notify_resident(
            complaint=complaint,
            title="Complaint Escalated",
            message=(
                f"Complaint {complaint.complaint_number} was escalated "
                "to the Senior Electricity Officer because its resolution "
                "deadline expired before resolution."
            ),
            notification_type="Complaint Escalated",
        )

        if complaint.assigned_staff_id:
            staff = db.session.get(
                User,
                complaint.assigned_staff_id,
            )
            notify_staff(
                staff=staff,
                complaint=complaint,
                title="Complaint Escalated",
                message=(
                    f"Complaint {complaint.complaint_number} exceeded "
                    "its resolution period and is now escalated for "
                    "Senior Electricity Officer review."
                ),
                notification_type="Complaint Escalated",
            )

        for officer in officers:
            create_notification(
                user_id=officer.id,
                complaint=complaint,
                title="Escalated Complaint Requires Review",
                message=(
                    f"Complaint {complaint.complaint_number} has been "
                    "automatically escalated because its resolution "
                    "deadline expired."
                ),
                notification_type="Escalation Review",
            )

        changed += 1

    if changed:
        db.session.commit()

    return changed


# ============================================================================
# DASHBOARD HELPERS
# ============================================================================

def dashboard_counts(staff_id):
    total_assigned = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id
        )
        .count()
    )

    pending = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(ASSIGNABLE_STATUSES),
        )
        .count()
    )

    in_progress = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(
                func.coalesce(Complaint.status, "")
            ) == IN_PROGRESS.lower(),
        )
        .count()
    )

    resolved = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(
                func.coalesce(Complaint.status, "")
            ) == RESOLVED.lower(),
        )
        .count()
    )

    escalated = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            func.lower(
                func.coalesce(Complaint.status, "")
            ) == ESCALATED.lower(),
        )
        .count()
    )

    overdue = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff_id,
            Complaint.deadline_at.isnot(None),
            Complaint.deadline_at <= utc_now(),
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(
                {
                    SUBMITTED.lower(),
                    IN_PROGRESS.lower(),
                    ESCALATED.lower(),
                }
            ),
        )
        .count()
    )

    unread = (
        Notification.query
        .filter(
            Notification.user_id == staff_id,
            Notification.is_read.is_(False),
        )
        .count()
    )

    return {
        "total_assigned": total_assigned,
        "pending": pending,
        "in_progress": in_progress,
        "resolved": resolved,
        "escalated": escalated,
        "overdue": overdue,
        "unread": unread,
    }


def staff_complaints(
    staff_id,
    active_only=True,
    status_filter=None,
):
    query = Complaint.query.filter(
        Complaint.assigned_staff_id == staff_id
    )

    if status_filter:
        query = query.filter(
            func.lower(
                func.coalesce(Complaint.status, "")
            ) == normalize(status_filter)
        )
    elif active_only:
        query = query.filter(
            func.lower(
                func.coalesce(Complaint.status, "")
            ).in_(
                {
                    SUBMITTED.lower(),
                    IN_PROGRESS.lower(),
                    ESCALATED.lower(),
                }
            )
        )

    return (
        query
        .order_by(
            PRIORITY_ORDER.asc(),
            Complaint.deadline_at.asc(),
            Complaint.created_at.asc(),
            Complaint.id.asc(),
        )
        .all()
    )


def _dashboard_complaint_rows(complaints):
    """
    Build template-safe complaint rows for the staff dashboard.

    The dashboard template has existed in several versions and may reference
    either `resident.name` or `user.name`, while the current User model stores
    the display name as `full_name`.  Returning small, stable view objects here
    keeps the dashboard presentation independent from ORM relationship naming
    and prevents one malformed optional relationship from hiding the entire
    assigned-complaint queue.
    """
    rows = []

    for complaint in complaints or []:
        resident = getattr(complaint, "resident", None)
        full_name = getattr(resident, "full_name", None) or "Resident"

        resident_view = SimpleNamespace(name=full_name)

        rows.append(
            SimpleNamespace(
                id=getattr(complaint, "id", None),
                complaint_number=getattr(complaint, "complaint_number", None),
                complaint_type=getattr(complaint, "complaint_type", None),
                location=getattr(complaint, "location", None),
                priority=getattr(complaint, "priority", None),
                status=getattr(complaint, "status", None),
                resolution_hours=getattr(complaint, "resolution_hours", None),
                preferred_resolution_time=getattr(
                    complaint,
                    "preferred_resolution_time",
                    None,
                ),
                created_at=getattr(complaint, "created_at", None),
                resident=resident_view,
                user=resident_view,
            )
        )

    return rows


def render_dashboard(
    staff,
    complaints=None,
    status_filter="",
    history=None,
):
    counts = dashboard_counts(staff.id)

    notifications = get_staff_notifications(
        staff.id,
        unread_only=False,
        limit=20,
    )

    # If the caller did not provide a queue, always retrieve the staff's
    # current active assignments directly from the database.  This is important
    # when a complaint was submitted before the staff account was registered:
    # the first approved staff dashboard visit can assign and then immediately
    # display those existing complaints.
    if complaints is None:
        complaints = staff_complaints(
            staff.id,
            active_only=True,
        )

    complaint_list = list(complaints or [])
    dashboard_rows = _dashboard_complaint_rows(complaint_list)

    # The staff dashboard templates used by this project have existed in
    # several versions.  Some versions expect `complaints`, some expect
    # `assignments`, and some expect `assigned_complaints`.  Supplying all
    # three aliases keeps the route/template contract stable and prevents a
    # valid assignment from appearing as an empty dashboard.
    active_assigned = (
        counts["pending"]
        + counts["in_progress"]
        + counts["escalated"]
    )

    return render_template(
        "staff/dashboard.html",
        staff=staff,
        complaints=dashboard_rows,
        assignments=dashboard_rows,
        assigned_complaints=dashboard_rows,
        active_assigned_complaints=dashboard_rows,
        history=history or [],
        notifications=notifications,
        unread_notifications=counts["unread"],
        unread_notification_count=counts["unread"],
        status_filter=status_filter,
        # `total_assigned` is the lifetime assignment count.
        total_assigned=counts["total_assigned"],
        # `assigned_count` is the current actionable queue count.
        # This is the number the dashboard's "Assigned Complaints" card
        # should display; it therefore agrees with Pending/In Progress/
        # Escalated counts.
        assigned_count=active_assigned,
        assigned_complaints_count=active_assigned,
        pending_complaints=counts["pending"],
        pending_count=counts["pending"],
        in_progress_complaints=counts["in_progress"],
        in_progress_count=counts["in_progress"],
        resolved_complaints=counts["resolved"],
        resolved_count=counts["resolved"],
        escalated_complaints=counts["escalated"],
        overdue_complaints=counts["overdue"],
    )


# ============================================================================
# STAFF DASHBOARD
# ============================================================================

@staff_bp.route("/dashboard")
@staff_required
def dashboard():
    staff = current_staff()

    try:
        # Existing complaints can still have assigned_staff_id = NULL.
        # This also handles complaints submitted before this staff account
        # existed: once the staff member is active and approved, the next
        # dashboard visit assigns the waiting complaints automatically.
        auto_assign_unassigned_complaints()

        # Check the configured 24/48-hour resolution deadline.
        auto_escalate_overdue_complaints()

        complaints = staff_complaints(
            staff.id,
            active_only=True,
        )

    except Exception:
        current_app.logger.exception(
            "Staff dashboard queue preparation failed for staff_id=%s",
            staff.id,
        )
        db.session.rollback()
        flash(
            "The staff complaint queue could not be refreshed. Showing current assignments.",
            "warning",
        )

        # Do not replace a valid assigned queue with [] merely because the
        # automatic assignment/escalation maintenance step failed.
        complaints = (
            Complaint.query
            .filter(Complaint.assigned_staff_id == staff.id)
            .order_by(
                PRIORITY_ORDER.asc(),
                Complaint.deadline_at.asc(),
                Complaint.created_at.asc(),
                Complaint.id.asc(),
            )
            .all()
        )

    return render_dashboard(
        staff=staff,
        complaints=complaints,
    )


# ============================================================================
# ASSIGNED COMPLAINTS
# ============================================================================

@staff_bp.route("/assigned-complaints")
@staff_required
def assigned_complaints():
    staff = current_staff()
    status_filter = request.args.get(
        "status",
        "",
    ).strip()

    try:
        auto_assign_unassigned_complaints()
        auto_escalate_overdue_complaints()

        complaints = staff_complaints(
            staff.id,
            active_only=not bool(status_filter),
            status_filter=status_filter or None,
        )

    except Exception:
        current_app.logger.exception(
            "Assigned complaint queue preparation failed for staff_id=%s",
            staff.id,
        )
        db.session.rollback()
        flash(
            "The assigned complaint queue could not be refreshed. Showing current assignments.",
            "warning",
        )

        query = Complaint.query.filter(
            Complaint.assigned_staff_id == staff.id
        )

        if status_filter:
            query = query.filter(
                func.lower(
                    func.coalesce(Complaint.status, "")
                ) == normalize(status_filter)
            )
        else:
            query = query.filter(
                func.lower(
                    func.coalesce(Complaint.status, "")
                ).in_(
                    {
                        SUBMITTED.lower(),
                        IN_PROGRESS.lower(),
                        ESCALATED.lower(),
                    }
                )
            )

        complaints = (
            query
            .order_by(
                PRIORITY_ORDER.asc(),
                Complaint.deadline_at.asc(),
                Complaint.created_at.asc(),
                Complaint.id.asc(),
            )
            .all()
        )

    return render_dashboard(
        staff=staff,
        complaints=complaints,
        status_filter=status_filter,
    )


# ============================================================================
# COMPLAINT DETAIL
# ============================================================================

def _get_assigned_complaint(staff_id, complaint_id):
    return (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            Complaint.assigned_staff_id == staff_id,
        )
        .first_or_404()
    )


def _get_complaint_history(complaint_id):
    return (
        ComplaintHistory.query
        .filter(
            ComplaintHistory.complaint_id == complaint_id
        )
        .order_by(
            ComplaintHistory.created_at.desc(),
            ComplaintHistory.id.desc(),
        )
        .all()
    )


@staff_bp.route(
    "/complaints/<int:complaint_id>"
)
@staff_required
def complaint_detail(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    resident = (
        db.session.get(User, complaint.resident_id)
        if complaint.resident_id
        else None
    )

    history = _get_complaint_history(
        complaint.id
    )

    return render_template(
        "staff/complaint_detail.html",
        staff=staff,
        complaint=complaint,
        resident=resident,
        history=history,
    )


# Compatibility endpoint used by templates from other staff versions.
staff_bp.add_url_rule(
    "/complaints/<int:complaint_id>",
    endpoint="complaint_details",
    view_func=complaint_detail,
)


# ============================================================================
# ACCEPT COMPLAINT
# ============================================================================

@staff_bp.route(
    "/complaints/<int:complaint_id>/accept",
    methods=["POST"],
)
@staff_required
def accept_complaint(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    status = normalize(complaint.status)

    if status == RESOLVED.lower():
        flash(
            "A resolved complaint cannot be accepted again.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    if status not in {
        SUBMITTED.lower(),
        ASSIGNED.lower(),
        ESCALATED.lower(),
    }:
        flash(
            "Only submitted, assigned, or escalated complaints can be accepted.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    now = utc_now()
    old_status = complaint.status

    complaint.status = IN_PROGRESS

    if hasattr(Complaint, "accepted_at"):
        complaint.accepted_at = (
            complaint.accepted_at or now
        )

    if hasattr(Complaint, "updated_at"):
        complaint.updated_at = now

    add_history(
        complaint=complaint,
        user_id=staff.id,
        action="Complaint Accepted",
        old_status=old_status,
        new_status=IN_PROGRESS,
        notes=(
            "Complaint accepted by the assigned Electricity "
            "Department Staff member."
        ),
    )

    notify_resident(
        complaint=complaint,
        title="Complaint Accepted",
        message=(
            f"Complaint {complaint.complaint_number} has been "
            "accepted by the assigned staff member and is now "
            "being processed."
        ),
        notification_type="Complaint Status",
    )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            "The complaint could not be accepted.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    flash(
        "Complaint accepted successfully.",
        "success",
    )

    return redirect(
        url_for(
            "staff.complaint_detail",
            complaint_id=complaint.id,
        )
    )


# ============================================================================
# UPDATE STATUS
# ============================================================================

@staff_bp.route(
    "/complaints/<int:complaint_id>/status",
    methods=["GET", "POST"],
)
@staff_required
def update_status(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    if request.method == "GET":
        return render_template(
            "staff/complaint_detail.html",
            staff=staff,
            complaint=complaint,
            history=_get_complaint_history(complaint.id),
        )

    new_status = request.form.get(
        "status",
        "",
    ).strip()

    notes = request.form.get(
        "notes",
        "",
    ).strip()

    if not notes:
        notes = request.form.get(
            "remarks",
            "",
        ).strip()

    normalized = normalize(new_status)

    if normalized == IN_PROGRESS.lower():
        new_status = IN_PROGRESS
    elif normalized == RESOLVED.lower():
        new_status = RESOLVED
    else:
        flash(
            "Invalid complaint status.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    current_status = normalize(
        complaint.status
    )

    if current_status == RESOLVED.lower():
        flash(
            "A resolved complaint cannot be moved to another status.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    if (
        new_status == IN_PROGRESS
        and current_status not in {
            SUBMITTED.lower(),
            ASSIGNED.lower(),
            IN_PROGRESS.lower(),
            ESCALATED.lower(),
        }
    ):
        flash(
            "This complaint cannot be moved to In Progress from its current status.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    if new_status == RESOLVED and not notes:
        flash(
            "Repair notes are required before resolving a complaint.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    old_status = complaint.status
    now = utc_now()

    complaint.status = new_status

    if hasattr(Complaint, "updated_at"):
        complaint.updated_at = now

    if new_status == IN_PROGRESS and hasattr(
        Complaint,
        "accepted_at",
    ):
        complaint.accepted_at = (
            complaint.accepted_at or now
        )

    if notes:
        existing = (
            str(complaint.repair_notes).strip()
            if getattr(complaint, "repair_notes", None)
            else ""
        )
        complaint.repair_notes = (
            f"{existing}\n{notes}"
            if existing
            else notes
        )

    if new_status == RESOLVED and hasattr(
        Complaint,
        "resolved_at",
    ):
        complaint.resolved_at = now

    add_history(
        complaint=complaint,
        user_id=staff.id,
        action=(
            "Complaint Resolved"
            if new_status == RESOLVED
            else "Complaint Status Updated"
        ),
        old_status=old_status,
        new_status=new_status,
        notes=notes or None,
    )

    if new_status == RESOLVED:
        notify_resident(
            complaint=complaint,
            title="Complaint Resolved",
            message=(
                f"Complaint {complaint.complaint_number} has been "
                "resolved by the Electricity Department."
            ),
            notification_type="Complaint Resolved",
        )
    else:
        notify_resident(
            complaint=complaint,
            title="Complaint Status Updated",
            message=(
                f"Complaint {complaint.complaint_number} is now "
                "In Progress and is being processed."
            ),
            notification_type="Complaint Status",
        )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            "The complaint status could not be updated.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    flash(
        (
            "Complaint marked as resolved successfully."
            if new_status == RESOLVED
            else "Complaint status updated successfully."
        ),
        "success",
    )

    return redirect(
        url_for(
            "staff.complaint_detail",
            complaint_id=complaint.id,
        )
    )


# ============================================================================
# REPAIR NOTES
# ============================================================================

@staff_bp.route(
    "/complaints/<int:complaint_id>/repair-notes",
    methods=["GET", "POST"],
)
@staff_required
def add_repair_notes(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    if request.method == "GET":
        return render_template(
            "staff/complaint_detail.html",
            staff=staff,
            complaint=complaint,
            history=_get_complaint_history(complaint.id),
        )

    notes = request.form.get(
        "repair_notes",
        "",
    ).strip()

    if not notes:
        notes = request.form.get(
            "notes",
            "",
        ).strip()

    if not notes:
        notes = request.form.get(
            "remarks",
            "",
        ).strip()

    if not notes:
        flash(
            "Repair notes cannot be empty.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    if normalize(complaint.status) == RESOLVED.lower():
        flash(
            "A resolved complaint cannot be updated with additional repair notes.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    existing = (
        str(complaint.repair_notes).strip()
        if getattr(complaint, "repair_notes", None)
        else ""
    )

    complaint.repair_notes = (
        f"{existing}\n{notes}"
        if existing
        else notes
    )

    if hasattr(Complaint, "updated_at"):
        complaint.updated_at = utc_now()

    add_history(
        complaint=complaint,
        user_id=staff.id,
        action="Repair Notes Added",
        old_status=complaint.status,
        new_status=complaint.status,
        notes=notes,
    )

    notify_resident(
        complaint=complaint,
        title="Complaint Repair Update",
        message=(
            f"Repair information for complaint "
            f"{complaint.complaint_number} has been updated."
        ),
        notification_type="Repair Update",
    )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            "Repair notes could not be saved.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    flash(
        "Repair notes added successfully.",
        "success",
    )

    return redirect(
        url_for(
            "staff.complaint_detail",
            complaint_id=complaint.id,
        )
    )


# Compatibility endpoint used by templates using staff.repair_notes.
staff_bp.add_url_rule(
    "/complaints/<int:complaint_id>/repair-notes",
    endpoint="repair_notes",
    view_func=add_repair_notes,
)


# ============================================================================
# LOCATION HISTORY
# ============================================================================

@staff_bp.route("/location-history")
@staff_required
def location_history():
    staff = current_staff()

    location = request.args.get(
        "location",
        "",
    ).strip()

    complaints = []

    if location:
        complaints = (
            Complaint.query
            .filter(
                func.lower(
                    func.coalesce(
                        Complaint.location,
                        "",
                    )
                ).like(
                    f"%{location.lower()}%"
                )
            )
            .order_by(
                Complaint.created_at.desc(),
                Complaint.id.desc(),
            )
            .all()
        )

    rows = (
        db.session.query(
            Complaint.location
        )
        .filter(
            Complaint.location.isnot(None)
        )
        .distinct()
        .order_by(
            Complaint.location.asc()
        )
        .all()
    )

    locations = [
        row[0]
        for row in rows
        if row[0]
    ]

    return render_template(
        "staff/location_history.html",
        staff=staff,
        complaints=complaints,
        location=location,
        locations=locations,
    )


@staff_bp.route(
    "/complaints/<int:complaint_id>/history"
)
@staff_required
def complaint_history(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    history_records = _get_complaint_history(
        complaint.id
    )

    return render_template(
        "staff/location_history.html",
        staff=staff,
        complaint=complaint,
        complaints=[complaint],
        history=history_records,
        history_records=history_records,
        location=complaint.location or "",
        locations=(
            [complaint.location]
            if complaint.location
            else []
        ),
    )


# ============================================================================
# STAFF ACTION HISTORY
# ============================================================================

@staff_bp.route("/history")
@staff_required
def history():
    staff = current_staff()

    records = (
        ComplaintHistory.query
        .filter(
            ComplaintHistory.user_id == staff.id
        )
        .order_by(
            ComplaintHistory.created_at.desc(),
            ComplaintHistory.id.desc(),
        )
        .all()
    )

    complaints = (
        Complaint.query
        .filter(
            Complaint.assigned_staff_id == staff.id
        )
        .order_by(
            Complaint.created_at.desc(),
            Complaint.id.desc(),
        )
        .all()
    )

    return render_dashboard(
        staff=staff,
        complaints=complaints,
        history=records,
    )


# ============================================================================
# STAFF NOTIFICATIONS
# ============================================================================

@staff_bp.route("/notifications")
@staff_required
def notifications():
    staff = current_staff()

    # Use the shared notification UI when that blueprint is registered.
    if "notification.index" in current_app.view_functions:
        return redirect(
            url_for("notification.index")
        )

    notification_list = get_staff_notifications(
        staff.id,
        unread_only=False,
        limit=200,
    )

    unread_count = (
        Notification.query
        .filter(
            Notification.user_id == staff.id,
            Notification.is_read.is_(False),
        )
        .count()
    )

    return render_template(
        "resident/notifications.html",
        resident=staff,
        staff=staff,
        notifications=notification_list,
        unread_count=unread_count,
    )


@staff_bp.route(
    "/notifications/<int:notification_id>/read",
    methods=["POST"],
)
@staff_required
def mark_notification_read(notification_id):
    staff = current_staff()

    notification = (
        Notification.query
        .filter(
            Notification.id == notification_id,
            Notification.user_id == staff.id,
        )
        .first_or_404()
    )

    if hasattr(notification, "mark_as_read"):
        notification.mark_as_read()
    else:
        notification.is_read = True

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            "Unable to update the notification.",
            "error",
        )

    return redirect(
        request.referrer
        or url_for("staff.notifications")
    )


@staff_bp.route(
    "/notifications/read-all",
    methods=["POST"],
)
@staff_required
def mark_all_notifications_read():
    staff = current_staff()

    unread = (
        Notification.query
        .filter(
            Notification.user_id == staff.id,
            Notification.is_read.is_(False),
        )
        .all()
    )

    for notification in unread:
        if hasattr(notification, "mark_as_read"):
            notification.mark_as_read()
        else:
            notification.is_read = True

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            "Unable to update staff notifications.",
            "error",
        )
        return redirect(
            url_for("staff.dashboard")
        )

    flash(
        "All staff notifications have been marked as read.",
        "success",
    )

    return redirect(
        request.referrer
        or url_for("staff.dashboard")
    )


# ============================================================================
# COMPLAINT IMAGE
# ============================================================================

@staff_bp.route(
    "/complaints/<int:complaint_id>/image"
)
@staff_required
def complaint_image(complaint_id):
    staff = current_staff()

    complaint = _get_assigned_complaint(
        staff.id,
        complaint_id,
    )

    filename = getattr(
        complaint,
        "image_filename",
        None,
    )

    if not filename:
        flash(
            "No complaint image is available.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    upload_folder = Path(
        current_app.config.get(
            "UPLOAD_FOLDER",
            Path(current_app.instance_path) / "uploads",
        )
    )

    if not (upload_folder / filename).is_file():
        flash(
            "The complaint image could not be found.",
            "error",
        )
        return redirect(
            url_for(
                "staff.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    return send_from_directory(
        upload_folder,
        filename,
    )


# ============================================================================
# REFRESH
# ============================================================================

@staff_bp.route(
    "/refresh",
    methods=["GET", "POST"],
)
@staff_required
def refresh():
    try:
        assigned = auto_assign_unassigned_complaints()
        escalated = auto_escalate_overdue_complaints()

        if assigned:
            flash(
                f"{assigned} complaint(s) were forwarded to "
                "Electricity Department Staff.",
                "success",
            )

        if escalated:
            flash(
                f"{escalated} overdue complaint(s) were escalated "
                "for Senior Electricity Officer review.",
                "warning",
            )

    except Exception:
        db.session.rollback()
        flash(
            "The complaint queue could not be refreshed.",
            "error",
        )

    return redirect(
        url_for("staff.dashboard")
    )


__all__ = [
    "staff_bp",
    "staff_required",
    "ASSIGNED",
    "dashboard",
    "assigned_complaints",
    "complaint_detail",
    "accept_complaint",
    "update_status",
    "add_repair_notes",
    "location_history",
    "complaint_history",
    "history",
    "notifications",
    "mark_notification_read",
    "mark_all_notifications_read",
    "complaint_image",
    "refresh",
]
