from django.urls import path
from . import views
app_name = 'billing'
urlpatterns = [path('',views.index,name='index'),path('payments/new/',views.payment_create,name='payment_create'),
    path('suspensions/',views.suspensions,name='suspensions'),path('suspensions/propose/<int:pk>/',views.suspension_propose,name='suspension_propose'),
    path('suspensions/<int:pk>/review/',views.suspension_review,name='suspension_review'),path('suspensions/<int:pk>/apply/',views.suspension_apply,name='suspension_apply'),
    path('credits/',views.credits,name='credits'),path('credits/outage/<int:pk>/apply/',views.apply_outage,name='apply_outage'),path('credits/<int:pk>/refund/',views.refund,name='refund'),
    path('subscription/<int:pk>/renew/',views.renew,name='renew'),
    path('payments/<int:pk>/',views.receipt,name='receipt'),path('payments/<int:pk>/reverse/',views.payment_reverse,name='payment_reverse'),
    path('cash/',views.cash,name='cash'),path('bank/',views.bank,name='bank'),path('bank/<int:pk>/reconcile/',views.reconcile,name='reconcile')]
