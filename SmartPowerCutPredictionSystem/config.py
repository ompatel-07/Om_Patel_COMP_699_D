import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv(
        "SECRET_KEY",
        "smart-power-cut-prediction-system-secret-key"
    )

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'instance' / 'smart_power_cut.db'}"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024

    UPLOAD_FOLDER = BASE_DIR / "uploads" / "complaints"

    ALLOWED_IMAGE_EXTENSIONS = {
        "jpg",
        "jpeg",
        "png",
        "webp"
    }

    MODEL_FOLDER = BASE_DIR / "models"

    MODEL_PATH = MODEL_FOLDER / "random_forest.pkl"

    DATA_FOLDER = BASE_DIR / "data"

    SAMPLE_DATASET_PATH = DATA_FOLDER / "sample_complaints.csv"

    SESSION_COOKIE_HTTPONLY = True

    SESSION_COOKIE_SAMESITE = "Lax"

    SESSION_COOKIE_SECURE = False

    EMAIL_ENABLED = os.getenv(
        "EMAIL_ENABLED",
        "False"
    ).lower() == "true"

    MAIL_SERVER = os.getenv(
        "MAIL_SERVER",
        "smtp.gmail.com"
    )

    MAIL_PORT = int(
        os.getenv(
            "MAIL_PORT",
            "587"
        )
    )

    MAIL_USE_TLS = os.getenv(
        "MAIL_USE_TLS",
        "True"
    ).lower() == "true"

    MAIL_USERNAME = os.getenv(
        "MAIL_USERNAME",
        ""
    )

    MAIL_PASSWORD = os.getenv(
        "MAIL_PASSWORD",
        ""
    )

    MAIL_DEFAULT_SENDER = os.getenv(
        "MAIL_DEFAULT_SENDER",
        MAIL_USERNAME
    )

    ADMIN_EMAIL = os.getenv(
        "ADMIN_EMAIL",
        ""
    )

    ADMIN_DEFAULT_PASSWORD = os.getenv(
        "ADMIN_DEFAULT_PASSWORD",
        "Admin@123"
    )

    COMPLAINT_RESOLUTION_OPTIONS = (
        24,
        48
    )

    COMPLAINT_TYPES = (
        "Power Cut",
        "Streetlight",
        "Electric Pole Damage",
        "Voltage Issue"
    )

    COMPLAINT_STATUSES = (
        "Submitted",
        "In Progress",
        "Resolved",
        "Escalated",
        "Withdrawn"
    )

    COMPLAINT_PRIORITIES = (
        "High",
        "Medium",
        "Low"
    )

    USER_ROLES = (
        "resident",
        "staff",
        "officer",
        "admin"
    )

    APPROVAL_STATUSES = (
        "Pending",
        "Approved",
        "Rejected"
    )

    MODEL_MINIMUM_RECORDS = int(
        os.getenv(
            "MODEL_MINIMUM_RECORDS",
            "10"
        )
    )

    MODEL_TEST_SIZE = float(
        os.getenv(
            "MODEL_TEST_SIZE",
            "0.20"
        )
    )

    MODEL_RANDOM_STATE = int(
        os.getenv(
            "MODEL_RANDOM_STATE",
            "42"
        )
    )

    MODEL_ESTIMATORS = int(
        os.getenv(
            "MODEL_ESTIMATORS",
            "100"
        )
    )

    AUTOMATION_INTERVAL_MINUTES = int(
        os.getenv(
            "AUTOMATION_INTERVAL_MINUTES",
            "15"
        )
    )

    @staticmethod
    def init_app(app):
        instance_path = BASE_DIR / "instance"
        upload_path = BASE_DIR / "uploads" / "complaints"
        data_path = BASE_DIR / "data"
        model_path = BASE_DIR / "models"

        instance_path.mkdir(
            parents=True,
            exist_ok=True
        )

        upload_path.mkdir(
            parents=True,
            exist_ok=True
        )

        data_path.mkdir(
            parents=True,
            exist_ok=True
        )

        model_path.mkdir(
            parents=True,
            exist_ok=True
        )

        app.config["UPLOAD_FOLDER"] = str(
            upload_path
        )

        app.config["MODEL_PATH"] = str(
            Config.MODEL_PATH
        )

        app.config["SAMPLE_DATASET_PATH"] = str(
            Config.SAMPLE_DATASET_PATH
        )


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True

    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    DEBUG = False

    SESSION_COOKIE_SECURE = True


configurations = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig
}