# Alembic Database Migrations Guide

This guide provides the essential commands and workflows for managing our PostgreSQL database schema using Alembic and SQLAlchemy.

## Prerequisites
Before running any Alembic commands, ensure you are in the project root (where `alembic.ini` is located) and your virtual environment is active:
*   Activate the environment using `uv` and `pyenv`: `source .venv/bin/activate`
*   Ensure your new SQLAlchemy models are imported within `alembic/env.py` so Alembic can detect them.

## 1. Creating a New Migration (Schema Update)
When you add a new model or modify an existing one, you must generate a migration script. We use **sequential numbering** (e.g., 0011, 0012) instead of Alembic's default random hashes to keep our history readable.
*   Check the `alembic/versions/` folder to find the latest revision number.
*   Run the autogenerate command, explicitly setting the next sequential ID:
    ```bash
    alembic revision --autogenerate --rev-id <NEXT_NUMBER> -m "brief_description"
    ```
*   **Example:** `alembic revision --autogenerate --rev-id 0011 -m "add_qa_metrics_tables"`
*   **Crucial Step:** Always open the newly generated file to review the `upgrade()` and `downgrade()` functions. Alembic is smart, but manual review ensures constraints and schemas are correct.

## 2. Applying and Reverting Migrations
Once your migration script is generated and reviewed, you interact with the database using the upgrade and downgrade commands.
*   **Apply all pending migrations:** This brings your database up to date.
    ```bash
    alembic upgrade head
    ```
*   **Revert to a specific version:** Rolls back the database state (useful for local development).
    ```bash
    alembic downgrade 0010
    ```
*   **Undo only the very last migration:**
    ```bash
    alembic downgrade -1
    ```

## 3. Best Practices & Troubleshooting
*   **Never alter history:** If a migration has already been merged and applied to shared environments (e.g., UAT/PROD), do not modify its file. Always create a *new* migration to apply fixes or drop tables.
*   **Empty migrations:** If your generated file has empty `upgrade()`/`downgrade()` functions, double-check that your model is properly imported in `alembic/env.py`.
*   **The Mako template:** The `alembic/script.py.mako` file is the blueprint Alembic uses to generate new migration files. Do not delete it.