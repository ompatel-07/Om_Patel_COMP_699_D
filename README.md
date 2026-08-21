# Smart Power Cut Prediction System

## Project Overview

The Smart Power Cut Prediction System is a web-based application developed to manage electricity-related complaints and provide area-based power cut prediction information. Residents can register, submit complaints, upload supporting images, select a preferred resolution period, track complaint progress, view complaint history, receive notifications, and check prediction information.

The system provides separate functions for Residents, Electricity Department Staff, Senior Electricity Officers, and the System Administrator. Staff members process assigned complaints, officers handle escalated complaints, and the administrator manages users, staff accounts, reports, and complaint datasets.

The prediction component uses a Random Forest model. Complaint records stored in the system are prepared as training data, divided for training and testing, and used to generate prediction results.

## Main Features

- Resident account registration and login
- Resident logout and account management
- Complaint submission
- Power cut complaint reporting
- Streetlight issue reporting
- Electric pole damage reporting
- Voltage issue reporting
- Complaint location, date, and time recording
- Complaint image upload
- 24-hour or 48-hour resolution selection
- Complaint status tracking
- Complaint history
- Complaint withdrawal before assignment
- Staff complaint processing
- Complaint status updates and repair notes
- Senior officer escalation handling
- Administrator user and staff management
- Complaint reports and export
- Administrator dataset upload
- Random Forest model training
- Area-based power cut prediction
- Prediction result storage
- In-system notifications

## Technologies Used

- Python
- Flask
- Flask-SQLAlchemy
- SQLite
- HTML5
- CSS3
- Jinja2
- SQLAlchemy
- Pandas
- NumPy
- Scikit-learn
- Joblib
- Visual Studio Code
- Git
- GitHub

## Project Structure

```text
SmartPowerCutPredictionSystem/
│
├── run.py
├── config.py
├── requirements.txt
├── README.md
├── .env
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── extensions.py
│   ├── models.py
│   │
│   ├── routes/
│   │   ├── auth.py
│   │   ├── resident.py
│   │   ├── staff.py
│   │   ├── officer.py
│   │   └── admin.py
│   │
│   ├── services/
│   │   ├── complaint_service.py
│   │   ├── prediction_service.py
│   │   ├── notification_service.py
│   │   └── automation_service.py
│   │
│   └── ml/
│       └── train_model.py
│
├── models/
│   └── random_forest.pkl
│
└── instance/
    └── application database files
```

## Requirements

Install Python 3.11 or another compatible Python version.

Git should be installed to clone and manage the repository.

Visual Studio Code can be used to open and manage the project.

A Python virtual environment is recommended for the project.

## Installation

Open Command Prompt or PowerShell and clone the repository:

```text
git clone YOUR_GITHUB_REPOSITORY_URL
cd SmartPowerCutPredictionSystem
```

Create a virtual environment:

```text
python -m venv venv
```

Activate the virtual environment on Windows:

```text
venv\Scripts\activate
```

Upgrade pip:

```text
python -m pip install --upgrade pip
```

Install project dependencies:

```text
pip install -r requirements.txt
```

If the `python` command is not available, use:

```text
py -m venv venv
venv\Scripts\activate
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

## Environment Configuration

Create or update the `.env` file in the project root.

Example:

```text
SECRET_KEY=your-secret-key
```

Do not upload real passwords, secret keys, or other private configuration values to GitHub.

## Database

The application uses SQLite for local database storage.

The database is managed by the application through Flask and SQLAlchemy.

Complaint records contain information such as complaint type, location, resolution hours, priority, status, assignment information, timestamps, repair notes, and escalation details.

## Running the Application

Make sure the virtual environment is activated:

```text
venv\Scripts\activate
```

Start the Flask application:

```text
python run.py
```

The application is available at:

```text
http://127.0.0.1:5000
```

Open the address in Google Chrome, Microsoft Edge, or another supported browser.

To stop the application, press:

```text
CTRL + C
```

## Application Workflow

### Resident

The resident creates an account and signs in. After authentication, the resident can access the dashboard and submit an electricity complaint. Complaint information is stored in the database and can be viewed through the complaint status and history pages.

The resident can access:

- Dashboard
- My Complaints
- New Complaint
- Predictions
- Notifications
- Profile
- Change Password

### Staff

Electricity Department Staff sign in using an approved staff account. Staff members can view assigned complaints, accept complaints, update complaint status, add repair notes, and complete complaint processing.

Staff accounts must be active and approved before staff functions can be accessed.

### Senior Officer

Senior officers review complaints that require escalation and perform the required assignment and review activities.

### System Administrator

The System Administrator manages users, staff accounts, reports, and complaint datasets. The administrator can upload sample complaint records for model training.

## Dataset and Prediction Workflow

The prediction workflow uses complaint records stored in the system.

```text
Complaint Records
        |
        v
