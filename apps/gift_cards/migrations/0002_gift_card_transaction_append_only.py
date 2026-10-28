from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("gift_cards", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                """
                CREATE FUNCTION
                    gift_cards.prevent_gift_card_transaction_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RAISE EXCEPTION
                        'gift_card_transactions is append-only: % is not allowed',
                        TG_OP
                        USING ERRCODE = '55000';
                END;
                $function$
                """,
                """
                CREATE TRIGGER gift_card_transactions_no_update_delete
                BEFORE UPDATE OR DELETE
                ON gift_cards.gift_card_transactions
                FOR EACH ROW
                EXECUTE FUNCTION
                    gift_cards.prevent_gift_card_transaction_mutation()
                """,
            ],
            reverse_sql=[
                """
                DROP TRIGGER IF EXISTS gift_card_transactions_no_update_delete
                ON gift_cards.gift_card_transactions
                """,
                """
                DROP FUNCTION IF EXISTS
                    gift_cards.prevent_gift_card_transaction_mutation()
                """,
            ],
        ),
    ]