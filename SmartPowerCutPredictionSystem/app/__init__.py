from pathlib import Path

from flask import Flask

from config import configurations
from app.extensions import db, mail, login_manager


def create_app(config_name="default"):
    app = Flask(
        __name__,
        instance_relative_config=True
    )

    app.config.from_object(
        configurations.get(
            config_name,
            configurations["default"]
        )
    )

    instance_path = Path(app.instance_path)
    instance_path.mkdir(
        parents=True,
        exist_ok=True
    )

    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    upload_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    model_folder = Path(app.config["MODEL_FOLDER"])
    model_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    data_folder = Path(app.config["DATA_FOLDER"])
    data_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    app.config["UPLOAD_FOLDER"] = str(upload_folder)
    app.config["MODEL_FOLDER"] = str(model_folder)
    app.config["DATA_FOLDER"] = str(data_folder)

    db.init_app(app)
    mail.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    from app.routes.auth import auth_bp
    from app.routes.resident import resident_bp
    from app.routes.staff import staff_bp
    from app.routes.officer import officer_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(resident_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(officer_bp)
    app.register_blueprint(admin_bp)

    from app.routes.auth import create_default_admin

    with app.app_context():
        from app import models

        db.create_all()
        create_default_admin()

    return app