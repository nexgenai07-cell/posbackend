from django.db import models

from common.models import BaseModel


class Branch(BaseModel):
    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    currency = models.CharField(max_length=8, default="USD")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geofence_radius_m = models.DecimalField(max_digits=8, decimal_places=2, default=2)
    attendance_radius_m = models.DecimalField(max_digits=8, decimal_places=2, default=100)

    def __str__(self):
        return self.name_en
