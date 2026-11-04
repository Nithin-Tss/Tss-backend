"""
Unit tests for the store services (services.py) and the team rules (access.py),
called directly rather than through the API.

    python manage.py test apps.tenancy.test_units --settings=config.test_settings
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core import rbac
from apps.identity.models import User
from apps.tenancy import access, services
from apps.tenancy.models import (
    AccessLog, Invitation, Store, StoreMembership, StoreRole,
)


def make_user(email, **extra):
    return User.objects.create_user(email, "Sturdy-pass-123", first_name="T", last_name="U", **extra)


def role_of(store, key):
    return store.roles.get(key=key)


class StoreServicesTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner@example.com")

    def test_create_store_makes_the_creator_its_active_owner(self):
        store = services.create_store(self.owner, "Linen & Co")
        self.assertEqual(store.status, services.ACTIVE)
        self.assertEqual(store.created_by, "owner@example.com")
        m = StoreMembership.objects.get(store=store, user=self.owner)
        self.assertEqual((m.role, m.status), (services.OWNER, services.ACTIVE))

    def test_create_store_seeds_the_built_in_roles(self):
        store = services.create_store(self.owner, "Shop")
        self.assertTrue(store.roles_seeded)
        self.assertEqual(set(store.roles.values_list("key", flat=True)), set(rbac.BUILT_IN_ROLES))

    def test_slug_comes_from_the_name_and_is_made_unique(self):
        first = services.create_store(self.owner, "Linen Shop")
        second = services.create_store(self.owner, "Linen Shop")
        third = services.create_store(self.owner, "Linen Shop")
        self.assertEqual(first.store_slug, "linen-shop")
        self.assertEqual(second.store_slug, "linen-shop-2")
        self.assertEqual(third.store_slug, "linen-shop-3")

    def test_a_name_with_no_usable_characters_falls_back_to_store(self):
        self.assertEqual(services.create_store(self.owner, "!!!").store_slug, "store")

    def test_a_long_name_gives_a_slug_within_the_limit(self):
        store = services.create_store(self.owner, "x" * 200)
        self.assertLessEqual(len(store.store_slug), 90)

    def test_an_explicit_slug_is_used_as_given(self):
        self.assertEqual(services.create_store(self.owner, "Shop", "my-slug").store_slug, "my-slug")

    def test_close_sets_members_aside_and_reopen_restores_them(self):
        store = services.create_store(self.owner, "Shop")
        staff = make_user("staff@example.com")
        StoreMembership.objects.create(user=staff, store=store, role=access.STAFF, status=services.ACTIVE)
        removed = make_user("gone@example.com")
        StoreMembership.objects.create(user=removed, store=store, role=access.STAFF, status=services.REMOVED)

        services.close_store(store)
        store.refresh_from_db()
        self.assertEqual(store.status, services.CLOSED)
        statuses = dict(store.memberships.values_list("user__email", "status"))
        self.assertEqual(statuses["owner@example.com"], services.STORE_CLOSED)
        self.assertEqual(statuses["staff@example.com"], services.STORE_CLOSED)
        self.assertEqual(statuses["gone@example.com"], services.REMOVED)

        services.reopen_store(store)
        store.refresh_from_db()
        self.assertEqual(store.status, services.ACTIVE)
        statuses = dict(store.memberships.values_list("user__email", "status"))
        self.assertEqual(statuses["owner@example.com"], services.ACTIVE)
        self.assertEqual(statuses["staff@example.com"], services.ACTIVE)
        self.assertEqual(statuses["gone@example.com"], services.REMOVED)

    def test_platform_membership_is_an_unsaved_owner_membership(self):
        store = services.create_store(self.owner, "Shop")
        admin = make_user("root@example.com", is_owner=True)
        m = services.platform_membership(admin, store)
        self.assertEqual((m.role, m.status), (services.OWNER, services.ACTIVE))
        self.assertFalse(StoreMembership.objects.filter(user=admin).exists())

    def test_stores_for_user_lists_only_active_memberships(self):
        mine = services.create_store(self.owner, "Mine")
        closed = services.create_store(self.owner, "Closed")
        services.close_store(closed)
        other_owner = make_user("other@example.com")
        services.create_store(other_owner, "Theirs")

        rows = services.stores_for_user(self.owner)
        self.assertEqual([r["storeSlug"] for r in rows], [mine.store_slug])
        row = rows[0]
        self.assertEqual(row["role"], services.OWNER)
        self.assertEqual(row["roles"], [])
        self.assertIn(rbac.STORE_MANAGE, row["permissions"])
        self.assertEqual(row["permissions"], sorted(row["permissions"]))

    def test_stores_for_user_hands_a_platform_admin_every_open_store(self):
        shop = services.create_store(self.owner, "Shop")
        closed = services.create_store(self.owner, "Closed")
        services.close_store(closed)
        admin = make_user("root@example.com", is_owner=True)
        rows = services.stores_for_user(admin)
        self.assertEqual([r["storeSlug"] for r in rows], [shop.store_slug])
        self.assertEqual(rows[0]["role"], services.OWNER)

    def test_stores_for_user_names_a_staff_members_roles_and_permissions(self):
        store = services.create_store(self.owner, "Shop")
        staff = make_user("staff@example.com")
        access.add_member(
            store, StoreMembership.objects.get(user=self.owner, store=store),
            staff.email, [role_of(store, "online_store_editor").role_id],
        )
        row = services.stores_for_user(staff)[0]
        self.assertEqual(row["roles"], ["Online store editor"])
        self.assertEqual(row["permissions"], ["themes.manage", "themes.view"])


class TeamTestBase(TestCase):
    def setUp(self):
        self.owner = make_user("owner@example.com")
        self.store = services.create_store(self.owner, "Shop")
        self.owner_m = StoreMembership.objects.get(user=self.owner, store=self.store)

    def role(self, key):
        return role_of(self.store, key)

    def staff(self, email, *keys):
        user = make_user(email)
        m = StoreMembership.objects.create(user=user, store=self.store, role=access.STAFF, status=services.ACTIVE)
        access._set_roles(m, [self.role(k) for k in keys])
        return m

    def actions(self):
        return list(self.store.access_log.order_by("created_at").values_list("action", flat=True))


class PermissionTests(TeamTestBase):
    def test_system_roles_always_take_permissions_from_code(self):
        admin = self.role("administrator")
        admin.permissions = []  # a stale or tampered row doesn't matter
        admin.save()
        self.assertEqual(access.role_permissions(admin), set(rbac.ALL_PERMISSIONS))

    def test_custom_roles_take_permissions_from_the_row_with_implied_view(self):
        custom = StoreRole.objects.create(
            store=self.store, kind=rbac.CUSTOM, name="Packer", permissions=["products.manage", "bogus.key"],
        )
        perms = access.role_permissions(custom)
        self.assertIn("products.manage", perms)
        self.assertIn("products.view", perms)
        self.assertNotIn("bogus.key", perms)

    def test_owner_holds_every_permission_and_store_manage(self):
        perms = access.membership_permissions(self.owner_m)
        self.assertEqual(perms, set(rbac.ALL_PERMISSIONS) | {rbac.STORE_MANAGE})

    def test_staff_permissions_are_the_union_of_their_roles(self):
        m = self.staff("s@example.com", "online_store_editor", "store_user_administrator")
        self.assertEqual(
            access.membership_permissions(m),
            {"themes.view", "themes.manage", "members.view", "members.manage", "roles.manage"},
        )

    def test_staff_without_roles_hold_nothing(self):
        self.assertEqual(access.membership_permissions(self.staff("s@example.com")), set())

    def test_permissions_are_cached_on_the_membership(self):
        m = self.staff("s@example.com", "online_store_editor")
        first = access.membership_permissions(m)
        with self.assertNumQueries(0):
            self.assertIs(access.membership_permissions(m), first)

    def test_member_roles_is_empty_for_owners(self):
        self.assertEqual(access.member_roles(self.owner_m), [])
        m = self.staff("s@example.com", "online_store_editor")
        self.assertEqual([r.key for r in access.member_roles(m)], ["online_store_editor"])

    def test_ensure_built_in_roles_is_idempotent_and_keeps_deleted_roles_deleted(self):
        before = self.store.roles.count()
        access.ensure_built_in_roles(self.store)
        self.assertEqual(self.store.roles.count(), before)

        self.role("online_store_editor").delete()
        self.store.refresh_from_db()
        access.ensure_built_in_roles(self.store)
        self.assertFalse(self.store.roles.filter(key="online_store_editor").exists())

    def test_built_in_role_name_clash_gets_a_suffix(self):
        store = Store.objects.create(store_name="Clash", store_slug="clash", status="active")
        StoreRole.objects.create(store=store, kind=rbac.CUSTOM, name="Administrator", permissions=[])
        access.ensure_built_in_roles(store)
        self.assertTrue(store.roles.filter(key="administrator", name="Administrator (built-in)").exists())


class RoleTests(TeamTestBase):
    def test_create_role_cleans_input_and_logs(self):
        role = access.create_role(
            self.store, self.owner_m, "  Night   Packer ", "  pack boxes ", ["products.manage", "products.manage"],
        )
        self.assertEqual(role.name, "Night Packer")
        self.assertEqual(role.description, "pack boxes")
        self.assertEqual(role.kind, rbac.CUSTOM)
        self.assertEqual(role.permissions, ["products.manage", "products.view"])
        self.assertIn("role.created", self.actions())

    def test_blank_description_is_stored_as_null(self):
        role = access.create_role(self.store, self.owner_m, "R", "   ", [])
        self.assertIsNone(role.description)

    def test_create_role_rejects_bad_input(self):
        access.create_role(self.store, self.owner_m, "Taken", "", [])
        for name, perms in [
            ("", []), ("   ", []), ("x" * 101, []),
            ("taken", []),                       # duplicate, case-insensitive
            ("New", "products.view"),            # not a list
            ("New", ["no.such.permission"]),
        ]:
            with self.subTest(name=name[:10], perms=perms):
                with self.assertRaises(ValidationError):
                    access.create_role(self.store, self.owner_m, name, "", perms)

    def test_no_escalation_when_creating_a_role(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        with self.assertRaises(PermissionDenied):
            access.create_role(self.store, admin, "Sneaky", "", ["products.manage"])
        access.create_role(self.store, admin, "Fine", "", ["members.view"])

    def test_update_role_changes_fields_and_logs_the_difference(self):
        role = access.create_role(self.store, self.owner_m, "Packer", "", ["products.view"])
        access.update_role(role, self.owner_m, name="Senior packer", permissions=["orders.view"])
        role.refresh_from_db()
        self.assertEqual((role.name, role.permissions), ("Senior packer", ["orders.view"]))
        entry = AccessLog.objects.filter(store=self.store, action="role.updated").get()
        self.assertEqual(entry.detail["added"], ["orders.view"])
        self.assertEqual(entry.detail["removed"], ["products.view"])
        self.assertEqual(entry.detail["previousName"], "Packer")

    def test_update_role_can_keep_its_own_name(self):
        role = access.create_role(self.store, self.owner_m, "Packer", "", [])
        access.update_role(role, self.owner_m, name="packer", description="x")
        role.refresh_from_db()
        self.assertEqual((role.name, role.description), ("packer", "x"))

    def test_system_roles_cannot_be_updated_or_deleted(self):
        admin_role = self.role("administrator")
        with self.assertRaises(PermissionDenied):
            access.update_role(admin_role, self.owner_m, name="Boss")
        with self.assertRaises(PermissionDenied):
            access.delete_role(admin_role, self.owner_m)

    def test_cannot_edit_a_role_that_grants_more_than_you_hold(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        role = access.create_role(self.store, self.owner_m, "Packer", "", ["products.manage"])
        with self.assertRaises(PermissionDenied):
            access.update_role(role, admin, name="Renamed")
        with self.assertRaises(PermissionDenied):
            access.delete_role(role, admin)

    def test_delete_role_removes_it_and_logs(self):
        role = access.create_role(self.store, self.owner_m, "Packer", "", [])
        access.delete_role(role, self.owner_m)
        self.assertFalse(StoreRole.objects.filter(pk=role.pk).exists())
        self.assertIn("role.deleted", self.actions())

    def test_duplicate_role_numbers_the_copies(self):
        source = self.role("online_store_editor")
        first = access.duplicate_role(source, self.owner_m)
        second = access.duplicate_role(source, self.owner_m)
        self.assertEqual(first.name, "Online store editor (copy)")
        self.assertEqual(second.name, "Online store editor (copy 2)")
        self.assertEqual(first.kind, rbac.CUSTOM)
        self.assertEqual(first.permissions, ["themes.manage", "themes.view"])

    def test_duplicating_a_system_role_still_needs_its_permissions(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        with self.assertRaises(PermissionDenied):
            access.duplicate_role(self.role("administrator"), admin)
        access.duplicate_role(self.role("store_user_administrator"), admin)


class MemberTests(TeamTestBase):
    def editor_ids(self):
        return [self.role("online_store_editor").role_id]

    def test_add_existing_account_creates_a_staff_membership(self):
        user = make_user("new@example.com")
        m = access.add_member(self.store, self.owner_m, "NEW@example.com", self.editor_ids())
        self.assertEqual((m.user, m.role, m.status), (user, access.STAFF, services.ACTIVE))
        self.assertEqual([r.key for r in access.member_roles(m)], ["online_store_editor"])
        self.assertIn("member.added", self.actions())

    def test_add_unknown_email_saves_and_emails_an_invitation(self):
        with mock.patch.object(access, "send_email_async") as send:
            inv = access.add_member(self.store, self.owner_m, "Fresh@Example.com", self.editor_ids())
        self.assertIsInstance(inv, Invitation)
        self.assertEqual((inv.email, inv.status, inv.invited_by), ("fresh@example.com", "pending", self.owner))
        self.assertEqual(send.call_args.args[2], ["fresh@example.com"])
        self.assertIn("member.invited", self.actions())

    def test_inviting_twice_is_refused(self):
        access.add_member(self.store, self.owner_m, "fresh@example.com", self.editor_ids())
        with self.assertRaises(ValidationError):
            access.add_member(self.store, self.owner_m, "FRESH@example.com", self.editor_ids())

    def test_invitation_email_failure_does_not_roll_back_the_invitation(self):
        with mock.patch.object(access, "send_email_async", side_effect=RuntimeError("smtp down")):
            access.add_member(self.store, self.owner_m, "fresh@example.com", self.editor_ids())
        self.assertTrue(Invitation.objects.filter(email="fresh@example.com").exists())

    def test_add_member_validation(self):
        for email in ("", "   ", "not-an-email", None):
            with self.subTest(email=email):
                with self.assertRaises(ValidationError):
                    access.add_member(self.store, self.owner_m, email, self.editor_ids())
        make_user("new@example.com")
        with self.assertRaises(ValidationError):
            access.add_member(self.store, self.owner_m, "new@example.com", ["not-a-uuid"])
        with self.assertRaises(ValidationError):  # a role from another store
            other = services.create_store(make_user("o2@example.com"), "Other")
            access.add_member(self.store, self.owner_m, "new@example.com", [other.roles.first().role_id])

    def test_adding_someone_who_already_has_access_is_refused(self):
        make_user("new@example.com")
        access.add_member(self.store, self.owner_m, "new@example.com", self.editor_ids())
        with self.assertRaises(ValidationError):
            access.add_member(self.store, self.owner_m, "new@example.com", self.editor_ids())

    def test_a_removed_member_can_be_added_back(self):
        make_user("new@example.com")
        m = access.add_member(self.store, self.owner_m, "new@example.com", self.editor_ids())
        access.remove_member(m, self.owner_m)
        again = access.add_member(self.store, self.owner_m, "new@example.com", [])
        self.assertEqual(again.pk, m.pk)
        self.assertEqual(again.status, services.ACTIVE)
        self.assertEqual(access.member_roles(again), [])

    def test_only_an_owner_can_make_an_owner(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        make_user("new@example.com")
        with self.assertRaises(PermissionDenied):
            access.add_member(self.store, admin, "new@example.com", [], make_owner=True)
        m = access.add_member(self.store, self.owner_m, "new@example.com", [], make_owner=True)
        self.assertEqual(m.role, services.OWNER)
        self.assertEqual(access.member_roles(m), [])

    def test_no_escalation_when_adding_a_member(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        make_user("new@example.com")
        with self.assertRaises(PermissionDenied):
            access.add_member(self.store, admin, "new@example.com", [self.role("administrator").role_id])
        self.assertFalse(StoreMembership.objects.filter(user__email="new@example.com").exists())

    def test_update_member_replaces_roles(self):
        m = self.staff("s@example.com", "online_store_editor")
        access.update_member(m, self.owner_m, role_ids=[self.role("store_user_administrator").role_id])
        self.assertEqual([r.key for r in access.member_roles(m)], ["store_user_administrator"])
        self.assertIn("member.updated", self.actions())

    def test_update_member_with_no_change_logs_nothing(self):
        m = self.staff("s@example.com")
        access.update_member(m, self.owner_m)
        self.assertNotIn("member.updated", self.actions())

    def test_owners_do_not_take_roles(self):
        with self.assertRaises(ValidationError):
            access.update_member(self.owner_m, self.owner_m, role_ids=self.editor_ids())

    def test_promoting_and_demoting_an_owner(self):
        m = self.staff("s@example.com", "online_store_editor")
        access.update_member(m, self.owner_m, make_owner=True)
        m.refresh_from_db()
        self.assertEqual(m.role, services.OWNER)
        self.assertEqual(access.member_roles(m), [])  # roles are dropped on promotion
        access.update_member(m, self.owner_m, make_owner=False)
        m.refresh_from_db()
        self.assertEqual(m.role, access.STAFF)

    def test_the_last_owner_cannot_be_demoted_or_removed(self):
        with self.assertRaises(ValidationError):
            access.update_member(self.owner_m, self.owner_m, make_owner=False)
        with self.assertRaises(ValidationError):
            access.remove_member(self.owner_m, self.owner_m)
        self.owner_m.refresh_from_db()
        self.assertEqual((self.owner_m.role, self.owner_m.status), (services.OWNER, services.ACTIVE))

    def test_an_owner_can_leave_once_another_owner_exists(self):
        second = access.add_member(
            self.store, self.owner_m, make_user("o2@example.com").email, [], make_owner=True,
        )
        access.remove_member(self.owner_m, second)
        self.owner_m.refresh_from_db()
        self.assertEqual(self.owner_m.status, services.REMOVED)

    def test_non_owner_cannot_touch_owners(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        with self.assertRaises(PermissionDenied):
            access.update_member(self.owner_m, admin, role_ids=[])
        with self.assertRaises(PermissionDenied):
            access.remove_member(self.owner_m, admin)
        with self.assertRaises(PermissionDenied):
            access.update_member(self.staff("s@example.com"), admin, make_owner=True)

    def test_cannot_remove_or_reassign_someone_holding_more_than_you(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        boss = self.staff("boss@example.com", "administrator")
        with self.assertRaises(PermissionDenied):
            access.remove_member(boss, admin)
        with self.assertRaises(PermissionDenied):
            access.update_member(boss, admin, role_ids=[])

    def test_remove_member_marks_removed_and_logs(self):
        m = self.staff("s@example.com", "online_store_editor")
        access.remove_member(m, self.owner_m)
        m.refresh_from_db()
        self.assertEqual(m.status, services.REMOVED)
        self.assertIn("member.removed", self.actions())


class InvitationTests(TeamTestBase):
    def invite(self, email="fresh@example.com", *keys, make_owner=False, caller=None):
        with mock.patch.object(access, "send_email_async"):
            return access.add_member(
                self.store, caller or self.owner_m, email,
                [self.role(k).role_id for k in keys], make_owner=make_owner,
            )

    def verified(self, email):
        return make_user(email, email_verified_at=timezone.now())

    def test_pending_invitations_hides_old_and_settled_ones(self):
        fresh = self.invite("a@example.com")
        old = self.invite("b@example.com")
        Invitation.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=access.INVITATION_DAYS + 1),
        )
        revoked = self.invite("c@example.com")
        access.revoke_invitation(revoked, self.owner_m)
        self.assertEqual(list(access.pending_invitations(self.store)), [fresh])

    def test_revoke_marks_revoked_and_logs(self):
        inv = self.invite()
        access.revoke_invitation(inv, self.owner_m)
        inv.refresh_from_db()
        self.assertEqual(inv.status, access.REVOKED)
        self.assertIn("invitation.revoked", self.actions())

    def test_revoking_needs_the_permissions_the_invitation_carries(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        inv = self.invite("fresh@example.com", "administrator")
        with self.assertRaises(PermissionDenied):
            access.revoke_invitation(inv, admin)
        owner_inv = self.invite("o@example.com", make_owner=True)
        with self.assertRaises(PermissionDenied):
            access.revoke_invitation(owner_inv, admin)

    def test_accepting_grants_the_invited_access(self):
        self.invite("fresh@example.com", "online_store_editor")
        user = self.verified("fresh@example.com")
        accepted = access.accept_invitations(user)
        self.assertEqual(len(accepted), 1)
        self.assertEqual([r.key for r in access.member_roles(accepted[0])], ["online_store_editor"])
        inv = Invitation.objects.get()
        self.assertEqual(inv.status, access.ACCEPTED)
        self.assertIsNotNone(inv.accepted_at)
        self.assertIn("invitation.accepted", self.actions())

    def test_accepting_matches_the_email_case_insensitively(self):
        self.invite("fresh@example.com")
        self.assertEqual(len(access.accept_invitations(self.verified("Fresh@Example.com"))), 1)

    def test_nothing_is_granted_until_the_email_is_verified(self):
        self.invite("fresh@example.com")
        unverified = make_user("fresh@example.com")
        self.assertEqual(access.accept_invitations(unverified), [])
        self.assertEqual(Invitation.objects.get().status, access.PENDING)
        self.assertFalse(StoreMembership.objects.filter(user=unverified).exists())

    def test_expired_invitations_are_not_accepted(self):
        inv = self.invite("fresh@example.com")
        Invitation.objects.filter(pk=inv.pk).update(
            created_at=timezone.now() - timedelta(days=access.INVITATION_DAYS + 1),
        )
        self.assertEqual(access.accept_invitations(self.verified("fresh@example.com")), [])

    def test_invitation_lapses_when_the_inviter_lost_access(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        self.invite("fresh@example.com", caller=admin)
        admin.status = services.REMOVED
        admin.save()
        self.assertEqual(access.accept_invitations(self.verified("fresh@example.com")), [])
        self.assertEqual(Invitation.objects.get().status, access.REVOKED)

    def test_invitation_lapses_when_the_store_is_closed(self):
        self.invite("fresh@example.com")
        Store.objects.filter(pk=self.store.pk).update(status=services.CLOSED)
        self.assertEqual(access.accept_invitations(self.verified("fresh@example.com")), [])
        self.assertEqual(Invitation.objects.get().status, access.REVOKED)

    def test_roles_the_inviter_no_longer_holds_are_dropped(self):
        admin = self.staff("ua@example.com", "store_user_administrator", "online_store_editor")
        self.invite("fresh@example.com", "online_store_editor", caller=admin)
        access._set_roles(admin, [self.role("store_user_administrator")])  # inviter loses the editor role
        (membership,) = access.accept_invitations(self.verified("fresh@example.com"))
        self.assertEqual(access.member_roles(membership), [])

    def test_owner_invitation_needs_an_owner_inviter_at_acceptance(self):
        self.invite("fresh@example.com", make_owner=True)
        (membership,) = access.accept_invitations(self.verified("fresh@example.com"))
        self.assertEqual(membership.role, services.OWNER)

        # An owner invitation whose sender has since been demoted gets no ownership
        admin = self.staff("ua@example.com", "store_user_administrator")
        inv = Invitation.objects.create(
            store=self.store, email="x@example.com", is_owner=True, invited_by=admin.user,
            role_ids=[str(self.role("online_store_editor").role_id)],
        )
        (membership,) = access.accept_invitations(self.verified("x@example.com"))
        self.assertEqual(membership.role, access.STAFF)
        inv.refresh_from_db()
        self.assertEqual(inv.status, access.ACCEPTED)

    def test_invitation_for_someone_already_a_member_is_revoked(self):
        self.invite("fresh@example.com")
        user = self.verified("fresh@example.com")
        StoreMembership.objects.create(user=user, store=self.store, role=access.STAFF, status=services.ACTIVE)
        self.assertEqual(access.accept_invitations(user), [])
        self.assertEqual(Invitation.objects.get().status, access.REVOKED)

    def test_garbage_role_ids_are_ignored(self):
        admin = self.staff("ua@example.com", "store_user_administrator")
        Invitation.objects.create(
            store=self.store, email="x@example.com", invited_by=admin.user,
            role_ids=["not-a-uuid", None, str(self.role("store_user_administrator").role_id)],
        )
        (membership,) = access.accept_invitations(self.verified("x@example.com"))
        self.assertEqual([r.key for r in access.member_roles(membership)], ["store_user_administrator"])

    def test_valid_uuids_helper(self):
        good = self.role("administrator").role_id
        self.assertEqual(access._valid_uuids([str(good), "nope", good]), [good, good])
        self.assertEqual(access._valid_uuids(None), [])
