from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

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
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from app.extensions import db
from app.services.prediction_service import (
    get_minimum_records,
    build_prediction_input,
    get_model_metadata,
    load_model,
    model_is_available,
)
from app.models import (
    Complaint,
    ComplaintHistory,
    DatasetRecord,
    Notification,
    Prediction,
    User,
)


# ============================================================
# BLUEPRINT
# ============================================================

resident_bp = Blueprint(
    "resident",
    __name__,
    url_prefix="/resident",
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


def normalize_location(location: str | None) -> str:
    """Normalize extra spaces in a location."""
    if not location:
        return ""

    return " ".join(location.strip().split())


def get_current_resident() -> User | None:
    """Return the currently authenticated resident."""
    user_id = session.get("user_id")

    if not user_id:
        return None

    return db.session.get(User, user_id)


def create_complaint_number() -> str:
    """Create a unique complaint reference number."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    return (
        f"PC-{timestamp}-"
        f"{uuid4().hex[:6].upper()}"
    )


def allowed_image(filename: str | None) -> bool:
    """Check whether an uploaded complaint image is allowed."""
    if not filename:
        return False

    extension = (
        Path(filename)
        .suffix
        .lower()
        .lstrip(".")
    )

    allowed_extensions = current_app.config.get(
        "ALLOWED_IMAGE_EXTENSIONS",
        {
            "jpg",
            "jpeg",
            "png",
            "webp",
        },
    )

    return extension in allowed_extensions


# ============================================================
# COMPLAINT TYPE HELPERS
# ============================================================

DEFAULT_COMPLAINT_TYPES = (
    "Power Cut",
    "Streetlight",
    "Electric Pole Damage",
    "Voltage Issue",
)


COMPLAINT_TYPE_ALIASES = {
    "power cut": "Power Cut",
    "power_cut": "Power Cut",
    "power-cut": "Power Cut",
    "powercut": "Power Cut",
    "power outage": "Power Cut",
    "power_outage": "Power Cut",
    "power-outage": "Power Cut",
    "outage": "Power Cut",

    "streetlight": "Streetlight",
    "street light": "Streetlight",
    "street_light": "Streetlight",
    "street-light": "Streetlight",

    "electric pole damage": "Electric Pole Damage",
    "electric_pole_damage": "Electric Pole Damage",
    "electric-pole-damage": "Electric Pole Damage",
    "pole damage": "Electric Pole Damage",
    "pole_damage": "Electric Pole Damage",
    "pole-damage": "Electric Pole Damage",

    "voltage issue": "Voltage Issue",
    "voltage_issue": "Voltage Issue",
    "voltage-issue": "Voltage Issue",
    "voltage": "Voltage Issue",
}


def _normalize_complaint_type_key(value: str | None) -> str:
    """
    Normalize a complaint type into a comparison key.

    Examples:
        Power Cut -> power cut
        power_cut -> power cut
        power-cut -> power cut
        POWER CUT -> power cut
    """
    if value is None:
        return ""

    value = str(value).strip().lower()

    value = value.replace("_", " ")
    value = value.replace("-", " ")

    value = " ".join(value.split())

    return value


def canonicalize_complaint_type(
    complaint_type: str | None,
) -> str | None:
    """
    Convert a submitted complaint type into the system's
    canonical display/database value.

    This fixes the problem where the HTML form may submit
    'power_cut' while the backend expects 'Power Cut'.
    """
    if not complaint_type:
        return None

    normalized = _normalize_complaint_type_key(
        complaint_type
    )

    if not normalized:
        return None

    # First use the known aliases.
    if normalized in COMPLAINT_TYPE_ALIASES:
        return COMPLAINT_TYPE_ALIASES[normalized]

    # Then compare against configured complaint types.
    for configured_type in get_complaint_types():
        configured_key = _normalize_complaint_type_key(
            configured_type
        )

        if configured_key == normalized:
            return str(configured_type).strip()

    return None


def get_complaint_types():
    """
    Return the supported complaint types.

    The application configuration can override the defaults.
    """
    configured = current_app.config.get(
        "COMPLAINT_TYPES"
    )

    if not configured:
        return DEFAULT_COMPLAINT_TYPES

    if isinstance(configured, str):
        configured = [
            item.strip()
            for item in configured.split(",")
            if item.strip()
        ]

    try:
        configured = tuple(
            str(item).strip()
            for item in configured
            if str(item).strip()
        )
    except TypeError:
        return DEFAULT_COMPLAINT_TYPES

    return configured or DEFAULT_COMPLAINT_TYPES


def get_resolution_options():
    """
    Return supported preferred resolution periods.
    """
    configured = current_app.config.get(
        "COMPLAINT_RESOLUTION_OPTIONS"
    )

    if not configured:
        return (24, 48)

    if isinstance(configured, str):
        configured = [
            item.strip()
            for item in configured.split(",")
            if item.strip()
        ]

    normalized = []

    try:
        for value in configured:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue

            if value not in normalized:
                normalized.append(value)
    except TypeError:
        return (24, 48)

    if not normalized:
        return (24, 48)

    return tuple(normalized)


def normalize_resolution_hours(value: object) -> int | None:
    """Convert a submitted resolution value into 24 or 48."""
    if value is None:
        return None

    text = str(value).strip().lower()
    if not text:
        return None

    normalized = (
        text.replace("hours", "")
        .replace("hour", "")
        .replace("hrs", "")
        .replace("hr", "")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )

    try:
        hours = int(normalized)
    except (TypeError, ValueError):
        return None

    return hours if hours in (24, 48) else None


def get_resolution_form_value() -> str:
    """Read the preferred-resolution value from the complaint form."""
    field_names = (
        "resolution_hours",
        "resolution_time",
        "preferred_resolution_time",
        "preferred_resolution_hours",
        "resolution_period",
        "preferred_resolution_period",
    )

    for field_name in field_names:
        value = request.form.get(field_name)
        if value is not None and str(value).strip():
            return str(value).strip()

    return ""


# ============================================================
# RESIDENT ACCESS CONTROL
# ============================================================

def resident_required(view):
    """
    Protect resident routes.

    Requirements:
    - User must be logged in.
    - User must exist.
    - User must have resident role.
    - User account must be active.
    """

    @wraps(view)
    def wrapped_view(*args, **kwargs):

        user_id = session.get("user_id")

        if not user_id:
            flash(
                "Please sign in to continue.",
                "error",
            )

            return redirect(
                url_for("auth.login")
            )

        resident = db.session.get(
            User,
            user_id,
        )

        if resident is None:
            session.clear()

            flash(
                "Your account could not be found.",
                "error",
            )

            return redirect(
                url_for("auth.login")
            )

        if resident.role != "resident":
            flash(
                "You are not authorized to access the resident area.",
                "error",
            )

            return redirect(
                url_for("auth.login")
            )

        if not resident.is_active:
            session.clear()

            flash(
                "Your account is inactive.",
                "error",
            )

            return redirect(
                url_for("auth.login")
            )

        return view(*args, **kwargs)

    return wrapped_view


# ============================================================
# PASSWORD HELPERS
# ============================================================

def verify_user_password(
    user: User,
    password: str,
) -> bool:
    """
    Verify the user's existing password.

    Supports models that provide check_password()
    as well as models storing password_hash.
    """

    if not password:
        return False

    check_method = getattr(
        user,
        "check_password",
        None,
    )

    if callable(check_method):
        try:
            return bool(
                check_method(password)
            )
        except Exception:
            return False

    password_hash = getattr(
        user,
        "password_hash",
        None,
    )

    if not password_hash:
        return False

    try:
        return check_password_hash(
            password_hash,
            password,
        )
    except Exception:
        return False


def set_user_password(
    user: User,
    new_password: str,
) -> bool:
    """
    Store a new password securely.
    """

    set_method = getattr(
        user,
        "set_password",
        None,
    )

    if callable(set_method):
        set_method(new_password)
        return True

    if hasattr(user, "password_hash"):
        user.password_hash = generate_password_hash(
            new_password
        )
        return True

    return False


# ============================================================
# RULE-BASED PRIORITY
# ============================================================

def calculate_priority(
    complaint_type: str,
    description: str,
) -> str:
    """
    Assign complaint priority using rule-based business logic.
    """

    complaint_type = (
        complaint_type or ""
    ).lower()

    description = (
        description or ""
    ).lower()

    high_keywords = {
        "fire",
        "spark",
        "sparking",
        "shock",
        "electric shock",
        "broken pole",
        "fallen pole",
        "live wire",
        "exposed wire",
        "transformer",
        "transformer damage",
        "major power cut",
        "burning",
        "danger",
        "hazard",
    }

    medium_keywords = {
        "voltage",
        "low voltage",
        "high voltage",
        "streetlight",
        "street light",
        "flickering",
        "frequent",
        "interruption",
        "power cut",
        "outage",
    }

    text = (
        f"{complaint_type} "
        f"{description}"
    )

    if any(
        keyword in text
        for keyword in high_keywords
    ):
        return "High"

    if complaint_type in {
        "electric pole damage",
        "voltage issue",
    }:
        return "Medium"

    if any(
        keyword in text
        for keyword in medium_keywords
    ):
        return "Medium"

    return "Low"


# ============================================================
# COMPLAINT HISTORY
# ============================================================

def add_history(
    complaint: Complaint,
    user_id: int,
    action: str,
    old_status: str | None = None,
    new_status: str | None = None,
    notes: str | None = None,
) -> ComplaintHistory:
    """Create a complaint history record."""

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


# ============================================================
# NOTIFICATION
# ============================================================

def create_notification(
    user_id: int,
    title: str,
    message: str,
    notification_type: str,
    complaint_id: int | None = None,
) -> Notification:
    """Create an in-system resident notification."""

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

    return notification


# ============================================================
# DATASET CREATION
# ============================================================

def create_dataset_record(
    complaint: Complaint,
) -> DatasetRecord:
    """
    Add a resident complaint to the system dataset.
    """

    record = DatasetRecord(
        complaint_type=complaint.complaint_type,
        location=complaint.location,
        complaint_date=complaint.complaint_date,
        complaint_time=complaint.complaint_time,
        resolution_hours=complaint.resolution_hours,
        priority=complaint.priority,
        status=complaint.status,
        power_cut_occurred=(
            _normalize_complaint_type_key(
                complaint.complaint_type
            )
            == "power cut"
        ),
        source="resident",
        source_complaint_id=complaint.id,
        created_at=utc_now(),
    )

    db.session.add(record)

    return record


# ============================================================
# FORM RENDERING
# ============================================================

def render_complaint_form(
    resident,
    **extra_context,
):
    """Render complaint form with all required data."""

    context = {
        "resident": resident,
        "complaint_types": get_complaint_types(),
        "resolution_options": get_resolution_options(),
    }

    context.update(extra_context)

    return render_template(
        "resident/complaint_form.html",
        **context,
    )


# ============================================================
# RESIDENT DASHBOARD
# ============================================================

@resident_bp.route("/dashboard")
@resident_required
def dashboard():
    """Resident dashboard."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    total_complaints = (
        Complaint.query
        .filter_by(
            resident_id=resident.id
        )
        .count()
    )

    active_complaints = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id,
            Complaint.status.in_(
                [
                    "Submitted",
                    "In Progress",
                    "Escalated",
                ]
            ),
        )
        .count()
    )

    resolved_complaints = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id,
            Complaint.status == "Resolved",
        )
        .count()
    )

    withdrawn_complaints = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id,
            Complaint.status == "Withdrawn",
        )
        .count()
    )

    unread_notifications = (
        Notification.query
        .filter(
            Notification.user_id == resident.id,
            Notification.is_read.is_(False),
        )
        .count()
    )

    recent_complaints = (
        Complaint.query
        .filter_by(
            resident_id=resident.id
        )
        .order_by(
            Complaint.created_at.desc()
        )
        .limit(5)
        .all()
    )

    return render_template(
        "resident/dashboard.html",
        resident=resident,
        total_complaints=total_complaints,
        active_complaints=active_complaints,
        resolved_complaints=resolved_complaints,
        withdrawn_complaints=withdrawn_complaints,
        unread_notifications=unread_notifications,
        recent_complaints=recent_complaints,
    )


