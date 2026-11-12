"""
Help Center tickets: owner side, staff side, attachments and the table rules.

    python manage.py test apps.helpdesk --settings=config.test_settings
"""
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from apps.audit.models import AuditLog
from apps.core.tests.test_flow import StoreClientMixin
from apps.helpdesk import live, services, views
from apps.helpdesk.models import Ticket, TicketMessage
from apps.identity.models import User
from apps.media.models import MediaAsset
from apps.media.tests import PNG

TICKETS = "/api/v1/support/tickets/"
STAFF = "/api/v1/platform/support/tickets/"
GOOD = {"subject": "Price problem", "message": "The price does not save when I press Save."}


class Base(StoreClientMixin, APITestCase):
    def setUp(self):
        cache.clear()
        self.token, self.store = self.sign_up("owner@example.com", "Tea Shop")
        self.owner = User.objects.get(email="owner@example.com")

    def as_owner(self):
        self.use(self.token, self.store["storeId"])

    def as_admin(self, email="root@example.com"):
        token, _ = self.sign_up(email, "Root store")
        User.objects.filter(email=email).update(is_owner=True)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def upload(self, url="/api/v1/support/attachments/", name="shot.png", content=PNG):
        return self.client.post(url, {"image": SimpleUploadedFile(name, content)}, format="multipart")

    def new_ticket(self, **extra):
        self.as_owner()
        return self.ok("post", TICKETS, {**GOOD, **extra}, expect=201)


class CreateTicketTests(Base):
    def test_creates_the_ticket_and_first_message_from_the_request_not_the_body(self):
        body = {**GOOD, "transcript": "Me: hi", "page": "/admin/help/x", "includeStoreDetails": True,
                "storeName": "Evil", "email": "evil@example.com", "user": "x", "status": "closed"}
        ticket = self.new_ticket(**body)

        row = Ticket.objects.get(pk=ticket["id"])
        self.assertEqual((str(row.store_id), row.user_id, row.status), (self.store["storeId"], self.owner.pk, "open"))
        [message] = row.messages.all()
        self.assertEqual((message.sender_type, message.sender_id), ("owner", self.owner.pk))
        self.assertIn(GOOD["message"], message.body)
        self.assertIn("Store: Tea Shop", message.body)
        self.assertIn("Email: owner@example.com", message.body)
        self.assertIn("Page I came from: /admin/help/x", message.body)
        self.assertIn("--- Chat with the assistant ---\nMe: hi", message.body)
        self.assertNotIn("Evil", message.body)
        self.assertNotIn("evil@example.com", message.body)

    def test_store_details_only_when_the_owner_agrees(self):
        ticket = self.new_ticket(page="/admin/help/x")
        body = Ticket.objects.get(pk=ticket["id"]).messages.get().body
        self.assertEqual(body, GOOD["message"])

    def test_validation(self):
        self.as_owner()
        for bad in (
            {**GOOD, "subject": ""}, {**GOOD, "message": "short"}, {**GOOD, "message": "x" * 2001},
            {**GOOD, "attachments": "nope"}, {**GOOD, "attachments": ["not-an-id"]},
            {**GOOD, "attachments": [str(uuid.uuid4()) for _ in range(4)]},
        ):
            cache.clear()  # every case counts against the 5/hour support_ticket throttle
            self.assertEqual(self.client.post(TICKETS, bad, format="json").status_code, 400, bad)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_five_new_tickets_an_hour(self):
        self.as_owner()
        for _ in range(5):
            self.ok("post", TICKETS, GOOD, expect=201)
        self.assertEqual(self.client.post(TICKETS, GOOD, format="json").status_code, 429)

    def test_an_api_key_cannot_open_a_ticket(self):
        self.as_owner()
        key = self.ok("post", "/api/v1/api-keys/", {"name": "Sync", "scopes": ["products.view"]}, expect=201)["key"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        self.assertEqual(client.post(TICKETS, GOOD, format="json").status_code, 403)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_nothing_is_added_to_the_audit_log(self):
        self.as_owner()
        before = AuditLog.objects.count()
        ticket = self.ok("post", TICKETS, GOOD, expect=201)
        self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "More detail here."}, expect=201)
        self.assertEqual(AuditLog.objects.count(), before)


