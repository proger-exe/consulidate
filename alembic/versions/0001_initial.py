"""Initial Consulidate schema.

Revision ID: 0001_initial
Revises:
"""

from alembic import op
from consulidate import models  # noqa: F401
from consulidate.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ponytail: metadata baseline is enough for the MVP; use explicit revisions after schema v1.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
