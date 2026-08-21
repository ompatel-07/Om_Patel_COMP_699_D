from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from flask import current_app
from flask_mail import Message

from app.extensions import db, mail
from app.models import Complaint, Notification, User


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(value: Optional[str]) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value).strip().split()
    )


def get_user(user_id: int) -> Optional[User]:
    return db.session.get(
        User,
        user_id,
    )


def create_notification(
    user_id: int,
    title: str,
    message: str,
    notification_type: str = "general",
    complaint_id: Optional[int] = None,
    commit: bool = True,
) -> Notification:
    """
    Create an in-system notification for a user.
    """

    user = get_user(user_id)

    if not user:
        raise ValueError(
            "User account was not found."
        )

    title = normalize_text(title)
    message = normalize_text(message)
    notification_type = normalize_text(
        notification_type
    )

    if not title:
        raise ValueError(
            "Notification title is required."
        )

    if not message:
        raise ValueError(
            "Notification message is required."
        )

    notification = Notification(
        user_id=user.id,
        complaint_id=complaint_id,
        title=title,
        message=message,
        notification_type=(
            notification_type
            or "general"
        ),
        is_read=False,
        created_at=utc_now(),
    )

    db.session.add(
        notification
    )

    if commit:
        db.session.commit()

    return notification


def send_email_notification(
    user_id: int,
    subject: str,
    message: str,
) -> bool:
    """
    Send an email notification when email notification is enabled.

    Email delivery is optional. In-system notifications remain the
    primary notification mechanism.
    """

    email_enabled = current_app.config.get(
        "EMAIL_ENABLED",
        False,
    )

    if not email_enabled:
        return False

    user = get_user(user_id)

    if not user:
        return False

    email_address = normalize_text(
        getattr(
            user,
            "email",
            "",
        )
    )

    if not email_address:
        return False

    try:
        sender = current_app.config.get(
            "MAIL_DEFAULT_SENDER"
        )

        msg = Message(
            subject=normalize_text(
                subject
            ),
            recipients=[
                email_address
            ],
            body=message,
            sender=sender,
        )

        mail.send(msg)

        return True

    except Exception:
        current_app.logger.exception(
            "Unable to send email notification."
        )

        return False


def notify_user(
    user_id: int,
    title: str,
    message: str,
    notification_type: str = "general",
    complaint_id: Optional[int] = None,
    send_email: bool = True,
) -> Notification:
    """
    Create an application notification and optionally send
    the same information by email.
    """

    notification = create_notification(
        user_id=user_id,
        title=title,
        message=message,
        notification_type=notification_type,
        complaint_id=complaint_id,
        commit=False,
    )

    db.session.commit()

    if send_email:
        send_email_notification(
            user_id=user_id,
            subject=title,
            message=message,
        )

    return notification


def notify_complaint_submitted(
    complaint: Complaint,
) -> Notification:
    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Submitted",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} has been submitted "
            f"successfully. "
            f"Priority: {complaint.priority}. "
            f"Preferred resolution time: "
            f"{complaint.resolution_hours} hours."
        ),
        notification_type="complaint_submitted",
        complaint_id=complaint.id,
    )


def notify_complaint_assigned(
    complaint: Complaint,
) -> Optional[Notification]:
    if not complaint.resident_id:
        return None

    if not complaint.assigned_staff_id:
        return None

    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Assigned",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} has been assigned "
            f"to Electricity Department Staff for processing."
        ),
        notification_type="complaint_assigned",
        complaint_id=complaint.id,
    )


def notify_staff_complaint_assigned(
    complaint: Complaint,
) -> Optional[Notification]:
    if not complaint.assigned_staff_id:
        return None

    return notify_user(
        user_id=complaint.assigned_staff_id,
        title="New Complaint Assigned",
        message=(
            f"Complaint "
            f"{complaint.complaint_number} has been assigned "
            f"to you. "
            f"Type: {complaint.complaint_type}. "
            f"Priority: {complaint.priority}. "
            f"Location: {complaint.location}."
        ),
        notification_type="staff_assignment",
        complaint_id=complaint.id,
    )


def notify_complaint_accepted(
    complaint: Complaint,
) -> Notification:
    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Accepted",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} has been accepted "
            f"by Electricity Department Staff and is now "
            f"being processed."
        ),
        notification_type="complaint_accepted",
        complaint_id=complaint.id,
    )


def notify_complaint_status_changed(
    complaint: Complaint,
    old_status: Optional[str],
    new_status: str,
) -> Optional[Notification]:
    if old_status == new_status:
        return None

    status_messages = {
        "Submitted": (
            "Your complaint has been submitted."
        ),
        "In Progress": (
            "Your complaint is currently being processed "
            "by Electricity Department Staff."
        ),
        "Resolved": (
            "Your complaint has been resolved."
        ),
        "Escalated": (
            "Your complaint has been escalated to a "
            "Senior Electricity Officer for review."
        ),
        "Withdrawn": (
            "Your complaint has been withdrawn successfully."
        ),
    }

    message = status_messages.get(
        new_status,
        (
            f"The status of your complaint has changed "
            f"from {old_status or 'Unknown'} to {new_status}."
        ),
    )

    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Status Updated",
        message=(
            f"Complaint "
            f"{complaint.complaint_number}: "
            f"{message}"
        ),
        notification_type="status_update",
        complaint_id=complaint.id,
    )


