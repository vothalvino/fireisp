from django.urls import path
from . import views
from . import portal_notices
app_name = 'core'
urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('customers/', views.customers, name='customers'),
    path('lookup/<str:kind>/', views.lookup, name='lookup'),
    path('customers/new/', views.customer_create, name='customer_create'),
    path('customers/<int:pk>/', views.customer_detail, name='customer_detail'),
    path('customers/<int:pk>/edit/', views.customer_edit, name='customer_edit'),
    path('customers/<int:pk>/service/', views.subscription_create, name='subscription_create'),
    path('customers/<int:pk>/invite/', views.customer_invite, name='customer_invite'),
    path('plans/', views.plans, name='plans'), path('plans/new/', views.plan_create, name='plan_create'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/health/', views.system_health, name='system_health'),
    path('settings/branches/new/', views.branch_create, name='branch_create'),
    path('staff/new/', views.staff_create, name='staff_create'),
    path('audit/', views.audit_view, name='audit'),
    path('portal/', views.portal, name='portal'),
    path('portal/payments/', views.portal_payments, name='portal_payments'),
    path('portal/documents/<int:pk>/<str:format>/', views.portal_document, name='portal_document'),
    path('portal/support/', views.portal_support, name='portal_support'),
    path('portal/privacy/', views.portal_privacy, name='portal_privacy'),
    path('portal/notices/', portal_notices.index, name='portal_notices'),
    path('portal/notices/<int:pk>/', portal_notices.respond, name='portal_notice_respond'),
    path('portal/privacy/document/<int:pk>/', views.portal_consent, name='portal_consent'),
    path('portal/service/<int:pk>/cancel/', views.portal_cancel, name='portal_cancel'),
]
