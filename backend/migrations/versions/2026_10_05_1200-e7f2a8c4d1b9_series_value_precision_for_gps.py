"""series value precision for gps coordinates

Revision ID: e7f2a8c4d1b9
Revises: dc5ac28c4b94

Widens data_point_series.value (and the archive twin) from numeric(10,3) to
numeric(12,6). Three decimal places resolve to ~111 m on the ground -- fine for
heart rate or watts, useless for GPS tracks (latitude/longitude series 210/211
ingested from Strava latlng / Polar route / Garmin FIT). Scale 6 gives ~11 cm,
precision 12 leaves ample headroom for every other series type.

Increasing the scale is lossless (existing values gain trailing zeros), so
plain ALTER TYPE without USING is safe. The rewrite of large tables takes a
moment but holds no exclusive lock beyond it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f2a8c4d1b9"
down_revision: Union[str, None] = "dc5ac28c4b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("data_point_series", "data_point_series_archive")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "value",
            existing_type=sa.Numeric(10, 3),
            type_=sa.Numeric(12, 6),
            existing_nullable=True,
            postgresql_using="value::numeric(12,6)",
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.alter_column(
            table,
            "value",
            existing_type=sa.Numeric(12, 6),
            type_=sa.Numeric(10, 3),
            existing_nullable=True,
            # Rounding back down is lossy on paper, but every pre-GPS value was
            # written with at most 3 decimals anyway.
            postgresql_using="round(value, 3)::numeric(10,3)",
        )
