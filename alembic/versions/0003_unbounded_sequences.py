"""Preserve positive courier sequence integers without a machine-integer overflow."""

import sqlalchemy as sa

from alembic import op

revision = "0003_sequences"
down_revision = "0002_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("shipments", "last_sequence", type_=sa.Numeric(), existing_type=sa.Integer())
    op.alter_column("business_effects", "sequence", type_=sa.Numeric(), existing_type=sa.Integer())


def downgrade():
    # Downgrade deliberately fails if recorded sequences no longer fit the old integer columns.
    op.alter_column(
        "business_effects",
        "sequence",
        type_=sa.Integer(),
        existing_type=sa.Numeric(),
        postgresql_using="sequence::integer",
    )
    op.alter_column(
        "shipments",
        "last_sequence",
        type_=sa.Integer(),
        existing_type=sa.Numeric(),
        postgresql_using="last_sequence::integer",
    )