class OwnerReadAndReplyTests(Base):
    def test_a_member_sees_only_their_own_tickets_in_this_store(self):
        mine = self.new_ticket()
        other = User.objects.create_user(
            email="colleague@example.com", password="Sturdy-pass-123", first_name="Co", last_name="Worker",
        )
        theirs = Ticket.objects.create(store_id=self.store["storeId"], user=other, subject="Not mine")
        TicketMessage.objects.create(ticket=theirs, sender_type="owner", sender=other, body="private")

        rows = self.ok("get", TICKETS)["results"]
        self.assertEqual([r["id"] for r in rows], [mine["id"]])
        self.assertEqual(rows[0]["messageCount"], 1)
        self.assertEqual(self.client.get(f"{TICKETS}{theirs.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"{TICKETS}{theirs.id}/messages/", {"message": "hello"}, format="json").status_code, 404)

    def test_another_store_cannot_open_it(self):
        ticket = self.new_ticket()
        token_b, store_b = self.sign_up("b@example.com", "Other shop")
        self.use(token_b, store_b["storeId"])
        self.assertEqual(self.client.get(f"{TICKETS}{ticket['id']}/").status_code, 404)
        self.assertEqual(self.ok("get", TICKETS)["count"], 0)

    def test_status_filter(self):
        ticket = self.new_ticket()
        Ticket.objects.filter(pk=ticket["id"]).update(status="closed")
        self.assertEqual(self.ok("get", f"{TICKETS}?status=open")["count"], 0)
        self.assertEqual(self.ok("get", f"{TICKETS}?status=closed")["count"], 1)

    def test_a_reply_is_added_and_reopens_the_ticket(self):
        ticket = self.new_ticket()
        Ticket.objects.filter(pk=ticket["id"]).update(status="closed")
        detail = self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "It happens again."}, expect=201)
        self.assertEqual(detail["status"], "open")
        self.assertEqual([m["senderType"] for m in detail["messages"]], ["owner", "owner"])
        self.assertEqual(detail["messages"][1]["body"], "It happens again.")
        self.assertEqual(self.client.post(f"{TICKETS}{ticket['id']}/messages/", {"message": ""}, format="json").status_code, 400)

    def test_replies_are_limited_to_sixty_an_hour(self):
        ticket = self.new_ticket()
        for _ in range(60):
            self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "again"}, expect=201)
        self.assertEqual(self.client.post(f"{TICKETS}{ticket['id']}/messages/", {"message": "again"}, format="json").status_code, 429)


class EmailTests(Base):
    @override_settings(SUPPORT_EMAIL="")
    def test_no_email_when_support_email_is_empty(self):
        with mock.patch.object(services, "send_store_email") as send:
            self.new_ticket()
        send.assert_not_called()
        self.assertEqual(Ticket.objects.count(), 1)

    @override_settings(SUPPORT_EMAIL="help@tss.test")
    def test_one_email_for_a_new_ticket_and_one_for_a_reply(self):
        with mock.patch.object(services, "send_store_email") as send:
            ticket = self.new_ticket()
            self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "More detail here."}, expect=201)
        self.assertEqual(send.call_count, 2)
        subject, body, recipients = send.call_args_list[0].args
        self.assertEqual(recipients, ["help@tss.test"])
        self.assertEqual(subject, "[Support] Tea Shop: Price problem")
        self.assertIn(f"Ticket ID: {ticket['id']}", body)
        self.assertEqual(send.call_args_list[0].kwargs, {"reply_to": "owner@example.com"})

    @override_settings(SUPPORT_EMAIL="help@tss.test")
    def test_a_failed_email_does_not_fail_the_ticket(self):
        with mock.patch.object(services, "send_store_email", side_effect=RuntimeError("smtp down")):
            self.new_ticket()
        self.assertEqual(Ticket.objects.count(), 1)