# ============================================================
# RESIDENT PROFILE
# ============================================================

@resident_bp.route(
    "/profile",
    methods=["GET", "POST"],
)
@resident_required
def profile():
    """View and update resident profile."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    if request.method == "POST":

        full_name = request.form.get(
            "full_name",
            "",
        ).strip()

        phone = request.form.get(
            "phone",
            "",
        ).strip()

        address = request.form.get(
            "address",
            "",
        ).strip()

        if not full_name:
            flash(
                "Full name is required.",
                "error",
            )

            return render_template(
                "resident/profile.html",
                resident=resident,
            )

        if len(full_name) < 2:
            flash(
                "Please enter a valid full name.",
                "error",
            )

            return render_template(
                "resident/profile.html",
                resident=resident,
            )

        resident.full_name = full_name
        resident.phone = phone or None
        resident.address = address or None

        if hasattr(
            resident,
            "updated_at",
        ):
            resident.updated_at = utc_now()

        session["full_name"] = resident.full_name

        db.session.commit()

        flash(
            "Profile information updated successfully.",
            "success",
        )

        return redirect(
            url_for("resident.profile")
        )

    return render_template(
        "resident/profile.html",
        resident=resident,
    )


# ============================================================
# CHANGE PASSWORD
# ============================================================

@resident_bp.route(
    "/change-password",
    methods=["GET", "POST"],
)
@resident_required
def change_password():
    """Change resident account password."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    if request.method == "POST":

        current_password = request.form.get(
            "current_password",
            "",
        )

        new_password = request.form.get(
            "new_password",
            "",
        )

        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if not current_password:
            flash(
                "Current password is required.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if not new_password:
            flash(
                "New password is required.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if not confirm_password:
            flash(
                "Please confirm your new password.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if not verify_user_password(
            resident,
            current_password,
        ):
            flash(
                "Current password is incorrect.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if len(new_password) < 8:
            flash(
                "New password must contain at least 8 characters.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if new_password != confirm_password:
            flash(
                "New password and confirmation password do not match.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if new_password == current_password:
            flash(
                "New password must be different from the current password.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if not set_user_password(
            resident,
            new_password,
        ):
            flash(
                "Password update is not available for this account configuration.",
                "error",
            )

            return render_template(
                "resident/change_password.html",
                resident=resident,
            )

        if hasattr(
            resident,
            "updated_at",
        ):
            resident.updated_at = utc_now()

        db.session.commit()

        flash(
            "Your password has been changed successfully.",
            "success",
        )

        return redirect(
            url_for("resident.profile")
        )

    return render_template(
        "resident/change_password.html",
        resident=resident,
    )


# ============================================================
# RESIDENT COMPLAINT LIST
# ============================================================

@resident_bp.route("/complaints")
@resident_required
def complaints():
    """Display all complaints belonging only to this resident."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    status_filter = request.args.get(
        "status",
        "",
    ).strip()

    complaint_type = request.args.get(
        "type",
        "",
    ).strip()

    query = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id
        )
    )

    if status_filter:
        query = query.filter(
            Complaint.status == status_filter
        )

    if complaint_type:
        canonical_type = canonicalize_complaint_type(
            complaint_type
        )

        if canonical_type:
            query = query.filter(
                Complaint.complaint_type
                == canonical_type
            )
        else:
            query = query.filter(
                Complaint.complaint_type
                == complaint_type
            )

    complaints_list = (
        query
        .order_by(
            Complaint.created_at.desc()
        )
        .all()
    )

    return render_template(
        "resident/complaints.html",
        resident=resident,
        complaints=complaints_list,
        selected_status=status_filter,
        selected_type=complaint_type,
        complaint_types=get_complaint_types(),
    )


# ============================================================
# CREATE COMPLAINT
# ============================================================

@resident_bp.route(
    "/complaints/new",
    methods=["GET", "POST"],
)
@resident_required
def complaint_form():
    """
    Create a new electricity complaint.

    Accepted complaint types:
    - Power Cut
    - Streetlight
    - Electric Pole Damage
    - Voltage Issue

    The backend accepts both human-readable values and
    common HTML slug values such as power_cut.
    """

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    complaint_types = get_complaint_types()
    resolution_options = get_resolution_options()

    if request.method == "POST":

        # ----------------------------------------------------
        # READ FORM VALUES
        # ----------------------------------------------------

        raw_complaint_type = request.form.get(
            "complaint_type",
            "",
        ).strip()

        description = request.form.get(
            "description",
            "",
        ).strip()

        location = normalize_location(
            request.form.get(
                "location",
                "",
            )
        )

        complaint_date_text = request.form.get(
            "complaint_date",
            "",
        ).strip()

        complaint_time_text = request.form.get(
            "complaint_time",
            "",
        ).strip()

        # Accept the canonical field name plus common template aliases.
        # This prevents the form from failing when the HTML select uses
        # resolution_time / preferred_resolution_time instead of
        # resolution_hours.
        resolution_hours_text = get_resolution_form_value()

        image = request.files.get(
            "image"
        )

        # ----------------------------------------------------
        # COMPLAINT TYPE VALIDATION
        # ----------------------------------------------------

        complaint_type = canonicalize_complaint_type(
            raw_complaint_type
        )

        if complaint_type is None:
            flash(
                "Please select a valid complaint type.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # DESCRIPTION VALIDATION
        # ----------------------------------------------------

        if not description:
            flash(
                "Complaint description is required.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        if len(description) < 10:
            flash(
                "Please provide a more detailed complaint description.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # LOCATION VALIDATION
        # ----------------------------------------------------

        if not location:
            flash(
                "Complaint location is required.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # DATE VALIDATION
        # ----------------------------------------------------

        complaint_date = None

        date_formats = (
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
        )

        for date_format in date_formats:

            try:
                complaint_date = datetime.strptime(
                    complaint_date_text,
                    date_format,
                ).date()

                break

            except ValueError:
                continue

        if complaint_date is None:
            flash(
                "Please enter a valid complaint date.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # DATE CANNOT BE IN THE FUTURE
        # ----------------------------------------------------

        today = utc_now().date()

        if complaint_date > today:
            flash(
                "Complaint date cannot be in the future.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # TIME VALIDATION
        # ----------------------------------------------------

        complaint_time = None

        time_formats = (
            "%H:%M",
            "%H:%M:%S",
            "%I:%M %p",
            "%I:%M:%S %p",
        )

        for time_format in time_formats:

            try:
                complaint_time = datetime.strptime(
                    complaint_time_text,
                    time_format,
                ).time()

                break

            except ValueError:
                continue

        if complaint_time is None:
            flash(
                "Please enter a valid complaint time.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # RESOLUTION TIME VALIDATION
        # ----------------------------------------------------

        resolution_hours = normalize_resolution_hours(
            resolution_hours_text
        )

        if resolution_hours is None:
            flash(
                "Please select a valid resolution time: 24 or 48 hours.",
                "error",
            )

            return render_complaint_form(
                resident,
                selected_resolution_hours=resolution_hours_text,
            )

        # The project requirement is explicitly limited to 24 or 48 hours.
        if resolution_hours not in (24, 48):
            flash(
                "Resolution time must be 24 or 48 hours.",
                "error",
            )

            return render_complaint_form(
                resident,
                selected_resolution_hours=resolution_hours,
            )

        # ----------------------------------------------------
        # IMAGE VALIDATION
        # ----------------------------------------------------

        if image and image.filename:

            if not allowed_image(
                image.filename
            ):
                flash(
                    "Only JPG, JPEG, PNG, and WEBP images are allowed.",
                    "error",
                )

                return render_complaint_form(
                    resident
                )

        # ----------------------------------------------------
        # PRIORITY
        # ----------------------------------------------------

        priority = calculate_priority(
            complaint_type,
            description,
        )

        # ----------------------------------------------------
        # TIMING
        # ----------------------------------------------------

        created_at = utc_now()

        deadline_at = (
            created_at
            + timedelta(
                hours=resolution_hours
            )
        )

        # ----------------------------------------------------
        # CREATE COMPLAINT
        # ----------------------------------------------------

        complaint = Complaint(
            complaint_number=create_complaint_number(),
            resident_id=resident.id,
            complaint_type=complaint_type,
            description=description,
            location=location,
            complaint_date=complaint_date,
            complaint_time=complaint_time,
            resolution_hours=resolution_hours,
            deadline_at=deadline_at,
            priority=priority,
            status="Submitted",
            created_at=created_at,
            updated_at=created_at,
        )

        db.session.add(
            complaint
        )

        try:
            db.session.flush()

        except Exception:
            db.session.rollback()

            current_app.logger.exception(
                "Unable to create complaint record."
            )

            flash(
                "The complaint could not be created. Please try again.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # SAVE IMAGE
        # ----------------------------------------------------

        saved_image_path = None

        if image and image.filename:

            original_filename = secure_filename(
                image.filename
            )

            if not original_filename:
                original_filename = (
                    "complaint_image"
                )

            unique_filename = (
                f"{uuid4().hex}_"
                f"{original_filename}"
            )

            upload_folder = Path(
                current_app.config.get(
                    "UPLOAD_FOLDER",
                    Path(
                        current_app.instance_path
                    ) / "uploads",
                )
            )

            upload_folder.mkdir(
                parents=True,
                exist_ok=True,
            )

            saved_image_path = (
                upload_folder
                / unique_filename
            )

            try:

                image.save(
                    saved_image_path
                )

                complaint.image_filename = (
                    unique_filename
                )

            except Exception:

                db.session.rollback()

                current_app.logger.exception(
                    "Unable to save complaint image."
                )

                flash(
                    "The complaint image could not be uploaded.",
                    "error",
                )

                return render_complaint_form(
                    resident
                )

        # ----------------------------------------------------
        # HISTORY
        # ----------------------------------------------------

        add_history(
            complaint=complaint,
            user_id=resident.id,
            action="Complaint Submitted",
            old_status=None,
            new_status="Submitted",
            notes=(
                "Complaint submitted by resident. "
                f"Preferred resolution time: "
                f"{resolution_hours} hours. "
                f"Priority assigned: {priority}."
            ),
        )

        # ----------------------------------------------------
        # RESIDENT NOTIFICATION
        # ----------------------------------------------------

        create_notification(
            user_id=resident.id,
            complaint_id=complaint.id,
            notification_type="Complaint Submitted",
            title="Complaint Submitted",
            message=(
                f"Complaint "
                f"{complaint.complaint_number} "
                f"has been submitted successfully. "
                f"Priority: {priority}. "
                f"Preferred resolution: "
                f"{resolution_hours} hours."
            ),
        )

        # ----------------------------------------------------
        # DATASET RECORD
        # ----------------------------------------------------

        create_dataset_record(
            complaint
        )

        # ----------------------------------------------------
        # COMMIT EVERYTHING
        # ----------------------------------------------------

        try:

            db.session.commit()

        except Exception:

            db.session.rollback()

            # Remove uploaded image if database commit failed.
            if saved_image_path is not None:

                try:
                    if saved_image_path.exists():
                        saved_image_path.unlink()

                except Exception:
                    current_app.logger.exception(
                        "Unable to remove orphaned complaint image."
                    )

            current_app.logger.exception(
                "Unable to save resident complaint."
            )

            flash(
                "The complaint could not be saved. Please try again.",
                "error",
            )

            return render_complaint_form(
                resident
            )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        flash(
            f"Complaint "
            f"{complaint.complaint_number} "
            "submitted successfully.",
            "success",
        )

        return redirect(
            url_for(
                "resident.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    # --------------------------------------------------------
    # GET REQUEST
    # --------------------------------------------------------

    return render_complaint_form(
        resident
    )


# ============================================================
# BACKWARD COMPATIBILITY
# ============================================================

# Some older code may import create_complaint.
# Keep the compatibility alias without creating a second route.
create_complaint = complaint_form


# ============================================================
# COMPLAINT DETAIL
# ============================================================

@resident_bp.route(
    "/complaints/<int:complaint_id>"
)
@resident_required
def complaint_detail(
    complaint_id: int,
):
    """
    Display complete details of a complaint.

    Residents can only view their own complaints.
    """

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    complaint = (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            Complaint.resident_id == resident.id,
        )
        .first_or_404()
    )

    history_records = (
        ComplaintHistory.query
        .filter(
            ComplaintHistory.complaint_id
            == complaint.id
        )
        .order_by(
            ComplaintHistory.created_at.asc()
        )
        .all()
    )

    return render_template(
        "resident/complaint_detail.html",
        resident=resident,
        complaint=complaint,
        history_records=history_records,
    )


# ============================================================
# WITHDRAW COMPLAINT
# ============================================================

@resident_bp.route(
    "/complaints/<int:complaint_id>/withdraw",
    methods=["POST"],
)
@resident_required
def withdraw_complaint(
    complaint_id: int,
):
    """
    Withdraw a complaint only while it is still Submitted.
    """

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    complaint = (
        Complaint.query
        .filter(
            Complaint.id == complaint_id,
            Complaint.resident_id == resident.id,
        )
        .first_or_404()
    )

    if complaint.status != "Submitted":

        flash(
            "A complaint can only be withdrawn before it is assigned.",
            "error",
        )

        return redirect(
            url_for(
                "resident.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    old_status = complaint.status
    now = utc_now()

    complaint.status = "Withdrawn"

    if hasattr(
        complaint,
        "withdrawn_at",
    ):
        complaint.withdrawn_at = now

    complaint.updated_at = now

    add_history(
        complaint=complaint,
        user_id=resident.id,
        action="Complaint Withdrawn",
        old_status=old_status,
        new_status="Withdrawn",
        notes=(
            "Complaint withdrawn by the resident "
            "before assignment."
        ),
    )

    create_notification(
        user_id=resident.id,
        complaint_id=complaint.id,
        notification_type="Complaint Withdrawn",
        title="Complaint Withdrawn",
        message=(
            f"Complaint "
            f"{complaint.complaint_number} "
            "has been withdrawn successfully."
        ),
    )

    try:
        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Unable to withdraw complaint."
        )

        flash(
            "The complaint could not be withdrawn. Please try again.",
            "error",
        )

        return redirect(
            url_for(
                "resident.complaint_detail",
                complaint_id=complaint.id,
            )
        )

    flash(
        "Complaint withdrawn successfully.",
        "success",
    )

    return redirect(
        url_for(
            "resident.complaints"
        )
    )


# ============================================================
# COMPLAINT IMAGE
# ============================================================

@resident_bp.route(
    "/complaints/image/<path:filename>"
)
@resident_required
def complaint_image(
    filename: str,
):
    """
    Safely serve an uploaded complaint image.

    The image must belong to the current resident.
    """

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    complaint = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id,
            Complaint.image_filename == filename,
        )
        .first()
    )

    if complaint is None:
        flash(
            "Complaint image was not found.",
            "error",
        )

        return redirect(
            url_for(
                "resident.complaints"
            )
        )

    upload_folder = Path(
        current_app.config.get(
            "UPLOAD_FOLDER",
            Path(
                current_app.instance_path
            ) / "uploads",
        )
    )

    return send_from_directory(
        upload_folder,
        filename,
    )


# ============================================================
# COMPLAINT HISTORY
# ============================================================

@resident_bp.route("/history")
@resident_required
def history():
    """Display previous complaint history."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    history_records = (
        ComplaintHistory.query
        .join(
            Complaint,
            Complaint.id
            == ComplaintHistory.complaint_id,
        )
        .filter(
            Complaint.resident_id
            == resident.id
        )
        .order_by(
            ComplaintHistory.created_at.desc()
        )
        .all()
    )

    return render_template(
        "resident/history.html",
        resident=resident,
        history_records=history_records,
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@resident_bp.route("/notifications")
@resident_required
def notifications():
    """Display resident notifications."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    notification_list = (
        Notification.query
        .filter_by(
            user_id=resident.id
        )
        .order_by(
            Notification.created_at.desc()
        )
        .all()
    )

    unread_count = (
        Notification.query
        .filter(
            Notification.user_id == resident.id,
            Notification.is_read.is_(False),
        )
        .count()
    )

    return render_template(
        "resident/notifications.html",
        resident=resident,
        notifications=notification_list,
        unread_count=unread_count,
    )


# ============================================================
# MARK SINGLE NOTIFICATION READ
# ============================================================

@resident_bp.route(
    "/notifications/<int:notification_id>/read",
    methods=["POST"],
)
@resident_required
def mark_notification_read(
    notification_id: int,
):
    """Mark one resident notification as read."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    notification = (
        Notification.query
        .filter(
            Notification.id == notification_id,
            Notification.user_id == resident.id,
        )
        .first_or_404()
    )

    if hasattr(
        notification,
        "mark_as_read",
    ):

        notification.mark_as_read()

    else:

        notification.is_read = True

    try:
        db.session.commit()

    except Exception:

        db.session.rollback()

        flash(
            "Unable to update notification.",
            "error",
        )

    return redirect(
        request.referrer
        or url_for(
            "resident.notifications"
        )
    )


# ============================================================
# MARK ALL NOTIFICATIONS READ
# ============================================================

@resident_bp.route(
    "/notifications/read-all",
    methods=["POST"],
)
@resident_required
def mark_all_notifications_read():
    """Mark every unread resident notification as read."""

    resident = get_current_resident()

    if resident is None:
        return redirect(
            url_for("auth.login")
        )

    notification_list = (
        Notification.query
        .filter(
            Notification.user_id == resident.id,
            Notification.is_read.is_(False),
        )
        .all()
    )

    for notification in notification_list:

        if hasattr(
            notification,
            "mark_as_read",
        ):

            notification.mark_as_read()

        else:

            notification.is_read = True

    try:
        db.session.commit()

    except Exception:

        db.session.rollback()

        flash(
            "Unable to update notifications.",
            "error",
        )

        return redirect(
            url_for(
                "resident.notifications"
            )
        )

    flash(
        "All notifications marked as read.",
        "success",
    )

    return redirect(
        url_for(
            "resident.notifications"
        )
    )


# ============================================================
# PREDICTIONS
# ============================================================

def _get_resident_prediction_locations(resident: User):
    """Return unique complaint locations available to this resident."""
    location_rows = (
        db.session.query(Complaint.location)
        .filter(Complaint.resident_id == resident.id)
        .filter(Complaint.location.isnot(None))
        .distinct()
        .order_by(Complaint.location.asc())
        .all()
    )

    return [
        normalize_location(row[0])
        for row in location_rows
        if row[0] and normalize_location(row[0])
    ]


def _parse_prediction_date(value: str | None):
    """Parse the prediction date supplied by the resident."""
    value = (value or "").strip()
    if not value:
        return utc_now().date()

    # The HTML date input normally submits YYYY-MM-DD.
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Accept DD/MM/YYYY as a safe fallback for manually supplied values.
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as exc:
        raise ValueError("Please select a valid prediction date.") from exc


class _PredictionView:
    """Template-safe adapter for the current Prediction database model.

    The database now stores area/prediction_result/prediction_date/created_at,
    while some older resident templates may still reference location,
    prediction, confidence, and predicted_at. This adapter exposes both
    naming styles without changing the database model or business logic.
    """

    def __init__(self, record: Prediction):
        self._record = record

    def __getattr__(self, name):
        return getattr(self._record, name)

    @property
    def id(self):
        return self._record.id

    @property
    def complaint_id(self):
        return self._record.complaint_id

    @property
    def area(self):
        return self._record.area

    @property
    def location(self):
        return self._record.area

    @property
    def prediction_date(self):
        return self._record.prediction_date

    @property
    def prediction_time(self):
        return self._record.prediction_time

    @property
    def prediction_result(self):
        return self._record.prediction_result

    @property
    def prediction(self):
        return self._record.prediction_result == "Power Cut"

    @property
    def probability(self):
        return self._record.probability

    @property
    def confidence(self):
        if self._record.probability is None:
            return None
        return round(float(self._record.probability) * 100, 2)

    @property
    def model_name(self):
        return self._record.model_name

    @property
    def model_version(self):
        return self._record.model_version

    @property
    def created_at(self):
        return self._record.created_at

    @property
    def predicted_at(self):
        return self._record.created_at


def _prediction_views(records):
    """Convert database prediction records to template-safe views."""
    return [_PredictionView(record) for record in records]


def _prediction_context(
    resident: User,
    selected_area: str = "",
    prediction_date=None,
):
    """Build the common context used by both prediction routes."""
    locations = _get_resident_prediction_locations(resident)

    prediction_list = []
    if selected_area:
        prediction_list = _prediction_views(
            Prediction.query
            .filter(
                func.lower(Prediction.area)
                == selected_area.lower()
            )
            .order_by(Prediction.created_at.desc())
            .all()
        )
    elif locations:
        lowered_locations = [location.lower() for location in locations]
        # SQLite/MySQL both support LOWER(); use a normalized case-insensitive
        # filter rather than requiring exact user-entered casing.
        prediction_list = _prediction_views(
            Prediction.query
            .filter(
                func.lower(Prediction.area).in_(lowered_locations)
            )
            .order_by(Prediction.created_at.desc())
            .all()
        )

    return {
        "resident": resident,
        "predictions": prediction_list,
        "locations": locations,
        "selected_area": selected_area,
        "selected_date": prediction_date,
        "prediction_model_available": model_is_available(),
        "prediction_minimum_records": get_minimum_records(),
        "prediction_dataset_count": DatasetRecord.query.count(),
        "prediction_model_metadata": get_model_metadata(),
    }


def _save_resident_prediction(
    resident: User,
    area: str,
    prediction_date,
    prediction_time,
    result: dict,
    complaint: Complaint | None = None,
):
    """
    Persist a prediction using the current Prediction model schema.

    The current models.py uses area/prediction_date/prediction_time/
    prediction_result/probability/model_name/model_version/created_at.
    This helper intentionally does not use the older prediction-service
    persistence function, which belongs to an obsolete Prediction schema.
    """
    prediction_value = bool(result.get("power_cut_likely"))
    probability = result.get("probability")
    confidence = result.get("confidence")

    # Keep the stored probability as a 0..1 value, matching the model field.
    if probability is not None:
        probability = float(probability)
        if probability < 0 or probability > 1:
            probability = max(0.0, min(1.0, probability))

    metadata = get_model_metadata()
    model_version = None
    trained_at = metadata.get("trained_at") if metadata else None
    if trained_at:
        model_version = str(trained_at)[:50]

    prediction_record = Prediction(
        complaint_id=complaint.id if complaint is not None else None,
        area=area,
        prediction_date=prediction_date,
        prediction_time=prediction_time,
        prediction_result=(
            "Power Cut"
            if prediction_value
            else "No Power Cut"
        ),
        probability=probability,
        model_name="Random Forest",
        model_version=model_version,
        created_at=utc_now(),
    )

    db.session.add(prediction_record)
    db.session.commit()

    return prediction_record, confidence


def _generate_area_prediction(
    resident: User,
    area: str,
    prediction_date,
):
    """
    Generate and store a prediction for the selected resident area.

    The selected area is restricted to a location from the resident's own
    complaints. The latest complaint for that area supplies realistic model
    input fields while the resident-selected date is used as the prediction
    date.
    """
    latest_complaint = (
        Complaint.query
        .filter(
            Complaint.resident_id == resident.id,
            func.lower(Complaint.location) == area.lower(),
        )
        .order_by(Complaint.created_at.desc())
        .first()
    )

    if latest_complaint is None:
        raise ValueError(
            "Prediction is available only for an area from your complaints."
        )

    prediction_time = (
        latest_complaint.complaint_time
        or utc_now().time().replace(microsecond=0)
    )

    complaint_type = (
        latest_complaint.complaint_type
        or "Power Cut"
    )
    resolution_hours = (
        latest_complaint.resolution_hours
        or 24
    )
    priority = (
        latest_complaint.priority
        or "High"
    )

    if not model_is_available():
        raise RuntimeError(
            "The Random Forest model has not been trained yet. "
            "Please train the model before requesting predictions."
        )

    dataframe = build_prediction_input(
        complaint_type=complaint_type,
        location=area,
        complaint_date=prediction_date,
        complaint_time=prediction_time,
        resolution_hours=int(resolution_hours),
        priority=priority,
        status="Submitted",
    )

    if dataframe.empty:
        raise ValueError(
            "Prediction input could not be prepared."
        )

    feature_columns = [
        "complaint_type",
        "location",
        "resolution_hours",
        "priority",
        "status",
        "year",
        "month",
        "day",
        "day_of_week",
        "hour",
        "is_power_cut_complaint",
        "is_high_priority",
        "is_medium_priority",
        "is_escalated",
    ]

    missing_columns = [
        column
        for column in feature_columns
        if column not in dataframe.columns
    ]
    if missing_columns:
        raise ValueError(
            "Prediction input is missing required features."
        )

    model_package = load_model()
    model = (
        model_package.get("model")
        if isinstance(model_package, dict)
        else model_package
    )

    if model is None or not hasattr(model, "predict"):
        raise ValueError(
            "The saved Random Forest model is invalid."
        )

    dataframe = dataframe[feature_columns]
    prediction_value = int(model.predict(dataframe)[0])
    probability = None

    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(dataframe)[0]
        probability = float(max(probabilities))

    result = {
        "prediction": prediction_value,
        "power_cut_likely": bool(prediction_value),
        "probability": probability,
        "confidence": (
            round(probability * 100, 2)
            if probability is not None
            else None
        ),
    }

    prediction_record, confidence = _save_resident_prediction(
        resident=resident,
        area=area,
        prediction_date=prediction_date,
        prediction_time=prediction_time,
        result=result,
        complaint=latest_complaint,
    )

    return result, prediction_record, confidence


@resident_bp.route("/predictions")
@resident_required
def predictions():
    """
    Resident power-cut prediction page.

    GET parameters supported for compatibility:
    - location or area: selected complaint area
    - date or prediction_date: requested prediction date

    If both are supplied, the system generates a fresh Random Forest
    prediction, stores it, and then displays the stored result.
    """
    resident = get_current_resident()

    if resident is None:
        return redirect(url_for("auth.login"))

    selected_area = normalize_location(
        request.args.get("location")
        or request.args.get("area")
        or ""
    )

    raw_date = (
        request.args.get("date")
        or request.args.get("prediction_date")
        or ""
    )

    selected_date = None
    generation_requested = bool(selected_area or raw_date)

    if raw_date:
        try:
            selected_date = _parse_prediction_date(raw_date)
        except ValueError as exc:
            flash(str(exc), "error")
            selected_date = utc_now().date()

    if selected_area and selected_date is None:
        selected_date = utc_now().date()

    context = _prediction_context(
        resident=resident,
        selected_area=selected_area,
        prediction_date=selected_date,
    )

    if generation_requested and selected_area:
        locations = context["locations"]
        allowed = any(
            location.lower() == selected_area.lower()
            for location in locations
        )

        if not allowed:
            flash(
                "Please select an area from your complaint locations.",
                "error",
            )
        elif not model_is_available():
            flash(
                "Prediction is not available because the Random Forest "
                "model has not been trained yet.",
                "warning",
            )
        else:
            try:
                result, prediction_record, confidence = (
                    _generate_area_prediction(
                        resident=resident,
                        area=selected_area,
                        prediction_date=selected_date,
                    )
                )

                if result.get("power_cut_likely"):
                    message = (
                        f"Possible power cut predicted for {selected_area}."
                    )
                else:
                    message = (
                        f"No power cut is currently predicted for "
                        f"{selected_area}."
                    )

                if confidence is not None:
                    message += (
                        f" Prediction confidence: {confidence:.2f}%."
                    )

                flash(message, "success")

                # Re-query so the newly stored prediction is immediately
                # visible in the results section.
                context = _prediction_context(
                    resident=resident,
                    selected_area=selected_area,
                    prediction_date=selected_date,
                )
                context["latest_generated_prediction"] = _PredictionView(prediction_record)
                context["latest_prediction_result"] = result

            except (FileNotFoundError, RuntimeError) as exc:
                db.session.rollback()
                flash(str(exc), "warning")
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "Unable to generate resident power-cut prediction."
                )
                flash(
                    "The prediction could not be generated. "
                    "Please try again after checking the model and dataset.",
                    "error",
                )

    return render_template(
        "resident/predictions.html",
        **context,
    )


# ============================================================
# AREA-SPECIFIC PREDICTIONS
# ============================================================

@resident_bp.route(
    "/predictions/area",
    methods=["GET"],
)
@resident_required
def area_predictions():
    """
    Compatibility route for older prediction links/forms.

    It accepts both `area` and `location`, plus `date` or
    `prediction_date`, and delegates to the main prediction workflow.
    """
    resident = get_current_resident()

    if resident is None:
        return redirect(url_for("auth.login"))

    area = normalize_location(
        request.args.get("location")
        or request.args.get("area")
        or ""
    )

    if not area:
        flash("Please select an area.", "error")
        return redirect(url_for("resident.predictions"))

    raw_date = (
        request.args.get("date")
        or request.args.get("prediction_date")
        or ""
    )

    try:
        prediction_date = _parse_prediction_date(raw_date)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(
            url_for(
                "resident.predictions",
                location=area,
            )
        )

    return redirect(
        url_for(
            "resident.predictions",
            location=area,
            date=prediction_date.isoformat(),
        )
    )

