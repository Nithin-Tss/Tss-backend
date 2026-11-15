from django.db import migrations


class Migration(migrations.Migration):
    """
    inventory_history stays append-only, with one exception: the "Delete all products" action turns on
    inventory.allow_history_delete for its own transaction (SET LOCAL), so the stock history of the products it
    removes can go too. Any other DELETE, and every UPDATE, is still refused.
    """

    dependencies = [
        ("inventory", "0006_inventory_integrity_checks"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE OR REPLACE FUNCTION inventory.prevent_inventory_history_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    IF TG_OP = 'UPDATE' THEN
                        IF OLD.user_id IS NOT NULL
                           AND NEW.user_id IS NULL
                           AND (
                               to_jsonb(OLD) - 'user_id'
                           ) IS NOT DISTINCT FROM (
                               to_jsonb(NEW) - 'user_id'
                           )
                        THEN
                            RETURN NEW;
                        END IF;
                    END IF;

                    IF TG_OP = 'DELETE'
                       AND current_setting('inventory.allow_history_delete', true) = 'on'
                    THEN
                        RETURN OLD;
                    END IF;

                    RAISE EXCEPTION
                        'inventory_history is append-only: % is not allowed',
                        TG_OP
                        USING ERRCODE = '55000';
                END;
                $function$;
            """,
            reverse_sql="""
                CREATE OR REPLACE FUNCTION inventory.prevent_inventory_history_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    IF TG_OP = 'UPDATE' THEN
                        IF OLD.user_id IS NOT NULL
                           AND NEW.user_id IS NULL
                           AND (
                               to_jsonb(OLD) - 'user_id'
                           ) IS NOT DISTINCT FROM (
                               to_jsonb(NEW) - 'user_id'
                           )
                        THEN
                            RETURN NEW;
                        END IF;
                    END IF;

                    RAISE EXCEPTION
                        'inventory_history is append-only: % is not allowed',
                        TG_OP
                        USING ERRCODE = '55000';
                END;
                $function$;
            """,
        ),
    ]