Dataset Preparation
        |
        v
Feature Processing
        |
        v
Training and Testing Split
        |
        v
Random Forest Training
        |
        v
Model Evaluation
        |
        v
random_forest.pkl
        |
        v
Prediction Service
        |
        v
Area-Based Prediction Result
```

The training module prepares complaint records for machine learning and uses relevant complaint information such as complaint type, location, resolution period, priority, status, date, time, and power-cut information.

The trained model is stored as:

```text
models/random_forest.pkl
```

The model training process uses a training and testing split to evaluate the Random Forest model before the trained model is saved.

## Uploading a Complaint Dataset

Sign in as the System Administrator.

Open the Dataset section.

Select the complaint dataset CSV file.

Click the Upload Dataset button.

The system imports valid records and uses the available records for model training when the required data conditions are satisfied.

After successful upload, confirm the message showing the number of imported records and the model training result.

A CSV dataset should contain the fields required by the application's dataset and prediction workflow.

## Testing the Prediction Feature

Start the application:

```text
python run.py
```

Sign in as the System Administrator.

Upload a valid complaint dataset from the Dataset section.

Confirm that the dataset is imported successfully and that the Random Forest model is trained.

Sign out from the administrator account.

Sign in as a Resident.

Open the Predictions page:

```text
http://127.0.0.1:5000/resident/predictions
```

Enter an area that exists in the uploaded complaint dataset.

Select a prediction date.

Click:

```text
View Prediction
```

The system should display prediction information when the model has been trained successfully and suitable complaint records are available for the selected area and date.

## Git Commands

Check repository status:

```text
git status
```

Add changed files:

```text
git add .
```

Create a commit:

```text
git commit -m "Update Smart Power Cut Prediction System"
```

Push changes to GitHub:

```text
git push
```

Get the latest version from GitHub:

```text
git pull
```

## Important Notes

The project is intended for local development, testing, and academic demonstration.

The prediction feature depends on the available complaint dataset. A larger and more representative dataset can provide more useful prediction results.

The trained model file should remain consistent with the application code and data used during training.

The Flask development server is suitable for development and testing. A production deployment should use an appropriate production server and deployment configuration.

Do not commit `.env` files containing real credentials or private keys.

## Troubleshooting

### Python is not recognized

Try using the Python launcher:

```text
py --version
```

Then create the environment with:

```text
py -m venv venv
```

### Virtual environment does not activate

Use:

```text
venv\Scripts\activate
```

For PowerShell, if execution policy prevents activation, open Command Prompt and use the activation command there.

### Missing Python packages

Activate the virtual environment and run:

```text
pip install -r requirements.txt
```

### Prediction is not available

Make sure that:

- A valid complaint dataset has been uploaded.
- The dataset contains the required fields.
- The dataset contains enough usable records.
- Model training completed successfully.
- The trained model file exists.
- The selected prediction area matches an area in the available complaint records.

### Port 5000 is already in use

Stop the other Flask application using the port, or update the application's configuration to use another available port.

## Academic Project

This project was developed as a Master's academic project to demonstrate complaint management, database processing, role-based access, dataset management, and Random Forest based power cut prediction in a web application.

## License

This project is intended for academic and educational use.
