from django.urls import path
from . import views

app_name = "operations"
urlpatterns = [path("", views.index, name="index"), path("tickets/<int:pk>/", views.ticket_detail, name="ticket_detail"), path("work-orders/<int:pk>/", views.work_detail, name="work_detail"), path("outages/<int:pk>/", views.outage_detail, name="outage_detail")]
for kind in views.FORMS:
    urlpatterns += [path(f"{kind}/new/", views.edit, {"kind": kind}, name=f"{kind}_create"), path(f"{kind}/<int:pk>/edit/", views.edit, {"kind": kind}, name=f"{kind}_edit")]
