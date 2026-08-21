from datetime import datetime, timezone

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.extensions import db
from app.models import User


# ============================================================
# AUTHENTICATION BLUEPRINT
# ============================================================

auth_bp = Blueprint("auth", __name__)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def utc_now():
    """
    Return the current UTC date and time.
    """
    return datetime.now(timezone.utc)


def normalize_email(email):
    """
    Normalize an email address before storing or searching.
    """
    return (email or "").strip().lower()


def get_dashboard_endpoint(role):
    """
    Return the dashboard endpoint for a user's role.
    """

    dashboard_endpoints = {
        "resident": "resident.dashboard",
        "staff": "staff.dashboard",
        "officer": "officer.dashboard",
        "admin": "admin.dashboard",
    }

    return dashboard_endpoints.get(
        (role or "").strip().lower()
    )


def is_valid_password(password):
    """
    Validate the minimum password requirement.
    """

    return bool(
        password
        and len(password) >= 8
    )


def get_logged_in_user():
    """
    Return the currently logged-in user from the session.

    Returns:
        User object if valid session exists.
        None otherwise.
    """

    user_id = session.get("user_id")

    if not user_id:
        return None

    user = db.session.get(
        User,
        user_id
    )

    if not user:
        session.clear()
        return None

    if not user.is_active:
        session.clear()
        return None

    return user


# ============================================================
# DEFAULT SYSTEM ADMIN
# ============================================================

def create_default_admin():
    """
    Create the predefined System Administrator account if it
    does not already exist.

    ADMIN_EMAIL and ADMIN_DEFAULT_PASSWORD should be defined
    in the Flask configuration.
    """

    from flask import current_app

    admin_email = current_app.config.get(
        "ADMIN_EMAIL"
    )

    admin_password = current_app.config.get(
        "ADMIN_DEFAULT_PASSWORD"
    )

    if not admin_email or not admin_password:
        return

    admin_email = normalize_email(
        admin_email
    )

    # --------------------------------------------------------
    # Check whether an administrator already exists.
    # --------------------------------------------------------

    existing_admin = User.query.filter_by(
        role="admin"
    ).first()

    if existing_admin:
        return

    # --------------------------------------------------------
    # Prevent duplicate account by email.
    # --------------------------------------------------------

    existing_user = User.query.filter_by(
        email=admin_email
    ).first()

    if existing_user:
        return

    # --------------------------------------------------------
    # Create default administrator.
    # --------------------------------------------------------

    admin = User(
        full_name="System Administrator",
        email=admin_email,
        role="admin",
        approval_status="Approved",
        is_active=True,
        created_at=utc_now(),
        updated_at=utc_now(),
    )

    admin.set_password(
        admin_password
    )

    db.session.add(admin)
    db.session.commit()


# ============================================================
# HOME
# ============================================================

@auth_bp.route("/")
def home():
    """
    Application home page.

    If a valid user session exists, redirect the user to the
    appropriate dashboard.

    Otherwise, display the public home page.
    """

    if "user_id" in session:

        user = get_logged_in_user()

        if user:

            endpoint = get_dashboard_endpoint(
                user.role
            )

            if endpoint:
                return redirect(
                    url_for(endpoint)
                )

            # Invalid role.
            session.clear()

        else:
            session.clear()

    return render_template(
        "public/home.html"
    )


# ============================================================
# LOGIN
# ============================================================

