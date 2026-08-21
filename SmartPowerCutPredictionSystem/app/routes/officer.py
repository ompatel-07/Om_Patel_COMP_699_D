from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models import (
    Complaint,
    ComplaintHistory,
    Notification,
    User,
)


officer_bp = Blueprint(
    "officer",
    __name__,
    url_prefix="/officer",
)


def utc_now():
    return datetime.now(timezone.utc)


def officer_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash(
                "Please sign in to continue.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        user = db.session.get(
            User,
            session["user_id"],
        )

        if not user:
            session.clear()
            flash(
                "Your account could not be found.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        if user.role != "officer":
            flash(
                "You are not authorized to access the officer area.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        if not user.is_active:
            session.clear()
            flash(
                "Your account is inactive.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        if user.approval_status != "Approved":
            session.clear()
            flash(
                "Your officer account has not been approved.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        return view(*args, **kwargs)

    return wrapped_view


def get_current_officer():
    return db.session.get(
        User,
        session.get("user_id"),
    )


def _normalized_status(value):
    """Return a normalized status string for safe comparisons."""
    return str(value or "").strip().lower()


def complaint_was_escalated(complaint):
    """
    Determine whether a complaint has been escalated without referencing
    the non-existent ``Complaint.was_escalated`` model attribute.

    Escalation is recognized from the current status, the optional
    ``escalated`` flag used by the existing complaint service, or complaint
    history when the complaint has subsequently moved to another status.
    """
    if complaint is None:
        return False

    if _normalized_status(getattr(complaint, "status", None)) == "escalated":
        return True

    if bool(getattr(complaint, "escalated", False)):
        return True

    complaint_id = getattr(complaint, "id", None)
    if not complaint_id:
        return False

    try:
        return (
            ComplaintHistory.query
            .filter(
                ComplaintHistory.complaint_id == complaint_id,
                func.lower(ComplaintHistory.action).like("%escalat%"),
            )
            .first()
            is not None
        )
    except Exception:
        db.session.rollback()
        return False


def _escalated_complaint_filter(active_only=True):
    """Build a filter using the existing complaint escalation fields."""
    status_escalated = func.lower(Complaint.status) == "escalated"
    escalated_column = getattr(Complaint, "escalated", None)

    if escalated_column is not None:
        condition = or_(status_escalated, escalated_column.is_(True))
    else:
        condition = status_escalated

    if active_only:
        return and_(
            condition,
            func.lower(Complaint.status).notin_(
                {"resolved", "closed", "withdrawn", "cancelled"}
            ),
        )

    return condition


def _get_escalated_complaints(active_only=True):
    """Return escalated complaints in the same priority/deadline order."""
    return (
        Complaint.query
        .filter(_escalated_complaint_filter(active_only=active_only))
        .order_by(
            Complaint.priority.desc(),
            Complaint.deadline_at.asc(),
            Complaint.updated_at.desc(),
        )
        .all()
    )


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


def create_notification(
    user_id,
    complaint,
    title,
    message,
    notification_type,
):
    notification = Notification(
        user_id=user_id,
        complaint_id=complaint.id,
        notification_type=notification_type,
        title=title,
        message=message,
        is_read=False,
        email_sent=False,
        created_at=utc_now(),
    )

    db.session.add(notification)


@officer_bp.route("/dashboard")
@officer_required
def dashboard():
    officer = get_current_officer()

    escalated_complaints = _get_escalated_complaints(active_only=True)

    total_escalated = len(
        escalated_complaints
    )

    assigned_escalated = sum(
        1
        for complaint in escalated_complaints
        if complaint.assigned_staff_id
    )

    unassigned_escalated = sum(
        1
        for complaint in escalated_complaints
        if not complaint.assigned_staff_id
    )

    resolved_complaints = (
        Complaint.query
        .filter(func.lower(Complaint.status) == "resolved")
        .all()
    )

    resolved_after_escalation = sum(
        1
        for complaint in resolved_complaints
        if complaint_was_escalated(complaint)
    )

    return render_template(
        "officer/dashboard.html",
        officer=officer,
        complaints=escalated_complaints,
        total_escalated=total_escalated,
        assigned_escalated=assigned_escalated,
        unassigned_escalated=unassigned_escalated,
        resolved_after_escalation=resolved_after_escalation,
    )


@officer_bp.route("/escalated-complaints")
@officer_required
def escalated_complaints():
    officer = get_current_officer()

    status_filter = request.args.get(
        "assignment",
        "",
    ).strip().lower()

    query = Complaint.query.filter(
        _escalated_complaint_filter(active_only=True)
    )

    if status_filter == "assigned":
        query = query.filter(
            Complaint.assigned_staff_id.isnot(None)
        )

    elif status_filter == "unassigned":
        query = query.filter(
            Complaint.assigned_staff_id.is_(None)
        )

    complaints = (
        query.order_by(
            Complaint.priority.desc(),
            Complaint.deadline_at.asc(),
            Complaint.updated_at.desc(),
        )
        .all()
    )

    return render_template(
        "officer/escalated_complaints.html",
        officer=officer,
        complaints=complaints,
        assignment_filter=status_filter,
    )


@officer_bp.route(
    "/complaints/<int:complaint_id>"
)
@officer_required
def complaint_detail(complaint_id):
    officer = get_current_officer()

    complaint = (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            _escalated_complaint_filter(active_only=True),
        )
        .first_or_404()
    )

    staff_members = (
        User.query.filter(
            User.role == "staff",
            User.is_active.is_(True),
            User.approval_status == "Approved",
        )
        .order_by(
            User.full_name.asc()
        )
        .all()
    )

    return render_template(
        "officer/complaint_detail.html",
        officer=officer,
        complaint=complaint,
        staff_members=staff_members,
    )


@officer_bp.route(
    "/complaints/<int:complaint_id>/assign",
    methods=["POST"],
)
@officer_required
def assign_complaint(complaint_id):
    officer = get_current_officer()

    complaint = (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            _escalated_complaint_filter(active_only=True),
        )
        .first_or_404()
    )

    staff_id_text = request.form.get(
        "staff_id",
        "",
    ).strip()

    notes = request.form.get(
        "notes",
        "",
    ).strip()

    try:
        staff_id = int(staff_id_text)
    except (TypeError, ValueError):
        flash(
            "Please select a valid staff member.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    staff = User.query.filter(
        User.id == staff_id,
        User.role == "staff",
        User.is_active.is_(True),
        User.approval_status == "Approved",
    ).first()

    if not staff:
        flash(
            "The selected staff member is not available.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    previous_staff_id = (
        complaint.assigned_staff_id
    )

    complaint.assigned_staff_id = staff.id
    complaint.assigned_at = utc_now()
    complaint.updated_at = utc_now()

    action = (
        "Escalated Complaint Reassigned"
        if previous_staff_id
        else "Escalated Complaint Assigned"
    )

    assignment_note = (
        f"Assigned to {staff.full_name}."
    )

    if notes:
        assignment_note = (
            f"{assignment_note} {notes}"
        )

    add_history(
        complaint=complaint,
        user_id=officer.id,
        action=action,
        old_status="Escalated",
        new_status="Escalated",
        notes=assignment_note,
    )

    create_notification(
        user_id=staff.id,
        complaint=complaint,
        title="Escalated Complaint Assigned",
        message=(
            f"Complaint {complaint.complaint_number} "
            f"has been assigned to you by the Senior "
            f"Electricity Officer. Priority: "
            f"{complaint.priority}."
        ),
        notification_type="Escalation Assignment",
    )

    if complaint.resident_id:
        create_notification(
            user_id=complaint.resident_id,
            complaint=complaint,
            title="Complaint Reassigned",
            message=(
                f"Complaint {complaint.complaint_number} "
                f"has been assigned to another electricity "
                f"department staff member for further action."
            ),
            notification_type="Complaint Assignment",
        )

    db.session.commit()

    flash(
        "Escalated complaint assigned successfully.",
        "success",
    )

    return redirect(
        url_for(
            "officer.complaint_detail",
            complaint_id=complaint.id,
        )
    )


@officer_bp.route(
    "/complaints/<int:complaint_id>/final-action",
    methods=["POST"],
)
@officer_required
def record_final_action(complaint_id):
    officer = get_current_officer()

    complaint = Complaint.query.filter(
        Complaint.id == complaint_id
    ).first_or_404()

    if not complaint_was_escalated(complaint):
        flash(
            "Final action can only be recorded for an escalated complaint.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    final_action = request.form.get(
        "final_action",
        "",
    ).strip()

    final_status = request.form.get(
        "final_status",
        "",
    ).strip()

    if not final_action:
        flash(
            "Final action details are required.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    allowed_statuses = {
        "In Progress",
        "Resolved",
    }

    if final_status not in allowed_statuses:
        flash(
            "Please select a valid final status.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    old_status = complaint.status

    complaint.final_action = final_action
    complaint.final_action_at = utc_now()
    complaint.final_action_by = officer.id
    complaint.status = final_status
    complaint.updated_at = utc_now()

    if final_status == "Resolved":
        complaint.resolved_at = utc_now()

    add_history(
        complaint=complaint,
        user_id=officer.id,
        action="Final Action Recorded",
        old_status=old_status,
        new_status=final_status,
        notes=final_action,
    )

    if complaint.resident_id:
        if final_status == "Resolved":
            create_notification(
                user_id=complaint.resident_id,
                complaint=complaint,
                title="Escalated Complaint Resolved",
                message=(
                    f"Complaint {complaint.complaint_number} "
                    "has been resolved after officer review."
                ),
                notification_type="Complaint Resolved",
            )
        else:
            create_notification(
                user_id=complaint.resident_id,
                complaint=complaint,
                title="Escalated Complaint Updated",
                message=(
                    f"Final action has been recorded for "
                    f"complaint {complaint.complaint_number}. "
                    "The complaint remains under processing."
                ),
                notification_type="Complaint Status",
            )

    db.session.commit()

    flash(
        "Final action recorded successfully.",
        "success",
    )

    return redirect(
        url_for(
            "officer.dashboard"
        )
    )


@officer_bp.route(
    "/complaints/<int:complaint_id>/reassign",
    methods=["POST"],
)
@officer_required
def reassign_complaint(complaint_id):
    officer = get_current_officer()

    complaint = (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            _escalated_complaint_filter(active_only=True),
        )
        .first_or_404()
    )

    staff_id_text = request.form.get(
        "staff_id",
        "",
    ).strip()

    reason = request.form.get(
        "reason",
        "",
    ).strip()

    try:
        staff_id = int(staff_id_text)
    except (TypeError, ValueError):
        flash(
            "Please select a valid staff member.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    staff = User.query.filter(
        User.id == staff_id,
        User.role == "staff",
        User.is_active.is_(True),
        User.approval_status == "Approved",
    ).first()

    if not staff:
        flash(
            "The selected staff member is not available.",
            "error",
        )
        return redirect(
            url_for(
                "officer.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    previous_staff_id = (
        complaint.assigned_staff_id
    )

    complaint.assigned_staff_id = staff.id
    complaint.assigned_at = utc_now()
    complaint.updated_at = utc_now()

    assignment_notes = (
        f"Complaint reassigned to {staff.full_name}."
    )

    if previous_staff_id:
        assignment_notes += (
            f" Previous staff ID: {previous_staff_id}."
        )

    if reason:
        assignment_notes += (
            f" Reason: {reason}"
        )

    add_history(
        complaint=complaint,
        user_id=officer.id,
        action="Complaint Reassigned",
        old_status="Escalated",
        new_status="Escalated",
        notes=assignment_notes,
    )

    create_notification(
        user_id=staff.id,
        complaint=complaint,
        title="Complaint Reassigned",
        message=(
            f"Escalated complaint "
            f"{complaint.complaint_number} "
            "has been reassigned to you."
        ),
        notification_type="Escalation Assignment",
    )

    db.session.commit()

    flash(
        "Complaint reassigned successfully.",
        "success",
    )

    return redirect(
        url_for(
            "officer.complaint_detail",
            complaint_id=complaint.id,
        )
    )


@officer_bp.route("/staff")
@officer_required
def staff_list():
    officer = get_current_officer()

    staff_members = (
        User.query.filter(
            User.role == "staff",
            User.is_active.is_(True),
            User.approval_status == "Approved",
        )
        .order_by(
            User.full_name.asc()
        )
        .all()
    )

    escalated_complaints = _get_escalated_complaints(active_only=True)

    resolved_complaints = (
        Complaint.query
        .filter(func.lower(Complaint.status) == "resolved")
        .all()
    )

    resolved_after_escalation = sum(
        1
        for complaint in resolved_complaints
        if complaint_was_escalated(complaint)
    )

    return render_template(
        "officer/dashboard.html",
        officer=officer,
        complaints=[],
        staff_members=staff_members,
        total_escalated=len(escalated_complaints),
        assigned_escalated=sum(
            1
            for complaint in escalated_complaints
            if getattr(complaint, "assigned_staff_id", None)
        ),
        unassigned_escalated=sum(
            1
            for complaint in escalated_complaints
            if not getattr(complaint, "assigned_staff_id", None)
        ),
        resolved_after_escalation=resolved_after_escalation,
    )