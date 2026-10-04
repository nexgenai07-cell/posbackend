from django.db import models


class SoftDeleteQuerySet(models.QuerySet):
    """Bulk .delete() soft-deletes; .hard_delete() is the explicit escape hatch."""

    def delete(self):
        return self.update(is_deleted=True)

    def hard_delete(self):
        return super().delete()

    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager — only ever sees non-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    objects = SoftDeleteManager()  # default — excludes soft-deleted rows
    all_objects = models.Manager.from_queryset(SoftDeleteQuerySet)()  # everything, incl. soft-deleted

    class Meta:
        abstract = True
        ordering = ["id"]  # deterministic order for every list endpoint's pagination

    def delete(self, *args, hard=False, **kwargs):
        """Soft-deletes by default. Pass hard=True for a real DELETE — document why at each call site that needs it."""
        if hard:
            return super().delete(*args, **kwargs)
        self.is_deleted = True
        self.save(update_fields=["is_deleted", "updated_at"])

    def hard_delete(self, *args, **kwargs):
        return super().delete(*args, **kwargs)
