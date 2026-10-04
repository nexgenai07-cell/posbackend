"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

# The admin is the one screen that isn't the React app, so it says which
# product it belongs to instead of the default "Django administration".
# (Brand string matches nav.json's "brand" in restaurant-admin.)
admin.site.site_header = "Smoke & Char — Back Office"
admin.site.site_title = "Smoke & Char admin"
admin.site.index_title = "Restaurant data"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('accounts.urls')),
    path('api/', include('branches.urls')),
    path('api/', include('catalog.urls')),
    path('api/', include('tables.urls')),
    path('api/', include('orders.urls')),
    path('api/', include('inventory.urls')),
    path('api/', include('purchasing.urls')),
    path('api/', include('customers.urls')),
    path('api/', include('reports.urls')),
]
