from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import joblib
import pandas as pd

from flask import current_app

from app.extensions import db
from app.models import DatasetRecord, Prediction


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_model_path() -> Path:
    return (
        Path(current_app.root_path).parent
        / "models"
        / "random_forest.pkl"
    )


def get_dataset_records():
    return (
        DatasetRecord.query
        .order_by(
            DatasetRecord.created_at.asc()
        )
        .all()
    )


def dataset_to_dataframe(records=None) -> pd.DataFrame:
    if records is None:
        records = get_dataset_records()

    rows = []

    for record in records:
        rows.append(
            {
                "complaint_type": record.complaint_type,
                "location": record.location,
                "complaint_date": (
                    record.complaint_date.isoformat()
                    if record.complaint_date
                    else ""
                ),
                "complaint_time": (
                    record.complaint_time.strftime("%H:%M:%S")
                    if record.complaint_time
                    else ""
                ),
                "resolution_hours": (
                    record.resolution_hours
                ),
                "priority": record.priority,
                "status": record.status,
                "power_cut_occurred": int(
                    bool(
                        record.power_cut_occurred
                    )
                ),
            }
        )

    return pd.DataFrame(rows)


def prepare_features(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    if dataframe.empty:
        return pd.DataFrame()

    data = dataframe.copy()

    data["complaint_type"] = (
        data["complaint_type"]
        .fillna("Unknown")
        .astype(str)
    )

    data["location"] = (
        data["location"]
        .fillna("Unknown")
        .astype(str)
    )

    data["priority"] = (
        data["priority"]
        .fillna("Medium")
        .astype(str)
    )

    data["status"] = (
        data["status"]
        .fillna("Submitted")
        .astype(str)
    )

    data["resolution_hours"] = pd.to_numeric(
        data["resolution_hours"],
        errors="coerce",
    ).fillna(24)

    data["complaint_date"] = pd.to_datetime(
        data["complaint_date"],
        errors="coerce",
    )

    data["complaint_time"] = pd.to_datetime(
        data["complaint_time"],
        format="%H:%M:%S",
        errors="coerce",
    )

    data["year"] = (
        data["complaint_date"]
        .dt.year
        .fillna(0)
        .astype(int)
    )

    data["month"] = (
        data["complaint_date"]
        .dt.month
        .fillna(0)
        .astype(int)
    )

    data["day"] = (
        data["complaint_date"]
        .dt.day
        .fillna(0)
        .astype(int)
    )

    data["day_of_week"] = (
        data["complaint_date"]
        .dt.dayofweek
        .fillna(0)
        .astype(int)
    )

    data["hour"] = (
        data["complaint_time"]
        .dt.hour
        .fillna(0)
        .astype(int)
    )

    data["is_power_cut_complaint"] = (
        data["complaint_type"]
        .str.lower()
        .eq("power cut")
        .astype(int)
    )

    data["is_high_priority"] = (
        data["priority"]
        .str.lower()
        .eq("high")
        .astype(int)
    )

    data["is_medium_priority"] = (
        data["priority"]
        .str.lower()
        .eq("medium")
        .astype(int)
    )

    data["is_escalated"] = (
        data["status"]
        .str.lower()
        .eq("escalated")
        .astype(int)
    )

    return data[
        [
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
    ]


def get_feature_columns() -> list[str]:
    return [
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


def build_prediction_input(
    complaint_type: str,
    location: str,
    complaint_date,
    complaint_time,
    resolution_hours: int,
    priority: str,
    status: str = "Submitted",
) -> pd.DataFrame:
    row = {
        "complaint_type": complaint_type,
        "location": location,
        "complaint_date": (
            complaint_date.isoformat()
            if complaint_date
            else ""
        ),
        "complaint_time": (
            complaint_time.strftime("%H:%M:%S")
            if complaint_time
            else ""
        ),
        "resolution_hours": resolution_hours,
        "priority": priority,
        "status": status,
    }

    dataframe = pd.DataFrame(
        [row]
    )

    return prepare_features(
        dataframe
    )


def load_model() -> Any:
    model_path = get_model_path()

    if not model_path.exists():
        raise FileNotFoundError(
            "Random Forest model has not been trained yet."
        )

    return joblib.load(
        model_path
    )


def model_is_available() -> bool:
    return get_model_path().exists()


def get_minimum_records() -> int:
    return int(
        current_app.config.get(
            "MODEL_MINIMUM_RECORDS",
            10,
        )
    )


def get_model_metadata() -> dict[str, Any]:
    model_path = get_model_path()

    if not model_path.exists():
        return {
            "available": False,
            "path": str(model_path),
        }

    try:
        model = joblib.load(
            model_path
        )

        metadata = {
            "available": True,
            "path": str(model_path),
        }

        if isinstance(model, dict):
            stored = model.get("metadata") or {}
            metadata.update(
                {
                    "accuracy": stored.get("accuracy", model.get("accuracy")),
                    "trained_at": stored.get("trained_at", model.get("trained_at")),
                    "training_records": stored.get("training_records", model.get("training_records")),
                    "testing_records": stored.get("testing_records", model.get("testing_records")),
                    "model_version": stored.get("model_version", "1.0"),
                }
            )

        return metadata

    except Exception:
        return {
            "available": False,
            "path": str(model_path),
        }


def train_prediction_model():
    """
    Train the Random Forest model through the central training
    module.

    The actual train/test split, Random Forest training and
    evaluation are kept in train_model.py so the training logic
    remains in one place.
    """

    from app.ml.train_model import train_model

    return train_model()


def predict_power_cut(
    complaint_type: str,
    location: str,
    complaint_date,
    complaint_time,
    resolution_hours: int,
    priority: str,
    status: str = "Submitted",
) -> dict[str, Any]:
    """
    Predict whether a power cut is likely from complaint
    patterns stored in the trained Random Forest model.
    """

    model = load_model()

    dataframe = build_prediction_input(
        complaint_type=complaint_type,
        location=location,
        complaint_date=complaint_date,
        complaint_time=complaint_time,
        resolution_hours=resolution_hours,
        priority=priority,
        status=status,
    )

    if dataframe.empty:
        raise ValueError(
            "Prediction input could not be prepared."
        )

    feature_columns = get_feature_columns()

    missing_columns = [
        column
        for column in feature_columns
        if column not in dataframe.columns
    ]

    if missing_columns:
        raise ValueError(
            "Prediction input is missing required features."
        )

    dataframe = dataframe[
        feature_columns
    ]

    try:
        prediction = model.predict(
            dataframe
        )[0]

        probability = None

        if hasattr(
            model,
            "predict_proba",
        ):
            probabilities = model.predict_proba(
                dataframe
            )[0]

            probability = float(
                max(probabilities)
            )

        return {
            "prediction": int(
                prediction
            ),
            "power_cut_likely": bool(
                prediction
            ),
            "prediction_result": (
                "Power Cut Likely"
                if bool(prediction)
                else "No Power Cut Predicted"
            ),
            "probability": probability,
            "confidence": (
                round(
                    probability * 100,
                    2,
                )
                if probability is not None
                else None
            ),
            "model_name": "Random Forest",
            "model_version": (
                (get_model_metadata().get("model_version") or "1.0")
            ),
        }

    except Exception as exc:
        raise ValueError(
            "Power-cut prediction could not be generated."
        ) from exc


def predict_for_complaint(
    complaint,
    save_result: bool = True,
) -> dict[str, Any]:
    result = predict_power_cut(
        complaint_type=complaint.complaint_type,
        location=complaint.location,
        complaint_date=complaint.complaint_date,
        complaint_time=complaint.complaint_time,
        resolution_hours=complaint.resolution_hours,
        priority=complaint.priority,
        status=complaint.status,
    )

    if save_result:
        save_prediction_result(
            complaint=complaint,
            result=result,
        )

    return result


def save_prediction_result(
    complaint,
    result: dict[str, Any],
) -> Prediction:
    """
    Store the prediction associated with a complaint for
    future analysis.
    """

    prediction_record = Prediction(
        complaint_id=getattr(complaint, "id", None),
        area=(getattr(complaint, "location", None) or "Unknown").strip(),
        prediction_date=(
            getattr(complaint, "complaint_date", None)
            or utc_now().date()
        ),
        prediction_time=getattr(complaint, "complaint_time", None),
        prediction_result=(
            result.get("prediction_result")
            or (
                "Power Cut Likely"
                if result.get("power_cut_likely")
                else "No Power Cut Predicted"
            )
        ),
        probability=(
            float(result["probability"])
            if result.get("probability") is not None
            else None
        ),
        model_name=result.get("model_name", "Random Forest"),
        model_version=result.get("model_version", "1.0"),
        created_at=utc_now(),
    )

    db.session.add(
        prediction_record
    )

    db.session.commit()

    return prediction_record


def get_complaint_predictions(
    complaint_id: int,
):
    return (
        Prediction.query
        .filter_by(
            complaint_id=complaint_id
        )
        .order_by(
            Prediction.created_at.desc()
        )
        .all()
    )


def get_latest_prediction(
    complaint_id: int,
) -> Optional[Prediction]:
    return (
        Prediction.query
        .filter_by(
            complaint_id=complaint_id
        )
        .order_by(
            Prediction.created_at.desc()
        )
        .first()
    )


def get_location_predictions(
    location: str,
):
    return (
        Prediction.query
        .filter(
            Prediction.area.ilike(
                f"%{location.strip()}%"
            )
        )
        .order_by(
            Prediction.created_at.desc()
        )
        .all()
    )


def _prediction_is_power_cut(prediction: Prediction) -> bool:
    """Interpret the current text-based prediction_result field safely."""
    value = getattr(prediction, "prediction_result", None)
    text = str(value or "").strip().lower()
    return text in {
        "1", "true", "yes", "power cut", "power cut likely",
        "likely", "power cut predicted"
    }


def get_location_prediction_summary(
    location: str,
) -> dict[str, Any]:
    predictions = get_location_predictions(
        location
    )

    if not predictions:
        return {
            "location": location,
            "total_predictions": 0,
            "power_cut_predictions": 0,
            "no_power_cut_predictions": 0,
            "average_probability": None,
            "latest_prediction": None,
        }

    power_cut_predictions = sum(
        1
        for prediction in predictions
        if _prediction_is_power_cut(prediction)
    )

    no_power_cut_predictions = (
        len(predictions)
        - power_cut_predictions
    )

    probabilities = [
        prediction.probability
        for prediction in predictions
        if prediction.probability
        is not None
    ]

    average_probability = (
        sum(probabilities)
        / len(probabilities)
        if probabilities
        else None
    )

    latest = predictions[0]

    return {
        "location": location,
        "total_predictions": len(
            predictions
        ),
        "power_cut_predictions": (
            power_cut_predictions
        ),
        "no_power_cut_predictions": (
            no_power_cut_predictions
        ),
        "average_probability": (
            round(
                average_probability,
                4,
            )
            if average_probability is not None
            else None
        ),
        "latest_prediction": latest,
    }


def get_available_prediction_locations():
    locations = (
        db.session.query(
            DatasetRecord.location
        )
        .filter(
            DatasetRecord.location.isnot(
                None
            )
        )
        .distinct()
        .order_by(
            DatasetRecord.location.asc()
        )
        .all()
    )

    return [
        row[0]
        for row in locations
        if row[0]
    ]


def get_prediction_dashboard_data(
    location: Optional[str] = None,
) -> dict[str, Any]:
    records = get_dataset_records()

    total_records = len(
        records
    )

    model_metadata = (
        get_model_metadata()
    )

    if location:
        summary = (
            get_location_prediction_summary(
                location
            )
        )
    else:
        all_predictions = (
            Prediction.query
            .order_by(
                Prediction.created_at.desc()
            )
            .all()
        )

        power_cut_predictions = sum(
            1
            for prediction
            in all_predictions
            if _prediction_is_power_cut(prediction)
        )

        probabilities = [
            prediction.probability
            for prediction
            in all_predictions
            if prediction.probability
            is not None
        ]

        summary = {
            "location": None,
            "total_predictions": len(
                all_predictions
            ),
            "power_cut_predictions": (
                power_cut_predictions
            ),
            "no_power_cut_predictions": (
                len(all_predictions)
                - power_cut_predictions
            ),
            "average_probability": (
                sum(probabilities)
                / len(probabilities)
                if probabilities
                else None
            ),
            "latest_prediction": (
                all_predictions[0]
                if all_predictions
                else None
            ),
        }

    return {
        "dataset_records": total_records,
        "minimum_records": (
            get_minimum_records()
        ),
        "model_available": (
            model_is_available()
        ),
        "model_metadata": model_metadata,
        "summary": summary,
        "locations": (
            get_available_prediction_locations()
        ),
    }


def prediction_ready() -> bool:
    """
    Check whether enough complaint data exists to build
    a prediction model.
    """

    return (
        DatasetRecord.query.count()
        >= get_minimum_records()
    )


def retrain_if_ready():
    """
    Retrain the Random Forest model when the configured
    minimum amount of complaint data is available.
    """

    if not prediction_ready():
        return {
            "trained": False,
            "reason": (
                "Minimum dataset size has not been reached."
            ),
            "records": DatasetRecord.query.count(),
            "minimum_records": (
                get_minimum_records()
            ),
        }

    result = train_prediction_model()

    return {
        "trained": True,
        "result": result,
        "records": DatasetRecord.query.count(),
    }


def predict_location(
    location: str,
    complaint_date,
    complaint_time,
    complaint_type: str = "Power Cut",
    resolution_hours: int = 24,
    priority: str = "High",
) -> dict[str, Any]:
    """
    Generate a prediction for a selected area.

    This is used by the resident prediction view when the
    resident selects an area and wants possible power-cut
    information.
    """

    location = (
        location or ""
    ).strip()

    if not location:
        raise ValueError(
            "Location is required."
        )

    return predict_power_cut(
        complaint_type=complaint_type,
        location=location,
        complaint_date=complaint_date,
        complaint_time=complaint_time,
        resolution_hours=resolution_hours,
        priority=priority,
        status="Submitted",
    )


def save_area_prediction_result(
    location: str,
    complaint_date,
    complaint_time,
    result: dict[str, Any],
    complaint_id: Optional[int] = None,
) -> Prediction:
    """Save an area-based prediction using the current Prediction schema."""
    area = (location or "").strip()
    if not area:
        raise ValueError("Location is required.")

    record = Prediction(
        complaint_id=complaint_id,
        area=area,
        prediction_date=complaint_date or utc_now().date(),
        prediction_time=complaint_time,
        prediction_result=(
            result.get("prediction_result")
            or ("Power Cut Likely" if result.get("power_cut_likely")
                else "No Power Cut Predicted")
        ),
        probability=(
            float(result["probability"])
            if result.get("probability") is not None else None
        ),
        model_name=result.get("model_name", "Random Forest"),
        model_version=result.get("model_version", "1.0"),
        created_at=utc_now(),
    )
    db.session.add(record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unable to save area prediction.")
        raise
    return record


def get_area_power_cut_information(
    location: str,
    complaint_date=None,
    complaint_time=None,
    save_result: bool = True,
) -> dict[str, Any]:
    """
    Provide power-cut prediction information for a selected
    area using the trained Random Forest model.
    """

    if not location or not location.strip():
        raise ValueError(
            "Please select a valid area."
        )

    now = utc_now()

    selected_date = (
        complaint_date
        if complaint_date is not None
        else now.date()
    )

    selected_time = (
        complaint_time
        if complaint_time is not None
        else now.time()
    )

    result = predict_location(
        location=location,
        complaint_date=selected_date,
        complaint_time=selected_time,
    )

    prediction_record = None
    if save_result:
        prediction_record = save_area_prediction_result(
            location=location,
            complaint_date=selected_date,
            complaint_time=selected_time,
            result=result,
        )

    if result["power_cut_likely"]:
        message = (
            "The system predicts a possible power cut "
            "for the selected area based on complaint patterns."
        )
    else:
        message = (
            "The system does not currently predict a "
            "power cut for the selected area based on "
            "available complaint patterns."
        )

    return {
        "location": location,
        "prediction": result,
        "message": message,
        "prediction_record": prediction_record,
        "saved": prediction_record is not None,
    }


def get_recent_predictions(
    limit: int = 20,
):
    limit = max(
        1,
        min(
            int(limit),
            100,
        ),
    )

    return (
        Prediction.query
        .order_by(
            Prediction.created_at.desc()
        )
        .limit(limit)
        .all()
    )