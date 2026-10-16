"""Immutable accepted envelopes and a writable-storage readiness probe."""

from alembic import op

revision = "0002_integrity"
down_revision = "84d9145085ad"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE readiness_probes (id integer PRIMARY KEY)")
    op.execute("""
        CREATE FUNCTION reject_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.organization_id, NEW.source_id, NEW.external_event_id, NEW.event_type,
                   NEW.shipment_external_id, NEW.occurred_at, NEW.received_at, NEW.raw_body,
                   NEW.payload, NEW.body_hash, NEW.correlation_id)
                IS DISTINCT FROM
               ROW(OLD.organization_id, OLD.source_id, OLD.external_event_id, OLD.event_type,
                   OLD.shipment_external_id, OLD.occurred_at, OLD.received_at, OLD.raw_body,
                   OLD.payload, OLD.body_hash, OLD.correlation_id) THEN
                RAISE EXCEPTION 'Accepted webhook envelope is immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER immutable_webhook_envelope BEFORE UPDATE ON webhook_events
        FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
    """)


def downgrade():
    op.execute("DROP TRIGGER immutable_webhook_envelope ON webhook_events")
    op.execute("DROP FUNCTION reject_event_mutation()")
    op.execute("DROP TABLE readiness_probes")
