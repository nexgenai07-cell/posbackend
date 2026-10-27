from django.db import models

from branches.models import Branch
from common.models import BaseModel


class KitchenStation(BaseModel):
    """
    A part of the kitchen that makes certain dishes — BBQ, Fryer, Desserts.
    Dishes are routed here (Product.station, else Category.station) and
    kitchen staff work at one or more stations (Staff.stations), so each chef's
    screen shows only their own lines while the order itself stays whole.

    There is no "General" row: a line with no station (OrderItem.station null)
    is General and every kitchen screen sees it. A station is switched off
    with `is_active`, never deleted, so old order lines keep pointing at it;
    an inactive station routes new lines to General.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="kitchen_stations")
    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name_en or self.name_ar
