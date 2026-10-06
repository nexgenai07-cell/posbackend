from django.db import models

from common.models import BaseModel


class Branch(BaseModel):
    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    # Branding, configurable per restaurant instead of hard-coded in each app.
    # URLs rather than FileFields: images live in Cloudinary (see
    # common/cloudinary.py), so the database stores the hosted URL exactly the
    # way Product.image already does. Blank = each client falls back to its
    # own default, so an unconfigured restaurant still renders.
    logo_url = models.CharField(max_length=500, blank=True)
    favicon_url = models.CharField(max_length=500, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    currency = models.CharField(max_length=8, default="USD")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geofence_radius_m = models.DecimalField(max_digits=8, decimal_places=2, default=2)
    attendance_radius_m = models.DecimalField(max_digits=8, decimal_places=2, default=100)
    max_clock_ins_per_day = models.PositiveSmallIntegerField(default=1)
    max_clock_outs_per_day = models.PositiveSmallIntegerField(default=1)
    late_threshold_minutes = models.PositiveSmallIntegerField(default=15)

    def __str__(self):
        return self.name_en
