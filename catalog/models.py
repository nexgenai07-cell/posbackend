from decimal import Decimal

from django.db import models

from branches.models import Branch
from common.models import BaseModel


class Weekday(models.TextChoices):
    SUN = "sun", "Sunday"
    MON = "mon", "Monday"
    TUE = "tue", "Tuesday"
    WED = "wed", "Wednesday"
    THU = "thu", "Thursday"
    FRI = "fri", "Friday"
    SAT = "sat", "Saturday"


class ProductBadge(models.TextChoices):
    NEW = "New", "New"
    POPULAR = "Popular", "Popular"
    DEAL = "Deal", "Deal"


class Category(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="categories")
    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return self.name_en


class Product(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    description_en = models.TextField(blank=True)
    description_ar = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.CharField(max_length=500, blank=True)
    is_available = models.BooleanField(default=True)
    badge = models.CharField(max_length=16, choices=ProductBadge.choices, null=True, blank=True)
    # Plain JSONField (not postgres ArrayField) so this still works against
    # the local sqlite fallback, not just Neon. Empty list = every day.
    days = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name_en


class Deal(BaseModel):
    """One deal per product. Hard-deleted, not soft-deleted (see
    catalog/views.py ProductDealView.delete) — the OneToOneField to Product
    would conflict with a new deal on re-create otherwise."""

    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="deal")
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Deal on {self.product.name_en}"


class DealWindow(BaseModel):
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="windows")
    day = models.CharField(max_length=3, choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.day} {self.start_time}-{self.end_time}"


class ProductVariant(BaseModel):
    """
    A size/portion a product can be ordered in — "Half KG", "1 KG", "Cup",
    "Family Tray". The restaurant defines the names and prices; nothing here
    assumes what a variant represents.

    OPTIONAL by design. A product with no variants behaves exactly as it always
    has, priced from Product.price, and no existing row needed migrating.
    Product.price therefore stays — it is the price for unvarianted products
    and the fallback everywhere else.

    `recipe_multiplier` scales the product's recipe for this variant: a 1 KG
    karahi consumes twice the chicken of a Half KG. Without it every variant
    would deduct identical stock and quietly corrupt inventory and food-cost
    reporting — nothing would error, the numbers would just be wrong. Costing
    (catalog/costing.py) and stock deduction (inventory/services.py) both read
    it, so the two can never disagree.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # 3 decimal places so a third-of-a-portion is expressible; 1.000 means
    # "exactly the product's recipe", which is what an unconfigured variant
    # should do rather than silently scaling anything.
    recipe_multiplier = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("1.000"))
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "name_en"],
                condition=models.Q(is_deleted=False),
                name="unique_active_variant_name_per_product",
            ),
        ]

    def __str__(self):
        return f"{self.product.name_en} — {self.name_en}"
