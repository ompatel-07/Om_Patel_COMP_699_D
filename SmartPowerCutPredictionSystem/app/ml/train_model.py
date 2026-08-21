from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from flask import current_app

from app.extensions import db
from app.models import DatasetRecord


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_model_path() -> Path:
    """
    Return the location where the trained Random Forest
    model is stored.
    """

    return (
        Path(current_app.root_path).parent
        / "models"
        / "random_forest.pkl"
    )


def get_minimum_records() -> int:
    return int(
        current_app.config.get(
            "MODEL_MINIMUM_RECORDS",
            10,
        )
    )


def get_test_size() -> float:
    value = float(
        current_app.config.get(
            "MODEL_TEST_SIZE",
            0.20,
        )
    )

    if value <= 0 or value >= 1:
        return 0.20

    return value


def get_random_state() -> int:
    return int(
        current_app.config.get(
            "MODEL_RANDOM_STATE",
            42,
        )
    )


def get_estimators() -> int:
    return int(
        current_app.config.get(
            "MODEL_ESTIMATORS",
            100,
        )
    )


def get_dataset_records() -> list[DatasetRecord]:
    return (
        DatasetRecord.query
        .order_by(
            DatasetRecord.created_at.asc()
        )
        .all()
    )


def records_to_dataframe(
    records: list[DatasetRecord],
) -> pd.DataFrame:
    """
    Convert the complaint dataset stored in SQLite into a
    pandas DataFrame suitable for model training.
    """

    rows: list[dict[str, Any]] = []

    for record in records:
        rows.append(
            {
                "complaint_type": (
                    record.complaint_type
                ),
                "location": (
                    record.location
                ),
                "resolution_hours": (
                    record.resolution_hours
                ),
                "priority": (
                    record.priority
                ),
                "status": (
                    record.status
                ),
                "complaint_date": (
                    record.complaint_date
                ),
                "complaint_time": (
                    record.complaint_time
                ),
                "power_cut_occurred": int(
                    bool(
                        record.power_cut_occurred
                    )
                ),
            }
        )

    return pd.DataFrame(rows)


