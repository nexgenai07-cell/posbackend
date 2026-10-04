from rest_framework import serializers


def require_bilingual_pair(attrs, instance, field_base, error_code="error.nameRequired"):
    """
    At least one of `{field_base}_en`/`{field_base}_ar` must be non-blank —
    the admin UI lets whichever language the manager has selected be the
    only one required (the other stays optional and dynamically flips when
    they switch), so a row just needs a displayable name in ONE language,
    not both. Both serializer fields must be declared `required=False,
    allow_blank=True` for this to run instead of DRF's own default
    required-field rejection. Raises against whichever field is currently
    blank, so the error lands on the field the UI actually has focused.
    """
    en_key, ar_key = f"{field_base}_en", f"{field_base}_ar"
    en = attrs.get(en_key, getattr(instance, en_key, "") if instance else "") or ""
    ar = attrs.get(ar_key, getattr(instance, ar_key, "") if instance else "") or ""
    if not en.strip() and not ar.strip():
        raise serializers.ValidationError({en_key: error_code, ar_key: error_code})


def reject_branch_mismatch(request, branch):
    """
    A staff member may only write to their own branch. BranchScopedQuerysetMixin
    (common/mixins.py) only scopes *reads* (it filters get_queryset()) — a
    client-writable `branch` field on create/update isn't touched by that at
    all, so without this a manager could otherwise create/move a row into a
    branch they don't belong to just by sending a different id in the request
    body. Shared by every branch-scoped serializer's validate() (catalog,
    tables, inventory, purchasing, accounts) — previously only enforced on
    catalog, tracked as a gap for the other apps until now.
    """
    if request and branch and branch != request.user.branch:
        raise serializers.ValidationError({"branch": "error.branchMismatch"})
