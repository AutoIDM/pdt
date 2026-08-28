"""create send_once_notifications

Revision ID: 3bd323b87beb
Revises:
Create Date: 2024-06-04 19:43:19.278574

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TEXT

# revision identifiers, used by Alembic.
revision: str = "3bd323b87beb"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS autoidm_state")
    op.create_table(
        "send_once_notifications",
        sa.Column("hash", TEXT, primary_key=True),
        sa.Column("title", TEXT),
        sa.Column("body", TEXT),
        sa.Column(
            "sent", sa.Boolean, server_default=sa.false()
        ),  # `server_default`, not just `default`, is important here: https://roman.pt/posts/sqlalchemy-and-alembic/#column-default-vs-server_default-and-onupdate-vs-server_onupdate
        schema="autoidm_state",
    )


def downgrade() -> None:
    op.drop_table("send_once_notifications", schema="autoidm_state")
    op.execute("DROP SCHEMA IF EXISTS autoidm_state CASCADE")