def prepare_dataset(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Clean the complaint dataset and create the derived
    features used by the Random Forest model.
    """

    if dataframe.empty:
        return pd.DataFrame()

    data = dataframe.copy()

    data["complaint_type"] = (
        data["complaint_type"]
        .fillna("Unknown")
        .astype(str)
        .str.strip()
    )

    data["location"] = (
        data["location"]
        .fillna("Unknown")
        .astype(str)
        .str.strip()
    )

    data["priority"] = (
        data["priority"]
        .fillna("Medium")
        .astype(str)
        .str.strip()
    )

    data["status"] = (
        data["status"]
        .fillna("Submitted")
        .astype(str)
        .str.strip()
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

    return data


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


def get_target_column() -> str:
    return "power_cut_occurred"


def validate_dataset(
    dataframe: pd.DataFrame,
) -> None:
    """
    Validate the dataset before attempting model training.
    """

    if dataframe.empty:
        raise ValueError(
            "The complaint dataset is empty."
        )

    minimum_records = (
        get_minimum_records()
    )

    if len(dataframe) < minimum_records:
        raise ValueError(
            f"At least {minimum_records} complaint records "
            f"are required before training the model. "
            f"Current records: {len(dataframe)}."
        )

    target = get_target_column()

    if target not in dataframe.columns:
        raise ValueError(
            "The dataset does not contain the required "
            "power-cut target column."
        )

    target_values = (
        dataframe[target]
        .dropna()
        .astype(int)
        .unique()
        .tolist()
    )

    if len(target_values) < 2:
        raise ValueError(
            "The dataset must contain both positive and negative "
            "power-cut outcomes before the Random Forest model "
            "can be trained."
        )


def create_training_pipeline() -> Pipeline:
    """
    Create the complete preprocessing and Random Forest
    training pipeline.
    """

    categorical_features = [
        "complaint_type",
        "location",
        "priority",
        "status",
    ]

    numerical_features = [
        "resolution_hours",
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

    preprocessor = ColumnTransformer(
        transformers=[
            (
                "categorical",
                OneHotEncoder(
                    handle_unknown="ignore"
                ),
                categorical_features,
            ),
            (
                "numerical",
                "passthrough",
                numerical_features,
            ),
        ]
    )

    classifier = RandomForestClassifier(
        n_estimators=get_estimators(),
        random_state=get_random_state(),
        class_weight="balanced",
        n_jobs=-1,
    )

    return Pipeline(
        steps=[
            (
                "preprocessor",
                preprocessor,
            ),
            (
                "classifier",
                classifier,
            ),
        ]
    )


def split_dataset(
    dataframe: pd.DataFrame,
):
    """
    Divide the complaint dataset into training and testing
    data before building the Random Forest model.
    """

    features = dataframe[
        get_feature_columns()
    ]

    target = dataframe[
        get_target_column()
    ].astype(int)

    test_size = get_test_size()

    try:
        return train_test_split(
            features,
            target,
            test_size=test_size,
            random_state=get_random_state(),
            stratify=target,
        )

    except ValueError:
        return train_test_split(
            features,
            target,
            test_size=test_size,
            random_state=get_random_state(),
        )


def calculate_metrics(
    model: Pipeline,
    x_test: pd.DataFrame,
    y_test: pd.Series,
) -> dict[str, Any]:
    predictions = model.predict(
        x_test
    )

    accuracy = accuracy_score(
        y_test,
        predictions,
    )

    report = classification_report(
        y_test,
        predictions,
        output_dict=True,
        zero_division=0,
    )

    return {
        "accuracy": float(
            accuracy
        ),
        "accuracy_percentage": round(
            float(
                accuracy * 100
            ),
            2,
        ),
        "classification_report": report,
        "testing_records": int(
            len(y_test)
        ),
    }


def save_model(
    model: Pipeline,
    metadata: dict[str, Any],
) -> Path:
    """
    Save the trained model and metadata to the project models
    directory.
    """

    model_path = get_model_path()

    model_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    package = {
        "model": model,
        "metadata": metadata,
        "feature_columns": (
            get_feature_columns()
        ),
        "target_column": (
            get_target_column()
        ),
        "trained_at": (
            utc_now().isoformat()
        ),
    }

    joblib.dump(
        package,
        model_path,
    )

    return model_path


def train_model() -> dict[str, Any]:
    """
    Complete Random Forest training workflow.

    Data source:
        Sample complaint records uploaded by the Administrator
        + complaint records submitted by Residents.

    Workflow:
        1. Read complaint dataset from SQLite.
        2. Validate minimum dataset requirements.
        3. Prepare training features.
        4. Divide data into training and testing datasets.
        5. Train Random Forest.
        6. Evaluate against testing data.
        7. Save the trained model.
    """

    records = get_dataset_records()

    dataframe = records_to_dataframe(
        records
    )

    validate_dataset(
        dataframe
    )

    dataframe = prepare_dataset(
        dataframe
    )

    validate_dataset(
        dataframe
    )

    (
        x_train,
        x_test,
        y_train,
        y_test,
    ) = split_dataset(
        dataframe
    )

    model = create_training_pipeline()

    model.fit(
        x_train,
        y_train,
    )

    metrics = calculate_metrics(
        model,
        x_test,
        y_test,
    )

    metadata = {
        "training_records": int(
            len(x_train)
        ),
        "testing_records": int(
            len(x_test)
        ),
        "total_records": int(
            len(dataframe)
        ),
        "accuracy": metrics[
            "accuracy"
        ],
        "accuracy_percentage": metrics[
            "accuracy_percentage"
        ],
        "classification_report": metrics[
            "classification_report"
        ],
        "test_size": get_test_size(),
        "random_state": get_random_state(),
        "n_estimators": get_estimators(),
        "feature_columns": (
            get_feature_columns()
        ),
        "target_column": (
            get_target_column()
        ),
        "trained_at": (
            utc_now().isoformat()
        ),
    }

    model_path = save_model(
        model,
        metadata,
    )

    return {
        "success": True,
        "message": (
            "Random Forest model trained and saved successfully."
        ),
        "model_path": str(
            model_path
        ),
        "training_records": (
            metadata["training_records"]
        ),
        "testing_records": (
            metadata["testing_records"]
        ),
        "total_records": (
            metadata["total_records"]
        ),
        "accuracy": (
            metadata["accuracy"]
        ),
        "accuracy_percentage": (
            metadata["accuracy_percentage"]
        ),
        "classification_report": (
            metadata["classification_report"]
        ),
    }


def train_model_if_ready() -> dict[str, Any]:
    """
    Train the model only when the configured minimum amount
    of complaint data is available.
    """

    record_count = (
        DatasetRecord.query.count()
    )

    minimum_records = (
        get_minimum_records()
    )

    if record_count < minimum_records:
        return {
            "success": False,
            "trained": False,
            "message": (
                f"Model training requires at least "
                f"{minimum_records} records. "
                f"Current records: {record_count}."
            ),
            "record_count": record_count,
            "minimum_records": minimum_records,
        }

    result = train_model()

    result["trained"] = True

    return result


def get_saved_model() -> dict[str, Any] | None:
    """
    Load the saved model package for prediction services.
    """

    model_path = get_model_path()

    if not model_path.exists():
        return None

    try:
        package = joblib.load(
            model_path
        )

        if isinstance(
            package,
            dict,
        ):
            return package

        return {
            "model": package,
            "metadata": {},
            "feature_columns": (
                get_feature_columns()
            ),
            "target_column": (
                get_target_column()
            ),
        }

    except Exception as exc:
        current_app.logger.exception(
            "Unable to load the saved Random Forest model."
        )

        raise RuntimeError(
            "Saved prediction model could not be loaded."
        ) from exc


def get_model_information() -> dict[str, Any]:
    """
    Return model information for the Administrator dashboard.
    """

    model_path = get_model_path()

    if not model_path.exists():
        return {
            "available": False,
            "model_path": str(
                model_path
            ),
            "total_records": (
                DatasetRecord.query.count()
            ),
            "minimum_records": (
                get_minimum_records()
            ),
        }

    package = get_saved_model()

    metadata = (
        package.get(
            "metadata",
            {},
        )
        if package
        else {}
    )

    return {
        "available": True,
        "model_path": str(
            model_path
        ),
        "total_records": (
            DatasetRecord.query.count()
        ),
        "minimum_records": (
            get_minimum_records()
        ),
        "training_records": metadata.get(
            "training_records"
        ),
        "testing_records": metadata.get(
            "testing_records"
        ),
        "accuracy": metadata.get(
            "accuracy"
        ),
        "accuracy_percentage": metadata.get(
            "accuracy_percentage"
        ),
        "trained_at": metadata.get(
            "trained_at"
        ),
        "n_estimators": metadata.get(
            "n_estimators",
            get_estimators(),
        ),
        "feature_columns": metadata.get(
            "feature_columns",
            get_feature_columns(),
        ),
    }


def rebuild_dataset_from_database() -> pd.DataFrame:
    """
    Rebuild the complete model dataset from SQLite.

    The dataset therefore includes both:
        - Administrator-uploaded sample records.
        - Resident-submitted complaint records that have been
          incorporated into DatasetRecord.
    """

    records = get_dataset_records()

    dataframe = records_to_dataframe(
        records
    )

    if dataframe.empty:
        return dataframe

    return prepare_dataset(
        dataframe
    )


def retrain_model() -> dict[str, Any]:
    """
    Explicit model retraining operation for the Administrator.

    New complaint data can therefore be incorporated into a
    future training cycle.
    """

    return train_model_if_ready()


def evaluate_existing_model(
    dataframe: pd.DataFrame,
) -> dict[str, Any]:
    """
    Evaluate the currently saved model against a supplied
    testing dataset.
    """

    package = get_saved_model()

    if not package:
        raise FileNotFoundError(
            "No trained Random Forest model is available."
        )

    model = package[
        "model"
    ]

    prepared = prepare_dataset(
        dataframe
    )

    if prepared.empty:
        raise ValueError(
            "The supplied evaluation dataset is empty."
        )

    x = prepared[
        get_feature_columns()
    ]

    y = prepared[
        get_target_column()
    ].astype(int)

    predictions = model.predict(
        x
    )

    accuracy = accuracy_score(
        y,
        predictions,
    )

    return {
        "accuracy": float(
            accuracy
        ),
        "accuracy_percentage": round(
            float(
                accuracy * 100
            ),
            2,
        ),
        "records": int(
            len(y)
        ),
        "classification_report": (
            classification_report(
                y,
                predictions,
                output_dict=True,
                zero_division=0,
            )
        ),
    }