def notify_complaint_resolved(
    complaint: Complaint,
) -> Notification:
    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Resolved",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} has been resolved. "
            f"Please review the repair information and "
            f"resolution details in your complaint history."
        ),
        notification_type="complaint_resolved",
        complaint_id=complaint.id,
    )


def notify_complaint_escalated(
    complaint: Complaint,
    officer_ids: Optional[list[int]] = None,
) -> list[Notification]:
    """
    Notify the resident and all supplied Senior Electricity
    Officers when a complaint is automatically escalated.
    """

    notifications = []

    resident_notification = notify_user(
        user_id=complaint.resident_id,
        title="Complaint Escalated",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} could not be resolved "
            f"within the selected "
            f"{complaint.resolution_hours}-hour period. "
            f"It has been escalated to a Senior Electricity "
            f"Officer for review."
        ),
        notification_type="complaint_escalated",
        complaint_id=complaint.id,
    )

    notifications.append(
        resident_notification
    )

    if officer_ids:
        for officer_id in officer_ids:
            officer = get_user(
                officer_id
            )

            if not officer:
                continue

            if officer.role != "officer":
                continue

            if not officer.is_active:
                continue

            notification = notify_user(
                user_id=officer.id,
                title="Escalated Complaint Requires Review",
                message=(
                    f"Complaint "
                    f"{complaint.complaint_number} has been "
                    f"escalated because the selected "
                    f"{complaint.resolution_hours}-hour resolution "
                    f"period expired without resolution. "
                    f"Location: {complaint.location}. "
                    f"Priority: {complaint.priority}."
                ),
                notification_type="escalation",
                complaint_id=complaint.id,
            )

            notifications.append(
                notification
            )

    return notifications


def notify_officers_about_escalation(
    complaint: Complaint,
) -> list[Notification]:
    """
    Find active approved Senior Electricity Officers and notify
    them about an escalated complaint.
    """

    officers = (
        User.query
        .filter(
            User.role == "officer",
            User.is_active.is_(True),
            User.approval_status == "Approved",
        )
        .all()
    )

    officer_ids = [
        officer.id
        for officer in officers
    ]

    return notify_complaint_escalated(
        complaint=complaint,
        officer_ids=officer_ids,
    )


def notify_officer_reassignment(
    complaint: Complaint,
    staff_id: int,
) -> Optional[Notification]:
    """
    Notify the selected staff member when a Senior Officer
    assigns or reassigns an escalated complaint.
    """

    staff = get_user(
        staff_id
    )

    if not staff:
        return None

    if staff.role != "staff":
        return None

    return notify_user(
        user_id=staff.id,
        title="Escalated Complaint Assigned",
        message=(
            f"Escalated complaint "
            f"{complaint.complaint_number} has been assigned "
            f"to you by the Senior Electricity Officer. "
            f"Location: {complaint.location}. "
            f"Priority: {complaint.priority}."
        ),
        notification_type="escalated_assignment",
        complaint_id=complaint.id,
    )


def notify_resident_reassignment(
    complaint: Complaint,
) -> Optional[Notification]:
    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Reassigned",
        message=(
            f"Your escalated complaint "
            f"{complaint.complaint_number} has been reassigned "
            f"to Electricity Department Staff for further action."
        ),
        notification_type="reassignment",
        complaint_id=complaint.id,
    )


def notify_final_action(
    complaint: Complaint,
) -> Optional[Notification]:
    action = normalize_text(
        complaint.final_action
    )

    if not action:
        action = (
            "The Senior Electricity Officer has recorded "
            "the final action for your complaint."
        )

    return notify_user(
        user_id=complaint.resident_id,
        title="Final Action Recorded",
        message=(
            f"Final action has been recorded for complaint "
            f"{complaint.complaint_number}. "
            f"Action: {action}"
        ),
        notification_type="final_action",
        complaint_id=complaint.id,
    )


def notify_complaint_withdrawn(
    complaint: Complaint,
) -> Notification:
    return notify_user(
        user_id=complaint.resident_id,
        title="Complaint Withdrawn",
        message=(
            f"Your complaint "
            f"{complaint.complaint_number} has been withdrawn "
            f"successfully because it was withdrawn before staff "
            f"assignment."
        ),
        notification_type="complaint_withdrawn",
        complaint_id=complaint.id,
    )


