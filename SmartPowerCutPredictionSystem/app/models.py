from datetime import datetime, timezone

from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    full_name = db.Column(db.String(120), nullable=False)

    email = db.Column(
        db.String(150),
        unique=True,
        nullable=False,
        index=True,
    )

    phone = db.Column(db.String(20), nullable=True)

    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(
        db.String(20),
        nullable=False,
        default="resident",
        index=True,
    )

    approval_status = db.Column(
        db.String(20),
        nullable=False,
        default="Approved",
        index=True,
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    address = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    last_login_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    complaints = db.relationship(
        "Complaint",
        foreign_keys="Complaint.resident_id",
        back_populates="resident",
        lazy=True,
    )

    assigned_complaints = db.relationship(
        "Complaint",
        foreign_keys="Complaint.assigned_staff_id",
        back_populates="assigned_staff",
        lazy=True,
    )

    notifications = db.relationship(
        "Notification",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy=True,
    )

    approved_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
    )

    approved_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    approver = db.relationship(
        "User",
        remote_side=[id],
        backref=db.backref(
            "approved_users",
            lazy=True,
        ),
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(
            self.password_hash,
            password,
        )

    def is_approved(self):
        """Return True when this user is allowed to access the system.

        Residents are public users and do not require administrator approval.
        Staff and Senior Electricity Officers must have an Approved status.
        The comparison is intentionally case-insensitive so existing database
        rows containing values such as "approved" or "Approved" behave the same.
        """
        role = (self.role or "").strip().lower()
        approval_status = (self.approval_status or "").strip().lower()

        return (
            role == "resident"
            or approval_status == "approved"
        )

    def can_login(self):
        """Return whether the account is active and permitted to log in."""
        return bool(self.is_active) and self.is_approved()

    @property
    def is_resident(self):
        return (self.role or "").strip().lower() == "resident"

    @property
    def is_staff(self):
        return (self.role or "").strip().lower() == "staff"

    @property
    def is_officer(self):
        return (self.role or "").strip().lower() in {
            "officer",
            "senior_officer",
            "senior electricity officer",
            "senior_electricity_officer",
        }

    def __repr__(self):
        return f"<User {self.email}>"


def cls_status_is_resolved(status):
    """Return True for resolved complaint status values."""
    return (status or "").strip().lower() == "resolved"


class Complaint(db.Model):
    __tablename__ = "complaints"

    id = db.Column(db.Integer, primary_key=True)

    complaint_number = db.Column(
        db.String(30),
        unique=True,
        nullable=False,
        index=True,
    )

    resident_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    complaint_type = db.Column(
        db.String(50),
        nullable=False,
        index=True,
    )

    description = db.Column(
        db.Text,
        nullable=False,
    )

    location = db.Column(
        db.String(255),
        nullable=False,
        index=True,
    )

    complaint_date = db.Column(
        db.Date,
        nullable=False,
    )

    complaint_time = db.Column(
        db.Time,
        nullable=False,
    )

    image_filename = db.Column(
        db.String(255),
        nullable=True,
    )

    resolution_hours = db.Column(
        db.Integer,
        nullable=False,
    )

    deadline_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    priority = db.Column(
        db.String(20),
        nullable=False,
        default="Low",
        index=True,
    )

    status = db.Column(
        db.String(30),
        nullable=False,
        default="Submitted",
        index=True,
    )

    assigned_staff_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    accepted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    resolved_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    escalated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    withdrawn_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    repair_notes = db.Column(
        db.Text,
        nullable=True,
    )

    final_action = db.Column(
        db.Text,
        nullable=True,
    )

    escalation_reason = db.Column(
        db.Text,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    resident = db.relationship(
        "User",
        foreign_keys=[resident_id],
        back_populates="complaints",
    )

    assigned_staff = db.relationship(
        "User",
        foreign_keys=[assigned_staff_id],
        back_populates="assigned_complaints",
    )

    history = db.relationship(
        "ComplaintHistory",
        back_populates="complaint",
        cascade="all, delete-orphan",
        order_by="ComplaintHistory.created_at.desc()",
        lazy=True,
    )

    notifications = db.relationship(
        "Notification",
        back_populates="complaint",
        cascade="all, delete-orphan",
        lazy=True,
    )

    predictions = db.relationship(
        "Prediction",
        back_populates="complaint",
        lazy=True,
    )

    # ------------------------------------------------------------------
    # Workflow / compatibility properties
    # ------------------------------------------------------------------
    # These are computed from the existing database columns.  They do NOT
    # require a new database column, so they are safe for the current SQLite
    # database and also work in SQLAlchemy query filters.
    @hybrid_property
    def was_escalated(self):
        """Whether this complaint has been escalated to officer review.

        Escalation is represented by ``escalated_at`` in the database.
        Older/newer route code may refer to ``Complaint.was_escalated``;
        exposing this compatibility property keeps that workflow consistent
        without adding a duplicate database field.
        """
        return self.escalated_at is not None

    @was_escalated.expression
    def was_escalated(cls):
        return cls.escalated_at.isnot(None)

    @hybrid_property
    def is_assigned(self):
        """Whether the complaint currently has a staff member assigned."""
        return self.assigned_staff_id is not None

    @is_assigned.expression
    def is_assigned(cls):
        return cls.assigned_staff_id.isnot(None)

    @hybrid_property
    def is_resolved(self):
        """Whether the complaint has reached the resolved state."""
        return cls_status_is_resolved(self.status)

    @is_resolved.expression
    def is_resolved(cls):
        return cls.status.in_(("Resolved", "resolved"))

    @hybrid_property
    def is_withdrawn(self):
        """Whether the resident withdrew the complaint."""
        return self.withdrawn_at is not None

    @is_withdrawn.expression
    def is_withdrawn(cls):
        return cls.withdrawn_at.isnot(None)

    @hybrid_property
    def is_overdue(self):
        """Whether the complaint deadline has passed while still unresolved."""
        if self.deadline_at is None:
            return False

        status = (self.status or "").strip().lower()
        if status in {"resolved", "withdrawn", "closed"}:
            return False

        return utc_now() > self.deadline_at

    @is_overdue.expression
    def is_overdue(cls):
        return db.and_(
            cls.deadline_at.isnot(None),
            cls.deadline_at < utc_now(),
            ~cls.status.in_(("Resolved", "resolved", "Withdrawn", "withdrawn", "Closed", "closed")),
        )

    @property
    def resolution_deadline_passed(self):
        """Alias used by escalation/automation code."""
        return self.is_overdue

    @property
    def has_final_action(self):
        return bool((self.final_action or "").strip())

    @property
    def has_repair_notes(self):
        return bool((self.repair_notes or "").strip())

    @property
    def display_status(self):
        return (self.status or "").replace("_", " ").strip().title()

    @property
    def display_priority(self):
        return (self.priority or "Low").replace("_", " ").strip().title()

    def __repr__(self):
        return f"<Complaint {self.complaint_number}>"


class ComplaintHistory(db.Model):
    __tablename__ = "complaint_history"

    id = db.Column(db.Integer, primary_key=True)

    complaint_id = db.Column(
        db.Integer,
        db.ForeignKey("complaints.id"),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    action = db.Column(
        db.String(100),
        nullable=False,
    )

    old_status = db.Column(
        db.String(30),
        nullable=True,
    )

    new_status = db.Column(
        db.String(30),
        nullable=True,
    )

    notes = db.Column(
        db.Text,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    complaint = db.relationship(
        "Complaint",
        back_populates="history",
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
    )

    @property
    def status_changed(self):
        return (
            (self.old_status or "").strip().lower()
            != (self.new_status or "").strip().lower()
        )

    def __repr__(self):
        return f"<ComplaintHistory {self.id}>"


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    complaint_id = db.Column(
        db.Integer,
        db.ForeignKey("complaints.id"),
        nullable=True,
        index=True,
    )

    notification_type = db.Column(
        db.String(50),
        nullable=False,
    )

    title = db.Column(
        db.String(150),
        nullable=False,
    )

    message = db.Column(
        db.Text,
        nullable=False,
    )

    is_read = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    email_sent = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    read_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    user = db.relationship(
        "User",
        back_populates="notifications",
    )

    complaint = db.relationship(
        "Complaint",
        back_populates="notifications",
    )

    def mark_as_read(self):
        self.is_read = True
        self.read_at = utc_now()

    def __repr__(self):
        return f"<Notification {self.id}>"


class Prediction(db.Model):
    __tablename__ = "predictions"

    id = db.Column(db.Integer, primary_key=True)

    complaint_id = db.Column(
        db.Integer,
        db.ForeignKey("complaints.id"),
        nullable=True,
        index=True,
    )

    area = db.Column(
        db.String(255),
        nullable=False,
        index=True,
    )

    prediction_date = db.Column(
        db.Date,
        nullable=False,
        index=True,
    )

    prediction_time = db.Column(
        db.Time,
        nullable=True,
    )

    prediction_result = db.Column(
        db.String(100),
        nullable=False,
    )

    probability = db.Column(
        db.Float,
        nullable=True,
    )

    model_name = db.Column(
        db.String(100),
        nullable=False,
        default="Random Forest",
    )

    model_version = db.Column(
        db.String(50),
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    complaint = db.relationship(
        "Complaint",
        back_populates="predictions",
    )

    def __repr__(self):
        return f"<Prediction {self.id} - {self.area}>"


class DatasetRecord(db.Model):
    __tablename__ = "dataset_records"

    id = db.Column(db.Integer, primary_key=True)

    complaint_type = db.Column(
        db.String(50),
        nullable=False,
    )

    location = db.Column(
        db.String(255),
        nullable=False,
        index=True,
    )

    complaint_date = db.Column(
        db.Date,
        nullable=False,
    )

    complaint_time = db.Column(
        db.Time,
        nullable=False,
    )

    resolution_hours = db.Column(
        db.Integer,
        nullable=True,
    )

    priority = db.Column(
        db.String(20),
        nullable=True,
    )

    status = db.Column(
        db.String(30),
        nullable=True,
    )

    power_cut_occurred = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    power_cut_time = db.Column(
        db.Time,
        nullable=True,
    )

    weather_condition = db.Column(
        db.String(100),
        nullable=True,
    )

    electricity_usage = db.Column(
        db.Float,
        nullable=True,
    )

    source = db.Column(
        db.String(30),
        nullable=False,
        default="resident",
    )

    source_complaint_id = db.Column(
        db.Integer,
        db.ForeignKey("complaints.id"),
        nullable=True,
        index=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    def __repr__(self):
        return f"<DatasetRecord {self.id} - {self.location}>"
    