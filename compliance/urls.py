from django.urls import path
from . import views

app_name = "compliance"
urlpatterns = [path("", views.index, name="index"), path("register.csv", views.register_export, name="register_export"), path("documents/<int:pk>/", views.document_detail, name="document_detail"), path("arco/new/", views.arco_create, name="arco_create"), path("arco/<int:pk>/", views.arco_detail, name="arco_detail"), path("consent/new/", views.consent_create, name="consent_create"), path("consent/<int:pk>/withdraw/", views.consent_withdraw, name="consent_withdraw"), path("hold/<int:pk>/release/", views.hold_release, name="hold_release")]
urlpatterns += [path("notices/new/", views.notice_create, name="notice_create"), path("notices/<int:pk>/", views.notice_detail, name="notice_detail"), path("disposal/new/", views.disposal_create, name="disposal_create"), path("disposal/<int:pk>/", views.disposal_detail, name="disposal_detail")]
urlpatterns += [path("cancellations/new/", views.cancellation_create, name="cancellation_create")]
for kind in views.FORMS:
    urlpatterns += [path(f"{kind}/new/", views.edit, {"kind": kind}, name=f"{kind}_create"), path(f"{kind}/<int:pk>/edit/", views.edit, {"kind": kind}, name=f"{kind}_edit")]
