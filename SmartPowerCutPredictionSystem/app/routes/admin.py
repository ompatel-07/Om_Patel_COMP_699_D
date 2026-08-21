from datetime import datetime, timezone
from functools import wraps
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import csv
from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import (
    Complaint,
    ComplaintHistory,
    DatasetRecord,
    Notification,
    Prediction,
    User,
)

# The training service is imported here so an administrator upload can
# automatically trigger model training once the configured minimum dataset
# and both target classes are available.
from app.ml.train_model import train_model_if_ready


admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin",
)


def utc_now():
    return datetime.now(timezone.utc)


def admin_required(view):
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

        if user.role != "admin":
            flash(
                "You are not authorized to access the administrator area.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        if not user.is_active:
            session.clear()
            flash(
                "Administrator account is inactive.",
                "error",
            )
            return redirect(
                url_for("auth.login")
            )

        return view(*args, **kwargs)

    return wrapped_view


def get_current_admin():
    return db.session.get(
        User,
        session.get("user_id"),
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
    title,
    message,
    notification_type,
    complaint_id=None,
):
    notification = Notification(
        user_id=user_id,
        complaint_id=complaint_id,
        notification_type=notification_type,
        title=title,
        message=message,
        is_read=False,
        email_sent=False,
        created_at=utc_now(),
    )

    db.session.add(notification)


def normalize_email(email):
    return email.strip().lower()


def valid_role(role):
    return role in {
        "resident",
        "staff",
        "officer",
    }


def valid_password(password):
    return bool(
        password
        and len(password) >= 8
    )


def get_report_rows():
    complaints = (
        Complaint.query
        .order_by(
            Complaint.created_at.desc()
        )
        .all()
    )

    rows = []

    for complaint in complaints:
        resident = (
            db.session.get(
                User,
                complaint.resident_id,
            )
            if complaint.resident_id
            else None
        )

        staff = (
            db.session.get(
                User,
                complaint.assigned_staff_id,
            )
            if complaint.assigned_staff_id
            else None
        )

        rows.append(
            {
                "Complaint Number": (
                    complaint.complaint_number
                ),
                "Resident": (
                    resident.full_name
                    if resident
                    else ""
                ),
                "Complaint Type": (
                    complaint.complaint_type
                ),
                "Location": (
                    complaint.location
                ),
                "Complaint Date": (
                    complaint.complaint_date
                ),
                "Complaint Time": (
                    complaint.complaint_time
                ),
                "Priority": (
                    complaint.priority
                ),
                "Status": (
                    complaint.status
                ),
                "Resolution Hours": (
                    complaint.resolution_hours
                ),
                "Assigned Staff": (
                    staff.full_name
                    if staff
                    else ""
                ),
                "Escalated": (
                    "Yes"
                    if complaint.was_escalated
                    else "No"
                ),
                "Created At": (
                    complaint.created_at
                ),
                "Resolved At": (
                    complaint.resolved_at
                    if complaint.resolved_at
                    else ""
                ),
            }
        )

    return rows


@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    admin = get_current_admin()

    total_users = User.query.count()

    total_residents = User.query.filter_by(
        role="resident"
    ).count()

    total_staff = User.query.filter_by(
        role="staff"
    ).count()

    total_officers = User.query.filter_by(
        role="officer"
    ).count()

    # Approval is required only for Staff and Senior Electricity
    # Officer accounts.  Use a case-insensitive comparison so older
    # records containing "pending", "Pending", etc. are all counted.
    pending_approvals = User.query.filter(
        User.role.in_(
            [
                "staff",
                "officer",
            ]
        ),
        db.func.lower(
            db.func.coalesce(User.approval_status, "")
        ) == "pending",
    ).count()

    active_users = User.query.filter_by(
        is_active=True
    ).count()

    inactive_users = User.query.filter_by(
        is_active=False
    ).count()

    total_complaints = Complaint.query.count()

    submitted_complaints = Complaint.query.filter_by(
        status="Submitted"
    ).count()

    in_progress_complaints = Complaint.query.filter_by(
        status="In Progress"
    ).count()

    resolved_complaints = Complaint.query.filter_by(
        status="Resolved"
    ).count()

    escalated_complaints = Complaint.query.filter_by(
        status="Escalated"
    ).count()

    withdrawn_complaints = Complaint.query.filter_by(
        status="Withdrawn"
    ).count()

    total_dataset_records = DatasetRecord.query.count()

    total_predictions = Prediction.query.count()

    recent_complaints = (
        Complaint.query
        .order_by(
            Complaint.created_at.desc()
        )
        .limit(10)
        .all()
    )

    return render_template(
        "admin/dashboard.html",
        admin=admin,
        total_users=total_users,
        total_residents=total_residents,
        total_staff=total_staff,
        total_officers=total_officers,
        pending_approvals=pending_approvals,
        pending_approval_count=pending_approvals,
        pending_count=pending_approvals,
        active_users=active_users,
        active_accounts=active_users,
        inactive_users=inactive_users,
        inactive_accounts=inactive_users,
        total_complaints=total_complaints,
        submitted_complaints=submitted_complaints,
        in_progress_complaints=in_progress_complaints,
        resolved_complaints=resolved_complaints,
        escalated_complaints=escalated_complaints,
        withdrawn_complaints=withdrawn_complaints,
        total_dataset_records=total_dataset_records,
        total_predictions=total_predictions,
        recent_complaints=recent_complaints,
    )


@admin_bp.route("/users")
@admin_required
def users():
    admin = get_current_admin()

    role_filter = request.args.get(
        "role",
        "",
    ).strip().lower()

    status_filter = request.args.get(
        "status",
        "",
    ).strip().lower()

    search = request.args.get(
        "search",
        "",
    ).strip()

    query = User.query

    if role_filter in {
        "resident",
        "staff",
        "officer",
        "admin",
    }:
        query = query.filter(
            User.role == role_filter
        )

    if status_filter == "active":
        query = query.filter(
            User.is_active.is_(True)
        )

    elif status_filter == "inactive":
        query = query.filter(
            User.is_active.is_(False)
        )

    elif status_filter == "pending":
        query = query.filter(
            User.role.in_(
                [
                    "staff",
                    "officer",
                ]
            ),
            db.func.lower(
                db.func.coalesce(User.approval_status, "")
            ) == "pending",
        )

    if search:
        search_pattern = (
            f"%{search.lower()}%"
        )

        query = query.filter(
            db.or_(
                db.func.lower(
                    User.full_name
                ).like(search_pattern),
                db.func.lower(
                    User.email
                ).like(search_pattern),
            )
        )

    user_list = (
        query
        .order_by(
            User.created_at.desc()
        )
        .all()
    )

    # Provide summary values to the user-management template.  The
    # aliases make the route compatible with templates that use either
    # the descriptive names or the shorter count names.
    total_users = User.query.count()

    active_users = User.query.filter_by(
        is_active=True
    ).count()

    inactive_users = User.query.filter_by(
        is_active=False
    ).count()

    pending_approvals = User.query.filter(
        User.role.in_(
            [
                "staff",
                "officer",
            ]
        ),
        db.func.lower(
            db.func.coalesce(User.approval_status, "")
        ) == "pending",
    ).count()

    return render_template(
        "admin/users.html",
        admin=admin,
        users=user_list,
        role_filter=role_filter,
        status_filter=status_filter,
        search=search,
        total_users=total_users,
        active_users=active_users,
        inactive_users=inactive_users,
        pending_approvals=pending_approvals,
        total_users_count=total_users,
        active_accounts=active_users,
        inactive_accounts=inactive_users,
        pending_approval_count=pending_approvals,
        pending_count=pending_approvals,
    )


@admin_bp.route(
    "/users/<int:user_id>/activate",
    methods=["POST"],
)
@admin_required
def activate_user(user_id):
    admin = get_current_admin()

    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        flash(
            "User account not found.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    if user.id == admin.id:
        flash(
            "The administrator cannot deactivate or reactivate the current account from this action.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    user.is_active = True
    user.updated_at = utc_now()

    db.session.commit()

    flash(
        f"{user.full_name}'s account has been activated.",
        "success",
    )

    return redirect(
        url_for("admin.users")
    )


@admin_bp.route(
    "/users/<int:user_id>/deactivate",
    methods=["POST"],
)
@admin_required
def deactivate_user(user_id):
    admin = get_current_admin()

    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        flash(
            "User account not found.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    if user.id == admin.id:
        flash(
            "The current administrator account cannot be deactivated.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    user.is_active = False
    user.updated_at = utc_now()

    db.session.commit()

    flash(
        f"{user.full_name}'s account has been deactivated.",
        "success",
    )

    return redirect(
        url_for("admin.users")
    )


@admin_bp.route(
    "/users/<int:user_id>/approve",
    methods=["GET", "POST"],
)
@admin_required
def approve_user(user_id):
    admin = get_current_admin()

    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        flash(
            "User account not found.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    if user.role not in {
        "staff",
        "officer",
    }:
        flash(
            "Only staff and officer registration requests require approval.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    current_approval_status = (
        str(user.approval_status or "")
        .strip()
        .lower()
    )

    if current_approval_status == "approved":
        flash(
            "This account is already approved.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    user.approval_status = "Approved"
    user.is_active = True
    user.approved_by = admin.id
    user.approved_at = utc_now()
    user.updated_at = utc_now()

    create_notification(
        user_id=user.id,
        title="Registration Approved",
        message=(
            "Your registration request has been approved "
            "by the System Administrator. You can now "
            "sign in to your authorized dashboard."
        ),
        notification_type="Account Approval",
    )

    db.session.commit()

    flash(
        f"{user.full_name}'s registration has been approved.",
        "success",
    )

    return redirect(
        url_for("admin.users")
    )


@admin_bp.route(
    "/users/<int:user_id>/reject",
    methods=["GET", "POST"],
)
@admin_required
def reject_user(user_id):
    admin = get_current_admin()

    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        flash(
            "User account not found.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    if user.role not in {
        "staff",
        "officer",
    }:
        flash(
            "Only staff and officer registration requests can be rejected.",
            "error",
        )
        return redirect(
            url_for("admin.users")
        )

    user.approval_status = "Rejected"
    user.is_active = False
    user.approved_by = admin.id
    user.approved_at = utc_now()
    user.updated_at = utc_now()

    create_notification(
        user_id=user.id,
        title="Registration Rejected",
        message=(
            "Your registration request was rejected "
            "by the System Administrator."
        ),
        notification_type="Account Approval",
    )

    db.session.commit()

    flash(
        f"{user.full_name}'s registration has been rejected.",
        "success",
    )

    return redirect(
        url_for("admin.users")
    )


@admin_bp.route(
    "/staff-accounts",
    methods=["GET", "POST"],
)
@admin_required
def staff_accounts():
    admin = get_current_admin()

    if request.method == "POST":
        full_name = request.form.get(
            "full_name",
            "",
        ).strip()

        email = normalize_email(
            request.form.get(
                "email",
                "",
            )
        )

        phone = request.form.get(
            "phone",
            "",
        ).strip()

        address = request.form.get(
            "address",
            "",
        ).strip()

        role = request.form.get(
            "role",
            "staff",
        ).strip().lower()

        password = request.form.get(
            "password",
            "",
        )

        if role not in {
            "staff",
            "officer",
        }:
            flash(
                "Only staff or officer accounts can be created here.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.staff_accounts"
                )
            )

        if not full_name:
            flash(
                "Full name is required.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.staff_accounts"
                )
            )

        if not email:
            flash(
                "Email address is required.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.staff_accounts"
                )
            )

        if not valid_password(password):
            flash(
                "Password must contain at least 8 characters.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.staff_accounts"
                )
            )

        existing_user = User.query.filter_by(
            email=email
        ).first()

        if existing_user:
            flash(
                "An account with this email address already exists.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.staff_accounts"
                )
            )

        user = User(
            full_name=full_name,
            email=email,
            phone=phone or None,
            address=address or None,
            role=role,
            approval_status="Approved",
            is_active=True,
            approved_by=admin.id,
            approved_at=utc_now(),
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        user.set_password(password)

        db.session.add(user)
        db.session.flush()

        create_notification(
            user_id=user.id,
            title="Staff Account Created",
            message=(
                "Your electricity department account "
                "has been created by the System Administrator. "
                "You can sign in using your registered credentials."
            ),
            notification_type="Account Created",
        )

        db.session.commit()

        flash(
            f"{role.title()} account created successfully.",
            "success",
        )

        return redirect(
            url_for(
                "admin.staff_accounts"
            )
        )

    staff_members = (
        User.query.filter(
            User.role.in_(
                [
                    "staff",
                    "officer",
                ]
            )
        )
        .order_by(
            User.created_at.desc()
        )
        .all()
    )

    return render_template(
        "admin/staff_accounts.html",
        admin=admin,
        staff_members=staff_members,
    )


@admin_bp.route(
    "/staff-accounts/<int:user_id>/activate",
    methods=["POST"],
)
@admin_required
def activate_staff_account(user_id):
    user = User.query.filter(
        User.id == user_id,
        User.role.in_(
            [
                "staff",
                "officer",
            ]
        ),
    ).first_or_404()

    user.is_active = True
    user.updated_at = utc_now()

    db.session.commit()

    flash(
        "Staff account activated successfully.",
        "success",
    )

    return redirect(
        url_for(
            "admin.staff_accounts"
        )
    )


@admin_bp.route(
    "/staff-accounts/<int:user_id>/deactivate",
    methods=["POST"],
)
@admin_required
def deactivate_staff_account(user_id):
    admin = get_current_admin()

    if user_id == admin.id:
        flash(
            "The current administrator account cannot be deactivated.",
            "error",
        )
        return redirect(
            url_for(
                "admin.staff_accounts"
            )
        )

    user = User.query.filter(
        User.id == user_id,
        User.role.in_(
            [
                "staff",
                "officer",
            ]
        ),
    ).first_or_404()

    user.is_active = False
    user.updated_at = utc_now()

    db.session.commit()

    flash(
        "Staff account deactivated successfully.",
        "success",
    )

    return redirect(
        url_for(
            "admin.staff_accounts"
        )
    )


@admin_bp.route("/reports")
@admin_required
def reports():
    admin = get_current_admin()

    total_complaints = Complaint.query.count()

    status_counts = {
        "Submitted": Complaint.query.filter_by(
            status="Submitted"
        ).count(),
        "In Progress": Complaint.query.filter_by(
            status="In Progress"
        ).count(),
        "Resolved": Complaint.query.filter_by(
            status="Resolved"
        ).count(),
        "Escalated": Complaint.query.filter_by(
            status="Escalated"
        ).count(),
        "Withdrawn": Complaint.query.filter_by(
            status="Withdrawn"
        ).count(),
    }

    priority_counts = {
        "High": Complaint.query.filter_by(
            priority="High"
        ).count(),
        "Medium": Complaint.query.filter_by(
            priority="Medium"
        ).count(),
        "Low": Complaint.query.filter_by(
            priority="Low"
        ).count(),
    }

    type_counts = {}

    complaint_types = (
        db.session.query(
            Complaint.complaint_type
        )
        .distinct()
        .all()
    )

    for row in complaint_types:
        complaint_type = row[0]

        if complaint_type:
            type_counts[complaint_type] = (
                Complaint.query.filter_by(
                    complaint_type=complaint_type
                ).count()
            )

    location_counts = {}

    locations = (
        db.session.query(
            Complaint.location
        )
        .distinct()
        .all()
    )

    for row in locations:
        location = row[0]

        if location:
            location_counts[location] = (
                Complaint.query.filter_by(
                    location=location
                ).count()
            )

    total_escalations = Complaint.query.filter(
        Complaint.was_escalated.is_(True)
    ).count()

    total_resolved = Complaint.query.filter_by(
        status="Resolved"
    ).count()

    total_dataset_records = DatasetRecord.query.count()

    total_predictions = Prediction.query.count()

    return render_template(
        "admin/reports.html",
        admin=admin,
        total_complaints=total_complaints,
        status_counts=status_counts,
        priority_counts=priority_counts,
        type_counts=type_counts,
        location_counts=location_counts,
        total_escalations=total_escalations,
        total_resolved=total_resolved,
        total_dataset_records=total_dataset_records,
        total_predictions=total_predictions,
    )


@admin_bp.route(
    "/reports/export"
)
@admin_required
def export_reports():
    rows = get_report_rows()

    output = BytesIO()

    text_output = []

    if rows:
        fieldnames = list(
            rows[0].keys()
        )
    else:
        fieldnames = [
            "Complaint Number",
            "Resident",
            "Complaint Type",
            "Location",
            "Complaint Date",
            "Complaint Time",
            "Priority",
            "Status",
            "Resolution Hours",
            "Assigned Staff",
            "Escalated",
            "Created At",
            "Resolved At",
        ]

    text_buffer = []

    import io

    string_buffer = io.StringIO()

    writer = csv.DictWriter(
        string_buffer,
        fieldnames=fieldnames,
    )

    writer.writeheader()

    for row in rows:
        writer.writerow(row)

    response = make_response(
        string_buffer.getvalue()
    )

    response.headers["Content-Type"] = (
        "text/csv; charset=utf-8"
    )

    response.headers["Content-Disposition"] = (
        "attachment; filename=complaint_report.csv"
    )

    return response


@admin_bp.route(
    "/upload-dataset",
    methods=["GET", "POST"],
)
@admin_required
def upload_dataset():
    admin = get_current_admin()

    if request.method == "POST":
        dataset_file = request.files.get(
            "dataset"
        )

        if not dataset_file:
            flash(
                "Please select a CSV file.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.upload_dataset"
                )
            )

        if not dataset_file.filename:
            flash(
                "Please select a CSV file.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.upload_dataset"
                )
            )

        extension = (
            Path(
                secure_filename(
                    dataset_file.filename
                )
            )
            .suffix
            .lower()
        )

        if extension != ".csv":
            flash(
                "Only CSV files are supported.",
                "error",
            )
            return redirect(
                url_for(
                    "admin.upload_dataset"
                )
            )

        try:
            content = dataset_file.read()

            decoded_content = content.decode(
                "utf-8-sig"
            )

            reader = csv.DictReader(
                decoded_content.splitlines()
            )

            if not reader.fieldnames:
                flash(
                    "The CSV file does not contain a header row.",
                    "error",
                )
                return redirect(
                    url_for(
                        "admin.upload_dataset"
                    )
                )

            normalized_headers = {
                header.strip().lower()
                for header in reader.fieldnames
                if header
            }

            required_headers = {
                "complaint_type",
                "location",
                "complaint_date",
                "complaint_time",
            }

            if not required_headers.issubset(
                normalized_headers
            ):
                flash(
                    "CSV must contain complaint_type, location, complaint_date, and complaint_time columns.",
                    "error",
                )
                return redirect(
                    url_for(
                        "admin.upload_dataset"
                    )
                )

            imported_count = 0
            skipped_count = 0

            for row in reader:
                cleaned_row = {
                    (
                        key.strip().lower()
                        if key
                        else ""
                    ): (
                        value.strip()
                        if isinstance(
                            value,
                            str,
                        )
                        else value
                    )
                    for key, value in row.items()
                }

                complaint_type = cleaned_row.get(
                    "complaint_type"
                )

                location = cleaned_row.get(
                    "location"
                )

                complaint_date_text = (
                    cleaned_row.get(
                        "complaint_date"
                    )
                )

                complaint_time_text = (
                    cleaned_row.get(
                        "complaint_time"
                    )
                )

                if not all(
                    [
                        complaint_type,
                        location,
                        complaint_date_text,
                        complaint_time_text,
                    ]
                ):
                    skipped_count += 1
                    continue

                complaint_date = None
                complaint_time = None

                for date_format in (
                    "%Y-%m-%d",
                    "%d-%m-%Y",
                    "%m/%d/%Y",
                ):
                    try:
                        complaint_date = (
                            datetime.strptime(
                                complaint_date_text,
                                date_format,
                            ).date()
                        )
                        break
                    except ValueError:
                        continue

                for time_format in (
                    "%H:%M",
                    "%H:%M:%S",
                    "%I:%M %p",
                ):
                    try:
                        complaint_time = (
                            datetime.strptime(
                                complaint_time_text,
                                time_format,
                            ).time()
                        )
                        break
                    except ValueError:
                        continue

                if not complaint_date or not complaint_time:
                    skipped_count += 1
                    continue

                resolution_hours_text = (
                    cleaned_row.get(
                        "resolution_hours"
                    )
                )

                try:
                    resolution_hours = int(
                        resolution_hours_text
                    ) if resolution_hours_text else 24
                except ValueError:
                    resolution_hours = 24

                if resolution_hours not in {
                    24,
                    48,
                }:
                    resolution_hours = 24

                priority = cleaned_row.get(
                    "priority"
                ) or "Medium"

                if priority not in {
                    "High",
                    "Medium",
                    "Low",
                }:
                    priority = "Medium"

                status = cleaned_row.get(
                    "status"
                ) or "Resolved"

                power_cut_value = cleaned_row.get(
                    "power_cut_occurred"
                )

                if power_cut_value:
                    power_cut_occurred = (
                        str(
                            power_cut_value
                        ).strip().lower()
                        in {
                            "1",
                            "true",
                            "yes",
                            "y",
                        }
                    )
                else:
                    power_cut_occurred = (
                        complaint_type.lower()
                        == "power cut"
                    )

                record = DatasetRecord(
                    complaint_type=complaint_type,
                    location=location,
                    complaint_date=complaint_date,
                    complaint_time=complaint_time,
                    resolution_hours=resolution_hours,
                    priority=priority,
                    status=status,
                    power_cut_occurred=(
                        power_cut_occurred
                    ),
                    source="admin_upload",
                    source_complaint_id=None,
                    created_at=utc_now(),
                )

                db.session.add(record)
                imported_count += 1

            db.session.commit()

            # Keep the upload transaction independent from model training.
            # The records must remain stored even when there is not enough
            # data yet (or the data contains only one target class).
            training_result = None
            if imported_count > 0:
                try:
                    training_result = train_model_if_ready()
                except Exception as exc:
                    current_app.logger.exception(
                        "Random Forest training failed after dataset upload."
                    )
                    training_result = {
                        "success": False,
                        "trained": False,
                        "message": (
                            "Dataset records were imported, but Random Forest "
                            "training could not be completed."
                        ),
                        "error": str(exc),
                    }

            if training_result and training_result.get("trained"):
                accuracy = training_result.get(
                    "accuracy_percentage"
                )
                flash(
                    f"{imported_count} dataset records imported successfully. "
                    f"{skipped_count} rows were skipped. "
                    f"Random Forest model trained successfully "
                    f"({accuracy}% test accuracy).",
                    "success",
                )
            elif training_result:
                flash(
                    f"{imported_count} dataset records imported successfully. "
                    f"{skipped_count} rows were skipped. "
                    f"{training_result.get('message', 'Model training is not ready yet.')}",
                    "success",
                )
            else:
                flash(
                    f"{imported_count} dataset records imported successfully. "
                    f"{skipped_count} rows were skipped.",
                    "success",
                )

        except UnicodeDecodeError:
            db.session.rollback()

            flash(
                "The CSV file must use UTF-8 encoding.",
                "error",
            )

        except Exception:
            db.session.rollback()

            flash(
                "The dataset could not be processed.",
                "error",
            )

        return redirect(
            url_for(
                "admin.upload_dataset"
            )
        )

    dataset_count = DatasetRecord.query.count()

    recent_records = (
        DatasetRecord.query
        .order_by(
            DatasetRecord.created_at.desc()
        )
        .limit(20)
        .all()
    )

    return render_template(
        "admin/upload_dataset.html",
        admin=admin,
        dataset_count=dataset_count,
        recent_records=recent_records,
    )


@admin_bp.route(
    "/dataset/clear",
    methods=["POST"],
)
@admin_required
def clear_dataset():
    DatasetRecord.query.delete(
        synchronize_session=False
    )

    db.session.commit()

    flash(
        "Dataset records cleared successfully.",
        "success",
    )

    return redirect(
        url_for(
            "admin.upload_dataset"
        )
    )


@admin_bp.route(
    "/complaints/<int:complaint_id>"
)
@admin_required
def complaint_detail(complaint_id):
    admin = get_current_admin()

    complaint = db.session.get(
        Complaint,
        complaint_id,
    )

    if not complaint:
        flash(
            "Complaint not found.",
            "error",
        )
        return redirect(
            url_for("admin.reports")
        )

    history = (
        ComplaintHistory.query
        .filter_by(
            complaint_id=complaint.id
        )
        .order_by(
            ComplaintHistory.created_at.asc()
        )
        .all()
    )

    return render_template(
        "admin/reports.html",
        admin=admin,
        complaint=complaint,
        history=history,
    )