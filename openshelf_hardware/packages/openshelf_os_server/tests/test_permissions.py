from openshelf_os_server.core.permissions import has_permission, has_any_permission, has_all_permissions


def test_permission_checks() -> None:
    perms = ["users:read", "roles:*", "*:{action}".format(action="read")]

    assert has_permission(perms, "users:read")
    assert has_permission(perms, "roles:write")  # roles:* covers
    assert has_permission(perms, "storage:read")  # *:read covers
    assert not has_permission(perms, "users:delete")

    assert has_any_permission(perms, ["users:delete", "users:read"])  # one matches
    assert not has_any_permission(perms, ["x:y", "a:b"])  # none match

    assert has_all_permissions(perms, ["users:read"])  # all matched
    assert not has_all_permissions(perms, ["users:read", "users:delete"])  # not all