def notify_prediction_available(
    user_id: int,
    location: str,
    power_cut_likely: bool,
    confidence: Optional[float] = None,
) -> Notification:
    if power_cut_likely:
        message = (
            f"The prediction system indicates a possible "
            f"power cut for {location} based on available "
            f"complaint patterns."
        )
    else:
        message = (
            f"The prediction system does not currently indicate "
            f"a likely power cut for {location} based on available "
            f"complaint patterns."
        )

    if confidence is not None:
        message += (
            f" Prediction confidence: {confidence:.2f}%."
        )

    return notify_user(
        user_id=user_id,
        title="Power Cut Prediction",
        message=message,
        notification_type="prediction",
        complaint_id=None,
        send_email=False,
    )


def get_user_notifications(
    user_id: int,
    unread_only: bool = False,
    limit: int = 100,
):
    query = Notification.query.filter_by(
        user_id=user_id
    )

    if unread_only:
        query = query.filter(
            Notification.is_read.is_(False)
        )

    limit = max(
        1,
        min(
            int(limit),
            200,
        ),
    )

    return (
        query
        .order_by(
            Notification.created_at.desc()
        )
        .limit(limit)
        .all()
    )


def get_unread_count(
    user_id: int,
) -> int:
    return (
        Notification.query
        .filter(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
        .count()
    )


def get_notification(
    user_id: int,
    notification_id: int,
) -> Optional[Notification]:
    return Notification.query.filter(
        Notification.id == notification_id,
        Notification.user_id == user_id,
    ).first()


def mark_notification_read(
    user_id: int,
    notification_id: int,
) -> Notification:
    notification = get_notification(
        user_id,
        notification_id,
    )

    if not notification:
        raise ValueError(
            "Notification was not found."
        )

    notification.is_read = True

    if hasattr(
        notification,
        "read_at",
    ):
        notification.read_at = utc_now()

    db.session.commit()

    return notification


def mark_all_notifications_read(
    user_id: int,
) -> int:
    notifications = (
        Notification.query
        .filter(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
        .all()
    )

    current_time = utc_now()

    for notification in notifications:
        notification.is_read = True

        if hasattr(
            notification,
            "read_at",
        ):
            notification.read_at = current_time

    db.session.commit()

    return len(
        notifications
    )


def delete_notification(
    user_id: int,
    notification_id: int,
) -> None:
    notification = get_notification(
        user_id,
        notification_id,
    )

    if not notification:
        raise ValueError(
            "Notification was not found."
        )

    db.session.delete(
        notification
    )

    db.session.commit()


def notify_status_change_and_resolution(
    complaint: Complaint,
    old_status: Optional[str],
    new_status: str,
) -> list[Notification]:
    """
    Central notification handler for complaint status changes.

    Resolution receives a dedicated notification so the resident
    receives an explicit resolution message.
    """

    notifications = []

    status_notification = (
        notify_complaint_status_changed(
            complaint=complaint,
            old_status=old_status,
            new_status=new_status,
        )
    )

    if status_notification:
        notifications.append(
            status_notification
        )

    if new_status == "Resolved":
        resolution_notification = (
            notify_complaint_resolved(
                complaint
            )
        )

        notifications.append(
            resolution_notification
        )

    if new_status == "Escalated":
        escalation_notifications = (
            notify_officers_about_escalation(
                complaint
            )
        )

        notifications.extend(
            escalation_notifications
        )

    return notifications


def notify_staff_about_new_complaint(
    complaint: Complaint,
) -> Optional[Notification]:
    if not complaint.assigned_staff_id:
        return None

    return notify_staff_complaint_assigned(
        complaint
    )


def notify_admin(
    title: str,
    message: str,
    complaint_id: Optional[int] = None,
) -> list[Notification]:
    """
    Notify all active administrators.

    This supports administrative visibility for important
    system events without exposing administrator functions
    to residents, staff, or officers.
    """

    administrators = (
        User.query
        .filter(
            User.role == "admin",
            User.is_active.is_(True),
        )
        .all()
    )

    notifications = []

    for administrator in administrators:
        notification = notify_user(
            user_id=administrator.id,
            title=title,
            message=message,
            notification_type="admin",
            complaint_id=complaint_id,
            send_email=False,
        )

        notifications.append(
            notification
        )

    return notifications


def notify_dataset_uploaded(
    uploaded_by_user_id: int,
    record_count: int,
) -> list[Notification]:
    return notify_admin(
        title="Complaint Dataset Updated",
        message=(
            f"Administrator dataset upload completed. "
            f"{record_count} complaint records were added "
            f"to the system dataset."
        ),
    )


def notify_model_retrained(
    accuracy: Optional[float] = None,
) -> list[Notification]:
    if accuracy is not None:
        message = (
            "The Random Forest prediction model was retrained "
            f"successfully. Evaluation accuracy: "
            f"{accuracy:.2f}%."
        )
    else:
        message = (
            "The Random Forest prediction model was retrained "
            "successfully."
        )

    return notify_admin(
        title="Prediction Model Retrained",
        message=message,
    )