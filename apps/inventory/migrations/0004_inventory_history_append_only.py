from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_inventory_history_and_continue_selling"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION inventory.prevent_inventory_history_mutation()
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

                CREATE TRIGGER inventory_history_no_update_delete
                BEFORE UPDATE OR DELETE
                ON inventory.inventory_history
                FOR EACH ROW
                EXECUTE FUNCTION inventory.prevent_inventory_history_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER IF EXISTS inventory_history_no_update_delete
                ON inventory.inventory_history;

                DROP FUNCTION IF EXISTS
                    inventory.prevent_inventory_history_mutation();
            """,
        ),
    ]