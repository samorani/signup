# signup

The office-hours sign-up sheet from *Building Software Systems with AI*, as it is at the end of chapter 11, *Adding Accounts and a Database*: accounts, the sheet, a SQLite database, and the Google Cloud setup script `gcp_setup.sh`. This is the starting point for chapter 12, *Deployment*.

To start chapter 12 from here: click *Use this template* on GitHub, name your repository `signup`, clone it into your home folder, then in `~/signup`:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
flask --app app run
```

The app is configured through a `.env` file next to `app.py` (never commit it):

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL_DEV` | Postgres connection string used when running locally, e.g. from Neon |
| `DATABASE_URL_PROD` | Postgres connection string used on Cloud Run (the app detects Cloud Run via its `K_SERVICE` variable) |
| `SECRET_KEY` | Random value that signs the session cookie: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SITE_NAME` | Title shown in the browser tab and page header |
| `ADMIN_USERNAME` | The user who can see who booked each slot |
| `TIMEZONE` | Optional IANA zone the office hours are in, e.g. `America/Los_Angeles` (default: the system zone) |
| `ATTACHMENTS_BUCKET` | Private Cloud Storage bucket for the PDFs and images attached to bookings, `PROJECT_ID-attachments` |

### Attachments

A logged-in user can attach one PDF or image (PNG, JPEG, GIF or WebP, at most 5 MB) to each of their upcoming bookings; a new file replaces the old one, and cancelling the booking deletes it. The file is stored in `ATTACHMENTS_BUCKET` as `bookings/<booking id>`, and its name in the `attachment` column of `bookings`. Only the booking's owner and the administrator can open it.

The bucket was created once, private, and the service's account (the project's default compute account) was allowed to read and write it:

```
gcloud storage buckets create gs://PROJECT_ID-attachments --location us-west1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding gs://PROJECT_ID-attachments --member serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role roles/storage.objectAdmin
```

When you run the app on your laptop it reaches the bucket with your own Google account; run `gcloud auth application-default login` once if you have not already.

`python smoke_test.py` runs the end-to-end checks in a throwaway schema of the development database.

## Deploying to Cloud Run

`gcp_setup.sh` prepares the Google Cloud project (billing, APIs, build permission). After that, `deploy.sh` deploys the app:

```
bash deploy.sh
```

It stores `DATABASE_URL_PROD` and `SECRET_KEY` as Secret Manager secrets (created from `.env` the first time, then left alone unless you pass `--update-secrets`), passes `SITE_NAME`, `ADMIN_USERNAME`, `TIMEZONE` and `ATTACHMENTS_BUCKET` as plain environment variables, and never uploads `.env` itself (`.gcloudignore`). `Procfile` tells Cloud Run to serve the app with gunicorn.
