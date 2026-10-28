from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_remove_payment_order_id_payment_order"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                """
                CREATE FUNCTION payments.prevent_payment_transaction_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RAISE EXCEPTION
                        'payment_transactions is append-only: % is not allowed',
                        TG_OP
                        USING ERRCODE = '55000';
                END;
                $function$
                """,
                """
                CREATE TRIGGER payment_transactions_no_update_delete
                BEFORE UPDATE OR DELETE
                ON payments.payment_transactions
                FOR EACH ROW
                EXECUTE FUNCTION
                    payments.prevent_payment_transaction_mutation()
                """,
            ],
            reverse_sql=[
                """
                DROP TRIGGER IF EXISTS payment_transactions_no_update_delete
                ON payments.payment_transactions
                """,
                """
                DROP FUNCTION IF EXISTS
                    payments.prevent_payment_transaction_mutation()
                """,
            ],
        ),
    ]