@auth_bp.route(
    "/login",
    methods=["GET", "POST"]
)
def login():
    """
    Authenticate a user.

    Residents can log in immediately after registration.

    Staff and Senior Electricity Officers must be approved
    by the System Administrator before they can access their
    dashboards.
    """

    # --------------------------------------------------------
    # If already logged in, do not show login page again.
    # --------------------------------------------------------

    if "user_id" in session:

        user = get_logged_in_user()

        if user:

            endpoint = get_dashboard_endpoint(
                user.role
            )

            if endpoint:
                return redirect(
                    url_for(endpoint)
                )

        session.clear()

    # --------------------------------------------------------
    # GET request
    # --------------------------------------------------------

    if request.method == "GET":
        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # POST request
    # --------------------------------------------------------

    email = normalize_email(
        request.form.get(
            "email",
            ""
        )
    )

    password = request.form.get(
        "password",
        ""
    )

    # --------------------------------------------------------
    # Required fields
    # --------------------------------------------------------

    if not email or not password:

        flash(
            "Email and password are required.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Find user
    # --------------------------------------------------------

    user = User.query.filter_by(
        email=email
    ).first()

    if not user:

        flash(
            "Invalid email or password.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Check password
    # --------------------------------------------------------

    if not user.check_password(
        password
    ):

        flash(
            "Invalid email or password.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Check account activation
    # --------------------------------------------------------

    if not user.is_active:

        flash(
            "Your account is currently deactivated. "
            "Please contact the administrator.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Normalize role
    # --------------------------------------------------------

    role = (
        user.role or ""
    ).strip().lower()

    # --------------------------------------------------------
    # Staff and Officer approval verification
    # --------------------------------------------------------

    if role in {
        "staff",
        "officer",
    }:

        approval_status = (
            user.approval_status or ""
        ).strip().lower()

        # ----------------------------------------------------
        # Pending approval
        # ----------------------------------------------------

        if approval_status == "pending":

            return render_template(
                "auth/pending_approval.html",
                user=user
            )

        # ----------------------------------------------------
        # Rejected
        # ----------------------------------------------------

        if approval_status == "rejected":

            flash(
                "Your registration request was rejected "
                "by the administrator.",
                "error"
            )

            return render_template(
                "public/login.html"
            )

        # ----------------------------------------------------
        # Anything other than Approved
        # ----------------------------------------------------

        if approval_status != "approved":

            flash(
                "Your account has not been approved "
                "by the administrator.",
                "error"
            )

            return render_template(
                "public/login.html"
            )

    # --------------------------------------------------------
    # Only valid application roles may log in.
    # --------------------------------------------------------

    allowed_roles = {
        "resident",
        "staff",
        "officer",
        "admin",
    }

    if role not in allowed_roles:

        session.clear()

        flash(
            "Your account has an invalid role. "
            "Please contact the administrator.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Clear any old session data before creating the new
    # authenticated session.
    # --------------------------------------------------------

    session.clear()

    # --------------------------------------------------------
    # Create authenticated session.
    # --------------------------------------------------------

    session["user_id"] = user.id
    session["role"] = role
    session["full_name"] = user.full_name

    # --------------------------------------------------------
    # Update login information.
    # --------------------------------------------------------

    user.last_login_at = utc_now()
    user.updated_at = utc_now()

    db.session.commit()

    # --------------------------------------------------------
    # Determine dashboard.
    # --------------------------------------------------------

    endpoint = get_dashboard_endpoint(
        role
    )

    if not endpoint:

        session.clear()

        flash(
            "Unable to determine your dashboard. "
            "Please contact the administrator.",
            "error"
        )

        return render_template(
            "public/login.html"
        )

    # --------------------------------------------------------
    # Successful login.
    # --------------------------------------------------------

    flash(
        f"Welcome, {user.full_name}.",
        "success"
    )

    return redirect(
        url_for(endpoint)
    )


# ============================================================
# REGISTRATION
# ============================================================

@auth_bp.route(
    "/register",
    methods=["GET", "POST"]
)
def register():
    """
    Register a new user.

    Resident:
        Immediately approved.

    Staff:
        Pending administrator approval.

    Officer:
        Pending administrator approval.

    Admin:
        Cannot be registered through the public form.
    """

    # --------------------------------------------------------
    # Already logged in
    # --------------------------------------------------------

    if "user_id" in session:

        user = get_logged_in_user()

        if user:

            endpoint = get_dashboard_endpoint(
                user.role
            )

            if endpoint:
                return redirect(
                    url_for(endpoint)
                )

        session.clear()

    # --------------------------------------------------------
    # GET request
    # --------------------------------------------------------

    if request.method == "GET":

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # POST request
    # --------------------------------------------------------

    full_name = request.form.get(
        "full_name",
        ""
    ).strip()

    email = normalize_email(
        request.form.get(
            "email",
            ""
        )
    )

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    address = request.form.get(
        "address",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    role = request.form.get(
        "role",
        "resident"
    ).strip().lower()

    # --------------------------------------------------------
    # Public registration roles.
    #
    # Admin is intentionally excluded.
    # --------------------------------------------------------

    allowed_registration_roles = {
        "resident",
        "staff",
        "officer",
    }

    # --------------------------------------------------------
    # Validate full name.
    # --------------------------------------------------------

    if not full_name:

        flash(
            "Full name is required.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Validate email.
    # --------------------------------------------------------

    if not email:

        flash(
            "Email address is required.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Validate password.
    # --------------------------------------------------------

    if not password:

        flash(
            "Password is required.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    if not is_valid_password(
        password
    ):

        flash(
            "Password must contain at least 8 characters.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Confirm password.
    # --------------------------------------------------------

    if password != confirm_password:

        flash(
            "Passwords do not match.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Validate role.
    # --------------------------------------------------------

    if role not in allowed_registration_roles:

        flash(
            "Invalid registration role.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Check duplicate email.
    # --------------------------------------------------------

    existing_user = User.query.filter_by(
        email=email
    ).first()

    if existing_user:

        flash(
            "An account with this email address "
            "already exists.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Determine approval status.
    # --------------------------------------------------------

    if role == "resident":
        approval_status = "Approved"
    else:
        approval_status = "Pending"

    # --------------------------------------------------------
    # Create user.
    # --------------------------------------------------------

    user = User(
        full_name=full_name,
        email=email,
        phone=phone or None,
        address=address or None,
        role=role,
        approval_status=approval_status,
        is_active=True,
        created_at=utc_now(),
        updated_at=utc_now(),
    )

    # --------------------------------------------------------
    # Hash password using User model.
    # --------------------------------------------------------

    user.set_password(
        password
    )

    # --------------------------------------------------------
    # Save user.
    # --------------------------------------------------------

    try:

        db.session.add(user)
        db.session.commit()

    except Exception:

        db.session.rollback()

        flash(
            "Registration could not be completed. "
            "Please try again.",
            "error"
        )

        return render_template(
            "auth/register.html"
        )

    # --------------------------------------------------------
    # Resident registration.
    # --------------------------------------------------------

    if role == "resident":

        flash(
            "Registration successful. "
            "You can now sign in.",
            "success"
        )

        return redirect(
            url_for("auth.login")
        )

    # --------------------------------------------------------
    # Staff / Officer registration.
    # --------------------------------------------------------

    flash(
        "Registration submitted successfully. "
        "Your account is waiting for administrator approval.",
        "success"
    )

    return redirect(
        url_for(
            "auth.pending_approval",
            email=email
        )
    )


# ============================================================
# PENDING APPROVAL
# ============================================================

@auth_bp.route(
    "/pending-approval"
)
def pending_approval():
    """
    Display the approval status page for Staff and Officers.
    """

    email = normalize_email(
        request.args.get(
            "email",
            ""
        )
    )

    user = None

    if email:

        user = User.query.filter_by(
            email=email
        ).first()

    return render_template(
        "auth/pending_approval.html",
        user=user
    )


# ============================================================
# LOGOUT
# ============================================================

@auth_bp.route(
    "/logout"
)
def logout():
    """
    Sign the current user out.

    The complete Flask session is cleared and the user is
    redirected to the login page.
    """

    # --------------------------------------------------------
    # Clear all authentication/session information.
    # --------------------------------------------------------

    session.clear()

    # --------------------------------------------------------
    # Show confirmation message.
    # --------------------------------------------------------

    flash(
        "You have been signed out successfully.",
        "success"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Always redirect to the login page after logout.
    # --------------------------------------------------------

    return redirect(
        url_for("auth.login")
    )


# ============================================================
# CHANGE PASSWORD
# ============================================================

@auth_bp.route(
    "/change-password",
    methods=["GET", "POST"]
)
def change_password():
    """
    Allow an authenticated user to change their password.

    The current project uses the resident change-password
    template.
    """

    # --------------------------------------------------------
    # Authentication check.
    # --------------------------------------------------------

    if "user_id" not in session:

        flash(
            "Please sign in to continue.",
            "error"
        )

        return redirect(
            url_for("auth.login")
        )

    # --------------------------------------------------------
    # Find current user.
    # --------------------------------------------------------

    user = db.session.get(
        User,
        session["user_id"]
    )

    if not user:

        session.clear()

        flash(
            "Your account could not be found.",
            "error"
        )

        return redirect(
            url_for("auth.login")
        )

    # --------------------------------------------------------
    # Check account status.
    # --------------------------------------------------------

    if not user.is_active:

        session.clear()

        flash(
            "Your account is inactive.",
            "error"
        )

        return redirect(
            url_for("auth.login")
        )

    # --------------------------------------------------------
    # POST request.
    # --------------------------------------------------------

    if request.method == "POST":

        current_password = request.form.get(
            "current_password",
            ""
        )

        new_password = request.form.get(
            "new_password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        # ----------------------------------------------------
        # Verify current password.
        # ----------------------------------------------------

        if not user.check_password(
            current_password
        ):

            flash(
                "Current password is incorrect.",
                "error"
            )

            return render_template(
                "resident/change_password.html",
                user=user
            )

        # ----------------------------------------------------
        # Validate new password.
        # ----------------------------------------------------

        if not is_valid_password(
            new_password
        ):

            flash(
                "New password must contain at least "
                "8 characters.",
                "error"
            )

            return render_template(
                "resident/change_password.html",
                user=user
            )

        # ----------------------------------------------------
        # Confirm new password.
        # ----------------------------------------------------

        if new_password != confirm_password:

            flash(
                "New passwords do not match.",
                "error"
            )

            return render_template(
                "resident/change_password.html",
                user=user
            )

        # ----------------------------------------------------
        # Make sure new password is different.
        # ----------------------------------------------------

        if user.check_password(
            new_password
        ):

            flash(
                "New password must be different "
                "from the current password.",
                "error"
            )

            return render_template(
                "resident/change_password.html",
                user=user
            )

        # ----------------------------------------------------
        # Update password.
        # ----------------------------------------------------

        user.set_password(
            new_password
        )

        user.updated_at = utc_now()

        db.session.commit()

        # ----------------------------------------------------
        # Success.
        # ----------------------------------------------------

        flash(
            "Password changed successfully.",
            "success"
        )

        # ----------------------------------------------------
        # Return to appropriate dashboard.
        # ----------------------------------------------------

        endpoint = get_dashboard_endpoint(
            user.role
        )

        if endpoint:

            return redirect(
                url_for(endpoint)
            )

        return redirect(
            url_for("auth.login")
        )

    # --------------------------------------------------------
    # GET request.
    # --------------------------------------------------------

    return render_template(
        "resident/change_password.html",
        user=user
    )