class AttachmentTests(Base):
    def test_upload_returns_an_id_and_url_and_keeps_the_media_rules(self):
        self.as_owner()
        r = self.upload()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(MediaAsset.objects.filter(pk=r.json()["id"], store_id=self.store["storeId"]).exists())
        self.assertIn("/s/_img/", r.json()["url"])
        self.assertEqual(self.upload(content=b"plain text").status_code, 400)
        self.assertEqual(self.upload(content=PNG + b"\x00" * (1024 * 1024)).status_code, 400)

    def test_the_endpoint_needs_store_membership_not_the_themes_permission(self):
        names = [c.__name__ for c in views.AttachmentView.permission_classes]
        self.assertEqual(names, ["IsAuthenticated", "NoApiKeys", "IsStoreMember"])

    def test_up_to_three_images_on_a_message(self):
        self.as_owner()
        ids = [self.upload().json()["id"] for _ in range(3)]
        ticket = self.ok("post", TICKETS, {**GOOD, "attachments": ids}, expect=201)
        self.assertEqual(len(ticket["messages"][0]["attachments"]), 3)
        self.assertTrue(all("/s/_img/" in url for url in ticket["messages"][0]["attachments"]))
        extra = self.upload().json()["id"]
        r = self.client.post(f"{TICKETS}{ticket['id']}/messages/", {"message": "more", "attachments": ids + [extra]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_only_my_own_pictures_of_this_store_can_be_attached(self):
        token_b, store_b = self.sign_up("b@example.com", "Other shop")
        theirs = self.upload().json()["id"]  # uploaded by B into B's store
        self.as_owner()
        self.assertEqual(self.client.post(TICKETS, {**GOOD, "attachments": [theirs]}, format="json").status_code, 400)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_uploads_are_limited(self):
        self.as_owner()
        with mock.patch.object(views.AttachThrottle, "get_rate", return_value="2/hour"):
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload().status_code, 429)


class StaffTests(Base):
    def test_only_platform_admins_get_in(self):
        ticket = self.new_ticket()
        for method, url in (("get", STAFF), ("get", f"{STAFF}{ticket['id']}/"), ("patch", f"{STAFF}{ticket['id']}/"),
                            ("post", f"{STAFF}{ticket['id']}/messages/")):
            self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 403, url)

    def test_list_filter_search_and_open(self):
        ticket = self.new_ticket()
        self.as_admin()
        listed = self.ok("get", STAFF)
        self.assertEqual(listed["count"], 1)
        row = listed["results"][0]
        self.assertEqual((row["store"]["name"], row["user"]["email"], row["lastSender"]), ("Tea Shop", "owner@example.com", "owner"))
        self.assertEqual(self.ok("get", f"{STAFF}?status=closed")["count"], 0)
        self.assertEqual(self.ok("get", f"{STAFF}?q=price")["count"], 1)
        self.assertEqual(self.ok("get", f"{STAFF}?q=tea shop")["count"], 1)
        self.assertEqual(self.ok("get", f"{STAFF}?q=nobody")["count"], 0)
        self.assertEqual(self.ok("get", f"{STAFF}?store={self.store['storeId']}")["count"], 1)
        detail = self.ok("get", f"{STAFF}{ticket['id']}/")
        self.assertEqual(detail["messages"][0]["senderName"], "Test User")

    def test_reply_sets_waiting_on_customer_is_audited_and_the_owner_sees_a_support_team_message(self):
        ticket = self.new_ticket()
        self.as_admin()
        detail = self.ok("post", f"{STAFF}{ticket['id']}/messages/", {"message": "We fixed it, please check."}, expect=201)
        self.assertEqual(detail["status"], "waiting_on_customer")
        self.assertEqual(detail["messages"][1]["senderType"], "staff")
        entry = AuditLog.objects.get(action="platform.support_reply")
        self.assertEqual(entry.object_id, ticket["id"])
        self.assertNotIn("We fixed it", str(entry.metadata))

        self.as_owner()
        seen = self.ok("get", f"{TICKETS}{ticket['id']}/")
        self.assertEqual(seen["status"], "waiting_on_customer")
        self.assertEqual(seen["messages"][1]["senderName"], "Support team")
        reopened = self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "Still broken."}, expect=201)
        self.assertEqual(reopened["status"], "open")

    def test_reply_can_choose_the_status(self):
        ticket = self.new_ticket()
        self.as_admin()
        detail = self.ok("post", f"{STAFF}{ticket['id']}/messages/", {"message": "Done.", "status": "closed"}, expect=201)
        self.assertEqual(detail["status"], "closed")
        bad = self.client.post(f"{STAFF}{ticket['id']}/messages/", {"message": "x", "status": "weird"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_status_change_is_audited_and_validated(self):
        ticket = self.new_ticket()
        self.as_admin()
        self.assertEqual(self.ok("patch", f"{STAFF}{ticket['id']}/", {"status": "closed"})["status"], "closed")
        self.assertEqual(AuditLog.objects.filter(action="platform.support_status").count(), 1)
        self.ok("patch", f"{STAFF}{ticket['id']}/", {"status": "closed"})  # no change, no second entry
        self.assertEqual(AuditLog.objects.filter(action="platform.support_status").count(), 1)
        self.assertEqual(self.client.patch(f"{STAFF}{ticket['id']}/", {"status": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(f"{STAFF}{ticket['id']}/", {}, format="json").status_code, 400)

    def test_staff_attach_a_picture_into_the_ticket_store(self):
        ticket = self.new_ticket()
        self.as_admin()
        shot = self.upload(url=f"{STAFF}{ticket['id']}/attachments/")
        self.assertEqual(shot.status_code, 201, shot.content)
        self.assertEqual(str(MediaAsset.objects.get(pk=shot.json()["id"]).store_id), self.store["storeId"])
        detail = self.ok("post", f"{STAFF}{ticket['id']}/messages/",
                         {"message": "See this.", "attachments": [shot.json()["id"]]}, expect=201)
        self.assertEqual(len(detail["messages"][1]["attachments"]), 1)


class LiveUpdateTests(Base):
    def test_a_staff_reply_is_published_for_that_ticket_after_commit(self):
        ticket = self.new_ticket()
        self.as_admin()
        last = live.TICKET_EVENTS.last_seq
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"{STAFF}{ticket['id']}/messages/", {"message": "On it."}, expect=201)
        events, _ = live.TICKET_EVENTS.wait(ticket["id"], last, 0.1)
        self.assertEqual(events[-1][1], {"type": "ticket", "ticketId": ticket["id"]})

    def test_an_owner_reply_is_published_too(self):
        ticket = self.new_ticket()
        last = live.TICKET_EVENTS.last_seq
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"{TICKETS}{ticket['id']}/messages/", {"message": "Still broken."}, expect=201)
        events, _ = live.TICKET_EVENTS.wait(ticket["id"], last, 0.1)
        self.assertTrue(events)

    def test_the_stream_is_open_to_the_owner_of_the_ticket_and_to_staff_only(self):
        ticket = self.new_ticket()
        with mock.patch.object(live, "STREAM_SECONDS", 0.2):
            res = self.client.get(f"{TICKETS}{ticket['id']}/events/")
            self.assertEqual((res.status_code, res["Content-Type"]), (200, "text/event-stream"))
            self.assertIn(b"event: hello", b"".join(res.streaming_content))
        other, _ = self.sign_up("other@example.com", "Other shop")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other}")
        self.assertEqual(self.client.get(f"{STAFF}{ticket['id']}/events/").status_code, 403)

    def test_staff_stream_opens_for_an_agent(self):
        ticket = self.new_ticket()
        self.as_admin()
        with mock.patch.object(live, "STREAM_SECONDS", 0.2):
            res = self.client.get(f"{STAFF}{ticket['id']}/events/")
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"event: hello", b"".join(res.streaming_content))


class TableRuleTests(Base):
    def test_the_database_refuses_bad_rows(self):
        ticket = Ticket.objects.create(store_id=self.store["storeId"], user=self.owner, subject="x")
        for fields in (
            {"sender_type": "owner", "body": ""},
            {"sender_type": "owner", "body": "x" * 8001},
            {"sender_type": "robot", "body": "hello"},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                TicketMessage.objects.create(ticket=ticket, **fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(store_id=self.store["storeId"], user=self.owner, subject="x", status="weird")

    def test_a_ticket_whose_user_was_deleted_still_shows_to_staff(self):
        ticket = Ticket.objects.create(store_id=self.store["storeId"], user=None, subject="Old one")
        TicketMessage.objects.create(ticket=ticket, sender_type="owner", sender=None, body="hello")
        self.as_admin()
        detail = self.ok("get", f"{STAFF}{ticket.id}/")
        self.assertIsNone(detail["user"])
        self.assertEqual(detail["messages"][0]["senderName"], "Former user")
        self.assertEqual(self.ok("get", STAFF)["count"], 1)
