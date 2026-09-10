"""Initial baseline

P0 contains no pipeline tables yet; this baseline revision records the
empty initial schema so that Alembic migrations are operable from the
start. Later phases add tables via their own revisions.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
